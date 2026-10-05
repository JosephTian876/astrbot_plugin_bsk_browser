"""bsk 会话生命周期管理。

把 bsk 的"会话"（``session start`` / ``session stop``）封装成可按业务 key
（通常是 umo，即一个聊天会话）隔离的、并发安全的对象池。

本模块把下面这些实测确认的 bsk 语义全部消化掉，上层 ``service.py``
不需要再关心任何一条：

1. ``session start --no-focus --browser <instance_id> --json`` 返回
   ``{session_id, browser_instance_id, agent_window_id, interaction}``，
   ``session_id`` 是 4 个小写字母（如 ``mnaa``）。
2. ``session stop <SESSION_ID>`` 的 id 是位置参数，这是 bsk 里唯一的例外，
   其余所有命令都是 ``--session <id>``。
3. 绝不使用 ``bsk session stop --all`` —— 它会连带停掉别的程序
   （例如用户自己的 DSH）创建的会话。这里只按精确 id 停自己的会话。
4. 同一 session 严格串行：bsk 同时只允许 1 个 in-flight 命令，第二个并发命令
   会返回 ``session_busy``。所以插件侧必须自己按 key 加锁（用一把全局锁会把
   互不相干的聊天会话也串行化，白白拖慢）。
5. session 空闲 5 分钟被 bsk 回收，而且回收不保证归还借用的标签页。
   因此必须显式 stop（``reap_idle`` / ``release_all``），不能指望自动回收。
6. 会话失效的表现是 ``not_found``（``BskSessionGone``）。正确策略是
   用失败触发重建：操作失败且是 ``not_found`` 时才重建并重试一次，
   不要每次操作前先探测（那是白白多一次往返）。
7. ``session_busy`` 等一小会儿重试一次即可。
8. ``outcome_unknown`` 类错误（扩展断连等）绝对不能重试 —— 动作可能已经生效。
   此时把会话标记为 ``uncertain``，此后拒绝新的操作类动作，除非调用方显式
   声明这是只读动作（``allow_uncertain=True``，例如 observe / 截图）。
9. daemon 的生命周期独立于 AstrBot。AstrBot 被强杀时 ``terminate()`` 不会执行，
   daemon 与那些会话却还活着 —— 用户桌面上就留下了没人管的浏览器窗口。
   对策是 ``journal.py`` 的持久化所有权记录：建会话时落盘、正常 stop 后删掉、
   下次 ``initialize()`` 时按记录精确回收（见 ``recover_orphans``）。
10. ``session stop`` 会间歇性失败（实测 bsk 0.3.2 报
    ``Background execution cleanup timed out``，此时会话其实还活着）。
    因此 stop 走有界重试：只对瞬时故障最多试 ``STOP_MAX_ATTEMPTS`` 次，
    且总耗时受预算约束 —— ``terminate()`` 绝不能被拖住。
    分工：本次运行内的重试是第一道防线，journal + ``recover_orphans``
    是第二道（下次启动时兜底）。

设计约束（写代码时请勿破坏）：

- 零第三方依赖，不 import astrbot，可脱离框架单测；
- 不自己起后台任务：空闲回收由 ``main.py`` 定时调用 ``reap_idle()``；
- 时间戳一律用 ``time.monotonic()``（``time.time()`` 会被系统时钟跳变影响）；
  例外是 journal 的 ``created_at``，那个要跨进程比较，必须用墙钟；
- 清理路径（``release`` / ``release_all`` / ``close``）绝不抛异常，
  否则会打断 ``terminate()``，留下无人回收的 Agent Window 打扰用户；
- journal 是尽力而为的辅助机制：它的读写失败不能影响会话的正常创建/停止。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .errors import (
    EXIT_TIMEOUT,
    OUTCOME_UNKNOWN_REASONS,
    BskBrowserAmbiguous,
    BskError,
    BskNotInstalled,
    BskOutcomeUnknown,
    BskProtocolError,
    BskSessionBusy,
    BskSessionGone,
    BskTimeout,
    BskVersionError,
)
from .journal import JournalEntry, SessionJournal
from .models import BrowserInstance, BskResult, BskSession
from .runner import DEFAULT_CANCEL_GRACE_SEC, DRAIN_TIMEOUT_SEC, BskRunner

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
    "STOP_MAX_ATTEMPTS",
    "STOP_RETRY_BUDGET_SEC",
    "STOP_RETRY_DELAY_SEC",
    "STOP_TIMEOUT_SEC",
    "STOP_TOTAL_BUDGET_SEC",
]


# --- 可调常量（全部有实测依据，改动前请先想清楚） ---

BUSY_RETRY_DELAY_SEC = 0.1
"""``session_busy`` 后的等待时长。bsk 的 in-flight 命令通常毫秒级结束。"""

MAX_ATTEMPTS = 2
"""一条命令最多执行几次。2 = 首次 + 重试一次，这是硬上限，别改成循环重试。"""

DEFAULT_MAX_SESSIONS = 3
"""未配置时的最大并发会话数（每个会话占一个 Agent Window，别开太多）。

刻意与 ``config.py`` 的 ``DEFAULT_MAX_SESSIONS`` 保持一致：这里只是
"配置对象缺这个字段"时的兜底，两边取值不同会变成很难查的行为漂移。
"""

DEFAULT_IDLE_RELEASE_SEC = 240.0
"""未配置时的空闲回收阈值。bsk 自己 5 分钟（300s）回收，我们取 4 分钟抢先一步，
这样回收动作是我们主动做的，标签页归还有机会走完整流程。"""

DEFAULT_COMMAND_TIMEOUT_SEC = 60.0
"""未配置时的默认命令超时（见 ARCHITECTURE §5 D6：必须小于框架的 120s 上限）。"""

START_TIMEOUT_FLOOR_SEC = 30.0
"""``session start`` 的超时下限。首次 start 要等浏览器扩展连接，不能给太短。"""

STOP_TIMEOUT_SEC = 15.0
"""``session stop`` 单次尝试的超时。stop 要等浏览器归还标签页，但也不能无限等。

