"""七个多动作工具的规格与参数校验（纯标准库、纯函数）。

本模块是**工具规格的唯一事实源**：

- :data:`TOOL_ACTIONS` —— 工具名 → 该工具声明的 action 元组；
- :data:`TOOL_SCHEMAS` —— 工具名 → 完整 JSON Schema（``enum`` 从 ``TOOL_ACTIONS`` 生成）；
- :data:`TOOL_DESCRIPTIONS` —— 工具名 → 给模型看的中文描述；
- :func:`normalize_args` / :func:`validate` —— 归一化与逐 action 校验。

三处设计约束（与 ``TOOL-SPEC.md`` / ``REVIEW-ROUND1.md`` 一致，改动前先读那两份）：

1. **schema 里不写 ``required``** —— AstrBot 的 ``spec_to_func`` 只构造
   ``{type: "object", properties}``，``required`` 写进去也没人读。必填一律靠
   :func:`validate` 在运行时报中文错。
2. **整数语义的参数 ``type`` 一律是 ``"number"``** —— AstrBot 的类型白名单是
   ``{string, number, object, array, boolean}``，**没有 ``integer``**；
   整数性与范围在运行时的 :func:`_as_int` 里检查。
3. **不 import astrbot、不 import logging** —— ``bsk/`` 包的分层约束，
   ``tests/test_logger_injection.py`` 会静态检查。本模块只用标准库的
   ``json`` 与 ``re``（两者都是纯 C 模块，不参与 ``bsk.logger`` 的注入链）。

参数名统一 ``snake_case``；:func:`normalize_args` 同时接受 camelCase
（DSH 文档里的写法）与连字符 action（``scroll-to`` → ``scroll_to``）。
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "BskToolError",
    "TOOL_ACTIONS",
    "TOOL_SCHEMAS",
    "TOOL_DESCRIPTIONS",
    "DEVICE_PRESETS",
    "WAIT_UNTIL_VALUES",
    "MODIFIER_VALUES",
    "DEBUG_ACTIONS",
    "DEBUG_PART_VALUES",
    "DEBUG_STATE_VALUES",
    "DEBUG_KIND_VALUES",
    "REQUEST_HELP_OUTCOMES",
    "ID_REQUIRED_DEBUG_ACTIONS",
    "CONTROLLED_ONLY_DEBUG_ACTIONS",
    "NO_SINCE_DEBUG_ACTIONS",
    "COMPLETION_CRITERIA_KEYS",
    "COMPLETION_CRITERIA_ALIASES",
    "COMPLETION_CRITERIA_CLI_KEYS",
    "normalize_args",
    "validate",
]


class BskToolError(Exception):
    """工具参数校验失败。``str()`` 出来的就是给模型看的中文提示。

    ``main.py`` 捕获它并把消息原样回给模型，所以消息必须写清「哪里错了、
    应该怎么改」，不能只写 ``invalid action`` 这种没信息量的字。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 枚举（唯一事实源）
# ---------------------------------------------------------------------------

DEBUG_ACTIONS: tuple[str, ...] = (
    "performance",
    "aggregate",
    "duplicates",
    "capabilities",
    "activity",
    "wait",
    "pin",
    "unpin",
    "start",
    "stop",
    "status",
    "requests",
    "request",
    "operations",
    "operation",
    "console",
    "pages",
    "export",
    "rules",
    "rule_add",
    "rule_enable",
    "rule_disable",
    "rule_remove",
    "replay",
)
"""``bsk_debug`` 的 24 个 action（顺序与 DSH 一致）。

刻意定义在 :data:`TOOL_ACTIONS` **之前**：``bsk_debug`` 的 action 元组就是它，
引用必须能成立，否则只能事后改字典（那就不是"唯一事实源"了）。
"""

TOOL_ACTIONS: dict[str, tuple[str, ...]] = {
    "bsk_session": ("start", "stop", "list"),
    "bsk_page": ("navigate", "back", "forward", "reload", "wait"),
    "bsk_inspect": (
        "observe",
        "snapshot",
        "html",
        "screenshot",
        "console",
        "network",
    ),
    "bsk_debug": DEBUG_ACTIONS,
    "bsk_interact": (
        "click",
        "hover",
        "wheel",
        "scroll-to",
        "focus",
        "blur",
        "fill",
        "select",
        "press",
    ),
    "bsk_tabs": ("list", "create", "select", "close", "borrow", "return"),
    "bsk_assist": ("resize", "emulate", "request-help"),
}
"""工具名 → 该工具声明的 action 元组（唯一事实源）。

``TOOL_SCHEMAS`` 里 ``action`` 的 ``enum`` 与 :func:`validate` 的合法 action
判断都从这里生成，避免两处枚举不一致。
"""

DEVICE_PRESETS: tuple[str, ...] = (
    "iphone-14",
    "iphone-14-pro-max",
    "iphone-se",
    "pixel-7",
    "galaxy-s23",
    "ipad-mini",
    "galaxy-tab-s8",
)
"""``device`` 的 7 个内置预设（与 DSH/bsk 一致）。"""

WAIT_UNTIL_VALUES: tuple[str, ...] = ("load", "domcontentloaded", "networkidle", "commit")
"""页面生命周期阶段，``load`` 是默认值。"""

MODIFIER_VALUES: tuple[str, ...] = ("alt", "ctrl", "meta", "shift")
"""``modifiers`` 数组里允许出现的键名。"""

TABS_SCOPE_VALUES: tuple[str, ...] = ("user", "agent", "all")
"""``bsk_tabs(scope=...)`` 的取值，默认 ``all``。"""

CLICK_BUTTON_VALUES: tuple[str, ...] = ("left", "middle", "right")

DEBUG_PART_VALUES: tuple[str, ...] = ("metadata", "request", "response", "headers", "timing")
DEBUG_STATE_VALUES: tuple[str, ...] = (
    "pending",
    "complete",
    "failed",
    "redirected",
    "interrupted",
)
DEBUG_KIND_VALUES: tuple[str, ...] = ("all", "business", "resource", "extension")

REQUEST_HELP_OUTCOMES: tuple[str, ...] = (
    "continued",
    "cancelled",
    "timed_out",
    "completed",
    "navigated",
    "disabled",
)
"""``request-help`` 的 outcome。

**只有 ``continued`` / ``completed`` 是「用户已完成」**，
``navigated`` / ``disabled`` / ``timed_out`` / ``cancelled`` 都不是。
"""

ID_REQUIRED_DEBUG_ACTIONS: frozenset[str] = frozenset(
    {
        "request",
        "operation",
        "rule_enable",
        "rule_disable",
        "rule_remove",
        "replay",
        "pin",
        "unpin",
    }
)
"""必须带 ``id`` 的 debug action。"""

CONTROLLED_ONLY_DEBUG_ACTIONS: frozenset[str] = frozenset({"aggregate", "duplicates"})
"""``include_controlled`` 只能用于这两个 action。"""

NO_SINCE_DEBUG_ACTIONS: frozenset[str] = frozenset({"performance", "aggregate", "duplicates"})
"""这三个 action 用 ``offset`` 翻页，不接受 ``since``。"""

COMPLETION_CRITERIA_KEYS: tuple[str, ...] = (
    "urlContains",
    "urlMatches",
    "selectorExists",
    "selectorMissing",
    "textExists",
    "textMissing",
)
"""``completion_criteria`` 里每个条件对象允许的键（**camelCase**，与 DSH 一致）。

模型看到的 schema 用这套写法，:func:`validate` 也优先接受它们；
下划线写法（``selector_exists``）一样收，但输出统一转成
:data:`COMPLETION_CRITERIA_CLI_KEYS` 的 CLI 形态。
"""

COMPLETION_CRITERIA_CLI_KEYS: dict[str, str] = {
    "urlContains": "url_contains",
    "urlMatches": "url_matches",
    "selectorExists": "selector_exists",
    "selectorMissing": "selector_missing",
    "textExists": "text_exists",
    "textMissing": "text_missing",
}
"""camelCase 条件键 → 送进 ``bsk request-help --completion-criteria`` 的 snake_case 键。"""

COMPLETION_CRITERIA_ALIASES: dict[str, str] = {
    **{key: key for key in COMPLETION_CRITERIA_KEYS},
    **{cli: camel for camel, cli in COMPLETION_CRITERIA_CLI_KEYS.items()},
}
"""接受的条件键 → 规范 camelCase 键。两种写法都收，便于模型自我纠正。"""

_REF_RE = re.compile(r"^@?e\d+$")
"""快照元素引用：``@e3`` / ``e3``。"""

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_MAX_SINCE = 2**63 - 1
"""``since`` 的上界，与 DSH 的 ``Number.MAX_SAFE_INTEGER`` 语义等价的整数哨兵。"""

_LOG_CONTROL_KEYS: tuple[str, ...] = ("since", "limit", "max_text_chars")
"""console/network 共用的游标三件套。"""

_DEBUG_CONTROL_KEYS: tuple[str, ...] = (
    "since",
    "limit",
    "offset",
    "max_chars",
    "budget",
    "slow_ms",
    "window_ms",
    "status",
    "wait_ms",
)

