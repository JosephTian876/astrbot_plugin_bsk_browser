"""bsk 领域模型。

这些类型是 ``bsk/`` 包内各模块之间的**接口契约**，也是单元测试的断言对象。
设计原则：

- 全部 ``dataclass``，纯数据，不含行为（行为放各模块的函数里）；
- 字段名与 bsk 的 JSON 保持一致，避免来回映射出错；
- **宽松解析**：bsk 的返回是外部输入，字段可能缺失（实测 ``entries`` 会整个消失），
  所以所有 ``from_json`` 都必须容忍缺字段并给默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_str(value: Any, default: str = "") -> str:
    """把任意 JSON 值安全地转成字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    """把任意 JSON 值安全地转成整数。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    """把任意 JSON 值安全地转成布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(slots=True)
class BskResult:
    """一次 bsk 命令的原始执行结果。

    这是 ``runner.run()`` 的返回值，**不代表业务成功**。
    调用方需自行检查 ``ok`` 或捕获 ``BskError``。
    """

    ok: bool
    exit_code: int
    data: Any = None
    """解析后的 JSON。命令不返回 JSON 时为 None。"""

    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    """耗时（秒），用于性能观测与超时调优。"""

    def as_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典（调试用，不含大字段原文）。"""
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "elapsed": round(self.elapsed, 3),
            "has_data": self.data is not None,
        }


@dataclass(slots=True)
class BrowserInstance:
    """一个已连接的浏览器（``bsk browsers --json`` 的元素）。

    Note:
        ``label`` 经常是**空字符串**（实测），不能用它来指定浏览器，
        必须用 ``instance_id``。
    """

    instance_id: str
    browser_name: str = ""
    browser_version: str = ""
    extension_version: str = ""
    label: str = ""
    session_count: int = 0
    unresponsive: bool = False
    version_skew: bool = False

    @classmethod
    def from_json(cls, raw: Any) -> BrowserInstance:
        """从 bsk 的 JSON 构造。容忍字段缺失。"""
        if not isinstance(raw, dict):
            return cls(instance_id="")
        return cls(
            instance_id=_as_str(raw.get("instance_id")),
            browser_name=_as_str(raw.get("browser_name")),
            browser_version=_as_str(raw.get("browser_version")),
            extension_version=_as_str(raw.get("extension_version")),
            label=_as_str(raw.get("label")),
            session_count=_as_int(raw.get("session_count")),
            unresponsive=_as_bool(raw.get("unresponsive")),
            version_skew=_as_bool(raw.get("version_skew")),
        )

    def display_name(self) -> str:
        """给用户看的名字，优先用 label，回退到浏览器名 + 短 id。"""
        if self.label.strip():
            return self.label.strip()
        name = self.browser_name or "浏览器"
        return f"{name} ({self.instance_id})"


@dataclass(slots=True)
class InteractionSettings:
    """浏览器扩展的人机协作设置（``session start`` 返回值的一部分）。

    Note:
        实测这两个字段是**字符串枚举**而不是布尔值，
        且 bsk 源码允许它们缺失，所以不要假设一定存在。
    """

    borrow_confirmation: str = ""
    request_help: str = ""

    @classmethod
    def from_json(cls, raw: Any) -> InteractionSettings:
        """从 bsk 的 JSON 构造。容忍缺失。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            borrow_confirmation=_as_str(raw.get("borrow_confirmation")),
            request_help=_as_str(raw.get("request_help")),
        )


@dataclass(slots=True)
class BskSession:
    """一个 bsk 浏览器会话。

    ``session_id`` 实测是 **4 个小写字母**（如 ``mnaa``）。
    """

    session_id: str
    browser_instance_id: str = ""
    agent_window_id: int = 0
    interaction: InteractionSettings = field(default_factory=InteractionSettings)
    uncertain: bool = False
    """为 True 表示上一次操作结果未知（``outcome_unknown``）。

    bsk 禁止重放这类操作，因此处于该状态时应拒绝新的操作动作，
    直到用户人工确认页面状态或重建会话。
    """

    @classmethod
    def from_json(cls, raw: Any) -> BskSession:
        """从 ``session start --json`` 的输出构造。"""
        if not isinstance(raw, dict):
            return cls(session_id="")
        return cls(
            session_id=_as_str(raw.get("session_id")),
            browser_instance_id=_as_str(raw.get("browser_instance_id")),
            agent_window_id=_as_int(raw.get("agent_window_id")),
            interaction=InteractionSettings.from_json(raw.get("interaction")),
        )

    def is_valid(self) -> bool:
        """会话是否可用（有 id 才算建成功）。"""
        return bool(self.session_id)


@dataclass(slots=True)
class PageRef:
    """页面上的一个可交互元素引用（``@eN``）。

    ``ref`` 形如 ``e1``（不含 ``@``），可直接传给 bsk 的 ``--ref``/target 参数。
    """

    ref: str
    role: str = ""
    name: str = ""
    target: str = ""
    """方括号里的附加信息，例如链接的 ``[→ iana.org]``。"""