首次尝试总是拿满它（除非总预算已经不够，见 ``STOP_RETRY_BUDGET_SEC``）。
"""

STOP_MAX_ATTEMPTS = 3
"""``session stop`` 最多尝试几次（含首次）。硬上限，别改成无限循环。

为什么是 3 而不是 2：实测 bsk 0.3.2 在"曾经有 navigate 失败过"的会话上会间歇性
报 ``Background execution cleanup timed out``（此时会话其实还活着），而
紧接一次重试就能成功 —— 说明是瞬时状态。给两次重试机会是为了覆盖
"连续两次都撞上同一个瞬时窗口"这种低概率情形，同时仍把最坏耗时钉死。
"""

STOP_RETRY_DELAY_SEC = 0.5
"""两次 ``session stop`` 之间的固定等待时长（秒）。

固定而不是递增：实测瞬时窗口极短（紧接一次重试即成功），递增带来的额外等待
只会拖长 ``terminate()``，换不来更高的成功率。
"""

STOP_RETRY_BUDGET_SEC = 5.0
"""``_stop_session_id`` 额外留给重试的时间预算（秒）。

它与前两项一起构成整次调用的总时间上限（见 ``STOP_TOTAL_BUDGET_SEC``）：

    总上限 = STOP_TIMEOUT_SEC + STOP_ATTEMPT_OVERHEAD_SEC + STOP_RETRY_BUDGET_SEC
           = 15 + 17 + 5 = 37 秒

前两项覆盖"首次尝试的正常最坏耗时"（bsk 真的卡满 15 秒超时，再加 runner 自己的
优雅取消与收管道），不做任何削减 —— 否则等于把 runner 的清理流程掐断，
反而更可能留下一个 bsk 子进程。这一项是额外多给的，专门用来重试。

为什么这 5 秒给得起：实测那条 ``cleanup timed out`` 是毫秒级快速失败，
根本用不到 15 秒超时，所以重试实际只花几十毫秒 —— 5 秒是几百倍的余量；
而它换来的是"不再泄漏一个用户桌面上的浏览器窗口"。
"""

STOP_RETRY_MIN_TIMEOUT_SEC = 1.0
"""一次尝试至少要有这么多剩余预算才值得发。

只剩 0.2 秒预算的 stop 几乎必然超时，发了也只是白花时间、白记一条错误。
低于这个值就直接放弃（计 ``stop_retry_budget_skips``）。
"""

STOP_ATTEMPT_OVERHEAD_SEC = DEFAULT_CANCEL_GRACE_SEC + DRAIN_TIMEOUT_SEC
"""单次 stop 在超时之后还可能多花的时间：runner 的优雅取消费 + 收管道。

``BskRunner.run`` 的内部最坏耗时是 ``timeout + cancel_grace + DRAIN_TIMEOUT_SEC``
（超时后先关 stdin 给 15 秒宽限，再 kill，然后有界地收管道 —— 见 ``runner.py``）。
本模块把这个开销显式计入自己的墙钟上界，否则"上界"就是假的。
"""

STOP_TOTAL_BUDGET_SEC = (
    STOP_TIMEOUT_SEC + STOP_ATTEMPT_OVERHEAD_SEC + STOP_RETRY_BUDGET_SEC
)
"""``_stop_session_id`` 整次调用的墙钟总上限（秒）= 15 + 17 + 5 = 37。

这是唯一的截止时刻，所有尝试与等待都从它反推剩余额度：

- 每次尝试的超时 = ``min(单次上限, 剩余)``；
- 每次尝试的兜底 = ``clamp(兜底上限, 尝试超时, 剩余)``；
- 每次重试前的等待 = ``min(重试延迟, 剩余)``；剩余不够就不发。

于是"整次调用不会越过这个上限"可以逐条推出来，而不是靠各处常量互相心算。
超了就放弃并记 ``stop_failed`` —— 绝不能把 ``terminate()`` 挂住。
"""

STOP_ATTEMPT_BACKSTOP_MARGIN_SEC = 5.0
"""墙钟兜底计时器的额外余量（秒），刻意给得宽松。

兜底值 = ``timeout + overhead + 本余量``，比 ``BskRunner`` 的理论最坏耗时再多 5 秒，
用来吸收它没算进那两个常量的开销（创建子进程、Windows 上杀软扫描 bsk.exe、
负载高时的调度延迟）。

为什么宁大勿小：兜底触发时会 ``cancel`` 掉正在执行的 ``BskRunner.run``，而那是
可能留下一个 bsk 子进程的操作。所以它的定位是"runner 彻底失控时的最后一道
保险丝"，而不是常规控制流 —— 正常路径永远不该碰到它。
（同理，它一旦触发，我们会把它翻译成 ``BskTimeout`` 并照常重试，而不是直接放弃。）
"""

STOP_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    # ↓↓ 实测原文（bsk 0.3.2）：
    # extension rejected tool.session_stop: RpcError {
    #     code: ProtocolError, message: "Background execution cleanup timed out" }
    # 此时会话仍然活着（Agent Window 还开着），而紧接一次重试就能成功。
    "background execution cleanup",
    "cleanup timed out",
    # bsk 侧 RPC 层的报错外壳。注意与 ``BskProtocolError``（那个是我们发错命令
    # 或输出不是 JSON）区分开：这里匹配的是错误文本里的 bsk 内部标记。
    "rpcerror",
    "protocolerror",
    # 扩展拒绝了这次 stop：可能是瞬时状态（清理还没收尾），也可能是权限。
    # 宁可多花一秒重试，也不要漏掉一次可以救回来的停止（漏掉 = 用户桌面留一个窗口）。
    "extension rejected",
    # 理论上会先被 errors.classify 归成 BskSessionBusy，这里只是文本兜底。
    "session_busy",
)
"""错误文本里出现任意一项，就认为 stop 失败是瞬时的、值得重试。