# 调试参数的中文说明。debug 的 24 个 action 共用同一批参数名，
# 所以描述必须写清「哪个 action 用得上」，否则模型会瞎传。
_DEBUG_PROPERTY_DESCRIPTIONS: dict[str, str] = {
    "id": (
        "request/operation/rule_enable/rule_disable/rule_remove/replay/pin/unpin 必填"
        "：request、operation、rule 的 ID，或 wait 的命令 ID（按 action 解释）。"
    ),
    "rule": (
        "rule_add 必填：规则的 JSON 字符串。形如 "
        '{"match":{"url":"https://a.com/*","method":"GET","resource_type":"Fetch"},'
        '"effect":{"type":"block"},"times":1}。'
        "url 必须是绝对地址，* 匹配路径与查询串；默认只作用于 Fetch/XHR，可加 Document。"
        "effect.type 取 block（拦截）、modify（改写同源 url/method/headers/body）或 "
        "mock（用 status/body/headers/delay_ms 伪造响应，delay_ms 0..10000）。"
        "只支持文本 body；times=0 表示一直生效到禁用或抓包结束；规则在本地生效，先匹配先赢。"
        "最大 81920 字符，必须是合法 JSON。"
    ),
    "replay": (
        "replay 必填：要重放的请求 JSON 字符串，形如 "
        '{"key":"唯一标识","url":"https://a.com/api","method":"POST","headers":{}, "body":"..."}。'
        "只会发送一次，重试请复用同一个 key。只允许同源；URL 或 body 有改动/被截断/"
        "未校验时必须整体替换而不是局部修补。URL 最长 16384 字符，抓到的 URL 最长 2048。"
        "重放可能会真的写入服务端数据。最大 81920 字符，必须是合法 JSON。"
    ),
    "slow_ms": "仅 aggregate 可用：慢请求阈值，0..60000 毫秒，默认 1000。",
    "window_ms": "仅 duplicates 可用：重复请求的固定窗口，100..10000 毫秒，默认 1000。",
    "include_controlled": (
        "仅 aggregate/duplicates 可用：把规则命中与重放产生的请求也算进分析"
        "（默认排除）。"
    ),
    "budget": (
        "返回 JSON 的总字节预算，4096..262144，默认 65536；export 不受它限制。"
        "结果里 omitted 为真时，按 next_since/next_offset 继续取。"
    ),
    "url": "requests/分析类 action：大小写敏感的 URL 子串过滤。",
    "method": "requests/分析类 action：精确的 HTTP 方法，例如 GET、POST。",
    "resource_type": "requests/分析类 action：精确的资源类型，例如 Fetch、XHR。",
    "status": "requests/分析类 action：精确的 HTTP 状态码，100..599。",
    "state": "requests/分析类 action：抓包状态过滤，取值 "
    "pending/complete/failed/redirected/interrupted。",
    "kind": "requests/分析类 action：流量分类，取值 all/business/resource/extension。",
    "fields": (
        "requests：要额外返回的字段，逗号分隔。身份字段与 body 可用性始终保留；"
        "可选字段清单用 capabilities 查。"
    ),
    "wait_ms": (
        "仅 wait 可用：等待时长，0..60000 毫秒，默认 10000。它只观察已有执行，"
        "不会重新发送任何命令；命令结束不等于成功，要另看结果。"
    ),
    "command_id": (
        "仅 wait 可用：要等待的命令 ID，来自 activity 或 session_busy 的报错；"
        "不传表示等到空闲为止。"
    ),
    "output": "仅 export 可用：导出 JSON 的新文件路径。返回路径与大小，而不是完整抓包。",
    "run_id": "抓包 ID，不传就用最近一次抓包。",
    "name": "仅 start 可用：这次抓包的简短名字。",
    "part": (
        "request 详情投影，取值 metadata/request/response/headers/timing，"
        "默认 metadata（不含 body 文本）。"
    ),
    "offset": (
        "翻页偏移：body 字符偏移，或 pages/console/performance/分析类的条目偏移，"
        "0..65536。performance/aggregate/duplicates 只能用 offset 翻页，不能用 since。"
    ),
    "max_chars": "body 切片长度，1..16384，默认 4096。",
    "pointer": (
        "RFC 6901 指针，指向完整的、已脱敏的 request/response JSON body；"
        "必须同时把 part 设为 request 或 response。"
    ),
}

_DEBUG_PROPERTY_TYPES: dict[str, str] = {
    "id": "string",
    "rule": "string",
    "replay": "string",
    "slow_ms": "number",
    "window_ms": "number",
    "include_controlled": "boolean",
    "budget": "number",
    "url": "string",
    "method": "string",
    "resource_type": "string",
    "status": "number",
    "state": "string",
    "kind": "string",
    "fields": "string",
    "wait_ms": "number",
    "command_id": "string",
    "output": "string",
    "run_id": "string",
    "name": "string",
    "part": "string",
    "offset": "number",
    "max_chars": "number",
    "pointer": "string",
}

_DEBUG_PROPERTY_ENUMS: dict[str, tuple[str, ...]] = {
    "state": DEBUG_STATE_VALUES,
    "kind": DEBUG_KIND_VALUES,
    "part": DEBUG_PART_VALUES,
}

# 数值范围表：参数名 → (下界, 上界, 中文单位说明)。
_DEBUG_RANGES: dict[str, tuple[int, int]] = {
    "since": (0, _MAX_SINCE),
    "limit": (1, 100),
    "offset": (0, 65536),
    "max_chars": (1, 16384),
    "budget": (4096, 262144),
    "slow_ms": (0, 60000),
    "window_ms": (100, 10000),
    "status": (100, 599),
    "wait_ms": (0, 60000),
}

_COMPLETION_CRITERIA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "request-help 的自动完成条件（可选）：any（满足任一）与 all（全部满足）里"
        "合计最多 8 条条件，stableForMs 表示条件需连续成立多久（毫秒，>=0）。"
        "条件键用 camelCase："
        + "/".join(COMPLETION_CRITERIA_KEYS)
        + "。"
    ),
    "properties": {
        "any": {
            "type": "array",
            "description": "满足其中任意一条就算完成。",
            "items": {
                "type": "object",
                "properties": {
                    key: {"type": "string", "description": f"条件 {key}。"}
                    for key in COMPLETION_CRITERIA_KEYS
                },
            },
        },
        "all": {
            "type": "array",
            "description": "必须全部满足才算完成。",
            "items": {
                "type": "object",
                "properties": {
                    key: {"type": "string", "description": f"条件 {key}。"}
                    for key in COMPLETION_CRITERIA_KEYS
                },
            },
        },
        "stableForMs": {
            "type": "number",
            "description": "条件需连续成立多久（毫秒）才算完成，>=0，默认 0。",
        },
    },
}


def _action_property(tool: str) -> dict[str, Any]:
    """构造 ``action`` 属性；``enum`` 一律从 :data:`TOOL_ACTIONS` 取。"""
    actions = TOOL_ACTIONS[tool]
    return {
        "type": "string",
        "enum": list(actions),
        "description": (
            "要执行的操作，取值 "
            + " / ".join(actions)
            + "。它是必填项，其余参数按 action 取舍，写错会被拒绝并说明原因。"
        ),
    }


