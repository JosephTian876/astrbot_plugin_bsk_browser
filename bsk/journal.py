"""会话所有权 journal —— 把"我创建了哪些浏览器会话"落到磁盘上。

要解决的问题：``bsk`` 的会话由独立于 AstrBot 的常驻 daemon 持有。
本插件只在内存里记着自己创建过哪些会话（``SessionManager._entries``），于是：

- AstrBot 被强杀（任务管理器结束进程、崩溃、断电）时 ``terminate()`` 不会执行；
- daemon 还活着，那些会话也还活着，用户桌面上留下没人管的浏览器窗口；
- AstrBot 重启后插件内存是空的，不认识这些会话，于是永远清理不掉。

对策是持久化的所有权记录：会话一建出来就落盘，正常 stop 后删掉，
下次启动时据此把"仍然活着的、自己的"会话停掉。

绝对不能用 ``bsk session stop --all`` 来"一把清干净" —— 那会连带停掉
别的程序（用户自己的 DSH、他们的另一个 AI 工具）创建的会话。所以：

1. journal 里除了 ``session_id``，还必须记 ``browser_instance_id`` 与
   ``agent_window_id``；
2. 清理时必须逐条比对（见 ``session.SessionManager.recover_orphans``），
   匹配不上就跳过 —— 那不是我们的。

v2 新增两个字段（``request_id`` / ``state``）：``bsk session start`` 支持
``--request-id`` 之后，"创建一个会话"变成两段式 —— 令牌先落盘，会话创建
成功后再补上 ``session_id``。于是 journal 里会出现**还没有 session_id、
只有令牌**的记录，那是"start 的回执丢了"时唯一能把窗口找回来的线索
（``session start`` 超时，窗口可能已经开出来了，但调用方拿不到 id）。
读旧文件、写新文件都必须容忍这种半成品记录。

设计约束（写代码时请勿破坏）：

- 纯标准库，不 import astrbot，也不 import 内置 ``logging``；日志由 ``main.py``
  注入（见 ``bsk/logger.py``），未注入时走 ``NULL_LOGGER``，可独立单测；
- 本模块的任何方法都不抛异常（``load`` / ``add`` / ``add_checked`` /
  ``remove`` / ``remove_by_request`` / ``clear``）：它在插件启动路径上跑，
  一个异常就是插件加载失败。``add_checked`` 同样不抛，但它**返回 bool**
  报告成败 —— 需要知道"令牌到底落盘了没有"的调用方只能靠返回值，
  因为这里的失败刻意不抛异常（见下一条）；
- 「写失败」是静默的，所以凡是要靠成败做决策的调用点（例如发出
  ``session start`` 之前必须先落盘启动令牌）**必须用** ``add_checked``
  的返回值判断，不能靠捕获 ``add`` 的异常 —— 它从不抛；
- 原子写：先写 ``<path>.tmp`` 再 ``os.replace()``，避免写一半被杀留下坏文件；
- 显式 UTF-8：Windows 中文环境下默认编码是 gbk，写中文诊断信息会炸；
- 读不到就当没有：文件不存在、是空文件、是半截 JSON、是二进制垃圾、
  路径是个目录、没有读权限 —— 一律返回空列表。宁可漏清，绝不因为解析失败
  让插件起不来。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logger import NULL_LOGGER, LoggerLike
from .paths import default_journal_path

__all__ = [
    "JOURNAL_VERSION",
    "JournalEntry",
    "SessionJournal",
    "default_journal_path",
    "now_seconds",
]

JOURNAL_VERSION = 2
"""文件格式版本。

- v1：只有 ``session_id`` 等字段；
- v2：新增 ``request_id`` 与 ``state``（见 ``JournalEntry``）。