为什么按文本而不是按异常类型判定：实测那个失败在插件侧是
``BskError``（甚至可能是 ``BskProtocolError``），与"我们自己参数写错"完全同类，
按类型一刀切会把这条唯一有实测证据的瞬时故障一起排除掉。
反过来说，类型只用来认那些与文本无关的瞬时类别（超时、忙）。
"""


def _is_transient_stop_error(exc: BaseException) -> bool:
    """这次 ``session stop`` 失败是否值得重试。

    判定依据（按优先级）：

    1. 明确不可重试的类别直接否掉 —— ``BskNotInstalled``（bsk 没装）、
       ``BskVersionError``（CLI 与扩展版本对不上）：这两类重试多少次结果都一样，
       只会白等。它们放在最前面是为了防止"错误文本里恰好含某个标记"导致误判。
    2. ``outcome_unknown`` 类错误不重试 —— 这是本模块的铁律（见 ``errors.py`` 的
       ``OUTCOME_UNKNOWN_REASONS``）。stop 本身是幂等的、重试其实安全，但这里刻意
       不开这个口子：一旦给"结果未知"开了重试，将来很容易被照着抄到 click/fill
       这类重放会出事的动作命令上。这类失败的兜底手段是 journal + 下次启动的
       ``recover_orphans``。
    3. 文本含 ``STOP_TRANSIENT_ERROR_MARKERS`` 之一 → 是瞬时故障，重试。
       这是实测那条 ``Background execution cleanup timed out`` 的判定路径。
    4. 类型本身就是瞬时类别 → 重试：``BskTimeout``（超时；stop 幂等，重放安全）、
       ``BskSessionBusy``（会话上有 in-flight 命令，等一小会儿再来）。
    5. 其余一律不重试（包括没有瞬时标记的 ``BskProtocolError``、``BskBrowserError``、
       普通 ``BskError``、以及任何非 bsk 异常）。宁可少试一次，也不要把
       ``terminate()`` 的时间浪费在注定失败的重试上。

    Note:
        重试 stop 是安全的：如果会话其实已经停了，bsk 会回 ``not_found``，
        而 ``_stop_session_id`` 已经把 ``not_found`` 当成"目的已达成"的成功处理。
        所以"多停一次"最多多花一次往返，不会误判、也不会伤到别人的会话
        （命令里带的是精确 id，绝不是 ``--all``）。

    Args:
        exc: ``run_or_raise`` 抛出的异常（也可能是任何别的异常）。

    Returns:
        True 表示应当重试。
    """
    if isinstance(exc, (BskNotInstalled, BskVersionError)):
        return False

    reason = getattr(exc, "reason", "")
    if reason in OUTCOME_UNKNOWN_REASONS:
        return False

    haystack = f"{exc}".lower()
    for marker in STOP_TRANSIENT_ERROR_MARKERS:
        if marker in haystack:
            return True

    return isinstance(exc, (BskTimeout, BskSessionBusy))


RECOVER_LIST_TIMEOUT_SEC = 5.0
"""恢复时 ``session list`` 的超时。