def _p(  # noqa: PLR0913 - 纯数据构造器，参数多但无逻辑
    type_: str,
    description: str,
    *,
    enum: tuple[str, ...] | None = None,
    items: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个 JSON Schema 属性。

    刻意不提供 ``required`` / ``integer`` 两个入口：前者 AstrBot 不读，
    后者会触发 ``SUPPORTED_TYPES`` 白名单失败（见模块 docstring）。
    """
    prop: dict[str, Any] = {"type": type_, "description": description}
    if enum is not None:
        prop["enum"] = list(enum)
    if items is not None:
        prop["items"] = items
    if properties is not None:
        prop["properties"] = properties
    return prop


# 跨工具共享的属性（同一含义在不同工具里必须字面一致）。
_SESSION_PROP = _p(
    "string",
    "要操作的浏览器会话 ID，必须是用 bsk_session(action=\"start\") 创建的。"
    "省略则用 current 会话（最近一次 start 或被指定过的那一个）。",
)
_TAB_ID_PROP = _p(
    "number",
    "目标标签页 ID（来自 bsk_tabs(action=\"list\")）。省略则用 Agent Window 的当前活动标签页。"
    "指定了就会把命令打在那一页上。",
)
_TARGET_PROP = _p("string", "元素定位：快照引用（如 @e3、e3）或 CSS 选择器。")
_HTML_REF_PROP = _p(
    "string",
    "从最近一次 observe/snapshot 拿到的快照引用（形如 @e3），限定只导出该子树的 HTML。",
)
_SCREENSHOT_REF_PROP = _p(
    "string", "从最近一次 observe/snapshot 拿到的快照引用（形如 @e3），只截该元素。"
)
_WAIT_UNTIL_PROP = _p(
    "string",
    "等待的页面生命周期阶段，默认 load。",
    enum=WAIT_UNTIL_VALUES,
)
_TIMEOUT_MS_PROP = _p("number", "本命令的超时时间（毫秒），必须大于 0。")
_DEVICE_PROP = _p("string", "设备预设名。", enum=DEVICE_PRESETS)
_WIDTH_PROP = _p("number", "宽度（CSS 像素）。resize 时为 100..7680，且必须与 height 同时给。")
_HEIGHT_PROP = _p("number", "高度（CSS 像素）。resize 时为 100..7680，且必须与 width 同时给。")

# ``bsk_debug`` 独占的 24 个参数（``debug_action`` 也在内）。
#
# 为什么单独抽一张表：``bsk_inspect`` 与 ``bsk_debug`` 是**互斥**的两套参数 ——
# 日常读页面不需要那 3652 字符的调试参数说明，用到调试时才带 ``bsk_debug``。
# 抽表是为了让"哪些参数属于 debug"只有一处定义，两个 schema 都从这里取，
# 也就不可能出现"拆分后某个参数两边都在/两边都没有"。
_DEBUG_ONLY_PROPS: dict[str, Any] = {
    "debug_action": _p(
        "string",
        "要执行的调试子动作的别名（与 action 同义，两者给一致的值即可，"
        "一般只填 action 就够了）。取值 "
        + " / ".join(DEBUG_ACTIONS)
        + "。都要先 start 抓包，再去访问页面，最后读结果。"
        "rule_add/rule_enable 能拦截、改写或伪造真实请求；"
        "replay 会重新发送一次请求，可能改动服务端数据。",
        enum=DEBUG_ACTIONS,
    ),
    "id": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["id"]),
    "rule": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["rule"]),
    "replay": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["replay"]),
    "slow_ms": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["slow_ms"]),
    "window_ms": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["window_ms"]),
    "include_controlled": _p("boolean", _DEBUG_PROPERTY_DESCRIPTIONS["include_controlled"]),
    "budget": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["budget"]),
    "url": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["url"]),
    "method": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["method"]),
    "resource_type": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["resource_type"]),
    "status": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["status"]),
    "state": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["state"], enum=DEBUG_STATE_VALUES),
    "kind": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["kind"], enum=DEBUG_KIND_VALUES),
    "fields": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["fields"]),
    "wait_ms": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["wait_ms"]),
    "command_id": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["command_id"]),
    "output": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["output"]),
    "run_id": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["run_id"]),
    "name": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["name"]),
    "part": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["part"], enum=DEBUG_PART_VALUES),
    "offset": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["offset"]),
    "max_chars": _p("number", _DEBUG_PROPERTY_DESCRIPTIONS["max_chars"]),
    "pointer": _p("string", _DEBUG_PROPERTY_DESCRIPTIONS["pointer"]),
}


# ---------------------------------------------------------------------------
# 七个工具的完整 schema
# ---------------------------------------------------------------------------

def _schema(tool: str, properties: dict[str, Any]) -> dict[str, Any]:
    """拼一个工具 schema。

    ``required`` 只声明 ``action``：AstrBot 的 ``spec_to_func`` 不生成它，
    但我们走的是「装饰器注册后覆写 ``parameters``」这条路，覆写后的 schema
    会被原样交给 provider，所以这里写的 ``required`` 是**真的会传给模型**的
    （DSH 侧同样是 ``required: ["action"]``）。其余参数的必填仍然只能靠
    :func:`validate` 在运行时报中文错 —— AstrBot 读不懂 per-action 的必填。
    """
    return {
        "type": "object",
        "required": ["action"],
        "properties": {"action": _action_property(tool), **properties},
    }


TOOL_SCHEMAS: dict[str, dict] = {
    "bsk_session": _schema(
        "bsk_session",
        {
            "url": _p("string", "start：启动后直接打开的网址；给了就不能是空串。"),
            "width": _p("number", "start：Agent Window 宽度（CSS 像素），必须与 height 同时给。"),
            "height": _p("number", "start：Agent Window 高度（CSS 像素），必须与 width 同时给。"),
            "no_focus": _p(
                "boolean",
                "start：是否在后台打开（不抢焦点）。本插件始终在后台打开会话，"
                "所以这一项目前没有效果；保留它只是为了与 BrowserSkill 的参数名对齐。",
            ),
            "browser": _p(
                "string",
                "start：要连接的浏览器实例 ID 或唯一的名称。已知要连哪个浏览器时，"
                "即使只连了一个也必须显式指定；不确定就先问用户，不要省略或换一个。",
            ),
            "device": _DEVICE_PROP,
            "session": _p(
                "string",
                "stop：要停止的自有会话 ID。不传时对 current 会话动手。",
            ),
        },
    ),
    "bsk_page": _schema(
        "bsk_page",
        {
            "session": _SESSION_PROP,
            "tab_id": _TAB_ID_PROP,
            "url": _p("string", "navigate 必填：要打开的网址，不能是空串。"),
            "wait_until": _WAIT_UNTIL_PROP,
            "timeout_ms": _TIMEOUT_MS_PROP,
            "hard": _p("boolean", "reload：true 表示绕过 HTTP 缓存强制刷新。"),
        },
    ),
    "bsk_inspect": _schema(
        "bsk_inspect",
        {
            "session": _SESSION_PROP,
            "tab_id": _TAB_ID_PROP,
            "cursor": _p(
                "string",
                "observe：继续取被省略的内容。翻页后上一页的 ref 会失效。"
                "snapshot 不支持 cursor。",
            ),
            "max_depth": _p("number", "observe/snapshot：树的最大深度，超过就截断。"),
            "max_tokens": _p("number", "observe/snapshot：渲染结果的 token 软上限（约 4 字符/token）。"),
            "max_bytes": _p(
                "number",
                "html：返回的 HTML 字节上限，必须大于 0，默认 524288。",
            ),
            "since": _p(
                "number",
                "console/network：增量游标，按 ID 去重，>=0。"
                "要按 offset 翻页的抓包分析请用 bsk_debug。",
            ),
            "limit": _p("number", "console/network：最多返回多少条，必须大于 0。"),
            "max_text_chars": _p("number", "console/network：每条文本最多多少字符，必须大于 0。"),
            "include_stack": _p("boolean", "console：是否附带结构化调用栈。"),
            "ref": _p("string", _HTML_REF_PROP["description"] + " screenshot 也用它裁剪截图。"),
        },
    ),
    "bsk_debug": _schema(
        "bsk_debug",
        {
            "session": _SESSION_PROP,
            "tab_id": _TAB_ID_PROP,
            # since / limit 是**跨工具共用**的：bsk_inspect 的 console/network 与
            # bsk_debug 的 console/pages 都靠它俩增量翻页（service.py 的
            # DEBUG_VALUE_FLAGS 里有它们，_validate_debug 也按 _DEBUG_RANGES 校验）。
            # 所以它们**不能**只挂在 bsk_inspect 上 —— 那会让 debug 的
            # console/pages 失去游标能力，且 _validate_debug 里「since 禁用于
            # performance/aggregate/duplicates」那条规则永远无法触发。
            "since": _p(
                "number",
                "console/pages：增量游标，按 ID 去重，>=0。"
                "performance/aggregate/duplicates 不接受 since，请改用 offset。",
            ),
            "limit": _p("number", "列表类 action：最多返回多少条，范围 1..100。"),
            **_DEBUG_ONLY_PROPS,
        },
    ),
    "bsk_interact": _schema(
        "bsk_interact",
        {
            "session": _SESSION_PROP,
            "tab_id": _TAB_ID_PROP,
            "target": _TARGET_PROP,
            "button": _p("string", "click：鼠标键，默认 left。", enum=CLICK_BUTTON_VALUES),
            "click_count": _p("number", "click：连击次数，双击就是 2；Canvas 只接受 1 或 2。"),
            "capture_id": _p(
                "string",
                "click：Canvas 截图的单次捕获 ID；用它定位时必须同时给 image_x 与 image_y。",
            ),
            "image_x": _p("number", "click：坐标 X，单位是原始 PNG 像素；必须与 capture_id、image_y 同时给。"),
            "image_y": _p("number", "click：坐标 Y，单位是原始 PNG 像素；必须与 capture_id、image_x 同时给。"),
            "value": _p("string", "fill 必填：要输入的文本。"),
            "no_clear": _p(
                "boolean",
                "fill：追加到原有内容末尾，而不是先清空。工具会自动把光标移到位，不用先点击。",
            ),
            "modifiers": _p(
                "array",
                "hover/wheel/click：过程中按住的修饰键。",
                items={"type": "string", "enum": list(MODIFIER_VALUES)},
            ),
            "settle_ms": _p("number", "hover：等待悬浮触发的内容稳定下来的毫秒数，必须大于 0，默认 200。"),
            "delta_x": _p("number", "wheel：水平滚动量，单位 CSS 像素，正数向右。"),
            "delta_y": _p("number", "wheel：垂直滚动量，单位 CSS 像素，正数向下。"),
            "timeout_ms": _TIMEOUT_MS_PROP,
            "values": _p(
                "array",
                "select 必填：要选中的 option value 列表，至少 1 项；会整体替换当前选择。",
                items={"type": "string"},
            ),
            "key": _p(
                "string",
                "press 必填：键名或组合键，例如 Enter、Escape、ArrowDown、Ctrl+A。",
            ),
            "hold_ms": _p("number", "press：按下到松开之间保持的毫秒数。"),
        },
    ),
    "bsk_tabs": _schema(
        "bsk_tabs",
        {
            "session": _SESSION_PROP,
            "tab_id": _p("number", "select/close/borrow/return 必填：来自 list 或 create 的标签页 ID。"),
            "scope": _p(
                "string",
                "list：列出哪个范围的标签页，默认 all。",
                enum=TABS_SCOPE_VALUES,
            ),
            "url": _p("string", "create：新标签页要打开的网址，默认 chrome://newtab/。"),
            "active": _p("boolean", "create：是否切到新标签页，默认 true。"),
            "index": _p("number", "create：插入位置下标。"),
        },
    ),
    "bsk_assist": _schema(
        "bsk_assist",
        {
            "session": _SESSION_PROP,
            "tab_id": _TAB_ID_PROP,
            "width": _p(
                "number",
                "resize 必填、emulate 可选：宽度（CSS 像素）。"
                "resize 时范围 100..7680；emulate 时要求与 height 同时给。",
            ),
            "height": _p(
                "number",
                "resize 必填、emulate 可选：高度（CSS 像素）。"
                "resize 时范围 100..7680；emulate 时要求与 width 同时给。",
            ),
            "device": _p("string", "emulate：内置设备预设。", enum=DEVICE_PRESETS),
            "mobile": _p(
                "boolean",
                "emulate：模拟移动端视口。它不能单独使用，必须同时给 width 与 height。",
            ),
            "off": _p("boolean", "emulate：清除该标签页上的全部模拟设置，只能单独使用。"),
            "prompt": _p(
                "string",
                "request-help 必填：显示在浏览器浮层里、写给用户看的具体操作说明。",
            ),
            "title": _p("string", "request-help：浮层标题。"),
            "targets": _p(
                "array",
                "request-help：要滚动到并高亮的快照引用或 CSS 选择器列表。",
                items={"type": "string"},
            ),
            "timeout_ms": _p("number", "request-help：最多等用户多久（毫秒），默认 300000。"),
            "completion_criteria": _COMPLETION_CRITERIA_SCHEMA,
        },
    ),
}
"""工具名 → 完整 JSON Schema（键与 :data:`TOOL_SCHEMAS` 一致，恰好 7 个）。

每个 schema 都是 ``{"type": "object", "required": ["action"], "properties": {...}}``，
properties 是该工具**全部 action 参数的并集**。
``required`` 只声明 ``action`` —— per-action 的必填在 AstrBot 侧表达不了，
只能靠 :func:`validate` 运行时报错。
"""


TOOL_DESCRIPTIONS: dict[str, str] = {
    "bsk_session": (
        "管理本插件自己的浏览器会话。action 取 start / stop / list："
        "start 打开一个 Agent Window（url、device、width+height、no_focus 可选）；"
        "stop 关闭自有会话；list 列出本插件创建的会话。"
        "要用浏览器前必须先 start；之后的工具会复用 current 会话。"
    ),
    "bsk_page": (
        "在 Agent Window 的当前标签页上导航与等待。action 取 navigate / back / forward / "
        "reload / wait。navigate 必填 url；reload 可加 hard 绕缓存；"
        "wait 只等页面生命周期事件、不做任何导航（适合点了会跳转的链接之后用）。"
        "页面明显变化后要重新 observe，再复用 ref。"
    ),
    "bsk_inspect": (
        "读取页面状态。action 取 observe / snapshot / html / screenshot / console / "
        "network。优先 observe，其次 snapshot，再考虑有上限的 html；要看视觉效果用 "
        "screenshot。console/network 支持 since/limit/max_text_chars 游标增量读。"
        "需要抓包、网络规则、重放等调试能力时改用 bsk_debug。"
    ),
    "bsk_debug": (
        "调试与网络流量控制：抓包、分析、导出证据，以及显式控制请求。"
        "什么时候用它：要查接口返回、慢请求、重复请求，或要拦截/改写/伪造请求时。"
        "action 取 performance / aggregate / duplicates / capabilities / activity / "
        "wait / pin / unpin / start / stop / status / requests / request / operations / "
        "operation / console / pages / export / rules / rule_add / rule_enable / "
        "rule_disable / rule_remove / replay。action 就是子动作本身。"
        "典型顺序：先 start 开抓包，再去访问页面，最后读结果。"
        "rule_add/rule_enable 能拦截、改写或伪造真实请求；"
        "replay 会带着用户的 cookie 重新发送一次请求，可能改动服务端数据。"
    ),
    "bsk_interact": (
        "与当前标签页交互。action 取 click / hover / wheel / scroll-to / focus / blur / fill / "
        "select / press。target 是快照引用（如 @e3）或 CSS 选择器；click/hover/scroll-to/"
        "focus/blur/fill/select 必填 target，fill 还要 value，select 还要 values，"
        "press 必填 key 并可先用 target 聚焦。wheel 要求 delta_x 或 delta_y 至少一个非零。"
        "操作后要 observe 一次确认结果。"
    ),
    "bsk_tabs": (
        "管理当前会话可见的标签页。action 取 list / create / select / close / borrow / return。"
        "select/close/borrow/return 必填 tab_id，取自 list 或 create。"
        "borrow 会把用户自己的标签页移进 Agent Window，用完请尽快 return。"
    ),
    "bsk_assist": (
        "显示设置与求助真人。action 取 resize / emulate / request-help。"
        "resize 必填 width 与 height（各 100..7680）；emulate 用 device 或 width+height(+mobile)，"
        "或单独用 off 清除；request-help 必填 prompt，可以等用户完成页内步骤。"
        "outcome 里只有 continued 与 completed 表示用户已完成，"
        "cancelled/timed_out/navigated/disabled 都不是。"
    ),
}
"""工具名 → 给模型看的中文描述，键与 :data:`TOOL_SCHEMAS` 完全一致。"""


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(name: str) -> str:
    """``maxDepth`` → ``max_depth``；已经是 snake_case 的原样返回。"""
    if not name:
        return name
    if "-" in name:
        name = name.replace("-", "_")
    if "_" in name or name.islower():
        return name
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def normalize_args(raw: dict) -> dict:
    """把模型传来的参数归一化成内部 ``snake_case``。

    规则：

    - 同时接受 ``snake_case`` 与 ``camelCase``（camelCase 转成 snake_case）；
    - 键名里的连字符也算分隔符（``tab-id`` → ``tab_id``）；
    - ``action`` 值里的连字符转下划线（``"scroll-to"`` → ``"scroll_to"``）；
    - 去掉值为 ``None`` 的键。

    **不抛异常**：非法输入（不是 dict、键名不是字符串等）原样跳过，
    真正的报错交给 :func:`validate`，这样错误消息能说清是哪个参数。

    Args:
        raw: 模型传来的原始参数字典。

    Returns:
        新的 dict，键是 snake_case。
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        normalized = _to_snake(key.strip())
        if not normalized or not _SNAKE_RE.match(normalized):
            # 键名归一化后仍不是合法标识符：保留原样，让 validate 报「未知参数」。
            normalized = key.strip()
        if value is None:
            continue
        if normalized == "action" and isinstance(value, str):
            out[normalized] = value.strip().lower().replace("-", "_")
        elif normalized == "debug_action" and isinstance(value, str):
            out[normalized] = value.strip().lower().replace("-", "_")
        elif isinstance(value, str):
            out[normalized] = value.strip()
        else:
            out[normalized] = value
    return out


# ---------------------------------------------------------------------------
# 校验：基础断言
# ---------------------------------------------------------------------------


def _where(tool: str, action: str) -> str:
    return f"{tool} 的 action={action}" if action else tool


class _Ctx:
    """一次校验的上下文：工具名、action、参数、schema 里声明过的键。"""

    __slots__ = ("tool", "action", "args", "known")

    def __init__(self, tool: str, args: dict) -> None:
        self.tool = tool
        self.action = str(args.get("action", "") or "")
        self.args = args
        self.known = set(TOOL_SCHEMAS[tool]["properties"])

    # --- 存在性 ---

    def has(self, key: str) -> bool:
        """参数是否**有效**存在。

        空串、纯空白串、空数组都算「没给」：模型经常把可选参数填成 ``""``
        来表示「不设置」，把它当成有效值会让必填校验和互斥校验都失效。
        """
        if key not in self.args:
            return False
        value = self.args[key]
        if isinstance(value, str):
            return value.strip() != ""
        if isinstance(value, (list, tuple, dict)):
            return len(value) > 0
        return True

    def given(self, key: str) -> bool:
        """参数是否**出现在入参里**（哪怕是空串/空数组）。

        与 :meth:`has` 的分工：必填与互斥用 ``has``（空串视为没给），
        「这个 action 不该接受某参数」的拒绝用 ``given`` —— 模型显式传了
        ``target=""`` 也是传了，静默忽略会掩盖它写错的事实。
        """
        return key in self.args and self.args[key] is not None

    def raw(self, key: str) -> Any:
        return self.args.get(key)

    def where(self) -> str:
        return _where(self.tool, self.action)

    def debug_action(self) -> str:
        """当前 debug 子动作（调用前应先用 :meth:`require` 确认它非空）。"""
        value = self.args.get("debug_action", "")
        return value.strip().lower() if isinstance(value, str) else ""

    def fail(self, message: str) -> None:
        raise BskToolError(message)

    # --- 通用检查 ---

    def require(self, *keys: str) -> None:
        """必填检查；缺一个就整体报错，一次说完。"""
        missing = [key for key in keys if not self.has(key)]
        if missing:
            self.fail(
                f"{self.where()} 缺少必填参数 "
                + "、".join(missing)
                + "；请补上后重试。"
            )

    def check_known(self) -> None:
        """拒绝 schema 里没声明的参数。

        为什么必须拦：AstrBot 的 handler 是 ``**kwargs`` 透传，
        schema 外的参数不会被框架拦下，会一路走到 service 造成难查的行为差异。
        """
        unknown = sorted(key for key in self.args if key not in self.known)
        if unknown:
            self.fail(
                f"{self.tool} 不认识参数 "
                + "、".join(unknown)
                + "；请改用 schema 里声明的参数名（参数名是 snake_case，"
                "例如 max_depth、timeout_ms），或先读一次工具说明。"
            )

    def check_consumed(self, *keys: str) -> None:
        """拒绝属于**别的 action** 的参数。

        为什么还需要这一道（``check_known`` 拦不住）：六个工具的 schema 是
        该工具**全部 action 参数的并集**（对齐 DSH 的扁平设计），所以
        ``known`` 是并集 —— ``click`` 收到 ``delta_y``、``wheel`` 收到
        ``values`` 都能通过 ``check_known``，然后被**静默丢弃**。

        这与 ``tab_id``/``timeout_ms``/``session`` 那几次缺陷是同一类：
        模型以为参数生效了，实际命令里没有它。区别是那几次是漏写回，
        这次是跨 action 的键根本不该被接受。

        Args:
            *keys: 本 action **真正会消费**的参数名。不在其中的、且模型确实
                给了值的参数，一律报错。
        """
        allowed = set(keys) | {"action"}
        extras = sorted(
            key
            for key in self.args
            if key not in allowed and self.given(key)
        )
        if extras:
            self.fail(
                f"{self.where()} 不接受参数 "
                + "、".join(extras)
                + "（它们属于同一个工具里的其他 action，传进来只会被忽略）；"
                "请去掉它们，或改用对应的 action。"
            )

    def as_str(self, key: str) -> str:
        """取字符串参数（空串归一成 ``""``）。"""
        value = self.raw(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是字符串，"
                f"收到的是 {type(value).__name__}（{value!r}）；请改成字符串。"
            )
        return value

    def as_bool(self, key: str) -> bool:
        """取布尔参数。

        只认真正的布尔值。``"true"`` / ``1`` 这类模糊输入一律拒绝：静默转换会
        让模型以为自己传对了，而实际行为取决于我们猜的规则。
        """
        value = self.raw(key)
        if value is None:
            return False
        if not isinstance(value, bool):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是布尔值 true/false，"
                f"收到的是 {type(value).__name__}（{value!r}）；请不要用字符串或数字代替。"
            )
        return value

    def as_int(
        self,
        key: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        positive: bool = False,
        non_negative: bool = False,
        default: int | None = None,
    ) -> int | None:
        """取整数参数并按范围校验。

        整数语义的参数在 schema 里是 ``number``（AstrBot 白名单没有 ``integer``），
        所以整数性只能在这里查。``3.0`` 放行，``3.5`` 拒绝 —— 后者几乎必然
        是模型算错了，静默取整会导致行为与模型预期不符。
        """
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是整数，"
                f"收到的是 {type(value).__name__}（{value!r}）；请改成数字。"
            )
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                self.fail(f"{self.where()} 的参数 {key} 必须是有限数字，收到的是 {value!r}。")
            if not float(value).is_integer():
                self.fail(
                    f"{self.where()} 的参数 {key} 必须是整数，收到的是 {value!r}；"
                    "请去掉小数部分。"
                )
        number = int(value)

        if positive and number <= 0:
            self.fail(
                f"{self.where()} 的参数 {key} 必须大于 0，收到的是 {number}；请改成正整数。"
            )
        if non_negative and number < 0:
            self.fail(
                f"{self.where()} 的参数 {key} 必须大于等于 0，收到的是 {number}；"
                "请改成非负整数。"
            )
        if minimum is not None and number < minimum:
            self.fail(
                f"{self.where()} 的参数 {key} 必须在 {minimum}..{maximum} 之间，"
                f"收到的是 {number}；请改成不小于 {minimum} 的值。"
            )
        if maximum is not None and number > maximum:
            self.fail(
                f"{self.where()} 的参数 {key} 必须在 {minimum}..{maximum} 之间，"
                f"收到的是 {number}；请改成不大于 {maximum} 的值。"
            )
        return number

    def as_float(self, key: str) -> float | None:
        """取浮点参数（``image_x`` / ``image_y`` 用）。"""
        value = self.raw(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是数字，"
                f"收到的是 {type(value).__name__}（{value!r}）。"
            )
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            self.fail(f"{self.where()} 的参数 {key} 必须是有限数字，收到的是 {value!r}。")
        return number

    def as_str_list(self, key: str) -> list[str] | None:
        """取字符串数组参数。"""
        value = self.raw(key)
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是字符串数组，"
                f"收到的是 {type(value).__name__}（{value!r}）；"
                f'请写成 ["a", "b"] 这种形式。'
            )
        out: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                self.fail(
                    f"{self.where()} 的参数 {key} 第 {index + 1} 项必须是字符串，"
                    f"收到的是 {type(item).__name__}（{item!r}）。"
                )
            out.append(item)
        return out

    def as_enum(self, key: str, allowed: tuple[str, ...], *, default: str = "") -> str:
        """取枚举参数。"""
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, str):
            self.fail(
                f"{self.where()} 的参数 {key} 必须是字符串，"
                f"收到的是 {type(value).__name__}（{value!r}）；"
                f"可选值：{'/'.join(allowed)}。"
            )
        text = value.strip()
        if not text:
            return default
        if text not in allowed:
            self.fail(
                f"{self.where()} 的参数 {key} 只能是 {'/'.join(allowed)} 之一，"
                f"收到的是 {text!r}；请从这些取值里挑一个。"
            )
        return text