版本号只用于诊断与将来演进：读的时候不按版本号分流，能读出多少算多少
（``test_version_mismatch_still_loads_entries`` 钉住了这条）。
"""


def _as_str(value: Any) -> str:
    """把任意 JSON 值转成字符串（只接受真正的字符串，其余给空串）。"""
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    """把任意 JSON 值转成整数，转不了就给 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _as_float(value: Any) -> float:
    """把任意 JSON 值转成浮点数，转不了就给 0.0。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """一条会话所有权记录 —— "这个会话是我创建的"。

    三个身份字段缺一不可：

    - ``session_id``：bsk 的会话 id，只有 4 个小写字母（26^4 ≈ 45.7 万空间）；
    - ``agent_window_id``：那扇 Agent Window 的 id，数值空间大得多，
      是区分 id 碰撞的关键字段；
    - ``browser_instance_id``：会话挂在哪个浏览器实例上，用于二次佐证。

    只有 ``session_id`` 一个字段的话，一旦出现碰撞（我们记录的 ``mnaa`` 已经过期，
    之后另一个程序也建了一个 ``mnaa``），清理时就会误停别人的会话。
    """

    session_id: str
    browser_instance_id: str
    agent_window_id: int
    created_at: float
    """``time.time()`` 的秒级时间戳。

    这里必须用墙钟而不是 ``time.monotonic()``：要跨进程比较
    （上一次进程写、这一次进程读），而 monotonic 的原点在每个进程里都可能不同。
    """

    pid: int
    """写入时的进程 pid。仅用于诊断（"这条记录是哪个进程留下的"）。"""

    request_id: str = ""
    """``session start --request-id`` 的启动令牌（v2）。

    为什么它比 ``session_id`` 还早存在：令牌是**我们自己在发 start 之前**
    生成的，所以"窗口可能已经开出来、但我们还没拿到 session_id"这段时间里，
    令牌是唯一能定位那次启动的凭据。凭它调 ``session start --request-id``
    即可取回（或取消）那次启动，不必去猜 id。

    没有令牌的记录（v1 老文件、或普通 start）这里是空串。
    """

    state: str = ""
    """这条记录当前处于哪一步（v2）。

    取值由 ``SessionManager`` 定义，journal 只负责原样存取，不认识具体含义；
    例如 ``prepared``（令牌已落盘、会话还没建出来）与 ``active``（会话已建成）。
    空串表示"老记录，没有状态概念"。
    """

    def to_json(self) -> dict[str, Any]:
        """转成可直接 ``json.dumps`` 的字典（字段顺序固定，便于人工比对）。"""
        return {
            "session_id": self.session_id,
            "browser_instance_id": self.browser_instance_id,
            "agent_window_id": self.agent_window_id,
            "created_at": self.created_at,
            "pid": self.pid,
            "request_id": self.request_id,
            "state": self.state,
        }

    @classmethod
    def from_json(cls, raw: Any) -> JournalEntry | None:
        """从一条 JSON 记录构造；结构非法时返回 None（由调用方跳过这一条）。

        刻意"坏一条丢一条"而不是"坏一条丢整份"：手工编辑或半截写入的 journal
        里混进一条垃圾，不该让其他完好的记录一起作废。

        丢弃的条件是 ``session_id`` 与 ``request_id`` **都**为空 ——
        只知道令牌、还不知道 session_id 的记录必须保留：那正是
        "``session start`` 超时、回执丢了"时唯一能把窗口找回来的东西。
        """
        if not isinstance(raw, dict):
            return None
        session_id = _as_str(raw.get("session_id")).strip()
        request_id = _as_str(raw.get("request_id")).strip()
        if not session_id and not request_id:
            # 两个身份都没有 —— 不知道该去停谁，这条记录没有任何用处。
            return None
        return cls(
            session_id=session_id,
            browser_instance_id=_as_str(raw.get("browser_instance_id")).strip(),
            agent_window_id=_as_int(raw.get("agent_window_id")),
            created_at=_as_float(raw.get("created_at")),
            pid=_as_int(raw.get("pid")),
            request_id=request_id,
            state=_as_str(raw.get("state")).strip(),
        )


def _dedup_key(entry: JournalEntry) -> str:
    """一条记录的去重键：优先用令牌，没有令牌才用 ``session_id``。

    为什么不能只看 session_id：写前落盘的记录**天生还没有 session_id**，
    两条并发启动的 pending 记录 session_id 都是空串 ——
    按 session_id 去重会让它们互相覆盖（这是真实缺陷，不是理论风险）。

    返回空串表示"这条记录没有任何可用身份"，调用方必须直接追加、
    不做任何过滤（否则会把别的空键记录一起清掉）。
    """
    return entry.request_id or entry.session_id


class SessionJournal:
    """把会话所有权记录存成一个 JSON 文件的 journal。

    刻意做成无内存状态的：每次读写都直接面对文件，配合一把
    ``threading.Lock`` 串行化 ``add`` / ``add_checked`` / ``remove`` /
    ``remove_by_request`` / ``clear``。
    这样做的好处是"文件里有什么"与"我们以为什么"永远一致 ——
    这正是一个崩溃恢复机制最需要的性质。

    Args:
        path: journal 文件的路径。父目录不存在时会在写入时自动创建。
        logger: 可选的日志接口（见 ``bsk/logger.py`` 的 ``LoggerLike``）。
            由 ``main.py`` 注入插件 logger；不传（或传 ``None``）时走
            ``NULL_LOGGER``：不产生输出，也绝不抛异常。

    Note:
        本类的公开方法都不抛异常。写失败只记 debug 日志：
        journal 是尽力而为的辅助机制，它的失败绝不该影响会话的正常创建与停止。

        但"尽力而为"不等于"调用方无从知晓"：需要拿成败做决策的场景必须用
        :meth:`add_checked`，它把同样的写入结果作为 ``bool`` 返回。
    """

    def __init__(self, path: str | Path, logger: LoggerLike | None = None) -> None:
        self._path = Path(path)
        self._logger = logger or NULL_LOGGER
        # 一把锁保护"读-改-写"整个过程。add/remove 可能从不同协程被调到
        # （asyncio 是单线程事件循环，但用户工具函数之间没有互斥保证），
        # 而文件操作本身是同步阻塞的，用 threading.Lock 比 asyncio.Lock 更简单可靠。
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """journal 文件路径（诊断用）。"""
        return self._path

    @property
    def tmp_path(self) -> Path:
        """原子写用的临时文件路径（``<path>.tmp``）。"""
        return self._path.with_name(self._path.name + ".tmp")

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def load(self) -> list[JournalEntry]:
        """读出全部记录。

        任何异常都吞掉并返回空列表 —— 文件不存在、空文件、半截 JSON、
        非法 JSON、二进制垃圾、路径是目录、没有读权限，结果都是"读不到"。
        本方法在插件启动路径上跑，抛异常等于插件加载失败，那是不可接受的。

        Returns:
            记录列表（保持文件里的顺序）。坏掉的单条记录会被跳过。
        """
        with self._lock:
            return self._read_unlocked()

    def entries(self) -> list[JournalEntry]:
        """同 :meth:`load`（语义更直白的别名）。"""
        return self.load()

    def add(self, entry: JournalEntry) -> None:
        """写入一条记录（原子写：先写 ``.tmp`` 再 ``os.replace``）。

        去重键见 :func:`_dedup_key`：优先用 ``request_id``，没有令牌才用
        ``session_id``。同一个键只可能对应一条记录，重复写入即替换。

        写失败（权限不足、目录建不出来、磁盘满）只记 debug 日志，不抛异常；
        需要知道成败的调用方用 :meth:`add_checked`。
        """
        self.add_checked(entry)

    def add_checked(self, entry: JournalEntry) -> bool:
        """同 :meth:`add`，但**报告成败**。

        为什么需要它：``SessionManager`` 在发出 ``session start`` 之前必须把启动
        令牌落盘，而那一步要靠返回值判断能不能继续 —— 靠捕获 ``add`` 的异常是
        做不到的（它刻意从不抛异常）。

        Returns:
            True 表示记录已经原子落盘；False 表示这次写入没成功
            （序列化失败、目录建不出来、权限不足、磁盘满等），
            调用方应当据此放弃后续依赖该记录的动作。

        Note:
            与 ``add`` 一样不抛异常。
        """
        with self._lock:
            current = self._read_unlocked()
            key = _dedup_key(entry)
            if key:
                merged = [e for e in current if _dedup_key(e) != key]
            else:
                # 两个身份都为空：无法与其他记录比较，只能直接追加。
                # 若拿空键去过滤，会把 journal 里所有空键记录一并抹掉。
                merged = list(current)
            merged.append(entry)
            return self._write_unlocked(merged)

    def remove(self, session_id: str) -> None:
        """删掉指定 ``session_id`` 的记录。不存在时是空操作。"""
        if not session_id:
            return
        with self._lock:
            current = self._read_unlocked()
            remaining = [e for e in current if e.session_id != session_id]
            if len(remaining) == len(current):
                # 本来就没有 —— 不必要地重写文件只会增加损坏窗口。
                return
            self._write_unlocked(remaining)

    def remove_by_request(self, request_id: str) -> None:
        """删掉指定 ``request_id``（启动令牌）的记录。不存在时是空操作。

        按令牌删的能力是必须的：取消一次可恢复启动之后，那条 pending 记录
        手里只有一个令牌，用 :meth:`remove` 是删不掉的。

        空串直接返回 —— 令牌为空时"删掉所有没有令牌的记录"显然不是调用方的
        意思（那会连带删掉 v1 老记录），宁可什么都不做。
        没有匹配项时不重写文件：少一次写就少一个损坏窗口。
        """
        if not request_id:
            return
        with self._lock:
            current = self._read_unlocked()
            remaining = [e for e in current if e.request_id != request_id]
            if len(remaining) == len(current):
                return
            self._write_unlocked(remaining)

    def clear(self) -> None:
        """清空 journal（删文件，不是写一个空列表）。

        先删主文件再删残留的 ``.tmp``；删不掉（被占用、权限不足）也不抛异常。
        删文件而不是写空内容，是为了让"没有遗留会话"这个状态在磁盘上一眼可见。
        """
        with self._lock:
            for target in (self._path, self.tmp_path):
                with contextlib.suppress(Exception):
                    target.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 内部：读写（调用方必须已经持有 self._lock）
    # ------------------------------------------------------------------

    def _read_unlocked(self) -> list[JournalEntry]:
        """真正读文件的实现。调用方必须持有锁。"""
        try:
            # 显式 utf-8：Windows 中文环境下默认是 gbk，写进中文诊断信息
            #   或路径含中文时会抛 UnicodeDecodeError。
            text = self._path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 读不到就是"没有记录"
            self._logger.debug("会话 journal 读取失败（按空处理）：%r", exc)
            return []

        # 用户用记事本打开并保存过的话，文件头可能被加上 UTF-8 BOM，
        # 而 json.loads 不认它。这里剥掉 U+FEFF，不影响不带 BOM 的常规文件。
        text = text.lstrip("\ufeff")

        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - 半截 JSON / 二进制垃圾
            self._logger.debug("会话 journal 不是合法 JSON（按空处理）：%r", exc)
            return []

        # 正式格式：{"version": N, "entries": [...]}。版本号只用于诊断，
        # 这里不按它分流 —— v1 与 v2 的差异在 JournalEntry.from_json 里按
        # 字段缺省处理（缺 request_id/state 就是空串）。
        raw_entries: Any = None
        if isinstance(data, dict):
            raw_entries = data.get("entries")
        elif isinstance(data, list):
            # 兼容裸数组写法（手工编辑或更早的格式）。多认一种不亏。
            raw_entries = data

        if not isinstance(raw_entries, list):
            self._logger.debug("会话 journal 结构不认识（按空处理）")
            return []

        result: list[JournalEntry] = []
        for raw in raw_entries:
            entry = JournalEntry.from_json(raw)
            if entry is not None:
                result.append(entry)
        return result

    def _write_unlocked(self, entries: list[JournalEntry]) -> bool:
        """真正写文件的实现（原子写）。调用方必须持有锁，且本方法不抛异常。

        步骤：建目录 → 写 ``.tmp`` → ``flush`` + ``fsync`` → ``os.replace``。

        ``os.replace`` 在 Windows 与 POSIX 上都是原子的覆盖，所以任何时刻
        读到的要么是旧的完整内容、要么是新的完整内容，不会读到写了一半的文件。

        Returns:
            True 表示新内容已经完整落到主文件上；False 表示这次写入失败
            （序列化失败、目录建不出来、权限不足、磁盘满等），主文件保持原样。
            失败时只记 debug 日志，绝不抛异常 —— 调用方需要靠返回值判断。
        """
        payload = {
            "version": JOURNAL_VERSION,
            "entries": [e.to_json() for e in entries],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - 理论上不会发生，兜底
            self._logger.debug("会话 journal 序列化失败（忽略）：%r", exc)
            return False

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.tmp_path, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                # fsync 是为了防"断电"这一档故障：只 flush 的话数据可能还在
                # 操作系统缓存里，此时断电会留下一个空的 .tmp。
                with contextlib.suppress(Exception):
                    os.fsync(fh.fileno())
            os.replace(self.tmp_path, self._path)
        except Exception as exc:  # noqa: BLE001 - 写不进去也不能影响主流程
            self._logger.debug("会话 journal 写入失败（忽略）：%r", exc)
            with contextlib.suppress(Exception):
                self.tmp_path.unlink(missing_ok=True)
            return False
        return True


def now_seconds() -> float:
    """当前墙钟时间（秒）。抽成函数是为了让调用方与测试都能统一口径。"""
    return time.time()
