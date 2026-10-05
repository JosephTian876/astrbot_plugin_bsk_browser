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

设计约束（写代码时请勿破坏）：

- 纯标准库，不 import astrbot，也不 import 本包其他模块，可独立单测；
- 本模块的任何方法都不抛异常（``load`` / ``add`` / ``remove`` / ``clear``）：
  它在插件启动路径上跑，一个异常就是插件加载失败；
- 原子写：先写 ``<path>.tmp`` 再 ``os.replace()``，避免写一半被杀留下坏文件；
- 显式 UTF-8：Windows 中文环境下默认编码是 gbk，写中文诊断信息会炸；
- 读不到就当没有：文件不存在、是空文件、是半截 JSON、是二进制垃圾、
  路径是个目录、没有读权限 —— 一律返回空列表。宁可漏清，绝不因为解析失败
  让插件起不来。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "JOURNAL_VERSION",
    "JournalEntry",
    "SessionJournal",
    "default_journal_path",
]

JOURNAL_VERSION = 1
"""文件格式版本。将来字段有变时靠它区分，现在只有一种。"""

JOURNAL_DIR_NAME = "astrbot_bsk_browser"
"""默认目录名（系统临时目录下）。刻意与截图目录区分开，便于人工排查。"""

JOURNAL_FILE_NAME = "sessions.json"
"""默认文件名。用 JSON 而不是二进制，是为了出问题时能直接用记事本打开看。"""


def default_journal_path() -> Path:
    """默认的 journal 文件位置：系统临时目录下的 ``astrbot_bsk_browser/sessions.json``。

    为什么放临时目录而不是插件数据目录：AstrBot 的工作目录会随启动方式变化，
    插件数据目录也不保证一定可写；而临时目录是"总是存在、总是可写"的那个位置。
    代价是操作系统清理临时目录后记录会丢 —— 那时记录的会话多半也早就没了，
    可以接受（journal 本来就是尽力而为的辅助机制）。

    Returns:
        journal 文件的绝对路径（不保证文件或目录存在）。
    """
    try:
        base = Path(tempfile.gettempdir())
    except Exception:  # noqa: BLE001 - 极端环境下 gettempdir 也可能炸
        base = Path(".")
    return base / JOURNAL_DIR_NAME / JOURNAL_FILE_NAME


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

    def to_json(self) -> dict[str, Any]:
        """转成可直接 ``json.dumps`` 的字典（字段顺序固定，便于人工比对）。"""
        return {
            "session_id": self.session_id,
            "browser_instance_id": self.browser_instance_id,
            "agent_window_id": self.agent_window_id,
            "created_at": self.created_at,
            "pid": self.pid,
        }

    @classmethod
    def from_json(cls, raw: Any) -> JournalEntry | None:
        """从一条 JSON 记录构造；结构非法时返回 None（由调用方跳过这一条）。

        刻意"坏一条丢一条"而不是"坏一条丢整份"：手工编辑或半截写入的 journal
        里混进一条垃圾，不该让其他完好的记录一起作废。
        """
        if not isinstance(raw, dict):
            return None
        session_id = _as_str(raw.get("session_id")).strip()
        if not session_id:
            # 没有 session_id 的记录毫无用处，直接丢弃。
            return None
        return cls(
            session_id=session_id,
            browser_instance_id=_as_str(raw.get("browser_instance_id")).strip(),
            agent_window_id=_as_int(raw.get("agent_window_id")),
            created_at=_as_float(raw.get("created_at")),
            pid=_as_int(raw.get("pid")),
        )


class SessionJournal:
    """把会话所有权记录存成一个 JSON 文件的 journal。

    刻意做成无内存状态的：每次读写都直接面对文件，配合一把
    ``threading.Lock`` 串行化 ``add`` / ``remove`` / ``clear``。
    这样做的好处是"文件里有什么"与"我们以为什么"永远一致 ——
    这正是一个崩溃恢复机制最需要的性质。

    Args:
        path: journal 文件的路径。父目录不存在时会在写入时自动创建。

    Note:
        本类的公开方法都不抛异常。写失败只记 debug 日志：
        journal 是尽力而为的辅助机制，它的失败绝不该影响会话的正常创建与停止。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
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

        ``session_id`` 相同的旧记录会被替换 —— 同一个 id 只可能对应一条记录，
        靠它去重可以避免崩溃重启后 journal 里堆出重复条目。

        写失败（权限不足、目录建不出来、磁盘满）只记 debug 日志，不抛异常。
        """
        with self._lock:
            current = self._read_unlocked()
            merged = [e for e in current if e.session_id != entry.session_id]
            merged.append(entry)
            self._write_unlocked(merged)

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
            logger.debug("会话 journal 读取失败（按空处理）：%r", exc)
            return []

        # 用户用记事本打开并保存过的话，文件头可能被加上 UTF-8 BOM，
        # 而 json.loads 不认它。这里剥掉 U+FEFF，不影响不带 BOM 的常规文件。
        text = text.lstrip("\ufeff")

        try:
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001 - 半截 JSON / 二进制垃圾
            logger.debug("会话 journal 不是合法 JSON（按空处理）：%r", exc)
            return []

        # 正式格式：{"version": 1, "entries": [...]}。
        raw_entries: Any = None
        if isinstance(data, dict):
            raw_entries = data.get("entries")
        elif isinstance(data, list):
            # 兼容裸数组写法（手工编辑或更早的格式）。多认一种不亏。
            raw_entries = data

        if not isinstance(raw_entries, list):
            logger.debug("会话 journal 结构不认识（按空处理）")
            return []

        result: list[JournalEntry] = []
        for raw in raw_entries:
            entry = JournalEntry.from_json(raw)
            if entry is not None:
                result.append(entry)
        return result

    def _write_unlocked(self, entries: list[JournalEntry]) -> None:
        """真正写文件的实现（原子写）。调用方必须持有锁，且本方法不抛异常。

        步骤：建目录 → 写 ``.tmp`` → ``flush`` + ``fsync`` → ``os.replace``。

        ``os.replace`` 在 Windows 与 POSIX 上都是原子的覆盖，所以任何时刻
        读到的要么是旧的完整内容、要么是新的完整内容，不会读到写了一半的文件。
        """
        payload = {
            "version": JOURNAL_VERSION,
            "entries": [e.to_json() for e in entries],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - 理论上不会发生，兜底
            logger.debug("会话 journal 序列化失败（忽略）：%r", exc)
            return

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
            logger.debug("会话 journal 写入失败（忽略）：%r", exc)
            with contextlib.suppress(Exception):
                self.tmp_path.unlink(missing_ok=True)


def now_seconds() -> float:
    """当前墙钟时间（秒）。抽成函数是为了让调用方与测试都能统一口径。"""
    return time.time()