def _check_log_controls(ctx: _Ctx) -> None:
    """console/network 共用的游标三件套校验。"""
    ctx.as_int("since", minimum=0, maximum=_MAX_SINCE)
    ctx.as_int("limit", positive=True)
    ctx.as_int("max_text_chars", positive=True)
    if ctx.has("cursor"):
        ctx.fail(
            f"{ctx.where()} 不支持 cursor（只有 observe 有续读游标）；"
            "console/network 请改用 since 游标。"
        )


def _check_tab_id(ctx: _Ctx) -> int | None:
    """校验 ``tab_id`` 并返回它。

    返回值必须由调用方放进结果 dict 里 —— DSH 把 ``tabId`` 放进全部六个工具
    的参数并集，并由 ``appendTabId`` 转发成 ``--tab-id``（只在未传时才退回活动
    标签页）。只校验不写回会让模型传了 tab_id 却打在别的标签上，属于静默错误。
    """
    return ctx.as_int("tab_id", minimum=0)


# ---------------------------------------------------------------------------
# 校验：每个工具的 per-action 规则
# ---------------------------------------------------------------------------


def _validate_session(ctx: _Ctx) -> dict:
    action = ctx.action

    if action == "start":
        if ctx.given("tab_id"):
            ctx.fail(
                f"{ctx.where()} 不接受 tab_id：start 是新建会话，"
                "会话建好后才有标签页可用。"
            )
        if ctx.given("session"):
            ctx.fail(
                f"{ctx.where()} 不接受 session：start 本来就是新建会话，"
                "要给会话定位请改用 stop。"
            )
        url = ctx.as_str("url")
        _reject_blank(ctx, "url", "要么给一个完整网址，要么整个省略它。")
        width_given = ctx.has("width")
        height_given = ctx.has("height")
        if width_given != height_given:
            ctx.fail(
                f"{ctx.where()} 的 width 与 height 必须同时给："
                f"现在只给了 {'width' if width_given else 'height'}，请补上另一个。"
            )
        width = ctx.as_int("width", positive=True) if width_given else None
        height = ctx.as_int("height", positive=True) if height_given else None
        ctx.check_known()
        out: dict[str, Any] = {
            "url": url,
            "no_focus": ctx.as_bool("no_focus"),
            "browser": ctx.as_str("browser"),
            "device": ctx.as_enum("device", DEVICE_PRESETS),
        }
        if width is not None and height is not None:
            out["width"] = width
            out["height"] = height
        return out

    if action == "stop":
        for key in ("url", "device", "width", "height", "no_focus", "browser"):
            if ctx.given(key):
                ctx.fail(
                    f"{ctx.where()} 不接受参数 {key}：它只对 start 有意义。"
                    "要停止会话只需要 session。"
                )
        if ctx.given("tab_id"):
            ctx.fail(
                f"{ctx.where()} 不接受 tab_id：会话管理不涉及标签页，"
                "要停止会话只需要 session。"
            )
        ctx.check_known()
        return {"session": ctx.as_str("session")}

    # list：不接受任何额外参数
    for key in ("session", "url", "device", "width", "height", "no_focus",
                "browser", "tab_id"):
        if ctx.given(key):
            ctx.fail(
                f"{ctx.where()} 不接受参数 {key}：list 只读插件自己的注册表，"
                "不访问浏览器，所以不需要任何参数。"
            )
    ctx.check_known()
    return {}