@dataclass(slots=True)
class PageObservation:
    """一次 ``observe`` 的结果。

    Note:
        ``observe`` 返回的是**带标记的缩进文本树**，不是结构化 JSON。
        标题、元素等都要从 ``text`` 里解析出来 —— bsk **没有**独立的
        ``title`` / ``url`` 字段。
    """

    text: str = ""
    refs: list[PageRef] = field(default_factory=list)
    ref_count: int = 0
    tab_id: int = 0
    truncated: bool = False
    title: str = ""
    viewport: tuple[int, int] | None = None
    """``@view`` 里的视口尺寸（CSS px）。

    注意这**不是**截图的像素尺寸：高分屏下截图会大 DPR 倍
    （实测 observe 报 910x604，截图是 1850x1208，DPR≈2）。
    """

    @classmethod
    def from_json(cls, raw: Any) -> PageObservation:
        """从 ``observe --json`` 的输出构造（``text`` 的解析在 pages.py 里做）。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            text=_as_str(raw.get("text")),
            ref_count=_as_int(raw.get("ref_count")),
            tab_id=_as_int(raw.get("tab_id")),
            truncated=_as_bool(raw.get("truncated")),
        )

    def summary(self, max_refs: int = 40) -> str:
        """给模型看的紧凑摘要（避免把整棵 VOM 树塞进上下文）。"""
        lines: list[str] = []
        if self.title:
            lines.append(f"标题：{self.title}")
        if self.refs:
            shown = self.refs[:max_refs]
            lines.append(f"可交互元素（共 {self.ref_count} 个）：")
            for r in shown:
                extra = f" {r.target}" if r.target else ""
                lines.append(f"  @{r.ref} {r.role} \"{r.name}\"{extra}")
            if len(self.refs) > max_refs:
                lines.append(f"  ……还有 {len(self.refs) - max_refs} 个未列出")
        else:
            lines.append("这个页面上没有发现可点击/可输入的元素。")
        if self.truncated:
            lines.append("（页面内容过长，已被截断）")
        return "\n".join(lines)


@dataclass(slots=True)
class NavigateResult:
    """``navigate`` 的结果。"""

    url: str = ""
    final_url: str = ""
    """真实落点。**应读这个而不是 url**：实测会补上尾斜杠、可能跟随跳转。"""

    reached: str = ""
    tab_id: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> NavigateResult:
        """从 ``navigate --json`` 的输出构造。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            url=_as_str(raw.get("url")),
            final_url=_as_str(raw.get("final_url")),
            reached=_as_str(raw.get("reached")),
            tab_id=_as_int(raw.get("tab_id")),
        )


@dataclass(slots=True)
class Screenshot:
    """``screenshot`` 的结果。

    Note:
        bsk **只给文件路径**，不给 base64。所以插件必须自己管好文件。
    """

    path: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    byte_size: int = 0
    tab_id: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> Screenshot:
        """从 ``screenshot --json`` 的输出构造。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            path=_as_str(raw.get("path")),
            width=_as_int(raw.get("width")),
            height=_as_int(raw.get("height")),
            format=_as_str(raw.get("format")),
            byte_size=_as_int(raw.get("byte_size")),
            tab_id=_as_int(raw.get("tab_id")),
        )


@dataclass(slots=True)
class ConsoleEntry:
    """一条控制台消息（``console --json`` 的 entries 元素）。"""

    sequence: int = 0
    kind: str = ""
    level: str = ""
    text: str = ""
    url: str = ""
    timestamp: float = 0.0
    truncated: bool = False

    @classmethod
    def from_json(cls, raw: Any) -> ConsoleEntry:
        """从 bsk 的 JSON 构造。容忍缺失。"""
        if not isinstance(raw, dict):
            return cls()
        ts = raw.get("timestamp")
        return cls(
            sequence=_as_int(raw.get("sequence")),
            kind=_as_str(raw.get("kind")),
            level=_as_str(raw.get("level")),
            text=_as_str(raw.get("text")),
            url=_as_str(raw.get("url")),
            timestamp=float(ts) if isinstance(ts, (int, float)) else 0.0,
            truncated=_as_bool(raw.get("truncated")),
        )


@dataclass(slots=True)
class ConsoleLog:
    """``console`` / ``network`` 的结果。

    Note:
        **``entries`` 字段在空结果时会整个消失**（实测），所以这里给默认空列表，
        并且解析时必须用 ``raw.get("entries", [])``。
    """

    entries: list[ConsoleEntry] = field(default_factory=list)
    next_since: int = 0
    """游标。``--since N`` 是**开区间**（只返回 sequence > N），原样回传即可增量拉取。"""

    tab_id: int = 0
    truncated: bool = False

    @classmethod
    def from_json(cls, raw: Any) -> ConsoleLog:
        """从 bsk 的 JSON 构造。"""
        if not isinstance(raw, dict):
            return cls()
        raw_entries = raw.get("entries", [])
        entries = (
            [ConsoleEntry.from_json(e) for e in raw_entries]
            if isinstance(raw_entries, list)
            else []
        )
        return cls(
            entries=entries,
            next_since=_as_int(raw.get("next_since")),
            tab_id=_as_int(raw.get("tab_id")),
            truncated=_as_bool(raw.get("truncated")),
        )
