"""bsk 会话生命周期管理。

把 bsk 的"会话"（``session start`` / ``session stop``）封装成可按业务 key
（通常是 umo，即一个聊天会话）隔离的、并发安全的对象池。

本模块把下面这些**实测确认的 bsk 语义**全部消化掉，上层 ``service.py``
不需要再关心任何一条：

1. ``session start --no-focus --browser <instance_id> --json`` 返回
   ``{session_id, browser_instance_id, agent_window_id, interaction}``，
   ``session_id`` 是 **4 个小写字母**（如 ``mnaa``）。
2. ``session stop <SESSION_ID>`` 的 id 是**位置参数**，这是 bsk 里唯一的例外，
   其余所有命令都是 ``--session <id>``。
3. **绝不使用 ``bsk session stop --all``** —— 它会连带停掉**别的程序**
   （例如用户自己的 DSH）创建的会话。这里只按精确 id 停自己的会话。
4. **同一 session 严格串行**：bsk 同时只允许 1 个 in-flight 命令，第二个并发命令
   会返回 ``session_busy``。所以插件侧必须自己按 key 加锁（用一把全局锁会把
   互不相干的聊天会话也串行化，白白拖慢）。
5. **session 空闲 5 分钟被 bsk 回收**，而且回收**不保证**归还借用的标签页。
   因此必须显式 stop（``reap_idle`` / ``release_all``），不能指望自动回收。
6. 会话失效的表现是 ``not_found``（``BskSessionGone``）。正确策略是
   **用失败触发重建**：操作失败且是 ``not_found`` 时才重建并重试一次，
   **不要**每次操作前先探测（那是白白多一次往返）。
7. ``session_busy`` 等一小会儿重试一次即可。
8. ``outcome_unknown`` 类错误（扩展断连等）**绝对不能重试** —— 动作可能已经生效。
   此时把会话标记为 ``uncertain``，此后拒绝新的**操作类**动作，除非调用方显式
   声明这是只读动作（``allow_uncertain=True``，例如 observe / 截图）。

设计约束（写代码时请勿破坏）：

- **零第三方依赖**，不 import astrbot，可脱离框架单测；
- **不自己起后台任务**：空闲回收由 ``main.py`` 定时调用 ``reap_idle()``；
- 时间戳一律用 ``time.monotonic()``（``time.time()`` 会被系统时钟跳变影响）；
- 清理路径（``release`` / ``release_all`` / ``close``）**绝不抛异常**，
  否则会打断 ``terminate()``，留下无人回收的 Agent Window 打扰用户。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .errors import (
    OUTCOME_UNKNOWN_REASONS,
    BskError,
    BskOutcomeUnknown,
    BskProtocolError,
    BskSessionBusy,
    BskSessionGone,
)
from .models import BrowserInstance, BskResult, BskSession
from .runner import BskRunner

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期存在，运行时不 import
    # config.py 由另一路并行开发，可能存在也可能不存在。
    # 放在 TYPE_CHECKING 里可以两边都跑通：真正 import 它会把本模块
    # 和对方的进度绑死，而运行时我们只用 getattr 读几个属性。
    from .config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "SessionManager",
    "BUSY_RETRY_DELAY_SEC",
    "DEFAULT_IDLE_RELEASE_SEC",
    "DEFAULT_MAX_SESSIONS",
]


# --- 可调常量（全部有实测依据，改动前请先想清楚） ---

BUSY_RETRY_DELAY_SEC = 0.1
"""``session_busy`` 后的等待时长。bsk 的 in-flight 命令通常毫秒级结束。"""

MAX_ATTEMPTS = 2
"""一条命令最多执行几次。2 = 首次 + 重试一次，**这是硬上限**，别改成循环重试。"""

DEFAULT_MAX_SESSIONS = 8
"""未配置时的最大并发会话数（每个会话占一个 Agent Window，别开太多）。"""

DEFAULT_IDLE_RELEASE_SEC = 240.0
"""未配置时的空闲回收阈值。bsk 自己 5 分钟（300s）回收，我们取 4 分钟抢先一步，
这样回收动作是我们主动做的，标签页归还有机会走完整流程。"""

DEFAULT_COMMAND_TIMEOUT_SEC = 60.0
"""未配置时的默认命令超时（见 ARCHITECTURE §5 D6：必须小于框架的 120s 上限）。"""

START_TIMEOUT_FLOOR_SEC = 30.0
"""``session start`` 的超时下限。首次 start 要等浏览器扩展连接，不能给太短。"""

STOP_TIMEOUT_SEC = 15.0
"""``session stop`` 的超时。stop 要等浏览器归还标签页，但也不能无限等。"""

RELEASE_WAIT_SEC = 5.0
"""释放会话前最多等多久让 in-flight 命令结束。超过就强行 stop（宁可命令失败，
也不能让 ``terminate()`` 挂住）。"""

RELEASE_POLL_STEP_SEC = 0.02
"""等待 in-flight 命令结束的轮询间隔。