def _validate_page(ctx: _Ctx) -> dict:
    action = ctx.action
    tab_id = _check_tab_id(ctx)
    wait_until = ctx.as_enum("wait_until", WAIT_UNTIL_VALUES, default="load")
    timeout_ms = ctx.as_int("timeout_ms", positive=True)

    if action == "navigate":
        ctx.require("url")
        url = ctx.as_str("url")
        _reject_blank(ctx, "url", "请给一个完整网址，例如 https://example.com。")
        if ctx.given("hard"):
            ctx.fail(f"{ctx.where()} 不接受 hard：它只对 reload 有意义。")
        ctx.check_known()
        return {
            "url": url,
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "tab_id": tab_id,
            "hard": False,
        }

    if ctx.given("url"):
        ctx.fail(
            f"{ctx.where()} 不接受 url：它只对 navigate 有意义"
            f"（back/forward 由浏览器历史决定，reload 用当前地址）。"
        )

    if action == "reload":
        ctx.check_known()
        return {
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "hard": ctx.as_bool("hard"),
            "tab_id": tab_id,
        }

    if ctx.given("hard"):
        ctx.fail(f"{ctx.where()} 不接受 hard：它只对 reload 有意义。")
    ctx.check_known()
    return {
        "wait_until": wait_until,
        "timeout_ms": timeout_ms,
        "tab_id": tab_id,
    }


def _validate_inspect(ctx: _Ctx) -> dict:
    action = ctx.action
    tab_id = _check_tab_id(ctx)

    # ``debug`` 已拆成独立的 ``bsk_debug`` 工具。模型如果还按旧习惯往这里传
    # ``debug_action``，必须明确告诉它换工具 —— 只说"不认识这个参数"会让它
    # 以为参数名写错了，然后一直重试同一个工具。
    if ctx.given("debug_action"):
        ctx.fail(
            f"{ctx.where()} 不接受 debug_action：调试已拆成独立的 bsk_debug 工具，"
            "请改用 bsk_debug（把 action 设为原来的 debug_action 取值）。"
        )

    if action in ("observe", "snapshot"):
        if ctx.given("ref"):
            ctx.fail(
                f"{ctx.where()} 不接受 ref：树状快照不需要限定范围，"
                "要按元素取内容请用 html。"
            )
        max_depth = ctx.as_int("max_depth")
        max_tokens = ctx.as_int("max_tokens")
        ctx.check_known()
        out: dict[str, Any] = {"tab_id": tab_id}
        if ctx.has("cursor"):
            if action == "snapshot":
                ctx.fail(
                    f"{ctx.where()} 不接受 cursor：snapshot 是静态快照，没有续读游标；"
                    "需要翻页请用 observe。"
                )
            out["cursor"] = ctx.as_str("cursor")
        if max_depth is not None:
            out["max_depth"] = max_depth
        if max_tokens is not None:
            out["max_tokens"] = max_tokens
        return out

    if action == "html":
        for key in ("cursor", "max_depth", "max_tokens"):
            if ctx.given(key):
                ctx.fail(
                    f"{ctx.where()} 不接受 {key}：它只对 observe/snapshot 有意义。"
                )
        ref = ctx.as_str("ref")
        _reject_blank(ctx, "ref", "要么给 @e3 这样的快照引用，要么省略它。")
        if ctx.given("ref"):
            if not _REF_RE.match(ref.strip()):
                ctx.fail(
                    f"{ctx.where()} 的 ref 必须形如 @e3 或 e3（来自最近一次 observe/snapshot），"
                    f"收到的是 {ref!r}；CSS 选择器不支持。"
                )
            ref = ref.strip()
        max_bytes = ctx.as_int("max_bytes", positive=True)
        ctx.check_known()
        out = {"ref": ref, "tab_id": tab_id}
        if max_bytes is not None:
            out["max_bytes"] = max_bytes
        return out

    if action == "screenshot":
        for key in ("cursor", "max_depth", "max_tokens", "max_bytes"):
            if ctx.given(key):
                ctx.fail(f"{ctx.where()} 不接受 {key}：它不属于 screenshot。")
        ref = ctx.as_str("ref").strip()
        _reject_blank(ctx, "ref", "要么给 @e3 这样的快照引用，要么整个省略它（截整页）。")
        ctx.check_known()
        return {"ref": ref, "tab_id": tab_id}

    if action == "console":
        # console 只吃 since/limit/max_text_chars/include_stack。下面这些键已经
        # 不在 bsk_inspect 的 schema 里（check_known 也会拦），但这里的消息能说清
        # "该去哪个工具"，比"不认识参数"有用得多。
        for key in ("ref", "max_chars", "max_depth", "max_tokens", "offset", "budget",
                    "pointer", "run_id", "id", "rule", "replay", "slow_ms", "window_ms",
                    "part", "state", "kind", "fields", "wait_ms", "command_id", "output",
                    "name", "method", "resource_type", "status", "url",
                    "include_controlled", "debug_action"):
            if ctx.given(key):
                ctx.fail(
                    f"{ctx.where()} 不接受 {key}：它属于 bsk_debug 或 observe/snapshot，"
                    "console 只支持 since / limit / max_text_chars / include_stack。"
                )
        _check_log_controls(ctx)
        _check_tab_id(ctx)
        ctx.check_known()
        return {
            "since": ctx.as_int("since", minimum=0, maximum=_MAX_SINCE, default=0),
            "limit": ctx.as_int("limit", positive=True),
            "max_text_chars": ctx.as_int("max_text_chars", positive=True),
            "include_stack": ctx.as_bool("include_stack"),
            "tab_id": tab_id,
        }

    if action == "network":
        for key in ("ref", "max_chars", "max_depth", "max_tokens", "offset", "budget",
                    "pointer", "run_id", "id", "rule", "replay", "slow_ms", "window_ms",
                    "part", "state", "kind", "fields", "wait_ms", "command_id", "output",
                    "name", "method", "resource_type", "status", "url",
                    "include_controlled", "debug_action", "include_stack"):
            if ctx.given(key):
                ctx.fail(
                    f"{ctx.where()} 不接受 {key}：它属于 bsk_debug 或 console，"
                    "network 只支持 since / limit / max_text_chars。"
                )
        _check_log_controls(ctx)
        _check_tab_id(ctx)
        ctx.check_known()
        return {
            "since": ctx.as_int("since", minimum=0, maximum=_MAX_SINCE, default=0),
            "limit": ctx.as_int("limit", positive=True),
            "max_text_chars": ctx.as_int("max_text_chars", positive=True),
            "tab_id": tab_id,
        }

    # 走不到这里：action 必须是 TOOL_ACTIONS["bsk_inspect"] 里的六个之一，
    # 而上面五个分支已经覆盖全部（debug 已拆成独立的 bsk_debug 工具）。
    # 用 raise 而不是 ctx.fail：这一行同时是"函数不会返回 None"的类型保证。
    raise BskToolError(
        f"{ctx.where()} 不是 bsk_inspect 支持的操作；"
        "可选值：" + " / ".join(TOOL_ACTIONS["bsk_inspect"]) + "。"
    )