实测这条命令 0.02-0.03 秒返回，5 秒是 100 倍余量；而它跑在插件启动路径上，
不能让用户对着一个卡住的 daemon 干等（超时也只是退化成"清掉记录"）。
"""

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
加这个上限是为了保证不出现活锁。
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
    """把新建成功的会话字段原地写进旧对象。

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

    锁和会话是分开的两件事：会话重建后 ``session`` 会换内容，
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

    在这之后绝不允许再用它重建会话，否则会造出一个无人管理的僵尸会话。
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
        journal: 可选的持久化所有权记录（``bsk/journal.py``）。传了它，
            每建一个会话就落盘、每正常停一个就删记录，从而使
            :meth:`recover_orphans` 能在下一个进程里认出遗留会话。
            不传（None）时所有 journal 动作都变成无操作 —— 现有调用方
            与测试因此完全不受影响。
        stop_max_attempts: ``session stop`` 的最多尝试次数（含首次）。
            默认取模块常量 ``STOP_MAX_ATTEMPTS``。抽成参数只为测试能把
            次数与延迟调小，避免每个用例真的 sleep 半秒；生产代码不要传它。
        stop_retry_delay_sec: 两次 stop 之间的等待时长。默认 ``STOP_RETRY_DELAY_SEC``。
        stop_retry_budget_sec: 重试阶段的墙钟总预算。默认 ``STOP_RETRY_BUDGET_SEC``。
        stop_timeout_sec: 单次 stop 的超时。默认 ``STOP_TIMEOUT_SEC``。
        stop_total_budget_sec: 整次 ``_stop_session_id`` 的墙钟总上限。
            ``None``（默认）时按
            ``stop_timeout_sec + stop_attempt_overhead_sec + stop_retry_budget_sec``
            推导（生产值 37 秒），保证"首次尝试的正常最坏耗时"不被削减。
        stop_attempt_overhead_sec: 单次尝试超时之后 runner 还要花的清理时间。
            默认 ``STOP_ATTEMPT_OVERHEAD_SEC``（= 优雅取消宽限 + 收管道，17 秒）。
            测试用假 runner 时把它调成 0，因为假 runner 不做真实清理。
        stop_attempt_backstop_sec: 单次尝试的硬性墙钟兜底上限。``None``（默认）时
            按 ``stop_timeout_sec + 本开销 + 余量`` 自动推导。
            抽成参数同样只为测试能把兜底调小，不必真的等 30 秒。

    Attributes:
        _clock: ``time.monotonic`` 的接缝，单元测试可替换成假时钟来验证
            LRU 与空闲回收，避免测试里真的 sleep。生产代码不要动它。
            ⚠️ stop 的重试预算不用它（假时钟不会自己走，会把预算变成永不
            到期）；见 ``_stop_session_id`` 里的说明。
    """

    def __init__(
        self,
        runner: BskRunner,
        settings: "Settings | Any",
        *,
        browser_probe: BrowserProbe | None = None,
        journal: SessionJournal | None = None,
        stop_max_attempts: int = STOP_MAX_ATTEMPTS,
        stop_retry_delay_sec: float = STOP_RETRY_DELAY_SEC,
        stop_retry_budget_sec: float = STOP_RETRY_BUDGET_SEC,
        stop_timeout_sec: float = STOP_TIMEOUT_SEC,
        stop_total_budget_sec: float | None = None,
        stop_attempt_overhead_sec: float = STOP_ATTEMPT_OVERHEAD_SEC,
        stop_attempt_backstop_sec: float | None = None,
    ) -> None:
        self._runner = runner
        self._browser_probe = browser_probe
        self._journal = journal

        # stop 重试的可调参数。只给测试用：生产路径一律用模块常量的默认值。
        # 非法值（<=0、非数字）一律回退到常量，避免调用方传 0 导致
        # "预算为 0 → 永远不重试"或"次数为 0 → 一次都不试"这种静默失效。
        attempts = _as_int(stop_max_attempts, STOP_MAX_ATTEMPTS)
        self._stop_max_attempts = attempts if attempts >= 1 else STOP_MAX_ATTEMPTS
        delay = _as_float(stop_retry_delay_sec, STOP_RETRY_DELAY_SEC)
        self._stop_retry_delay_sec = delay if delay >= 0 else STOP_RETRY_DELAY_SEC
        budget = _as_float(stop_retry_budget_sec, STOP_RETRY_BUDGET_SEC)
        self._stop_retry_budget_sec = budget if budget > 0 else STOP_RETRY_BUDGET_SEC
        timeout = _as_float(stop_timeout_sec, STOP_TIMEOUT_SEC)
        self._stop_timeout_sec = timeout if timeout > 0 else STOP_TIMEOUT_SEC
        # runner 超时后的清理开销：生产用 runner.py 的真实常量（17s），
        # 测试用假 runner 时传 0（假 runner 不做任何真实清理）。
        overhead = _as_float(stop_attempt_overhead_sec, STOP_ATTEMPT_OVERHEAD_SEC)
        self._stop_attempt_overhead_sec = (
            overhead if overhead >= 0 else STOP_ATTEMPT_OVERHEAD_SEC
        )
        # 整次调用的总上限：显式传值就用它（测试用），否则按"首次尝试的正常最坏
        # 耗时 + 重试预算"推导。首次那一份不做削减，否则等于掐断 runner 的
        # 清理流程（见 STOP_RETRY_BUDGET_SEC 的说明）。
        if stop_total_budget_sec is None:
            self._stop_total_budget_sec = (
                self._stop_timeout_sec
                + self._stop_attempt_overhead_sec
                + self._stop_retry_budget_sec
            )
        else:
            total = _as_float(stop_total_budget_sec, 0.0)
            self._stop_total_budget_sec = (
                total if total > 0 else self._stop_timeout_sec
            )
        # 墙钟兜底：显式传值就用它（测试用），否则按 runner 的理论最坏耗时推导。
        if stop_attempt_backstop_sec is None:
            self._stop_attempt_backstop_sec = (
                self._stop_timeout_sec
                + self._stop_attempt_overhead_sec
                + STOP_ATTEMPT_BACKSTOP_MARGIN_SEC
            )
        else:
            backstop = _as_float(stop_attempt_backstop_sec, 0.0)
            self._stop_attempt_backstop_sec = (
                backstop if backstop > 0 else self._stop_timeout_sec
            )
        # 兜底永远不该晚于总预算生效：否则"总耗时有界"就不再成立。
        self._stop_attempt_backstop_sec = min(
            self._stop_attempt_backstop_sec, self._stop_total_budget_sec
        )

        self._max_sessions = max(
            1, _as_int(_setting(settings, "max_sessions", DEFAULT_MAX_SESSIONS),
                       DEFAULT_MAX_SESSIONS)
        )
        idle = _as_float(
            _setting(settings, "idle_release_sec", DEFAULT_IDLE_RELEASE_SEC),
            DEFAULT_IDLE_RELEASE_SEC,
        )
        self._idle_release_sec = idle
        """<= 0 表示关闭自动回收（用户显式选择），而不是"立刻全回收"。"""

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
        """只保护 ``_entries`` 字典的增删（内部不含长 await），
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
            # --- stop 重试的可观测性（见 _stop_session_id）---
            #
            # stop_retries：实际发生的 stop 重试次数（不含首次尝试）。
            #     用来诊断"瞬时故障有多常见"。
            # stop_recovered：靠重试才停成功的次数。这是本次修复直接救回来的
            #     会话数 —— 它 > 0 就说明旧代码会在这里泄漏一个浏览器窗口。
            # stop_retry_budget_skips：因重试预算耗尽而主动放弃剩余重试的次数
            #     （不是失败，是止损）。
            "stop_retries": 0,
            "stop_recovered": 0,
            "stop_retry_budget_skips": 0,
        }
        self._recent_stop_errors: list[str] = []

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def acquire(self, key: str) -> BskSession:
        """取当前 key 的会话；没有就建一个。

        同一 key 并发调用只会 ``start`` 一次（其余协程等待并复用），
        返回的也是同一个对象。

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
        """核心方法：带上会话 id 执行一条 bsk 命令。

        自动处理：per-key 加锁（同 key 串行）、会话失效时重建、``session_busy``
        短暂重试、``outcome_unknown`` 标记不确定态。任何情况下都只重试一次。

        Args:
            key: 业务键（umo 等）。
            args_builder: 接收 ``session_id`` 返回完整参数列表的可调用对象，
                例如 ``lambda sid: ["observe", "--session", sid, "--json"]``。
                可以是协程函数（返回值会被 await）。
                用函数而不是现成列表，是为了重建会话后能用新的 session_id
                重新构造参数。
            timeout: 超时秒数；None 时用 ``settings.command_timeout_sec``。
            allow_uncertain: 显式声明"这是只读动作"，允许在会话处于不确定态时
                继续执行（例如 observe、截图）。写/点击类动作绝对不要传 True。

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

            # 重试次数硬上限 = MAX_ATTEMPTS(2)，即"首次 + 重试一次"。
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
                        # 用失败触发重建：不先 stop 旧会话（bsk 已确认它不存在），
                        #   再由下一轮用新的 session_id 重新构造参数。
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
        ``session stop <id>``（位置参数，唯一例外，绝不用 ``--all``）。

        Returns:
            True 表示这个 key 原本有会话，且已确认在 bsk 侧不再存活
            （显式 stop 成功，或 bsk 回 ``not_found`` 说明早已被回收）。
            False 表示原本就没有会话记录，或 stop 因其他原因失败 ——
            无论哪种，本地记录都已删除，stop 失败原因见 ``stats()``。

        Note:
            本方法不会抛异常（terminate 路径不能被它打断）。
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

    async def release_all(self) -> int:
        """关闭所有会话，返回成功关闭的数量。``terminate()`` 用。

        先一次性把本地记录全部摘除（只做同步操作，不给并发留窗口），
        再并发 stop。任何一个 stop 失败都不影响其他会话的清理，也不会抛异常。

        时间上界（``terminate()`` 绝不能被拖住，这是硬要求）：
        :meth:`_shutdown_entries` 用 ``asyncio.gather`` 并发停所有会话，
        所以总耗时 ≈ 单个会话的最坏耗时，而不是会话数 × 单次耗时。
        单个会话的最坏耗时又有明确上界：

            RELEASE_WAIT_SEC（等在飞命令，5s）
          + STOP_TIMEOUT_SEC + STOP_RETRY_BUDGET_SEC（stop 含重试，15 + 5s）

        这个上界不随会话数增长。相比加重试之前（单个会话 5 + 15s），
        这里多出的只有 5 秒重试预算 —— 换来的是"不再泄漏用户桌面上的浏览器窗口"，
        而实测那条瞬时故障是毫秒级返回的，实际上根本用不到这 5 秒。
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

    async def recover_orphans(self) -> int:
        """清理上一次进程遗留的、仍然活着的自己的会话。

        场景：AstrBot 被强杀（任务管理器结束进程 / 崩溃 / 断电）时 ``terminate()``
        不会执行，而 bsk daemon 的生命周期独立于 AstrBot —— 它和那些会话都还活着，
        用户桌面上于是留着没人管的浏览器窗口。本方法在下次 ``initialize()`` 时
        按 journal 里的记录把它们收干净。

        算法（每一步都是"只停自己的"）：

        1. 读 journal；空就直接返回 0（绝大多数启动走这条路，零额外开销）；
        2. ``bsk session list --json`` 拿当前活着的会话；
        3. 逐条比对：``session_id`` 与 ``agent_window_id`` 都匹配才停；
        4. 清空 journal。

        绝不用 ``bsk session stop --all``：那会连带停掉别的程序
        （用户自己的 DSH、他们的另一个 AI 工具）创建的会话。这里只按精确 id 停。

        为什么要比对 ``agent_window_id``：``session_id`` 只有 4 个小写字母
        （26^4 ≈ 45.7 万空间），存在碰撞可能 —— 我们记录的 ``mnaa`` 已经过期，
        之后另一个程序也建了一个 ``mnaa``。此时仅凭 session_id 匹配就会误停别人的
        会话。``agent_window_id`` 是另一扇窗口的编号，数值空间大得多，两者同时
        相同才足以认定"这就是我们上次留下的那个"。

        Note:
            - 绝不抛异常：它在插件启动路径上跑，抛异常会让插件加载失败；
            - daemon 没在跑（``session list`` 失败）时不算错误：daemon 都没了，
              会话自然也没了，直接清空 journal 即可；
            - 停不掉某条会话时不阻止 journal 清空 —— 下一次启动还会再试一次
              也没有意义（那条记录对应的进程已经死了，反复重试只会拖慢启动）。

        Returns:
            实际清理掉的会话数量。
        """
        journal = self._journal
        if journal is None:
            return 0

        try:
            recorded = journal.load()
        except Exception as exc:  # noqa: BLE001 - journal 自身已经吞异常，这里再兜一层
            logger.debug("读取会话 journal 失败（忽略）：%r", exc)
            return 0

        if not recorded:
            return 0

        # --- 问 daemon 现在有哪些会话 ---
        try:
            live = await self._list_live_sessions()
        except Exception as exc:  # noqa: BLE001
            # 包括"daemon 没在跑"：那不是错误，会话自然也随着 daemon 一起没了。
            logger.debug("session list 失败，跳过孤儿会话恢复：%r", exc)
            live = None

        if live is None:
            self._journal_clear()
            return 0

        # --- 逐条比对，只停自己的 ---
        stopped = 0
        skipped = 0
        for entry in recorded:
            matches = live.get(entry.session_id)
            if matches is None or entry.agent_window_id not in matches:
                # 两种情况都跳过：
                #   - bsk 里已经没有这个 id（会话早被回收/正常停掉了）；
                #   - 有这个 id，但 agent_window_id 对不上 —— 那是别人的会话，
                #     只是恰好撞了 id。绝不能停。
                skipped += 1
                logger.debug(
                    "跳过遗留会话 %s（agent_window_id=%s）：daemon 侧不匹配",
                    entry.session_id,
                    entry.agent_window_id,
                )
                continue
            if await self._stop_session_id(entry.session_id):
                stopped += 1
            else:
                skipped += 1

        if stopped or skipped:
            logger.info(
                "恢复清理：停掉 %d 个上次遗留的浏览器会话，跳过 %d 个不匹配的",
                stopped,
                skipped,
            )
        # 无论停成功几个都清空：这些记录属于已经退出的进程，
        # 留着只会让每次启动都重复做同一轮无用功。
        self._journal_clear()
        return stopped

    async def _list_live_sessions(self) -> dict[str, set[int]]:
        """``bsk session list --json`` → ``{session_id: {agent_window_id, ...}}``。

        Raises:
            BskError: daemon 没在跑或命令失败。调用方会把"失败"当成
                "没有遗留会话"处理。
        """
        result = await self._runner.run_or_raise(
            ["session", "list", "--json"], timeout=RECOVER_LIST_TIMEOUT_SEC
        )
        raw = result.data
        mapping: dict[str, set[int]] = {}
        if not isinstance(raw, list):
            return mapping
        for item in raw:
            if not isinstance(item, dict):
                continue
            session_id = _as_str(item.get("session_id"))
            if not session_id:
                continue
            mapping.setdefault(session_id, set()).add(
                _as_int(item.get("agent_window_id"), 0)
            )
        return mapping

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

        本模块不创建任何后台任务（空闲回收由 ``main.py`` 调度），
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
        """取到 key 的 entry 并持有它的锁，退出时自动释放。

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
        """取 key 的槽位；没有就先占位再返回（真正的 start 在锁内做）。

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

        进函数先把 id 清空：万一 ``start`` 失败，槽位必须如实报告"当前没有
        会话"，否则下次 ``execute`` 会拿着死 id 再撞一次 ``not_found``。
        """
        previous_id = entry.session.session_id
        entry.session.session_id = ""
        fresh = await self._start()
        # 立刻落盘 —— 这是整个崩溃恢复机制的起点。
        #   必须在这里（而不是等 acquire 返回后）写：从 start 成功到调用方拿到
        #   会话之间有任何一处崩溃，那个会话就已经无人知晓了。
        #   注意这条记录此时还不属于任何槽位，所以即使下面发现 entry 已被
        #   摘除，也要先把记录清掉再抛异常。
        self._journal_add(fresh)
        if previous_id and previous_id != fresh.session_id:
            # 这是一次"带旧 id 的重建"（``not_found`` 触发的路径不会去 stop
            # 旧会话，所以那个 id 的 journal 记录还在）。把它删掉：bsk 已经确认
            # 旧会话不存在了，留着只会让下次启动拿它去比对，白多一轮无用功。
            self._journal_remove(previous_id)

        if entry.closed:
            # 极端竞态：槽位在 start 期间被 release/reap 摘走（start 慢于
            # RELEASE_WAIT_SEC 时可能发生）。这个新会话已经没人管了，必须立刻
            # 停掉 —— 否则它会一直占着用户的浏览器，而 bsk 5 分钟后才回收，
            # 且回收不保证归还借用的标签页。
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
            stop_old: 是否先显式 stop 旧会话。``not_found`` 触发的重建必须传
                False：bsk 已经说了这个会话不存在，再 stop 一次既多花一次往返，
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

        唯一的例外是 :class:`BskBrowserAmbiguous`：它表示"探测成功了，但
        同时连着多个浏览器，无法确定用哪一个"——这是用户配置问题，
        不是探测失败。它必须原样抛出去让用户看到，否则会被下面的
        ``except Exception`` 吞掉，退化成"不传 --browser，让 bsk 随便选一个"
        （正是本次要修掉的静默随机行为）。注意用户的显式配置在上面的
        ``if`` 里已经直接返回，所以"配了就必须尊重配置"不受此影响。
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
        except BskBrowserAmbiguous:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测失败只是回退，不是错误
            logger.debug("browser_probe 探测失败，回退到 bsk 默认浏览器：%r", exc)
            return ""

    async def _stop_entry(self, entry: _Entry) -> bool:
        """停掉一个槽位里的会话。绝不抛异常。

        Returns:
            True 表示 bsk 侧确认该会话已不存在（stop 成功，或本来就没了）。
        """
        if not entry.session.session_id:
            return True  # 只有占位槽位，没有真实会话可停。
        return await self._stop_session_id(entry.session.session_id)

    async def _stop_session_id(self, session_id: str) -> bool:
        """按 id 停掉一个会话，对瞬时故障做有界重试。绝不抛异常。

        为什么必须重试（实测缺陷）：bsk 0.3.2 会间歇性地对 ``session stop`` 返回

            extension rejected tool.session_stop: RpcError {
                code: ProtocolError, message: "Background execution cleanup timed out" }

        此时会话仍然活着（Agent Window 还开着），而旧实现只试一次就放弃 ——
        会话于是泄漏在 daemon 里，直到 bsk 自己 5 分钟后回收，且回收不保证归还
        借用的标签页，用户桌面上会留下没关的浏览器窗口。实测（本机真实浏览器）
        "曾经 navigate 失败过"的会话 48 轮泄漏 6 轮（≈12.5%），而紧接一次重试
        就能成功，说明这是瞬时状态 —— 重试正是对症的修法。

        重试 stop 是安全的，理由有两条：

        1. 幂等：如果会话其实已经停了，bsk 会回 ``not_found``，而本方法已经把
           ``not_found`` 当作"目的已达成"处理（见下面的 ``except BskSessionGone``）。
           所以"多停一次"最多多花一次往返，不会误判。
        2. 不碰别人的会话：命令里带的是精确 id，永远不是
           ``session stop --all``（红线，见模块文档第 3 条与既有守护测试）。

        时间上界（``terminate()`` 绝不能被拖住）：整次调用只有一个截止
        时刻，在进入循环前算好：

            总上限 = STOP_TIMEOUT_SEC + STOP_RETRY_BUDGET_SEC = 15 + 5 = 20 秒

        每次尝试的超时取 ``min(单次上限, 剩余预算)``，每次等待也取
        ``min(重试延迟, 剩余预算)``，所以再怎么写都不会越过这 20 秒。
        实测那条 ``cleanup timed out`` 是毫秒级快速失败，根本用不到 15 秒超时，
        因此重试实际只花几十毫秒；多给的 5 秒预算换来的是"不再泄漏一个窗口"。
        多会话并发时由 ``release_all`` 用 ``asyncio.gather`` 并发跑，总耗时不随
        会话数线性累加（见 ``release_all`` 的说明）。

        与 journal 的分工：这里的重试是本次运行内的第一道防线；
        ``journal.py`` + ``recover_orphans()``（下次启动时按记录精确回收）是
        第二道。两道防线互补：重试解决"进程还活着但这一次 stop 撞上瞬时故障"，
        journal 解决"进程被强杀、根本没有机会 stop"。所以下面对 journal 的
        增删语义必须与旧实现一致：

        - stop 成功 → 删记录；
        - stop 报 not_found → 也删（bsk 已确认会话不存在）；
        - stop 失败（含重试用尽） → 刻意保留记录，留给下次启动兜底。

        Args:
            session_id: bsk 会话 id。空串直接当成功（没有会话可停）。

        Returns:
            True 表示 bsk 侧确认该会话已不存在（stop 成功，或本来就没了）。
        """
        if not session_id:
            return True

        # id 是位置参数！这是 bsk 里唯一的例外（其余命令都用 --session）。
        # 绝不用 `session stop --all`：那会停掉别的程序（如用户的 DSH）的会话。
        args = ["session", "stop", session_id]

        # 整次调用的唯一截止时刻（单调钟）。所有尝试与等待都必须落在它之内，
        #   于是"总耗时有界"这件事是可证明的，而不是靠各处常量互相心算。
        #
        #   刻意用 time.monotonic() 而不是 self._clock：self._clock 是给 LRU/空闲
        #   回收用的接缝，单元测试里会被换成不会自己走的假时钟，拿它做超时预算会
        #   导致重试永不停止。_wait_idle 也是同样的选择。
        deadline = time.monotonic() + self._stop_total_budget_sec
        last_exc: BaseException | None = None
        attempt = 0

        # 循环次数硬上限 = self._stop_max_attempts（生产值 STOP_MAX_ATTEMPTS），
        #   且每个分支要么 return、要么 break，所以不存在无限重试的可能。
        for attempt in range(1, self._stop_max_attempts + 1):
            # 每次尝试的两条时限都从剩余总预算反推，而不是各用各的固定常量。
            #
            #   关键点：一次尝试的完整耗时是 ``attempt_timeout + 超时后的清理开销``
            #   （runner 要先关 stdin 给宽限、再 kill、再收管道）。所以必须先把这份
            #   开销预留出来，再决定这次能拿多少超时 —— 否则我们会把 runner 的
            #   清理流程从中间掐断，反而更可能留下一个 bsk 子进程。
            #
            #   于是每次尝试的耗时有上界 ``min(剩余, attempt_timeout + 开销)``，
            #   "整次调用不超过 deadline"因此可以逐条推出来。
            remaining = max(0.0, deadline - time.monotonic())
            budget_for_timeout = remaining - self._stop_attempt_overhead_sec
            attempt_timeout = min(self._stop_timeout_sec, max(0.0, budget_for_timeout))
            # "值得再试一次"的门槛：正常情况下是 STOP_RETRY_MIN_TIMEOUT_SEC（1s），
            # 但若单次超时本身就被配得比它还小，门槛必须跟着降下来 ——
            # 否则会出现"每次尝试本来就只给 0.05s，却要求剩余预算 ≥ 1s"的矛盾，
            # 让重试永远发不出去（把配置值当成错误来用）。
            min_worthwhile = min(
                STOP_RETRY_MIN_TIMEOUT_SEC, self._stop_timeout_sec
            )
            if attempt > 1 and attempt_timeout < min_worthwhile:
                # 剩余额度已经不够"一次有意义的尝试 + 它的清理开销"了。
                # 再发一次只会立刻超时，纯粹浪费 terminate() 的时间。
                # 首次尝试不走这个判断（它的额度由总预算本身保证）。
                self._counters["stop_retry_budget_skips"] += 1
                logger.warning(
                    "停止会话 %s 的剩余预算 %.2fs 不足以再试一次，放弃重试",
                    session_id,
                    remaining,
                )
                break
            if attempt == 1 and attempt_timeout < min_worthwhile:
                # 走到这里说明总预算被外部配得异常小（只可能出现在测试里）。
                # 首次尝试仍然要发一次，但额度绝不越过剩余总预算。
                attempt_timeout = min(remaining, min_worthwhile)
            if attempt > 1:
                # 只在真的要再发一次时记数，这样 stop_retries 的含义是
                # "实际发出的重试次数"，而不是"打算重试的次数"。
                self._counters["stop_retries"] += 1

            # 兜底的三条约束取最小：
            #   ① 兜底常量 —— "runner 彻底失控"时保险丝的长度；
            #   ② remaining —— 绝不越过总截止时刻（保证总耗时有界）；
            #   ③ 本次超时 + 清理开销 + 余量 —— 对守约的 runner 永不触发。
            # 因为 attempt_timeout ≤ remaining - 开销（上面的预留保证了这点），
            # 三者取最小后仍 ≥ runner 的合法最坏耗时（attempt_timeout + 开销），
            # 所以兜底不会抢在 runner 自己的超时之前动手 —— 那会把正常的
            # BskTimeout 变成一个语义更差的兜底错误。
            backstop = min(
                self._stop_attempt_backstop_sec,
                remaining,
                attempt_timeout
                + self._stop_attempt_overhead_sec
                + STOP_ATTEMPT_BACKSTOP_MARGIN_SEC,
            )
            # 兜底至少要 ≥ 本次超时，否则 wait_for 会立刻超时（等于不执行）。
            backstop = max(backstop, attempt_timeout)
            try:
                await self._stop_once(args, session_id, attempt_timeout, backstop)
            except BskSessionGone:
                # bsk 说它已经不存在了 —— 对我们来说目的已达成，不算失败。
                self._counters["stopped"] += 1
                self._journal_remove(session_id)
                if attempt > 1:
                    # 靠重试救回来的（上一次报了 not_found 之外的错，这次 bsk 说
                    # 会话没了）—— 单列一个计数器，便于诊断"瞬时故障有多常见"。
                    self._counters["stop_recovered"] += 1
                    logger.info(
                        "会话 %s 在第 %d 次尝试时确认已停止", session_id, attempt
                    )
                else:
                    logger.debug("会话 %s 已不存在，无需停止", session_id)
                return True
            except Exception as exc:  # noqa: BLE001 - 清理路径吞掉一切
                last_exc = exc
                remaining = deadline - time.monotonic()

                if attempt >= self._stop_max_attempts:
                    break  # 次数用尽
                if not _is_transient_stop_error(exc):
                    # 重试无意义（bsk 没装 / 版本不匹配 / 命令本身有问题）。
                    # 立刻放弃，别浪费 terminate() 的时间预算。
                    logger.debug(
                        "停止会话 %s 失败且不属于瞬时故障，不再重试：%r",
                        session_id,
                        exc,
                    )
                    break
                # 预算检查与 stop_retries 记数统一放在循环开头（各只有一处），
                # 这里只负责打日志并等待。
                logger.warning(
                    "停止会话 %s 第 %d 次失败（瞬时故障），%.2fs 后重试：%r",
                    session_id,
                    attempt,
                    self._stop_retry_delay_sec,
                    exc,
                )
                # 等待时长也受预算约束：绝不会睡过 deadline。
                await asyncio.sleep(
                    min(self._stop_retry_delay_sec, max(0.0, remaining))
                )
                continue
            else:
                self._counters["stopped"] += 1
                # 已确认停掉，立刻从 journal 里移除 —— 否则下次启动会去停一个已经
                # 不存在的 id，白白多一次往返（虽然比对逻辑会挡住误停）。
                self._journal_remove(session_id)
                if attempt > 1:
                    self._counters["stop_recovered"] += 1
                    logger.info(
                        "会话 %s 在第 %d 次尝试时停止成功（瞬时故障已恢复）",
                        session_id,
                        attempt,
                    )
                else:
                    logger.info("已停止浏览器会话 %s", session_id)
                return True

        self._counters["stop_failed"] += 1
        self._record_stop_error(f"{session_id}: {last_exc}")
        logger.warning(
            "停止会话 %s 失败（共尝试 %d 次，上限 %d）：%r",
            session_id,
            attempt,
            self._stop_max_attempts,
            last_exc,
        )
        # 刻意不删 journal 记录：这次没停掉，会话可能还活着，
        # 留着记录能让下一次启动的 recover_orphans 再试一次。
        return False

    async def _stop_once(
        self, args: list[str], session_id: str, attempt_timeout: float, backstop: float
    ) -> None:
        """发一次 ``session stop`` 命令，成功即返回，失败即抛。

        单独抽出来只为一件事：把"硬性墙钟兜底"与业务重试逻辑分开，让
        :meth:`_stop_session_id` 的重试循环保持可读。

        兜底的必要性：``BskRunner.run`` 自己就有超时，正常情况这一层永远不触发。
        但只要 runner 因为任何原因（bug、Windows 上进程卡在不可中断的系统调用）
        不返回，兜底保证 ``terminate()`` 仍然能在有限时间内结束 —— 那是硬要求。

        Args:
            args: 完整参数列表（``["session", "stop", <id>]``）。
            session_id: 仅用于错误信息。
            attempt_timeout: 本次尝试的超时（已按剩余总预算收敛过）。
            backstop: 本次尝试的硬性墙钟上限（同样已按剩余总预算收敛过，
                且严格大于 ``attempt_timeout``）。

        Raises:
            BskError: 原样抛出 runner 的分类异常；
                兜底触发时抛出 ``code="stop_backstop_timeout"`` 的 ``BskTimeout``
                （刻意不是裸 ``TimeoutError``：后者没有 ``friendly`` 中文提示，
                进 ``recent_stop_errors`` 也只是一句空话，排查时毫无指向性）。
        """
        try:
            await asyncio.wait_for(
                self._runner.run_or_raise(
                    args, timeout=attempt_timeout, expect_json=False
                ),
                timeout=backstop,
            )
        except asyncio.TimeoutError as exc:
            # 走到这里说明 runner 没有遵守自己的超时约定（正常路径永远到不了）。
            raise BskTimeout(
                f"session stop {session_id} 超过 {backstop:.1f}s 未返回"
                "（runner 未遵守自身超时约定）",
                friendly="关闭浏览器会话超时了，稍后会自动重试。",
                code="stop_backstop_timeout",
                exit_code=EXIT_TIMEOUT,
            ) from exc

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

    # ------------------------------------------------------------------
    # 内部：journal（全部是"尽力而为"，失败只记 debug 日志）
    #
    # journal 是辅助机制，不是会话生命周期的一部分。它的读写失败绝不能
    # 影响会话的正常创建/停止 —— 一个只会写诊断记录的功能，不该有能力
    # 让浏览器操作失败。
    # ------------------------------------------------------------------

    def _journal_add(self, session: BskSession) -> None:
        """把一个刚建成的会话记进 journal。任何失败都只记 debug 日志。"""
        if self._journal is None or not session.session_id:
            return
        try:
            self._journal.add(
                JournalEntry(
                    session_id=session.session_id,
                    browser_instance_id=session.browser_instance_id,
                    agent_window_id=session.agent_window_id,
                    # 墙钟，不是 monotonic：这条记录要跨进程读。
                    created_at=time.time(),
                    pid=os.getpid(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 只影响可恢复性，不影响本次会话
            logger.debug("写会话 journal 失败（忽略）：%r", exc)

    def _journal_remove(self, session_id: str) -> None:
        """会话已被正常停掉，从 journal 里移除它的记录。"""
        if self._journal is None or not session_id:
            return
        try:
            self._journal.remove(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("清理会话 journal 记录失败（忽略）：%r", exc)

    def _journal_clear(self) -> None:
        """清空 journal。"""
        if self._journal is None:
            return
        try:
            self._journal.clear()
        except Exception as exc:  # noqa: BLE001
            logger.debug("清空会话 journal 失败（忽略）：%r", exc)