这里刻意用"轮询 ``lock.locked()``"而不是 ``asyncio.wait_for(lock.acquire())``：
后者在超时取消时存在"锁被唤醒后又被取消"的经典边界，可能让锁状态错乱。
释放路径只在关闭/回收时走，轮询的代价可以忽略。
"""

MAX_ENTRY_RETRIES = 4
"""``execute`` 取 entry 的最大尝试次数。

只有当另一个协程在极窄的窗口里疯狂 release 时才可能用满，正常路径一次就够。
加这个上限是为了**保证不出现活锁**。
"""

ArgsBuilder = Callable[[str], "list[str] | Awaitable[list[str]]"]
"""参数构造函数：接收 session_id，返回完整参数列表（不含 bsk 自身路径）。

之所以要传函数而不是现成列表，是为了让"重建会话后用新 id 重新构造参数"
变得自然 —— 这是自动重建能正确工作的关键。
"""

BrowserProbe = Callable[[], Any]
"""可选的浏览器探测函数：返回 BrowserInstance（或它的列表 / instance_id 字符串）。

可以是同步函数也可以是协程函数，返回结果宽松解析。
"""


def _as_float(value: Any, default: float) -> float:
    """把配置里的任意值安全地转成 float。"""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    """把配置里的任意值安全地转成 int。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    """把配置里的任意值安全地转成去空白的字符串。"""
    if isinstance(value, str):
        return value.strip()
    return default


def _setting(settings: Any, name: str, default: Any) -> Any:
    """读一个配置项，缺属性 / 取值非法时回退到默认值。

    刻意用 ``getattr`` 而不是 ``settings.name``：``Settings`` 由另一路并行
    开发，字段名或存在性都可能在变，这里必须容错，不能因为对方少一个字段
    就让整个会话管理起不来。
    """
    if settings is None:
        return default
    return getattr(settings, name, default)


def _adopt(target: BskSession, fresh: BskSession) -> None:
    """把新建成功的会话字段**原地**写进旧对象。

    这样做的目的：``acquire()`` 返回的 ``BskSession`` 对象在同一个 key 上
    始终保持同一身份，自动重建之后调用方手里的引用不会变成陈旧副本。
    """
    target.session_id = fresh.session_id
    target.browser_instance_id = fresh.browser_instance_id
    target.agent_window_id = fresh.agent_window_id
    target.interaction = fresh.interaction
    target.uncertain = False
    """新会话是干净状态，不确定标记随之清除。"""