def _validate_debug(ctx: _Ctx) -> dict:
    """``bsk_debug`` 的 24 个 action 的规则，逐条对照 DSH。

    为什么整条 debug 规则链原封不动地留在这里、只是换了个入口：这些条件规则
    （id 必填、pointer↔part、slow_ms 仅 aggregate、since 禁用于分析类、
    rule_add↔rule、<=81920 字符……）是过去几轮实测逐条钉下来的，拆工具时
    放松任何一条都等于把已修好的缺陷放回去。
    """
    # 调试子动作有两个入口，取到同一个值：
    #
    # - ``action``：``bsk_debug`` 的工具级 action（schema 的 enum 就是那 24 个）；
    # - ``debug_action``：拆分前 ``bsk_inspect(action="debug")`` 的写法。保留它作
    #   别名，是为了让已经从旧描述里学会 "debug_action" 的对话/提示词不会因为
    #   工具拆分而整条失败 —— 但它**不再必填**，唯一的事实源是 ``action``。
    #
    # 两个都给且不一致时报错，不静默取其一：那会让模型以为另一个生效了。
    if ctx.has("debug_action") and ctx.debug_action() != ctx.action:
        ctx.fail(
            f"{ctx.where()} 同时给了 action={ctx.action!r} 与 "
            f"debug_action={ctx.debug_action()!r}，两者不一致；"
            "它们是同一个东西（要执行的调试子动作），请只给 action。"
        )
    tab_id = _check_tab_id(ctx)
    debug_action = ctx.action
    # ``__model__`` 是框架保留名，不是可用取值。tool 级的 action 已经显式拒绝它，
    # 这里再兜一道：``debug_action`` 绕过同一个名字时行为必须一致。
    if ctx.debug_action() == "__model__":
        ctx.fail(
            f"{ctx.where()} 的调试子动作不能是 __model__（那只是框架的内部提示符，"
            "不是可执行操作）；可选值：" + " / ".join(DEBUG_ACTIONS) + "。"
        )
    ctx.as_enum("debug_action", DEBUG_ACTIONS)

    # --- 范围（先查范围，再查条件规则：范围错时先说范围）---
    values: dict[str, int | None] = {}
    for key, (low, high) in _DEBUG_RANGES.items():
        values[key] = ctx.as_int(key, minimum=low, maximum=high)

    # --- 条件规则 ---
    # 下面每条的前半段都是 ``ctx.where()``（形如 "bsk_debug 的 action=replay"），
    # 所以正文里只说参数名的约束，不再重复一遍 action=xxx，避免报错读起来像绕口令。
    if debug_action in ID_REQUIRED_DEBUG_ACTIONS and not ctx.has("id"):
        ctx.fail(
            f"{ctx.where()} 必须带 id；"
            "request/operation/rule 的 ID 来自对应的列表类 action，"
            "pin/unpin 与 replay 也需要它来定位对象。"
        )
    if ctx.has("id"):
        ctx.as_str("id")

    if ctx.has("pointer"):
        ctx.as_str("pointer")
        part = ctx.as_enum("part", DEBUG_PART_VALUES)
        if part not in ("request", "response"):
            ctx.fail(
                f"{ctx.where()} 用了 pointer，就必须把 part 设成 request 或 response"
                "（pointer 只在完整请求/响应体里有意义），"
                f"当前 part 是 {part or '未设置'}。"
            )

    if ctx.has("slow_ms") and debug_action != "aggregate":
        ctx.fail(
            f"{ctx.where()} 的 slow_ms 只能用于 action=aggregate，"
            f"当前是 {debug_action}；慢请求阈值只在聚合分析里有意义。"
        )
    if ctx.has("window_ms") and debug_action != "duplicates":
        ctx.fail(
            f"{ctx.where()} 的 window_ms 只能用于 action=duplicates，"
            f"当前是 {debug_action}。"
        )
    if ctx.has("include_controlled") and debug_action not in CONTROLLED_ONLY_DEBUG_ACTIONS:
        ctx.fail(
            f"{ctx.where()} 的 include_controlled 只能用于 action=aggregate 或 "
            f"duplicates，当前是 {debug_action}。"
        )
    if ctx.has("since") and debug_action in NO_SINCE_DEBUG_ACTIONS:
        ctx.fail(
            f"{ctx.where()} 不接受 since，"
            "它用 offset 翻页；请把 since 换成 offset。"
        )

    has_rule = ctx.has("rule")
    has_replay = ctx.has("replay")
    if (debug_action == "rule_add") != has_rule:
        ctx.fail(
            f"{ctx.where()} 的 rule 与 rule_add 必须成对出现："
            "action=rule_add 必须带 rule（规则 JSON），其他 action 不要带 rule。"
        )
    if (debug_action == "replay") != has_replay:
        ctx.fail(
            f"{ctx.where()} 的 replay 与 replay 动作必须成对出现："
            "action=replay 必须带 replay（请求 JSON），其他 action 不要带 replay。"
        )

    for key, label in (("rule", "rule_add 的规则"), ("replay", "replay 的请求")):
        if not ctx.has(key):
            continue
        text = ctx.as_str(key)
        if len(text) > 81920:
            ctx.fail(
                f"{ctx.where()} 的 {key} 太长了（{len(text)} 字符），上限是 81920 字符；"
                f"请拆成多次调用，或改用 export 导出后再处理。"
            )
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            ctx.fail(
                f"{ctx.where()} 的 {key} 不是合法 JSON（{exc}）；"
                f"请给一个 JSON 字符串，例如 {label} 的合法对象。"
            )
        if not isinstance(parsed, dict):
            ctx.fail(
                f"{ctx.where()} 的 {key} 必须是 JSON 对象（以 {{ 开头），"
                f"收到的是 {type(parsed).__name__}。"
            )

    ctx.check_known()

    out: dict[str, Any] = {"debug_action": debug_action, "tab_id": tab_id}
    for key, value in values.items():
        if value is not None:
            out[key] = value
    for key in (
        "id",
        "rule",
        "replay",
        "url",
        "method",
        "resource_type",
        "fields",
        "command_id",
        "output",
        "run_id",
        "name",
        "pointer",
    ):
        text = ctx.as_str(key)
        if text:
            out[key] = text
    # 枚举字段必须走 as_enum —— 直接 as_str 会让非法取值静默通过，
    # 一路传进 argv 变成 bsk 的 clap 报错（对模型毫无指导价值）。
    for key, allowed in (
        ("part", DEBUG_PART_VALUES),
        ("state", DEBUG_STATE_VALUES),
        ("kind", DEBUG_KIND_VALUES),
    ):
        if ctx.has(key):
            out[key] = ctx.as_enum(key, allowed)
    out["include_controlled"] = ctx.as_bool("include_controlled")
    return out


def _validate_interact(ctx: _Ctx) -> dict:
    action = ctx.action
    tab_id = _check_tab_id(ctx)
    out: dict[str, Any] = {"tab_id": tab_id}

    modifiers = ctx.as_str_list("modifiers")
    if modifiers is not None:
        bad = [m for m in modifiers if m not in MODIFIER_VALUES]
        if bad:
            ctx.fail(
                f"{ctx.where()} 的 modifiers 只能包含 {'/'.join(MODIFIER_VALUES)}，"
                f"收到的是 {bad}；请去掉不支持的键名。"
            )
    timeout_ms = ctx.as_int("timeout_ms", positive=True)

    if action == "click":
        ctx.require("target")
        target = ctx.as_str("target")
        if not target.strip():
            ctx.fail(
                f"{ctx.where()} 的 target 不能是空白字符串；"
                "请给 @e3 这样的快照引用，或一个 CSS 选择器。"
            )
        button = ctx.as_enum("button", CLICK_BUTTON_VALUES, default="left")
        click_count = ctx.as_int("click_count", positive=True)
        capture_id = ctx.as_str("capture_id").strip()
        image_x = ctx.as_float("image_x")
        image_y = ctx.as_float("image_y")
        canvas_args = (bool(capture_id), image_x is not None, image_y is not None)
        if any(canvas_args):
            if not all(canvas_args):
                ctx.fail(
                    f"{ctx.where()} 走 Canvas 截图点击时必须同时给 capture_id、image_x、"
                    "image_y 三个参数；请补齐后再试。"
                )
            if click_count is not None and click_count not in (1, 2):
                ctx.fail(
                    f"{ctx.where()} 在 Canvas 截图上点击时 click_count 只能是 1 或 2，"
                    f"收到的是 {click_count}。"
                )
        ctx.check_known()
        # modifiers 为空时**不写进 out**：写空列表会让服务层的判空失效，
        # 于是每条命令都多带一个 `--modifiers ''`。CLI 默认值虽是空串，
        # 但显式传空串是否等价于"不指定"取决于扩展侧解析，不值得赌。
        out.update({"target": target, "button": button, "capture_id": capture_id})
        if modifiers:
            out["modifiers"] = modifiers
        if click_count is not None:
            out["click_count"] = click_count
        if image_x is not None and image_y is not None:
            out["image_x"] = image_x
            out["image_y"] = image_y
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    if action == "hover":
        ctx.require("target")
        target = ctx.as_str("target")
        if not target.strip():
            ctx.fail(f"{ctx.where()} 的 target 不能是空白字符串。")
        settle_ms = ctx.as_int("settle_ms", positive=True)
        ctx.check_known()
        out.update({"target": target})
        if modifiers:
            out["modifiers"] = modifiers
        if settle_ms is not None:
            out["settle_ms"] = settle_ms
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    if action == "wheel":
        if "target" in ctx.args:
            target = ctx.as_str("target").strip()
            if not target:
                ctx.fail(
                    f"{ctx.where()} 的 target 不能是空白字符串；不指定落点就整个省略它。"
                )
            out["target"] = target
        delta_x = ctx.as_float("delta_x")
        delta_y = ctx.as_float("delta_y")
        if (delta_x or 0.0) == 0.0 and (delta_y or 0.0) == 0.0:
            ctx.fail(
                f"{ctx.where()} 要求 delta_x 与 delta_y 至少有一个非零，"
                "否则等于什么都没滚；请给一个非零的滚动量。"
            )
        ctx.check_known()
        # delta 用整数渲染：CLI 报告里的默认值是整数 `0`，而 as_float 会得到
        # float，str() 之后变成 "0.0"/"100.0"。服务层再 str() 一次就会把
        # 浮点形态发进 argv —— 没必要冒这个险，滚动量本来就没有小数语义。
        out.update(
            {
                "delta_x": int(delta_x or 0),
                "delta_y": int(delta_y or 0),
            }
        )
        if modifiers:
            out["modifiers"] = modifiers
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    # action 已在 validate() 里归一到 TOOL_ACTIONS 的写法（连字符版），
    # 这里必须用 "scroll-to"，写 "scroll_to" 会永远匹配不上并掉到 press 分支。
    if action in ("scroll-to", "focus", "blur"):
        ctx.require("target")
        target = ctx.as_str("target")
        if not target.strip():
            ctx.fail(f"{ctx.where()} 的 target 不能是空白字符串。")
        ctx.check_known()
        out.update({"target": target})
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    if action == "fill":
        ctx.require("target")
        target = ctx.as_str("target")
        if not target.strip():
            ctx.fail(f"{ctx.where()} 的 target 不能是空白字符串。")
        # value 可以是空串（清空输入框是合法意图），但不能缺席。
        if "value" not in ctx.args or ctx.raw("value") is None:
            ctx.fail(
                f"{ctx.where()} 缺少必填参数 value；"
                '请补上要输入的文本（想看齐输入框就传空字符串 ""）。'
            )
        ctx.check_known()
        out.update(
            {
                "target": target,
                "value": ctx.as_str("value"),
                "no_clear": ctx.as_bool("no_clear"),
                "tab_id": tab_id,
            }
        )
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    if action == "select":
        ctx.require("target", "values")
        target = ctx.as_str("target")
        if not target.strip():
            ctx.fail(f"{ctx.where()} 的 target 不能是空白字符串。")
        values = ctx.as_str_list("values") or []
        if not values:
            ctx.fail(
                f"{ctx.where()} 的 values 至少要有一项；"
                '请写成 ["选项值"] 这样，否则等于没有选择任何选项。'
            )
        if any(not item.strip() for item in values):
            ctx.fail(f"{ctx.where()} 的 values 里不能有空字符串项。")
        ctx.check_known()
        out.update({"target": target, "values": values})
        if timeout_ms is not None:
            out["timeout_ms"] = timeout_ms
        return out

    # press
    ctx.require("key")
    key = ctx.as_str("key")
    if not key.strip():
        ctx.fail(
            f"{ctx.where()} 的 key 不能是空白字符串；"
            "请给键名或组合键，例如 Enter、Escape、ArrowDown、Ctrl+A。"
        )
    hold_ms = ctx.as_int("hold_ms", non_negative=True)
    ctx.check_known()
    out.update({"key": key.strip()})
    if "target" in ctx.args:
        target = ctx.as_str("target").strip()
        if not target:
            ctx.fail(
                f"{ctx.where()} 的 target 不能是空白字符串；"
                "不想先聚焦某个元素就整个省略它。"
            )
        out["target"] = target
    if hold_ms is not None:
        out["hold_ms"] = hold_ms
    if timeout_ms is not None:
        out["timeout_ms"] = timeout_ms
    return out


