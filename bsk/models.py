"""bsk 领域模型。

这些类型是 ``bsk/`` 包内各模块之间的接口契约，也是单元测试的断言对象。
设计原则：

- 全部 ``dataclass``，纯数据，不含行为（行为放各模块的函数里）；
- 字段名与 bsk 的 JSON 保持一致，避免来回映射出错；
- 宽松解析：bsk 的返回是外部输入，字段可能缺失（实测 ``entries`` 会整个消失），
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

    这是 ``runner.run()`` 的返回值，不代表业务成功。
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


@dataclass(slots=True)
class BrowserInstance:
    """一个已连接的浏览器（``bsk browsers --json`` 的元素）。

    Note:
        ``label`` 经常是空字符串（实测），不能用它来指定浏览器，
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
        实测这两个字段是字符串枚举而不是布尔值，
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

    ``session_id`` 实测是 4 个小写字母（如 ``mnaa``）。
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
        ``observe`` 返回的是带标记的缩进文本树，不是结构化 JSON。
        标题、元素等都要从 ``text`` 里解析出来 —— bsk 没有独立的
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

    注意这不是截图的像素尺寸：高分屏下截图会大 DPR 倍
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


@dataclass(slots=True)
class NavigateResult:
    """``navigate`` 的结果。"""

    url: str = ""
    final_url: str = ""
    """真实落点。应读这个而不是 url：实测会补上尾斜杠、可能跟随跳转。"""

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
        bsk 只给文件路径，不给 base64。所以插件必须自己管好文件。
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
class EvaluateError:
    """``evaluate`` 里 JavaScript 抛出的错误。

    关键事实（实测确认）：JS 抛异常时 ``bsk`` 进程的退出码仍然是 0，
    错误只体现在返回 JSON 的 ``ok: false`` 与这个对象里。所以判成败绝不能
    只看退出码，必须检查 ``ok``。实测原文：

    .. code-block:: text

        $ bsk evaluate "throw new Error('boom')" --session ycvt --json
        {
          "ok": false,
          "tab_id": 1398286752,
          "error": {
            "text": "Error: boom\\n    at <anonymous>:1:7",
            "line": 1,
            "column": 0
          }
        }
        exit=0          ← 注意：依然是 0
    """

    text: str = ""
    """错误的完整文本（含 ``Error:`` 前缀与 JS 堆栈）。"""

    line: int = 0
    column: int = 0
    """出错位置。实测常见 0：``column`` 经常是 0，``line`` 对多行表达式才有意义，
    所以文案里要能容忍它们没有信息量（不要写"第 0 行第 0 列"这种废话）。"""

    @classmethod
    def from_json(cls, raw: Any) -> EvaluateError:
        """从 bsk 的 JSON 构造。容忍缺失。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            text=_as_str(raw.get("text")),
            line=_as_int(raw.get("line")),
            column=_as_int(raw.get("column")),
        )

    def location(self) -> str:
        """把行列渲染成可读的片段，没有信息量时返回空串。

        实测 ``throw new Error('boom')`` 报的是 ``line=1, column=0``，
        而 ``column=0`` 对用户毫无意义 —— 这种情况只给行号。
        """
        if self.line <= 0:
            return ""
        if self.column > 0:
            return f"（第 {self.line} 行第 {self.column} 列）"
        return f"（第 {self.line} 行）"


@dataclass(slots=True)
class EvaluateDialog:
    """``evaluate`` 期间被自动处理掉的浏览器弹窗。

    实测风险（确认存在）：``bsk evaluate`` 会自动确认页面的 ``confirm``
    弹窗，也就是说 ``confirm()`` 直接返回 ``true``，用户根本看不到那个弹窗。
    实测原文：

    .. code-block:: text

        $ bsk evaluate "confirm('bsk-evaluate-probe')" --session ycvt --json
        {
          "ok": true, "tab_id": ..., "value": true,
          "dialogs": [{"tab_id": ..., "type": "confirm",
                       "message": "bsk-evaluate-probe", "url": "about:blank",
                       "default_prompt": "", "has_browser_handler": true,
                       "handled": "accepted", "sequence": 1}]
        }

    这意味着"模型写一段 JS 就能静默越过确认框"，必须把这件事告诉模型和用户
    （渲染时会明确写出来），不能让模型以为用户已经点过"确定"了。

    Note:
        ``dialogs`` 字段在没有弹窗时整个不存在（实测成功样例里就没有），
        所以解析必须用 ``raw.get("dialogs", [])``。
    """

    type: str = ""
    """弹窗类型：``confirm`` / ``alert`` / ``prompt`` / ``beforeunload`` 等。"""

    message: str = ""
    url: str = ""
    handled: str = ""
    """bsk 的处理方式。实测 ``confirm``/``alert`` 都是 ``"accepted"``。"""

    tab_id: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> EvaluateDialog:
        """从 bsk 的 JSON 构造。容忍缺失。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            type=_as_str(raw.get("type")),
            message=_as_str(raw.get("message")),
            url=_as_str(raw.get("url")),
            handled=_as_str(raw.get("handled")),
            tab_id=_as_int(raw.get("tab_id")),
        )