def _coerce_probe_result(value: Any) -> str:
    """从 ``browser_probe`` 的任意返回值里抠出一个 instance_id。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, BrowserInstance):
        # 无响应的浏览器不能选用（bsk 也会拒绝）。
        return "" if value.unresponsive else value.instance_id.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _coerce_probe_result(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        return _coerce_probe_result(value.get("instance_id"))
    # 鸭子类型兜底：任何带 instance_id 属性的对象。
    return _as_str(getattr(value, "instance_id", None))


@dataclass(slots=True)
class _Entry:
    """一个 key 对应的会话槽位。

    **锁和会话是分开的两件事**：会话重建后 ``session`` 会换内容，
    但 ``lock`` 必须始终是同一把 —— 否则重建窗口里会漏进并发命令。
    """

    key: str
    session: BskSession
    lock: asyncio.Lock
    last_used: float
    """``time.monotonic()`` 时间戳，LRU 淘汰与空闲回收都用它。"""

    seq: int = 0
    """创建序号。``last_used`` 相同时用它做稳定的次级排序键。"""

    closed: bool = False
    """为 True 表示这个槽位已从管理器中摘除（正在/已经被 stop）。

    在这之后**绝不允许**再用它重建会话，否则会造出一个无人管理的僵尸会话。
    """


class SessionManager:
    """按 key 管理 bsk 会话的并发安全对象池。

    Args:
        runner: 子进程执行器。只用到 ``run_or_raise``。
        settings: 插件配置（``bsk/config.py`` 的 ``Settings``，或任何同名属性的对象）。
            只读这四个字段，且都用 ``getattr`` 兜底：
            ``max_sessions`` / ``idle_release_sec`` / ``browser_instance_id`` /
            ``command_timeout_sec``。
        browser_probe: 可选。当 ``settings.browser_instance_id`` 为空时，用它
            探测一个可用浏览器（``bsk browsers`` 的薄封装）。探测失败就退化成
            不传 ``--browser``，让 bsk 用默认浏览器。

    Attributes:
        _clock: ``time.monotonic`` 的接缝，单元测试可替换成假时钟来验证
            LRU 与空闲回收，避免测试里真的 sleep。生产代码不要动它。
    """

    def __init__(
        self,
        runner: BskRunner,
        settings: "Settings | Any",
        *,
        browser_probe: BrowserProbe | None = None,
    ) -> None:
        self._runner = runner
        self._browser_probe = browser_probe

        self._max_sessions = max(
            1, _as_int(_setting(settings, "max_sessions", DEFAULT_MAX_SESSIONS),
                       DEFAULT_MAX_SESSIONS)
        )
        idle = _as_float(
            _setting(settings, "idle_release_sec", DEFAULT_IDLE_RELEASE_SEC),
            DEFAULT_IDLE_RELEASE_SEC,
        )
        self._idle_release_sec = idle
        """<= 0 表示**关闭**自动回收（用户显式选择），而不是"立刻全回收"。"""

        self._browser_instance_id = _as_str(
            _setting(settings, "browser_instance_id", "")
        )
        self._command_timeout = _as_float(
            _setting(settings, "command_timeout_sec", DEFAULT_COMMAND_TIMEOUT_SEC),
            DEFAULT_COMMAND_TIMEOUT_SEC,
        )
        if self._command_timeout <= 0:
            self._command_timeout = DEFAULT_COMMAND_TIMEOUT_SEC
        self._start_timeout = max(START_TIMEOUT_FLOOR_SEC, self._command_timeout)

        self._entries: dict[str, _Entry] = {}
        self._registry_lock = asyncio.Lock()
        """只保护 ``_entries`` 字典的增删（内部**不含**长 await），
        与 per-key 的会话锁是两把不同的锁，别混用。"""

        self._seq = 0
        self._closed = False
        self._clock: Callable[[], float] = time.monotonic

        self._counters: dict[str, int] = {
            "started": 0,
            "rebuilt": 0,
            "stopped": 0,
            "stop_failed": 0,
            "busy_retries": 0,
            "evicted": 0,
            "reaped": 0,
            "uncertain_blocks": 0,
            "not_found_rebuilds": 0,
        }
        self._recent_stop_errors: list[str] = []

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def acquire(self, key: str) -> BskSession:
        """取当前 key 的会话；没有就建一个。

        同一 key 并发调用只会 ``start`` 一次（其余协程等待并复用），
        返回的也是**同一个对象**。

        Note:
            返回值主要用于读取 ``session_id`` / ``agent_window_id``，
            或者用来判断 ``uncertain``。真正的命令执行请走 ``execute()``。
        """
        self._ensure_open()
        async with self._locked_entry(key) as entry:
            await self._ensure_session(entry)
            self._touch(entry)
            return entry.session

    async def execute(
        self,
        key: str,
        args_builder: ArgsBuilder,
        *,
        timeout: float | None = None,
        allow_uncertain: bool = False,
    ) -> BskResult:
        """★ 核心方法：带上会话 id 执行一条 bsk 命令。

        自动处理：per-key 加锁（同 key 串行）、会话失效时重建、``session_busy``
        短暂重试、``outcome_unknown`` 标记不确定态。**任何情况下都只重试一次**。

        Args:
            key: 业务键（umo 等）。
            args_builder: 接收 ``session_id`` 返回完整参数列表的可调用对象，
                例如 ``lambda sid: ["observe", "--session", sid, "--json"]``。
                可以是协程函数（返回值会被 await）。
                用函数而不是现成列表，是为了重建会话后能用**新的 session_id**
                重新构造参数。
            timeout: 超时秒数；None 时用 ``settings.command_timeout_sec``。
            allow_uncertain: 显式声明"这是只读动作"，允许在会话处于不确定态时
                继续执行（例如 observe、截图）。**写/点击类动作绝对不要传 True**。

        Returns:
            成功的 ``BskResult``。

        Raises:
            BskOutcomeUnknown: 会话不确定态被拒；或命令本身结果未知（此时会话已被
                标记为 uncertain，后续动作都会被拒绝）。
            BskSessionGone: 会话失效且重建后仍然失效（重试已用尽）。
            BskSessionBusy: 会话一直忙（重试已用尽）。
            BskError: 其他 bsk 错误，``friendly`` 可直接给用户/模型看。
        """
        self._ensure_open()
        effective_timeout = (
            float(timeout) if timeout is not None else self._command_timeout
        )

        async with self._locked_entry(key) as entry:
            await self._ensure_session(entry)
            self._touch(entry)

            if entry.session.uncertain and not allow_uncertain:
                self._counters["uncertain_blocks"] += 1
                raise self._uncertain_error(entry.session)

            # ★ 重试次数硬上限 = MAX_ATTEMPTS(2)，即"首次 + 重试一次"。
            #   循环体里的每个分支要么 return、要么 continue（消耗掉下一次机会）、
            #   要么 raise，所以不存在无限重试的可能。
            for attempt in range(1, MAX_ATTEMPTS + 1):
                args = await self._build_args(args_builder, entry.session.session_id)
                try:
                    result = await self._runner.run_or_raise(
                        args, timeout=effective_timeout
                    )
                except BskOutcomeUnknown as exc:
                    # 动作可能已经生效，绝不重试；标记后让异常冒出去。
                    self._mark_uncertain(entry, f"命令结果未知：{exc.message}")
                    raise
                except BskError as exc:
                    # classify() 已把 data.reason 归到 BskOutcomeUnknown，
                    # 这里再兜一层：任何带 outcome_unknown reason 的错误都按
                    # 不可重试处理，防止上游改了分类逻辑就悄悄退化成重试。
                    if exc.reason in OUTCOME_UNKNOWN_REASONS:
                        self._mark_uncertain(entry, f"命令结果未知：{exc.message}")
                        raise

                    if isinstance(exc, BskSessionGone):
                        if attempt >= MAX_ATTEMPTS or entry.closed:
                            # entry.closed：槽位已被 release/reap 摘走，
                            # 此时重建会造出无人管理的僵尸会话，必须放弃。
                            raise
                        # ★ 用失败触发重建：不先 stop 旧会话（bsk 已确认它不存在），
                        #   再由下一轮用**新的 session_id** 重新构造参数。
                        self._counters["not_found_rebuilds"] += 1
                        await self._restart(entry, stop_old=False)
                        logger.info(
                            "会话 %s 已失效，已重建为 %s，重试一次",
                            key,
                            entry.session.session_id,
                        )
                        continue

                    if isinstance(exc, BskSessionBusy):
                        if attempt >= MAX_ATTEMPTS:
                            raise
                        self._counters["busy_retries"] += 1
                        await asyncio.sleep(BUSY_RETRY_DELAY_SEC)
                        continue

                    raise

                self._touch(entry)
                return result

            # 循环必然以 return 或 raise 结束，正常走不到这里。
            raise AssertionError("unreachable")  # pragma: no cover

    async def release(self, key: str) -> bool:
        """显式关闭并移除某个 key 的会话。

        会先等在飞的命令结束（最多 ``RELEASE_WAIT_SEC`` 秒），再发
        ``session stop <id>``（**位置参数**，唯一例外，绝不用 ``--all``）。

        Returns:
            True 表示这个 key 原本有会话，且已确认在 bsk 侧不再存活
            （显式 stop 成功，或 bsk 回 ``not_found`` 说明早已被回收）。
            False 表示原本就没有会话记录，或 stop 因其他原因失败 ——
            无论哪种，**本地记录都已删除**，stop 失败原因见 ``stats()``。

        Note:
            本方法**不会抛异常**（terminate 路径不能被它打断）。
        """
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        entry.closed = True
        try:
            await self._wait_idle(entry, RELEASE_WAIT_SEC)
            return await self._stop_entry(entry)
        except Exception as exc:  # noqa: BLE001 - 清理路径必须吞掉一切
            logger.warning("释放会话 %s 时出现意外错误：%r", key, exc)
            self._record_stop_error(f"{key}: {exc!r}")
            return False
        finally:
            entry.closed = True

    async def release_all(self) -> int:
        """关闭所有会话，返回成功关闭的数量。``terminate()`` 用。

        先一次性把本地记录全部摘除（只做同步操作，不给并发留窗口），
        再并发 stop。任何一个 stop 失败都不影响其他会话的清理，也不会抛异常。
        """
        entries: list[_Entry] = []
        for key in list(self._entries.keys()):
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.closed = True
                entries.append(entry)
        if not entries:
            return 0
        return await self._shutdown_entries(entries)

    async def reap_idle(self) -> int:
        """回收空闲超时的会话，返回回收数量。由 ``main.py`` 的后台任务定期调用。

        判定用 ``self._clock() - entry.last_used >= idle_release_sec``。
        正在执行命令的会话（锁被持有）不算空闲，跳过不回收 —— 否则会打断
        一个正在跑的长命令（例如全页截图）。

        Note:
            ``idle_release_sec <= 0`` 表示用户关闭了自动回收，直接返回 0。
        """
        if self._idle_release_sec <= 0 or not self._entries:
            return 0

        now = self._clock()
        victims: list[_Entry] = []
        for key in list(self._entries.keys()):
            entry = self._entries.get(key)
            if entry is None or entry.lock.locked():
                continue
            if now - entry.last_used >= self._idle_release_sec:
                popped = self._entries.pop(key, None)
                if popped is not None:
                    popped.closed = True
                    victims.append(popped)

        if not victims:
            return 0
        count = await self._shutdown_entries(victims)
        self._counters["reaped"] += count
        logger.info("空闲回收了 %d 个浏览器会话", count)
        return count

    def stats(self) -> dict:
        """给 ``/bskstatus`` 用的运行状态摘要（同步方法，随时可调）。"""
        now = self._clock()
        details: list[dict[str, Any]] = []
        for key, entry in self._entries.items():
            details.append(
                {
                    "key": key,
                    "session_id": entry.session.session_id or "(未建立)",
                    "browser_instance_id": entry.session.browser_instance_id,
                    "agent_window_id": entry.session.agent_window_id,
                    "uncertain": entry.session.uncertain,
                    "busy": entry.lock.locked(),
                    "idle_sec": round(max(0.0, now - entry.last_used), 1),
                }
            )
        return {
            "closed": self._closed,
            "sessions": len(details),
            "max_sessions": self._max_sessions,
            "idle_release_sec": self._idle_release_sec,
            "default_timeout_sec": self._command_timeout,
            "uncertain_count": sum(1 for d in details if d["uncertain"]),
            "details": details,
            "counters": dict(self._counters),
            "recent_stop_errors": list(self._recent_stop_errors),
        }

    async def close(self) -> None:
        """释放全部资源（等价 ``release_all`` + 清理内部状态）。

        本模块**不创建任何后台任务**（空闲回收由 ``main.py`` 调度），
        所以"清理任务"这一步在这里没有对应动作，只做状态收尾。

        关闭后 ``acquire`` / ``execute`` 会抛 ``BskError(code="manager_closed")``：
        明确失败好过在插件卸载后悄悄新建一个没人回收的会话。
        本方法不抛异常。
        """
        try:
            await self.release_all()
        except Exception as exc:  # noqa: BLE001 - 关闭路径绝不抛
            logger.warning("close() 释放会话时出现意外错误：%r", exc)
        finally:
            self._entries.clear()
            self._closed = True

    # ------------------------------------------------------------------
    # 内部：entry 生命周期
    # ------------------------------------------------------------------

    def _ensure_open(self) -> None:
        """管理器已被 close 时，拒绝新建/执行（防止 terminate 后泄漏会话）。"""
        if self._closed:
            raise BskError(
                "会话管理器已关闭",
                friendly="浏览器插件正在关闭或已关闭，会话已全部释放。请稍后重试。",
                code="manager_closed",
            )

    @contextlib.asynccontextmanager
    async def _locked_entry(self, key: str):
        """取到 key 的 entry **并持有它的锁**，退出时自动释放。

        会处理"取到 entry 后、拿到锁前，该 entry 被 release 摘走"的竞态：
        发现槽位已不是字典里的那个就重新取（有次数上限，保证不活锁）。
        """
        for _ in range(MAX_ENTRY_RETRIES):
            entry = await self._get_or_create_entry(key)
            await entry.lock.acquire()
            try:
                if self._entries.get(key) is entry and not entry.closed:
                    yield entry
                    return
                logger.debug("会话槽位 %s 在等待锁期间已被释放，重新获取", key)
            finally:
                entry.lock.release()
        raise BskError(
            f"会话槽位 {key} 反复被释放",
            friendly="浏览器会话正在被反复释放，请稍后重试。",
            code="session_churn",
        )

    async def _get_or_create_entry(self, key: str) -> _Entry:
        """取 key 的槽位；没有就**先占位**再返回（真正的 start 在锁内做）。

        占位很关键：如果等 ``start`` 完成才写进字典，10 个并发 ``acquire``
        会各起一个会话（bsk 侧留下 10 个 Agent Window）。
        """
        # 重试路径可能已经跨过了 close()，这里再确认一次：
        # 关闭之后绝不允许再造新会话，否则 terminate 后还会泄漏 Agent Window。
        self._ensure_open()

        entry = self._entries.get(key)
        if entry is not None and not entry.closed:
            return entry

        # 可能需要新建：先按 LRU 腾出容量（这一步会 await stop，不能放在登记锁里）。
        await self._evict_for_capacity()

        async with self._registry_lock:
            entry = self._entries.get(key)
            if entry is not None and not entry.closed:
                return entry
            self._seq += 1
            entry = _Entry(
                key=key,
                session=BskSession(session_id=""),
                lock=asyncio.Lock(),
                last_used=self._clock(),
                seq=self._seq,
            )
            self._entries[key] = entry
            return entry

    async def _ensure_session(self, entry: _Entry) -> BskSession:
        """确保槽位里有一个可用的会话（占位 entry 在这里才真正 start）。"""
        if entry.session.is_valid():
            return entry.session
        return await self._start_into(entry)

    async def _start_into(self, entry: _Entry, *, is_rebuild: bool = False) -> BskSession:
        """新建一个 bsk 会话并原地写进槽位。

        进函数先把 id 清空：万一 ``start`` 失败，槽位必须**如实**报告"当前没有
        会话"，否则下次 ``execute`` 会拿着死 id 再撞一次 ``not_found``。
        """
        entry.session.session_id = ""
        fresh = await self._start()

        if entry.closed:
            # 极端竞态：槽位在 start 期间被 release/reap 摘走（start 慢于
            # RELEASE_WAIT_SEC 时可能发生）。这个新会话已经没人管了，必须立刻
            # 停掉 —— 否则它会一直占着用户的浏览器，而 bsk 5 分钟后才回收，
            # 且回收**不保证**归还借用的标签页。
            with contextlib.suppress(Exception):
                await self._stop_session_id(fresh.session_id)
            raise BskError(
                f"会话槽位 {entry.key} 在建立会话期间被释放",
                friendly="浏览器会话已被释放，请重新发起这次操作。",
                code="session_released",
            )

        _adopt(entry.session, fresh)
        entry.last_used = self._clock()
        self._counters["started"] += 1
        if is_rebuild:
            self._counters["rebuilt"] += 1
        return entry.session

    async def _restart(self, entry: _Entry, *, stop_old: bool) -> BskSession:
        """丢弃旧会话并重建。

        Args:
            stop_old: 是否先显式 stop 旧会话。**``not_found`` 触发的重建必须传
                False**：bsk 已经说了这个会话不存在，再 stop 一次既多花一次往返，
                又可能误杀"别的程序刚建出来的同 id 会话"（4 个小写字母的组合
                空间并不大）。
        """
        if stop_old and entry.session.is_valid():
            await self._stop_entry(entry)
        return await self._start_into(entry, is_rebuild=True)

    async def _start(self) -> BskSession:
        """执行 ``session start`` 并解析出会话。

        Raises:
            BskError: 启动失败，或返回里没有 session_id（协议异常）。
        """
        args = ["session", "start", "--no-focus", "--json"]
        browser_id = await self._resolve_browser_instance()
        if browser_id:
            # 必须用 instance_id：实测 label 经常是空串，拿它选浏览器会选错。
            args += ["--browser", browser_id]

        result = await self._runner.run_or_raise(args, timeout=self._start_timeout)
        session = BskSession.from_json(result.data)
        if not session.is_valid():
            raise BskProtocolError(
                f"session start 未返回 session_id：{(result.stdout or '')[:200]}",
                friendly="无法创建浏览器会话（bsk 返回的数据不完整），请稍后重试。",
                code="bad_session_payload",
                exit_code=result.exit_code,
            )
        logger.info(
            "已创建浏览器会话 %s（browser=%s）",
            session.session_id,
            session.browser_instance_id or browser_id or "默认",
        )
        return session

    async def _resolve_browser_instance(self) -> str:
        """决定 ``session start`` 要指定哪个浏览器。

        优先用配置里的 ``browser_instance_id``；没配就用 ``browser_probe``
        探一个；探测失败返回空串（不传 ``--browser``，让 bsk 自己选默认）。
        任何异常都吞掉 —— 探测只是优化，不该让建会话失败。
        """
        if self._browser_instance_id:
            return self._browser_instance_id
        if self._browser_probe is None:
            return ""
        try:
            probed = self._browser_probe()
            if inspect.isawaitable(probed):
                probed = await probed
            return _coerce_probe_result(probed)
        except Exception as exc:  # noqa: BLE001 - 探测失败只是回退，不是错误
            logger.debug("browser_probe 探测失败，回退到 bsk 默认浏览器：%r", exc)
            return ""

    async def _stop_entry(self, entry: _Entry) -> bool:
        """停掉一个槽位里的会话。**绝不抛异常**。

        Returns:
            True 表示 bsk 侧确认该会话已不存在（stop 成功，或本来就没了）。
        """
        if not entry.session.session_id:
            return True  # 只有占位槽位，没有真实会话可停。
        return await self._stop_session_id(entry.session.session_id)

    async def _stop_session_id(self, session_id: str) -> bool:
        """按 id 停掉一个会话。**绝不抛异常**。

        Note:
            这里**故意不**去清空 ``entry.session.session_id``：``acquire()`` 返回给
            调用方的就是这个对象，抹掉 id 会毁掉外面的句柄（测试与 /bskstatus
            都要读它）。"槽位已无会话"这个事实由 ``_start_into`` 负责表达，
            以及槽位被摘除（``entry.closed``）来保证。
        """
        if not session_id:
            return True

        # ★ id 是位置参数！这是 bsk 里唯一的例外（其余命令都用 --session）。
        # ★ 绝不用 `session stop --all`：那会停掉别的程序（如用户的 DSH）的会话。
        args = ["session", "stop", session_id]
        try:
            await self._runner.run_or_raise(
                args, timeout=STOP_TIMEOUT_SEC, expect_json=False
            )
        except BskSessionGone:
            # bsk 说它已经不存在了 —— 对我们来说目的已达成，不算失败。
            self._counters["stopped"] += 1
            logger.debug("会话 %s 已不存在，无需停止", session_id)
            return True
        except Exception as exc:  # noqa: BLE001 - 清理路径吞掉一切
            self._counters["stop_failed"] += 1
            self._record_stop_error(f"{session_id}: {exc}")
            logger.warning("停止会话 %s 失败：%r", session_id, exc)
            return False

        self._counters["stopped"] += 1
        logger.info("已停止浏览器会话 %s", session_id)
        return True

    async def _shutdown_entries(self, entries: list[_Entry]) -> int:
        """并发停掉一批会话，返回成功数量。任何失败都不会传播。"""
        results = await asyncio.gather(
            *(self._shutdown_one(entry) for entry in entries),
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _shutdown_one(self, entry: _Entry) -> bool:
        """等的命令结束 → stop。包了 try/except，绝不抛。"""
        try:
            await self._wait_idle(entry, RELEASE_WAIT_SEC)
            return await self._stop_entry(entry)
        except Exception as exc:  # noqa: BLE001 - 清理路径吞掉一切
            logger.warning("关闭会话槽位 %s 时出现意外错误：%r", entry.key, exc)
            self._record_stop_error(f"{entry.key}: {exc!r}")
            return False

    async def _evict_for_capacity(self) -> int:
        """LRU 淘汰：容量满了就把最久未使用的会话停掉。

        Returns:
            淘汰数量。
        """
        evicted = 0
        # 每轮都至少 pop 一个，字典单调收缩，不会死循环。
        while self._entries and len(self._entries) >= self._max_sessions:
            victim_key, victim = self._pick_lru()
            popped = self._entries.pop(victim_key, None)
            if popped is None:  # pragma: no cover - 防御，理论上不会发生
                break
            popped.closed = True
            await self._wait_idle(popped, RELEASE_WAIT_SEC)
            await self._stop_entry(popped)
            evicted += 1
            self._counters["evicted"] += 1
            logger.info(
                "会话数已达上限 %d，淘汰最久未使用的会话 %s（key=%s）",
                self._max_sessions,
                popped.session.session_id or "(未建立)",
                victim_key,
            )
            del victim  # 仅为可读性
        return evicted

    def _pick_lru(self) -> tuple[str, _Entry]:
        """挑出最久未使用的槽位。

        次级排序键用创建序号，保证"同一时刻创建"时淘汰的是最先建的那个
        （``time.monotonic`` 分辨率虽高，但测试里可能被假时钟拉平）。
        """
        return min(
            self._entries.items(),
            key=lambda kv: (kv[1].last_used, kv[1].seq),
        )

    async def _wait_idle(self, entry: _Entry, timeout: float) -> bool:
        """等在飞的命令结束（最多 timeout 秒）。

        Returns:
            True 表示锁已空闲；False 表示等超时了（调用方仍应继续 stop ——
            宁可让在飞命令失败，也不能让关闭流程挂住）。
        """
        if not entry.lock.locked():
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while entry.lock.locked() and time.monotonic() < deadline:
            await asyncio.sleep(RELEASE_POLL_STEP_SEC)
        if entry.lock.locked():
            logger.warning("等待会话 %s 空闲超过 %.1fs，仍继续停止", entry.key, timeout)
            return False
        return True

    # ------------------------------------------------------------------
    # 内部：小工具
    # ------------------------------------------------------------------

    def _touch(self, entry: _Entry) -> None:
        """刷新活跃时间（LRU 与空闲回收的唯一依据）。"""
        entry.last_used = self._clock()

    async def _build_args(self, builder: ArgsBuilder, session_id: str) -> list[str]:
        """用当前 session_id 构造参数列表（支持同步/异步 builder）。"""
        produced = builder(session_id)
        if inspect.isawaitable(produced):
            produced = await produced
        if isinstance(produced, tuple):
            produced = list(produced)
        if not isinstance(produced, list):
            raise BskProtocolError(
                f"args_builder 必须返回参数列表，实际返回 {type(produced).__name__}",
                friendly="插件内部错误：命令参数构造失败，请反馈。",
                code="bad_args_builder",
            )
        return [str(item) for item in produced]

    def _mark_uncertain(self, entry: _Entry, why: str) -> None:
        """把会话标记为不确定态：动作结果未知，此后拒绝新的操作动作。"""
        entry.session.uncertain = True
        logger.warning(
            "会话 %s 进入不确定态（%s）：后续动作将被拒绝，"
            "需要人工确认页面状态或 release 后重建",
            entry.session.session_id or entry.key,
            why,
        )

    @staticmethod
    def _uncertain_error(session: BskSession) -> BskOutcomeUnknown:
        """构造"因不确定态拒绝执行"的异常。"""
        return BskOutcomeUnknown(
            f"会话 {session.session_id} 处于不确定状态，拒绝执行新动作",
            friendly=(
                "上一次浏览器操作的结果无法确认，为避免重复点击/重复提交，"
                "我不会再执行新的操作。请先人工确认浏览器里的实际状态；"
                "确认没问题后可以让我重新建立会话再继续。"
            ),
            code="session_uncertain",
        )

    def _record_stop_error(self, detail: str) -> None:
        """记下 stop 失败原因（只留最近 5 条，供 /bskstatus 排查）。"""
        self._recent_stop_errors.append(detail)
        del self._recent_stop_errors[:-5]