def _validate_tabs(ctx: _Ctx) -> dict:
    action = ctx.action

    if action == "list":
        for key in ("url", "active", "index"):
            if ctx.given(key):
                ctx.fail(
                    f"{ctx.where()} 不接受参数 {key}：list 只列标签页，"
                    "请用 scope 选择要列的范围。"
                )
        ctx.check_known()
        # tab_id 允许出现：DSH 把它放在全部六个工具的并集里，校验层拒绝会让
        # 模型以为参数写错了。list 用不到它，原样回传，由 service 决定是否忽略。
        # 只在模型显式传了时才回传，避免凭空多出一个 None 键。
        out_list: dict[str, Any] = {
            "scope": ctx.as_enum("scope", TABS_SCOPE_VALUES, default="all")
        }
        if ctx.given("tab_id"):
            out_list["tab_id"] = ctx.as_int("tab_id", minimum=0)
        return out_list

    if action == "create":
        if ctx.given("scope"):
            ctx.fail(f"{ctx.where()} 不接受参数 scope：它只有 list 需要。")
        url = ctx.as_str("url")
        _reject_blank(ctx, "url", "要么给一个网址，要么整个省略它（默认 chrome://newtab/）。")
        active = ctx.as_bool("active") if ctx.given("active") else True
        index = ctx.as_int("index", minimum=0)
        ctx.check_known()
        out: dict[str, Any] = {"url": url.strip(), "active": active}
        if ctx.given("tab_id"):
            out["tab_id"] = ctx.as_int("tab_id", minimum=0)
        if index is not None:
            out["index"] = index
        return out

    # select / close / borrow / return
    for key in ("scope", "url", "active", "index"):
        if ctx.given(key):
            ctx.fail(
                f"{ctx.where()} 不接受参数 {key}：只需 tab_id"
                "（来自 list 或 create 的返回值）。"
            )
    ctx.require("tab_id")
    tab_id = ctx.as_int("tab_id", minimum=0)
    ctx.check_known()
    return {"tab_id": tab_id}


def _reject_blank(ctx: _Ctx, key: str, hint: str) -> None:
    """显式传了但值为空白 → 直接拒绝。

    不能靠 :meth:`_Ctx.has` 判定：``has`` 把空白当「没给」，于是
    ``{"url": "   "}`` 会静默通过并变成空串往下走。模型显式写了这个键，
    就说明它想传值，只是传错了，必须让它知道。
    """
    if ctx.given(key):
        value = ctx.raw(key)
        if isinstance(value, str) and not value.strip():
            ctx.fail(f"{ctx.where()} 的 {key} 不能是空白字符串；{hint}")


def _validate_assist(ctx: _Ctx) -> dict:
    action = ctx.action

    if action == "resize":
        for key in ("device", "mobile", "off", "prompt", "title", "targets",
                    "completion_criteria"):
            if ctx.given(key):
                ctx.fail(f"{ctx.where()} 不接受参数 {key}：resize 只需要 width 与 height。")
        ctx.require("width", "height")
        width = ctx.as_int("width", minimum=100, maximum=7680)
        height = ctx.as_int("height", minimum=100, maximum=7680)
        tab_id = _check_tab_id(ctx)
        ctx.check_known()
        return {"width": width, "height": height, "tab_id": tab_id}

    if action == "emulate":
        for key in ("prompt", "title", "targets", "completion_criteria"):
            if ctx.given(key):
                ctx.fail(f"{ctx.where()} 不接受参数 {key}：它只对 request-help 有意义。")
        off = ctx.as_bool("off")
        device = ctx.as_enum("device", DEVICE_PRESETS)
        width = ctx.as_int("width", positive=True)
        height = ctx.as_int("height", positive=True)
        mobile = ctx.as_bool("mobile")

        if off:
            conflicts = [name for name, given in
                         (("device", ctx.has("device")), ("width", ctx.has("width")),
                          ("height", ctx.has("height")), ("mobile", ctx.has("mobile")))
                         if given]
            if conflicts:
                ctx.fail(
                    f"{ctx.where()} 的 off 与 " + "、".join(conflicts)
                    + " 互斥：off 是清除全部模拟设置，请单独使用它。"
                )
        else:
            if not device and width is None:
                ctx.fail(
                    f"{ctx.where()} 没说要模拟什么：请给 device，"
                    "或同时给 width 与 height（mobile 也必须配 width+height），"
                    "或者用 off 清除已有设置。"
                )
            if (width is None) != (height is None):
                ctx.fail(
                    f"{ctx.where()} 的 width 与 height 必须同时给："
                    f"现在只给了 {'width' if width is not None else 'height'}，"
                    "请补上另一个。"
                )
            if mobile and width is None:
                ctx.fail(
                    f"{ctx.where()} 的 mobile 不能单独使用："
                    "bsk 不接受没有视口尺寸的 --mobile，请同时给 width 与 height。"
                )
        tab_id = _check_tab_id(ctx)
        ctx.check_known()
        out: dict[str, Any] = {"off": off, "device": device, "mobile": mobile,
                               "tab_id": tab_id}
        if width is not None and height is not None:
            out["width"] = width
            out["height"] = height
        return out

    # request-help
    for key in ("device", "mobile", "off", "url", "max_depth", "max_tokens", "max_bytes"):
        if ctx.given(key):
            ctx.fail(f"{ctx.where()} 不接受参数 {key}：它不属于 request-help。")
    ctx.require("prompt")
    prompt = ctx.as_str("prompt")
    if not prompt.strip():
        ctx.fail(
            f"{ctx.where()} 的 prompt 不能是空白字符串；"
            "请写给用户看的具体操作说明，例如「请在弹出的登录框里完成手机验证码」。"
        )
    title = ctx.as_str("title")
    targets = ctx.as_str_list("targets")
    if targets is not None:
        if not targets:
            ctx.fail(f"{ctx.where()} 的 targets 不能是空数组；不指定就整个省略它。")
        if any(not item.strip() for item in targets):
            ctx.fail(f"{ctx.where()} 的 targets 里不能有空字符串项。")
    timeout_ms = ctx.as_int("timeout_ms", positive=True)
    completion_criteria = _validate_completion_criteria(ctx)
    tab_id = _check_tab_id(ctx)
    ctx.check_known()
    out = {"prompt": prompt, "title": title, "targets": targets or [],
           "timeout_ms": timeout_ms if timeout_ms is not None else 300000,
           "tab_id": tab_id}
    if completion_criteria is not None:
        out["completion_criteria"] = completion_criteria
    return out