@dataclass(slots=True)
class EvaluateResult:
    """``evaluate`` 的结果 —— 成败的唯一依据是 ``ok``。

    为什么需要这个类型而不是直接看退出码：实测 JS 抛异常时 bsk 的退出码
    依然是 0，只看退出码会把失败当成功，把 ``error`` 结构当返回值塞给模型。

    三种实测形态：

    1. 成功且有值：``{"ok": true, "tab_id": N, "value": <任意 JSON>}``
    2. 成功但无值：``{"ok": true, "tab_id": N}`` —— 表达式求值成
       ``undefined`` / ``null`` 时 ``value`` 字段整个消失（实测
       ``evaluate "undefined"`` 与 ``evaluate "null"`` 都是这个形态）。
       所以 ``value`` 必须区分"没有值"与"值是 null"。
    3. JS 抛异常：``{"ok": false, "tab_id": N, "error": {...}}``

    Note:
        ``value`` 可能是任意大的 JSON：实测
        ``Array.from({length:2000},(_,i)=>'item-'+i)`` 的命令输出有
        32947 个字符。直接塞进模型上下文会撑爆，所以渲染时必须截断
        （见 ``service.BskService.render_evaluate``）。
    """

    ok: bool = False
    """JavaScript 是否执行成功。这是唯一的成败依据，不是退出码。"""

    value: Any = None
    """JS 的返回值（任意 JSON 类型）。``has_value`` 为 False 时无意义。"""

    has_value: bool = False
    """返回 JSON 里是否存在 ``value`` 字段。

    单独一个标志位是必要的：``null`` / ``undefined`` 会让 bsk 整个省掉
    ``value`` 字段（实测），而 ``null`` 与"没有这个字段"在语义上不同 ——
    前者是"表达式就是 null"，后者是"求值成了 undefined"。
    """

    error: EvaluateError | None = None
    dialogs: list[EvaluateDialog] = field(default_factory=list)
    tab_id: int = 0

    @classmethod
    def from_json(cls, raw: Any) -> EvaluateResult:
        """从 ``evaluate --json`` 的输出构造。

        Note:
            宽容策略：拿不到 ``ok`` 字段时（输出被截断、结构不认识）按
            ``False`` 处理。宁可把一次成功误判成失败（模型会重试或报错给用户），
            也不要把一次失败当成成功（模型会拿着 ``None`` 编答案）。
        """
        if not isinstance(raw, dict):
            return cls()

        raw_dialogs = raw.get("dialogs", [])
        dialogs = (
            [EvaluateDialog.from_json(d) for d in raw_dialogs]
            if isinstance(raw_dialogs, list)
            else []
        )
        raw_error = raw.get("error")
        return cls(
            ok=_as_bool(raw.get("ok"), False),
            value=raw.get("value"),
            has_value="value" in raw,
            error=EvaluateError.from_json(raw_error)
            if isinstance(raw_error, dict)
            else None,
            dialogs=dialogs,
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

    method: str = ""
    """HTTP 方法，只有 ``network`` 的条目才有（``console`` 的条目没有这个字段）。"""

    status: int = 0
    """HTTP 状态码。``kind == "failure"`` 的条目没有这个字段（实测），此时为 0。"""

    error_text: str = ""
    """失败原因，只有 ``kind == "failure"`` 的条目才有（例如 ``net::ERR_FAILED``）。"""

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
            method=_as_str(raw.get("method")),
            status=_as_int(raw.get("status")),
            error_text=_as_str(raw.get("error_text")),
        )


@dataclass(slots=True)
class ConsoleLog:
    """``console`` / ``network`` 的结果。

    Note:
        ``entries`` 字段在空结果时会整个消失（实测），所以这里给默认空列表，
        并且解析时必须用 ``raw.get("entries", [])``。
    """

    entries: list[ConsoleEntry] = field(default_factory=list)
    next_since: int = 0
    """游标。``--since N`` 是开区间（只返回 sequence > N），原样回传即可增量拉取。"""

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