def _validate_completion_criteria(ctx: _Ctx) -> dict | None:
    """校验 ``completion_criteria``，并转成 bsk CLI 需要的 snake_case。

    对模型暴露的是 **camelCase**（与 DSH 的 schema 一致，见
    :data:`COMPLETION_CRITERIA_KEYS`）；但 ``service.request_help`` 会把它塞进
    ``--completion-criteria`` 的 JSON 里，CLI 侧要的是 snake_case。

    所以这里两种写法都收（``stableForMs`` 与 ``stable_for_ms``、
    ``selectorExists`` 与 ``selector_exists``），**输出统一是 snake_case**。
    错误消息一律用 camelCase 指代字段 —— 那才是模型在 schema 里看到的写法，
    用别的写法报错会让它无从纠正。
    """
    value = ctx.raw("completion_criteria")
    if value is None:
        return None
    if not isinstance(value, dict):
        ctx.fail(
            f"{ctx.where()} 的 completion_criteria 必须是对象，"
            f"收到的是 {type(value).__name__}（{value!r}）；"
            '形如 {"any": [{"textExists": "已完成"}]}。'
        )

    where = f"{ctx.where()} 的 completion_criteria"
    normalized: dict[str, Any] = {}
    total = 0

    for bucket in ("any", "all"):
        conditions = value.get(bucket)
        if conditions is None:
            continue
        if not isinstance(conditions, (list, tuple)):
            ctx.fail(
                f"{where}.{bucket} 必须是数组，"
                f"收到的是 {type(conditions).__name__}（{conditions!r}）。"
            )
        cleaned: list[dict[str, str]] = []
        for index in range(len(conditions)):
            condition = conditions[index]
            if not isinstance(condition, dict):
                ctx.fail(
                    f"{where}.{bucket} 第 {index + 1} 项必须是对象，"
                    f"收到的是 {type(condition).__name__}（{condition!r}）。"
                )
            if not condition:
                ctx.fail(
                    f"{where}.{bucket} 第 {index + 1} 项是空对象；"
                    "请至少写一个条件键，例如 selectorExists 或 textExists。"
                )
            item: dict[str, str] = {}
            for key, raw in condition.items():
                canonical = COMPLETION_CRITERIA_ALIASES.get(key)
                if canonical is None:
                    ctx.fail(
                        f"{where}.{bucket} 第 {index + 1} 项含未知条件键 {key!r}；"
                        "可选键是 "
                        + "/".join(COMPLETION_CRITERIA_KEYS)
                        + "（camelCase，与工具参数说明一致）。"
                    )
                if not isinstance(raw, str):
                    ctx.fail(
                        f"{where}.{bucket} 第 {index + 1} 项的 {canonical} 必须是字符串，"
                        f"收到的是 {type(raw).__name__}（{raw!r}）。"
                    )
                # 输出用 CLI 侧的 snake_case。
                item[COMPLETION_CRITERIA_CLI_KEYS[canonical]] = raw
            cleaned.append(item)
        total += len(cleaned)
        if cleaned:
            normalized[bucket] = cleaned

    if total > 8:
        ctx.fail(
            f"{where} 里 any 与 all 合计最多 8 条条件，"
            f"现在是 {total} 条；请删掉一些，或拆成多次 request-help。"
        )

    stable_keys = [k for k in ("stableForMs", "stable_for_ms") if k in value]
    if stable_keys:
        stable = value[stable_keys[0]]
        if isinstance(stable, bool) or not isinstance(stable, (int, float)):
            ctx.fail(
                f"{where}.stableForMs 必须是整数，"
                f"收到的是 {type(stable).__name__}（{stable!r}）。"
            )
        if isinstance(stable, float) and not float(stable).is_integer():
            ctx.fail(
                f"{where}.stableForMs 必须是整数，收到的是 {stable!r}。"
            )
        if int(stable) < 0:
            ctx.fail(
                f"{where}.stableForMs 必须大于等于 0，"
                f"收到的是 {int(stable)}；0 表示条件一成立就算完成。"
            )
        normalized["stable_for_ms"] = int(stable)

    unknown_top = sorted(
        key for key in value if key not in ("any", "all", "stableForMs", "stable_for_ms")
    )
    if unknown_top:
        ctx.fail(
            f"{where} 含未知字段 "
            + "、".join(unknown_top)
            + "；只支持 any、all、stableForMs。"
        )
    return normalized or None


# ---------------------------------------------------------------------------
# 校验入口
# ---------------------------------------------------------------------------

# action 的两种写法（下划线与连字符）都接受，校验前先归一到 TOOL_ACTIONS 的写法。
_ACTION_ALIASES: dict[str, str] = {
    alias: canonical
    for actions in TOOL_ACTIONS.values()
    for canonical in actions
    for alias in (canonical.replace("-", "_"),)
    if alias != canonical
}

_VALIDATORS = {
    "bsk_session": _validate_session,
    "bsk_page": _validate_page,
    "bsk_inspect": _validate_inspect,
    "bsk_debug": _validate_debug,
    "bsk_interact": _validate_interact,
    "bsk_tabs": _validate_tabs,
    "bsk_assist": _validate_assist,
}

# debugAction 同样接受下划线写法（rule_add → rule-add 之类的反向别名）。
_DEBUG_ACTION_ALIASES: dict[str, str] = {
    action.replace("-", "_"): action for action in DEBUG_ACTIONS
}


def validate(tool_name: str, args: dict) -> dict:
    """校验并归一化某个工具的参数。

    Args:
        tool_name: 七个工具名之一。
        args: 已经过 :func:`normalize_args` 的参数（必须含 ``action``）。

    Returns:
        可直接交给 service 的 dict：键是 snake_case，值是校验过的正确类型。
        ``action`` 已归一成 :data:`TOOL_ACTIONS` 里的写法（``scroll_to`` → ``scroll-to``）。

    Raises:
        BskToolError: 工具名未知 / action 缺失或非法 / 必填缺失 / 类型错误 /
            范围越界 / 互斥冲突 / 条件规则违反。消息是中文，且说明该怎么改。

    Note:
        纯函数：不改 ``args``、不写全局状态，同样输入永远得到同样结果。
    """
    if tool_name not in TOOL_SCHEMAS:
        raise BskToolError(
            f"未知工具 {tool_name!r}；可用的工具是 "
            + "、".join(TOOL_SCHEMAS)
            + "。"
        )
    if not isinstance(args, dict):
        raise BskToolError(
            f"{tool_name} 的参数必须是对象，收到的是 {type(args).__name__}（{args!r}）。"
        )

    # ``debug_action`` 是拆分前 ``bsk_inspect(action="debug")` 的旧写法，在
    # ``bsk_debug`` 上保留为 ``action`` 的别名；同样接受下划线写法。
    # 先归一，后续的条件规则才敢直接和 DEBUG_ACTIONS 里的字面值比。
    if tool_name == "bsk_debug":
        raw_debug = args.get("debug_action")
        if isinstance(raw_debug, str):
            raw_debug = raw_debug.strip().lower().replace("-", "_")
            if raw_debug in _DEBUG_ACTION_ALIASES:
                args = {**args, "debug_action": _DEBUG_ACTION_ALIASES[raw_debug]}

    ctx = _Ctx(tool_name, args)

    if not ctx.action:
        raise BskToolError(
            f"{tool_name} 缺少必填参数 action；可选值："
            + " / ".join(TOOL_ACTIONS[tool_name])
            + "。"
        )
    if ctx.action == "__model__":
        raise BskToolError(
            f"{tool_name} 的 action 不能是 __model__（那只是提示符，不是可执行操作）；"
            "可选值：" + " / ".join(TOOL_ACTIONS[tool_name]) + "。"
        )

    canonical = _ACTION_ALIASES.get(ctx.action, ctx.action)
    if canonical not in TOOL_ACTIONS[tool_name]:
        # 旧写法 ``bsk_inspect(action="debug")`` 单独给一条指向新工具的提示。
        # 泛泛地说"取值不对"会让模型以为名字写错了，然后反复重试同一个工具；
        # 明确告诉它"调试已经搬到 bsk_debug"，它下一次调用就能成功。
        if canonical == "debug":
            raise BskToolError(
                f"{tool_name} 的 action 不再接受 debug：调试已拆成独立的 bsk_debug 工具，"
                "请改用 bsk_debug（把 action 设为原来的 debug_action 取值，例如 "
                "bsk_debug 的 action=\"capabilities\"）。"
            )
        raise BskToolError(
            f"{tool_name} 的 action 必须是 "
            + "/".join(TOOL_ACTIONS[tool_name])
            + f" 之一，收到的是 {ctx.action!r}；请从这些取值里挑一个。"
        )
    # 必须在分派**之前**写回规范写法：``scroll_to`` 若原样带进
    # _validate_interact，`action == "scroll_to"` 永远不成立，会一路掉到
    # 最后的 press 分支，报出「缺少必填参数 key」这种误导性错误。
    ctx.action = canonical
    ctx.known = set(TOOL_SCHEMAS[tool_name]["properties"])

    checked = _VALIDATORS[tool_name](ctx)
    if checked is None:  # pragma: no cover - 每个校验器都返回 dict
        checked = {}

    # 只保留校验器明确给出的键（None 表示「没给」），其余一律不进结果 ——
    # service 侧不需要靠「键在不在」判断，但少一些噪音键更好读日志。
    result: dict[str, Any] = {"action": canonical}
    for key, value in checked.items():
        if value is None:
            continue
        result[key] = value

    # ``session`` 统一在这里补：5 个多动作工具的 schema 里都有它（对齐 DSH 的
    # 参数并集），而每个工具的校验器只关心自己的 action 专属参数，不该各自重复
    # 处理会话。早先没有这一步，模型显式指定的 session 被**静默丢弃** ——
    # 多会话场景下命令打在另一个会话上（与 tab_id 那一类缺陷同型）。
    #
    # ``bsk_session`` 不在此列：它的 session 有独立语义（"要停止哪个会话"），
    # 该校验器自己会给出，这里不覆盖它。
    if tool_name != "bsk_session" and "session" in TOOL_SCHEMAS[tool_name]["properties"]:
        session = ctx.as_str("session").strip()
        if session:
            result["session"] = session

    # ------------------------------------------------------------------
    # 通用守卫：模型给了值、却没有出现在结果里的参数 = 被静默丢弃。
    #
    # 为什么要有这一道：「schema 收下了、执行时丢了」这个缺陷在本项目里
    # 已经出现过**四次**（tab_id → timeout_ms → session → 跨 action 的键），
    # 每一次都是靠人工审阅或突变测试才发现的，而且每次都只修了当时那一个参数。
    # 逐个补是治标；这条守卫把整类问题一次性堵住 —— 以后任何新增 action
    # 只要漏写回一个参数，这里立刻报错，而不是等下一轮审阅。
    #
    # 豁免清单只放**确实允许不写回**的键，且每个都要写明理由。
    # ------------------------------------------------------------------
    _DROP_OK = frozenset()
    dropped = sorted(
        key
        for key in ctx.args
        if key not in result
        and key not in _DROP_OK
        and ctx.given(key)
    )
    if dropped:
        raise BskToolError(
            f"{tool_name} 的 action={canonical} 收到了 "
            + "、".join(dropped)
            + "，但这一步用不上它们（照现在这样执行，它们会被忽略）。"
            "请去掉它们，或改用会用到这些参数的那个 action；"
            "如果你认为这是插件的缺陷，请把这个工具的调用参数告诉用户。"
        )
    return result
