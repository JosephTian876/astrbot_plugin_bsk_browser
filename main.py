"""astrbot_plugin_bsk_browser —— 框架适配层。

这是整个插件里唯一允许 import astrbot 的文件（其余逻辑全在 ``bsk/`` 包里，
可以脱离框架独立单测）。它只做四件事：

1. 持有 ``BskService``（业务编排）与配置；
2. 把 ``@filter.llm_tool`` 的调用参数解包后转给 service；
3. 做权限校验（默认仅管理员）；
4. 把图片结果 yield 给用户。

为什么不把逻辑写在这里：AstrBot 要求 ``@filter.llm_tool`` 装饰的函数必须定义
在插件主模块（``main.py``）里 —— 框架用 ``handler.__module__ == metadata.module_path``
精确匹配来决定是否给工具绑定 ``self`` 实例，定义在子模块里的工具会被框架
认为存在、却永远拿不到 self，调用时静默失败。所以这个约束无法绕过，
但可以通过"这里只做薄适配"来控制它的体积。

几个必须遵守的框架约束（均来自 AstrBot 4.28.1 源码实证）：

- 绝不定义 ``__del__``：``star_manager._terminate_plugin`` 是
  ``if "__del__" in cls.__dict__: ... elif "terminate" in cls.__dict__: ...``，
  定义了 ``__del__`` 会让 ``terminate()`` 永不执行，导致浏览器会话泄漏。
- ``terminate`` / ``initialize`` 必须定义在本类上（框架用 ``cls.__dict__`` 判断）。
- ``__init__`` 的 ``config`` 必须有默认值（框架在无配置时只传 ``context``）。
- docstring 的 ``Args:`` 段是工具参数 schema 的唯一来源，不读函数类型注解。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .bsk import tools as tools_mod
from .bsk.config import (
    Settings,
    parse_settings,
    read_framework_tool_timeout,
    validate_settings,
)
from .bsk.errors import BskError
from .bsk.models import ConsoleLog
from .bsk.service import BskService
from .bsk.tools import BskToolError

__all__ = ["BskBrowserPlugin"]

# 插件自己的名字。用于解析插件数据目录，必须与 metadata.yaml 的 name 一致。
# 显式写死是刻意的：``StarTools.get_data_dir()`` 不带名字时会去猜调用方模块，
# 那在插件加载期并不可靠（猜不到就抛 RuntimeError），而这里的名字本来就是
# 确定的常量。
PLUGIN_NAME = "astrbot_plugin_bsk_browser"

# 后台空闲回收的检查间隔（秒）。
# 不需要很频繁：会话空闲阈值是分钟级的，30 秒粒度足够，且开销可忽略。
IDLE_REAP_INTERVAL_SEC = 30.0

# bsk_logs 一次最多展示多少条。输出长度必须可控（与本插件 max_page_chars
# 同一个先例）：每条还可能带 200 字符的 URL 与 300 字符的正文。
LOGS_RENDER_LIMIT = 50

# 两种日志的中文名，用于拼给模型的文案。
LOGS_LABELS: dict[str, str] = {
    "console": "控制台消息",
    "network": "网络请求",
}

# 8 个旧工具的名字，按用途分成两组。``legacy_tools=false`` 时两组一起停用
# （见 ``_deactivate_legacy_tools``）；``legacy_tools=true`` 且
# ``legacy_fringe_tools=false`` 时只停用 fringe 那一组。
#
# 分组依据是"有没有等价的新工具"，以及"日常用不用得上"：
#
# - core：常用且**没有**等价替代的 4 个。``bsk_status`` 是本插件唯一的诊断入口；
#   ``bsk_close`` 关会话；``bsk_screenshot`` 是**唯一能把图片直接发给用户**的截图
#   （``bsk_inspect`` 的 screenshot 返回的是文件路径，模型看不到图）；
#   ``bsk_logs`` 读控制台/网络日志。
# - fringe：都有等价新工具，只是名字不同（``bsk_open`` → ``bsk_page``/``bsk_session``、
#   ``bsk_read`` → ``bsk_inspect``、``bsk_act`` → ``bsk_interact``），所以默认不注册，
#   省下约 1818 字符的固定 prompt 开销；仍在用旧名字的用户可以打开配置项找回它们。
#
# ``bsk_evaluate`` 在两个元组的**并集**里，但停用时会被跳过：它是**独立的高危开关**
# （``enable_evaluate``），把它绑到 ``legacy_tools``/``legacy_fringe_tools`` 上会让
# 用户误以为"关掉旧工具"就等于"关掉执行脚本"，而实际上它是被另一项控制的。
LEGACY_CORE_TOOL_NAMES: tuple[str, ...] = (
    "bsk_screenshot",
    "bsk_close",
    "bsk_status",
    "bsk_logs",
)

LEGACY_FRINGE_TOOL_NAMES: tuple[str, ...] = (
    "bsk_open",
    "bsk_read",
    "bsk_act",
)

LEGACY_TOOL_NAMES: tuple[str, ...] = (
    "bsk_open",
    "bsk_read",
    "bsk_act",
    "bsk_screenshot",
    "bsk_close",
    "bsk_status",
    "bsk_evaluate",
    "bsk_logs",
)
"""8 个旧工具的完整名单（= core + fringe + ``bsk_evaluate``），顺序与定义先后一致。

保留它是为了不破坏既有引用（文档、其它检查脚本按这 8 个名字核对注册数）。
运行时真正用来决定停用哪一组的是 :data:`LEGACY_CORE_TOOL_NAMES` 与
:data:`LEGACY_FRINGE_TOOL_NAMES`。
"""

# 7 个新工具的名字（schema 来自 ``bsk/tools.py`` 的 ``TOOL_SCHEMAS``）。
#
# ``bsk_debug`` 是从 ``bsk_inspect`` 里拆出来的：调试参数有 24 个、约 3652 字符，
# 而日常读页面（observe/snapshot/html/screenshot/console/network）用不到它们。
# 拆开后每轮对话只为用得上的那一半付 token。
NEW_TOOL_NAMES: tuple[str, ...] = (
    "bsk_session",
    "bsk_page",
    "bsk_inspect",
    "bsk_debug",
    "bsk_interact",
    "bsk_tabs",
    "bsk_assist",
)

# 失败回执的中文主语：{工具名: 动词短语}。用于 ``_dispatch`` 拼
# "{X}失败：{原因}"。刻意不写工具名本身（"bsk_page 失败"对模型没有信息量，
# 它已经知道自己调了哪个工具），而是写这个工具**在做的事**。
_ACTION_LABEL: dict[str, str] = {
    "bsk_session": "管理浏览器会话",
    "bsk_page": "页面导航",
    "bsk_inspect": "读取页面",
    "bsk_debug": "调试与流量控制",
    "bsk_interact": "页面交互",
    "bsk_tabs": "管理标签页",
    "bsk_assist": "窗口与设备设置",
}

# JSON Schema 属性允许的 ``type`` 取值 —— 与 AstrBot 的
# ``func_tool_manager.SUPPORTED_TYPES`` 一字不差。``integer`` **刻意不在**
# 这个集合里（AstrBot 不认它，本插件的整数语义参数一律声明成 ``number``）。
#
# 注意：写 ``integer`` 并不会让 ``FunctionTool(...)`` 抛异常（实测：
# jsonschema 的 meta-schema 允许它，只是 provider 侧未必认）—— 它是
# **本插件自己的**约定，属于"宁可在这里拦下"的那一类。
_SANE_TYPES: frozenset[str] = frozenset(
    {"string", "number", "object", "array", "boolean"}
)


def _schema_is_sane(schema: Any) -> bool:
    """跑一遍轻量的 JSON Schema 合法性预检。

    为什么必须有这道检查（第二轮审阅实测的坑）：``tool.parameters = {...}``
    这个赋值**不触发** pydantic 校验（校验器是 ``model_validator(mode="after")``，
    只在构造 ``FunctionTool(...)`` 时跑）。非法 schema 要到**下一次**
    ``get_full_tool_set()`` 里 ``FunctionTool(...)`` 才炸，而那条路径被
    ``internal.py`` 兜住并往聊天里发一句 "Error occurred while processing
    agent request: ..." —— 也就是说**一条 schema 笔误会让机器人对每条普通
    消息都报错**，远不止影响浏览器工具。

    所以覆写前先在这里拦一道：不合法就跳过覆写、保留 ``@filter.llm_tool``
    由 docstring 推出的基础 schema（那个永远是合法的），再打一条 warning。
    代价是那个工具的 ``action`` 枚举没写进去（模型只能靠描述文字理解），
    但这远比整个机器人每条消息都报错轻。

    检查项是**实测出来的**（逐个用 ``FunctionTool(...)`` 构造验证过），
    不是照着 spec 猜的 —— 见下面的 Note。刻意**不引入 jsonschema 依赖**：
    它是第三方库，本插件要求纯标准库。

    Args:
        schema: 待检查的 schema（``bsk/tools.py`` 的 ``TOOL_SCHEMAS`` 值）。

    Returns:
        ``True`` 表示可以安全覆写；``False`` 表示应跳过并告警。

    Note:
        实测（AstrBot 4.28.1）**会**让 ``FunctionTool(...)`` 抛
        ``ValidationError`` 的：``properties`` 不是对象、``properties`` 的值
        不是对象、``required`` 不是数组、``required`` 的元素不是字符串、
        ``enum`` 不是数组、``description`` 不是字符串、``items`` 不是对象、
        以及**嵌套**结构里的同类错误、``type`` 写了不认识的值（如 ``strng``）。

        实测**不会**抛的（所以不要为它们拒绝整条 schema）：``integer``、
        顶层 ``type`` 不是 object、``properties`` 为空、``required`` 引用了
        不存在的键、``enum`` 是空数组、属性写成 ``true``。

        也就是说本函数比"能拦住崩溃"更严一点：它额外拒绝 ``integer``
        与空的 ``properties``（前者是本插件的硬约束 H1，后者会让模型收到
        一个没有参数的工具）。**严一点是有意的** —— 误拒的代价只是那个工具
        退回基础 schema，漏放的代价是机器人对每条消息报错。
    """
    if not isinstance(schema, dict):
        return False
    if schema.get("type") != "object":
        return False
    if not _properties_are_sane(schema.get("properties"), require_non_empty=True):
        return False

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            return False
        if any(not isinstance(item, str) for item in required):
            return False
        if any(item not in schema["properties"] for item in required):
            return False
    return True


def _properties_are_sane(properties: Any, *, require_non_empty: bool) -> bool:
    """递归检查一层 ``properties`` 及其子结构。

    递归是必要的：``completion_criteria`` 的 ``any``/``all`` 里还嵌着一层
    ``properties``，而嵌套层的错误同样会让 ``FunctionTool(...)`` 构造失败
    （实测确认），只查顶层等于漏掉一半。
    """
    if not isinstance(properties, dict):
        return False
    if require_non_empty and not properties:
        return False

    for name, prop in properties.items():
        if not isinstance(name, str) or not name:
            return False
        if not _property_is_sane(prop):
            return False
    return True


def _property_is_sane(prop: Any) -> bool:
    """检查单个属性定义（含它的 ``items`` / 嵌套 ``properties``）。"""
    if not isinstance(prop, dict):
        return False
    if prop.get("type") not in _SANE_TYPES:
        return False

    description = prop.get("description")
    if description is not None and not isinstance(description, str):
        return False

    enum = prop.get("enum")
    if enum is not None and not isinstance(enum, list):
        return False

    items = prop.get("items")
    if items is not None:
        if not isinstance(items, dict):
            return False
        items_type = items.get("type")
        if items_type is not None and items_type not in _SANE_TYPES:
            return False
        # items 里也可以再嵌 properties（数组套对象）。
        if "properties" in items and not _properties_are_sane(
            items.get("properties"), require_non_empty=False
        ):
            return False

    if "properties" in prop and not _properties_are_sane(
        prop.get("properties"), require_non_empty=False
    ):
        return False
    return True


def _coerce_since(value: Any) -> int:
    """把模型传来的 ``since`` 安全地转成非负整数游标。

    ``since`` 在 docstring 里声明成 ``number``（AstrBot 会按 JSON number
    生成 schema），模型可能传 ``3``、``3.0``、``"3"`` 甚至 ``None``。
    这里一律收敛成 ``int``：负数按 0 处理（``--since -1`` 不是有意义的游标），
    实在转不动就退回 0（等于"从头读"），绝不让一个坏参数把工具打崩。
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _next_since(cursor: int, log: ConsoleLog) -> int:
    """算出下次该传的 ``since``。

    ``--since N`` 是开区间（bsk 帮助原文是 "Return entries with sequence
    greater than this cursor"，已实测确认），所以游标只需要追上"已经看到过的
    最大序号"即可。本机实测两次：``console`` 返回 1 条（sequence=1）时
    ``next_since`` 是 1；``network`` 返回 4 条（最大 sequence=4）时
    ``next_since`` 是 4 —— 即 ``next_since`` 就等于已返回的最大序号。

    因此这里不能写成"最大序号 + 1"：那会跳过还没产生、序号正好等于
    最大序号 + 1 的下一条日志（例如已看到 4，下一条新日志是 5，
    传 since=5 只返回 > 5 的内容，第 5 条就再也读不到了）。

    也不能盲信 bsk 的回值：实测把游标传得比现有日志还大时会回
    ``next_since: 0``，原样交给模型会让它下次传 0、把看过的日志重读一遍。
    所以取三者最大：调用方传进来的游标、bsk 的回值、本次见到的最大序号。
    三者都只会让游标前进，不会回退。
    """
    cursor_next = max(cursor, int(getattr(log, "next_since", 0) or 0))
    if log.entries:
        highest = max(int(getattr(e, "sequence", 0) or 0) for e in log.entries)
        cursor_next = max(cursor_next, highest)
    return cursor_next


# 这里刻意不加 ``@register(...)`` 装饰器。
#
# AstrBot 的 ``register_star`` 自 v3.5.19 起已废弃（框架源码里标着 [DEPRECATED]），
# 它注册的那 4 个位置参数（name/author/desc/version）在运行时**全部不生效**：
# ``Star.__init_subclass__`` 会自动把继承 Star 的子类登记进 star_map，而加载时
# ``star_manager`` 会用 metadata.yaml 的值逐字段覆盖（源码注释原文「yaml 文件的
# 元数据优先」），再用 ``plugin_id.split("/")`` 按市场身份覆盖 author/name。
#
# 所以唯一事实来源是 ``metadata.yaml``，写在这里的任何副本都只会随时间腐化
# （曾出现作者仍是占位符 "yourname"、版本停在 0.1.0、描述漏掉 4 个工具）。
# 本插件声明 ``astrbot_version: ">=4.16"``，远高于该装饰器废弃的 v3.5.19，
# 删除它不影响任何受支持的 AstrBot 版本。
class BskBrowserPlugin(Star):
    """把本机 bsk CLI 包装成 LLM 可调用的浏览器操作能力。

    设计要点：

    - 权限默认仅管理员（``admin_only``，可在插件配置里关闭）；
    - 按聊天会话隔离浏览器会话（``session_scope``：一个群一条 / 每人一条）；
    - 所有 bsk 细节都在 ``bsk/`` 包里，这里只做参数解包与结果包装。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)

        # 先解析插件数据目录：journal 与截图的默认落点都基于它
        # （持久化数据必须落在 data/plugin_data/<插件名> 下，见 bsk/paths.py）。
        # 这一步永不抛异常：拿不到就返回空串，由 bsk/paths.py 降级到系统临时目录。
        data_dir = self._resolve_data_dir()
        self.settings: Settings = parse_settings(config, data_dir=data_dir)

        # 读出框架自己的单次工具调用上限（AstrBot 主配置里的 tool_call_timeout），
        # 交给 bsk/ 层做超时钳制。这个读取永不抛异常（读不到就是 None），
        # 因为它在插件加载路径上 —— 拿不到这个值最多只是少一层保护，
        # 绝不该让插件起不来（见 bsk.config.read_framework_tool_timeout）。
        self.framework_tool_timeout: float | None = self._read_framework_timeout()

        # logger 以参数注入给 bsk/ 层：那边不 import astrbot（分层约束），
        # 也不 import 内置 logging（审核要求），只认 bsk/logger.py 的接口。
        # astrbot_logger 是框架的插件 logger 代理，会按调用方模块名路由到
        # 本插件专属的 logger 上，所以 bsk/ 各处打出来的日志归属仍然正确。
        self.service = BskService(
            self.settings,
            framework_tool_timeout=self.framework_tool_timeout,
            logger=astrbot_logger,
        )

        self._reap_task: asyncio.Task[None] | None = None
        self._closed = False

    def _resolve_data_dir(self) -> str:
        """解析插件数据目录（``data/plugin_data/astrbot_plugin_bsk_browser``）。

        三步降级，每一步失败都只记 warning：

        1. ``StarTools.get_data_dir("astrbot_plugin_bsk_browser")`` —— 显式传插件名，
            不依赖调用栈推断（``get_data_dir()`` 不带名字时会去猜调用方模块，
            在插件加载期并不可靠，且失败抛的是 ``RuntimeError``）；
        2. 退回 ``get_astrbot_data_path()/plugin_data/<插件名>`` —— 与第 1 步
            同一套目录约定，只是不代为创建；
        3. 再失败 → 返回空串，由 ``bsk/paths.py`` 走系统临时目录降级。

        Returns:
            可用的插件数据目录（字符串）；全部失败时返回空串。

        Note:
            本方法**绝不抛异常**，也绝不返回 ``None``：它在插件加载路径上，
            一个异常就是插件整个加载失败，而"数据目录拿不到"最多只是让 journal
            与截图落到临时目录。这里也不做"能不能写"的判断 —— 那由
            ``bsk/paths.py`` 在真正要用的时候做（它会 mkdir 并检查可写性，
            同样保证不抛异常），判断逻辑只有一处。
        """
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            if data_dir:
                return str(data_dir)
            astrbot_logger.warning(
                "[bsk_browser] 插件数据目录解析结果为空，改用降级路径。"
            )
        except Exception as exc:  # noqa: BLE001 - 拿不到数据目录不该让插件起不来
            astrbot_logger.warning(
                "[bsk_browser] 获取插件数据目录失败（%r），尝试用 AstrBot 数据路径拼接。",
                exc,
            )

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return str(Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.warning(
                "[bsk_browser] 拼接插件数据目录也失败（%r），"
                "会话记录与截图将退到系统临时目录。",
                exc,
            )
        return ""

    def _read_framework_timeout(self) -> float | None:
        """从 ``context.get_config()`` 里读出框架的工具调用超时。

        路径：``agent_runner`` → ``config`` → ``misc`` → ``tool_call_timeout``
        （已用真实配置文件确认过；本机取到 120）。解析逻辑在 ``bsk/config.py``
        里（那里不能 import astrbot，所以只接收这个鸭子类型对象）。

        Returns:
            正的浮点秒数；配置拿不到、结构不认识、值非法时一律返回 ``None``
            （表示"未知"，插件会保持原有超时行为，不做任何钳制）。
        """
        try:
            getter = getattr(self.context, "get_config", None)
            if not callable(getter):
                return None
            config_obj = getter()
        except Exception as exc:  # noqa: BLE001 - 读不到就是未知，不影响加载
            astrbot_logger.debug("读取 AstrBot 主配置失败（按未知处理）：%r", exc)
            return None
        return read_framework_tool_timeout(config_obj)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件加载后调用：提示配置问题，并启动空闲会话回收任务。"""
        # 配置问题只提示、不阻断 —— 用户可能还没装 bsk，不该因此让插件加载失败。
        for problem in validate_settings(self.settings):
            astrbot_logger.warning("[bsk_browser] 配置提醒：%s", problem)

        # 整页截图的超时预算顶到框架上限时才提示（默认组合：两边都是 120 秒，
        # 所以本机必然出现这条）。这是正确的，它如实说明"预算会被钳到 115 秒"。
        # 文案刻意写成解释而非报错（措辞由 service.startup_warnings 负责）：
        # 默认值对实测的 11.7 秒已有约 10 倍余量，绝大多数用户什么都不用做。
        # 框架上限读不到、或大于预算时一条都不打印，不制造无谓的担心。
        for warning in self.service.startup_warnings():
            astrbot_logger.warning("[bsk_browser] %s", warning)

        if not self.settings.enabled:
            astrbot_logger.info("[bsk_browser] 插件已在配置中停用，不会注册任何浏览器操作。")
            return

        if not self.settings.admin_only:
            astrbot_logger.warning(
                "[bsk_browser] admin_only = false：任何能与机器人对话的人都能操控"
                "你已登录的浏览器。请确认这是你想要的。"
            )

        if self.settings.idle_release_sec > 0:
            self._reap_task = asyncio.create_task(self._idle_reaper())

        # 清理上一次进程遗留的会话。AstrBot 被强杀（任务管理器结束进程、崩溃、
        # 断电）时 terminate() 不会执行，而 bsk daemon 的生命周期独立于 AstrBot
        # —— 它和那些会话都还活着，用户桌面上就留着没人管的浏览器窗口。
        # 这里按 journal 记录把"仍然是自己的"那些停掉（只用精确 id，绝不用
        # `session stop --all`，那会误停别的程序创建的会话）。
        # 放在启动回收任务之后：这一步可能要等 bsk 子进程，不该拖慢回收任务的建立。
        try:
            recovered = await self.service.recover_orphans()
            if recovered:
                astrbot_logger.info(
                    "[bsk_browser] 已清理 %d 个上次遗留的浏览器会话。", recovered
                )
        except Exception as exc:  # noqa: BLE001 - 恢复失败不能让插件加载失败
            astrbot_logger.warning("[bsk_browser] 清理遗留会话时出错（已忽略）：%r", exc)

        # 6 个新工具的完整 schema 由 bsk/tools.py 提供。@filter.llm_tool 在
        # import 期只给出由 docstring 推出的基础 schema（**表达不了 action 的
        # enum**），所以这里覆写成完整版。
        #
        # 为什么必须写在 initialize() 里（而不是 import 期做一次）：插件重载会
        # 把 FuncTool 整个重建（star_manager 先 remove_func 再走 spec_to_func），
        # import 期写的补丁会随旧对象一起丢掉。initialize() 是**每次加载都会
        # 执行**的那个点。覆写本身是幂等的：每次都是整体替换，不会累积。
        # 这也必须早于任何一次 get_full_tool_set() —— _PermissionGuardedTool
        # 在构造时对 parameters 做的是**快照**，不是实时视图。
        self._apply_tool_schemas()

        # 旧工具分两级开关：
        #
        # - ``legacy_tools=false``：8 个旧工具全部停用（只剩 6 个新工具 +
        #   ``bsk_evaluate`` 若它自己的开关也开着）。默认 true：老用户升级无感
        #   —— 若默认 false，等于**静默删掉** 8 个工具。
        # - ``legacy_tools=true`` 且 ``legacy_fringe_tools=false``（后者的默认值）：
        #   只停用有等价新工具的那 3 个（``bsk_open``/``bsk_read``/``bsk_act``），
        #   常用且无替代品的 4 个继续注册。这样用户不必"全开或全关"。
        #
        # ``bsk_evaluate`` 不进任何一组，它只由 ``enable_evaluate`` 决定 —— 见
        # ``_deactivate_legacy_tools`` 里的跳过分支。
        if not self.settings.legacy_tools:
            self._deactivate_legacy_tools()
        elif not self.settings.legacy_fringe_tools:
            self._deactivate_legacy_tools(fringe_only=True)

        astrbot_logger.info(
            "[bsk_browser] 已加载。bsk 路径=%s，最大会话=%d，仅管理员=%s",
            self.settings.bsk_path,
            self.settings.max_sessions,
            self.settings.admin_only,
        )

    # ------------------------------------------------------------------
    # 工具注册后的接线（schema 覆写 + 旧工具开关）
    # ------------------------------------------------------------------

    def _apply_tool_schemas(self) -> None:
        """把 ``bsk/tools.py`` 的完整 schema 覆写到已注册的 6 个新工具上。

        覆写而不是重新注册：``@filter.llm_tool`` 已经完成了 handler 注册与
        插件实例绑定（框架用 ``handler.__module__ == metadata.module_path``
        决定要不要绑 ``self``），重新注册会丢掉这层绑定。直接改
        ``tool.parameters`` 不影响绑定（已实测）。

        每个工具在覆写前都要过 :func:`_schema_is_sane`：非法 schema 不会在
        赋值时被发现，而是在下一次 ``get_full_tool_set()`` 构造
        ``FunctionTool`` 时才抛，那条路径会让机器人**对每条普通消息**都报错。
        不合法就跳过这个工具（保留 docstring 推出的基础 schema）并告警。
        """
        import copy

        from astrbot.core.provider.register import llm_tools

        for name, schema in tools_mod.TOOL_SCHEMAS.items():
            if not _schema_is_sane(schema):
                astrbot_logger.warning(
                    "[bsk_browser] 工具 %s 的 schema 未通过合法性预检，"
                    "已跳过覆写并保留由 docstring 推出的基础 schema。"
                    "这几乎肯定是 bsk/tools.py 里的笔误 —— 请检查它的 "
                    "type / properties / required / enum / description / items"
                    "（嵌套层同样检查）。",
                    name,
                )
                continue

            tool = llm_tools.get_func(name)
            if tool is None:
                astrbot_logger.warning(
                    "[bsk_browser] 工具 %s 未注册，schema 覆写跳过。", name
                )
                continue

            tool.parameters = copy.deepcopy(schema)

            # 描述也一并覆写：`bsk/tools.py` 是工具规格的唯一事实源，
            # 描述当然也该来自那里（它被写在模块头声明为事实源的一部分）。
            # 不覆写的话，模型看到的是 docstring，而 docstring 是给人读的
            # 长文（含换行、含 Markdown 加粗），既不与描述表同步、
            # 又比描述更长 —— 每轮对话都要为这点差异多付 token。
            #
            # 缺失时**保留 docstring 的描述**，绝不把 description 清空：
            # 描述为空会让模型完全不知道该工具做什么，比描述略长严重得多。
            description = tools_mod.TOOL_DESCRIPTIONS.get(name)
            if description:
                tool.description = description

            # 运行时探针：覆写后回读一次，确认 enum 真的写进去了。
            # 这是防"框架换了实现、赋值变成只读或被重建"这类静默失效 ——
            # 那种情况下工具仍然可用（描述文字里有 action 列表），
            # 但模型少了枚举约束，值得留一条日志。
            got = (
                (tool.parameters or {})
                .get("properties", {})
                .get("action", {})
                .get("enum")
            )
            if not got:
                astrbot_logger.warning(
                    "[bsk_browser] 工具 %s 的 action 枚举未能写入 schema"
                    "（AstrBot 版本可能已变更），模型仍可通过描述文字使用该工具。",
                    name,
                )

    def _deactivate_legacy_tools(self, *, fringe_only: bool = False) -> None:
        """停用旧工具（``bsk_evaluate`` 除外，它有自己的开关）。

        Args:
            fringe_only: ``False``（默认）停用**全部** 8 个旧工具
                （``legacy_tools=false`` 走这条）；``True`` 只停用 fringe 那一组
                的 3 个（``legacy_tools=true`` 且 ``legacy_fringe_tools=false``
                走这条），core 那 4 个保持注册。

        为什么直接设 ``tool.active = False`` 而不是调框架的
        ``deactivate_llm_tool_async``：后者会把这个名字写进**持久化**的
        ``inactivated_llm_tools``（全局 SharedPreferences）。用户只是临时关掉
        ``legacy_tools`` 试试，却会在全局配置里留下永久痕迹，卸载插件也带不走
        ——下次装回来那 8 个工具还是关着的。直接设 ``active`` 只影响本次进程，
        随插件重载自然恢复。

        Note:
            两个已知的边界，都已在 ``_conf_schema.json`` 的 hint 里如实写明：

            1. 用户若在 AstrBot WebUI 的「扩展 → 组件」里手动关过某个旧工具，
               ``star_manager`` 每次加载都会重算
               ``ft.active = not plugin_disabled and ft.name not in inactivated_llm_tools``
               —— 那是在本方法**之后**跑的，所以那一项始终优先；
            2. 本方法只在插件加载时执行一次，改配置需要重新加载插件。
        """
        from astrbot.core.provider.register import llm_tools

        names = LEGACY_FRINGE_TOOL_NAMES if fringe_only else LEGACY_TOOL_NAMES
        for name in names:
            if name == "bsk_evaluate":
                # 它有自己的开关（enable_evaluate）且是独立的高危工具，
                # 不该跟着"旧工具"一起被关掉，见 LEGACY_TOOL_NAMES 的注释。
                continue
            tool = llm_tools.get_func(name)
            if tool is not None:
                tool.active = False

    async def terminate(self) -> None:
        """插件卸载/停用时调用：关闭所有浏览器会话。

        ⚠️ 这个方法必须能跑完 —— 如果不关会话，用户桌面上会留下无人管的
        浏览器窗口，而且借用的标签页不会归还。所以整个函数体是"尽力而为 +
        不抛异常"的：任何一步失败都只记日志。
        """
        self._closed = True

        if self._reap_task is not None:
            self._reap_task.cancel()
            try:
                await self._reap_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reap_task = None

        try:
            closed = await self.service.shutdown()
            astrbot_logger.info("[bsk_browser] 已关闭 %d 个浏览器会话。", closed)
        except Exception as exc:  # noqa: BLE001 - terminate 绝不能被异常打断
            astrbot_logger.warning("[bsk_browser] 关闭浏览器会话时出错（已忽略）：%r", exc)

    async def _idle_reaper(self) -> None:
        """定期回收空闲过久的浏览器会话。

        为什么要自己回收：bsk 自己会在会话空闲 5 分钟后回收，但回收不保证
        归还借用的标签页。我们提前一点主动 stop，标签页归还有机会走完整流程。

        这个任务必须永不抛异常，否则会静默死掉、之后再没人回收会话。
        """
        while not self._closed:
            try:
                await asyncio.sleep(IDLE_REAP_INTERVAL_SEC)
                if self._closed:
                    return
                reaped = await self.service.sessions.reap_idle()
                if reaped:
                    astrbot_logger.info("[bsk_browser] 回收了 %d 个空闲浏览器会话。", reaped)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                astrbot_logger.debug("[bsk_browser] 空闲回收出错（忽略）：%r", exc)

    # ------------------------------------------------------------------
    # 权限与会话键
    # ------------------------------------------------------------------

    def _denied(self, event: AstrMessageEvent) -> str | None:
        """权限校验。返回错误文案表示拒绝，返回 None 表示放行。

        两套机制取严：

        1. 插件自己的 ``admin_only`` / ``allowed_users``（在插件配置页设置）；
        2. AstrBot 原生的 ``tool_permissions``（WebUI → 扩展 → 组件）。

        第 2 条由框架在调用工具前自动执行，这里不重复实现；这里只管第 1 条。
        """
        if not self.settings.enabled:
            return "浏览器操作已在插件配置中停用。"

        sender = ""
        try:
            sender = str(event.get_sender_id())
        except Exception:  # noqa: BLE001 - 拿不到发送者时按最严格处理
            sender = ""

        # 白名单优先级最高：配了白名单就只认白名单里的人。
        allowed = self.settings.allowed_users
        if allowed:
            if sender and sender in allowed:
                return None
            is_admin = self._safe_is_admin(event)
            if is_admin:
                return None
            return (
                "你没有使用浏览器操作的权限。"
                "（该插件配置了用户白名单，请联系管理员把你的 ID 加进去。）"
            )

        if self.settings.admin_only and not self._safe_is_admin(event):
            return (
                "浏览器操作仅限管理员使用。"
                f"你的 ID 是 {sender or '未知'}，可以请管理员在插件配置里"
                "把「仅管理员可用」关掉，或把你的 ID 加进白名单。"
            )
        return None

    @staticmethod
    def _safe_is_admin(event: AstrMessageEvent) -> bool:
        """调用 ``event.is_admin()``，任何异常都当作"不是管理员"。

        偏保守：权限判断出错时应当拒绝，而不是放行。
        """
        try:
            return bool(event.is_admin())
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.debug("is_admin() 调用失败，按非管理员处理：%r", exc)
            return False

    def _evaluate_denied(self, event: AstrMessageEvent) -> str | None:
        """``bsk_evaluate`` 专用的权限门。返回文案=拒绝，返回 None=放行。

        为什么不能复用 ``_denied()``：那个门表达的是"能不能操作浏览器"，
        而执行任意 JavaScript 是另一个量级的授权 —— 它能静默读页面数据、
        静默提交表单，绕开"用户看得见的动作"这层约束。所以它要过两道额外的关。

        判定顺序（顺序本身就是设计，不要调换）：

        1. 总开关：``enabled=False`` → 拒绝（与其它工具一致）。
        2. 独立开关：``enable_evaluate=False`` → 拒绝。默认就走这条路径。
           刻意不与 ``bsk_act`` 之类共用开关：升级插件不该凭空多出一个
           高危能力，用户必须显式去配置里打开它。
        3. 强制管理员：``evaluate_require_admin=True`` 且调用者不是管理员
           → 拒绝，即使 ``admin_only=False`` 也一样。
        4. 其余才交给 ``_denied()``，让白名单等既有规则继续生效。

        第 3 条为什么要盖过 ``admin_only=False``：
        ``admin_only=False`` 的语义是"我愿意把看得见的浏览器操作开放给
        其他人"（点按钮、填表单，用户都能在窗口里看着）。而执行脚本是静默的。
        如果让这个粗粒度的宽松开关顺手把最高危能力一起放开，就会出现一种
        极难察觉的权限放大：管理员只是想"让群里的人也能查网页"，结果同时
        交出了"在已登录页面里跑任意脚本"的能力。
        所以这里先判 ``evaluate_require_admin``，判完才轮到 ``_denied()``
        —— 而不是把两者写成一个条件。要开放就必须显式再关掉这一项。
        """
        if not self.settings.enabled:
            return "浏览器操作已在插件配置中停用。"

        # --- 第 1 道：独立开关（默认关闭）---
        if not self.settings.enable_evaluate:
            return (
                "执行 JavaScript 的能力默认关闭，当前未启用。"
                "（这是本插件风险最高的功能：它能在你已登录的页面里运行任意脚本，"
                "读取页面数据、带你的登录状态发请求，而且不会像点击那样显示在"
                "浏览器窗口里。如果确实需要，请让管理员在 AstrBot WebUI → 插件 → "
                "本插件 → 配置 里打开「允许执行 JavaScript（enable_evaluate）」。）"
            )

        # --- 第 2 道：强制管理员。放在 _denied() 之前，才能盖过 admin_only ---
        if self.settings.evaluate_require_admin and not self._safe_is_admin(event):
            sender = ""
            try:
                sender = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                sender = ""
            return (
                "执行 JavaScript 仅限 AstrBot 管理员使用，"
                "且这一限制不受「仅管理员可用」总开关与用户白名单影响。"
                f"你的 ID 是 {sender or '未知'}。"
                "（这是刻意设计的：执行脚本能静默读取页面数据、提交表单，"
                "风险远高于点击和输入，所以它比其他浏览器操作多一道独立的"
                "管理员要求。管理员确实想放开时，需要单独关闭配置里的"
                "「执行 JavaScript 仅限管理员」（evaluate_require_admin）。）"
            )

        # --- 第 3 道：其余规则（admin_only / allowed_users）继续生效 ---
        return self._denied(event)

    def _key(self, event: AstrMessageEvent) -> str:
        """计算浏览器会话的隔离键。

        ``umo`` 模式：一个聊天会话（群/私聊）共用一条浏览器会话。
        ``user`` 模式：群里每个人一条，互不干扰。

        取值做三级兜底，任何一级拿不到就退到下一级 —— 键算错会导致串会话，
        所以宁可退化成一个保守的常量，也不要抛异常。
        """
        umo = ""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:  # noqa: BLE001
            umo = ""

        if not umo:
            try:
                umo = str(event.get_session_id() or "")
            except Exception:  # noqa: BLE001
                umo = ""

        if not umo:
            try:
                umo = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                umo = ""

        if not umo:
            # 全都拿不到：所有请求共用一个会话。宁可共用也不要崩溃。
            umo = "default"

        if self.settings.session_scope == "user":
            try:
                sender = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                sender = ""
            if sender:
                return f"{umo}#u{sender}"
        return umo

    def _fail(self, message: str) -> str:
        """统一的失败回执（给模型看）。"""
        return message

    # ------------------------------------------------------------------
    # 七个新工具：统一入口 _dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, tool: str, event: AstrMessageEvent, raw: dict) -> str:
        """7 个新工具的统一入口：权限门 → 参数校验 → 分派 → 包装结果。

        这 7 个工具的全部 handler 都只是 ``return await self._dispatch(...)``
        —— 逻辑只有这一份。它们与旧工具共用同一套权限门（:meth:`_denied`，
        含 ``enabled`` / 白名单 / ``admin_only`` 三态）与同一个 ``service``。

        Args:
            tool: 7 个工具名之一（``bsk_session`` … ``bsk_assist``）。
            event: 消息事件，用于权限判定。
            raw: 框架按形参名注入的原始 kwargs（**未经任何处理**）。

        Returns:
            给模型看的字符串。所有异常都在这里被收敛成中文文案，
            绝不把异常抛进框架 —— 框架那层会把它渲染成
            "Tool execution error: ..." 这种对模型毫无用处的英文堆栈，
            还会把 ``event.send`` 里的错误提示发给用户。
        """
        # --- 第 1 步：权限门 ---
        # bsk_session 的 list 是纯本地只读（只列本插件自己建的会话），
        # 与 bsk_status 同一性质：它是用户"我这边还能用吗"的自助排查手段，
        # 拒掉它只会让人更没法自查。所以和 bsk_status 一样允许非管理员。
        # 注意先做一次**归一化**再判断 action：模型可能写 "List"、拼错大小写，
        # 也可能带上连字符写法，归一化后才是可以比较的形式。
        peek = tools_mod.normalize_args(raw)
        self_serve = tool == "bsk_session" and peek.get("action") == "list"
        if not self_serve:
            denied = self._denied(event)
            if denied:
                return self._fail(denied)
        elif not self.settings.enabled:
            # list 放行的是 admin_only / 白名单，不是总开关。
            return self._fail("浏览器操作已在插件配置中停用。")

        # --- 第 2 步：参数校验 ---
        # BskToolError 的 message 本身就是写好的中文提示（"哪里错了、
        # 应该怎么改"），直接回给模型，它照着改就能重试成功。
        try:
            args = tools_mod.validate(tool, peek)
        except BskToolError as exc:
            return self._fail(str(exc))

        # --- 第 3 步：分派 ---
        try:
            return await self._run_tool(tool, event, args)
        except BskError as exc:
            return self._fail(
                f"{_ACTION_LABEL.get(tool, tool)}失败：{exc.friendly}"
            )
        except Exception:  # noqa: BLE001 - 兜底，绝不让异常穿到框架
            astrbot_logger.exception("[bsk_browser] %s 未预期错误", tool)
            return self._fail(
                f"{_ACTION_LABEL.get(tool, tool)}时出现未预期的错误。"
                "这是插件内部的故障，请把这次调用的参数告诉用户，"
                "并建议他查看 AstrBot 日志。"
            )

    async def _run_tool(self, tool: str, event: AstrMessageEvent, args: dict) -> str:
        """把校验过的参数按工具名路由到 ``service`` 的对应方法并渲染结果。

        Args:
            tool: 7 个工具名之一。
            event: 用于算会话键。
            args: :func:`bsk.tools.validate` 的输出（已含规范化的 ``action``）。

        Returns:
            给模型看的中文回执。

        Note:
            这里**只做 action 分派 + 结果渲染**，一行 bsk 细节都不写 ——
            参数怎么变成 argv、超时怎么算、只读还是写操作，全在 ``bsk/service.py``
            里，那边是唯一实现（TOOL-SPEC H4：新工具必须复用既有实现）。
        """
        action = args["action"]
        # 会话键的解析顺序（与 DSH 的 registry.resolve(args.session) 一致）：
        # 1. 模型显式传了 session → 用它。service 的 _resolve 既认会话键也认
        #    session_id；认不出来会报"不属于本插件"，不会静默打到别的会话。
        # 2. 没传 → 用本对话的会话键（既有行为，向后兼容）。
        #
        # 为什么必须在这一层做：7 个工具的 schema 里都有 session 字段
        # （对齐 DSH 的参数并集），服务层每个方法的第一个参数也正是会话标识。
        # 早先只传 self._key(event)，于是模型显式指定的 session 被**静默丢弃**
        # —— 多会话场景下命令打在另一个会话上，模型无从察觉。
        #
        # bsk_session 的 start/stop 不受影响：它们走 _run_session 自己解析
        # session（那两处的 session 是"要操作哪个会话"的显式目标）。
        key = str(args.get("session") or "").strip() or self._key(event)

        if tool == "bsk_session":
            return await self._run_session(key, action, args)
        if tool == "bsk_page":
            return await self._run_page(key, action, args)
        if tool == "bsk_inspect":
            return await self._run_inspect(key, action, args)
        if tool == "bsk_debug":
            return await self._run_debug(key, args)
        if tool == "bsk_interact":
            return await self._run_interact(key, action, args)
        if tool == "bsk_tabs":
            return await self._run_tabs(key, action, args)
        return await self._run_assist(key, action, args)

    # --- bsk_session ---------------------------------------------------

    async def _run_session(self, key: str, action: str, args: dict) -> str:
        """bsk_session 的 start / stop / list 三个分支。"""
        if action == "start":
            info = await self.service.start_session(
                url=args.get("url", ""),
                width=args.get("width"),
                height=args.get("height"),
                no_focus=args.get("no_focus", False),
                browser=args.get("browser", ""),
                device=args.get("device", ""),
                key=key,
            )
            lines = [f"已启动浏览器会话：{info.get('session_id') or '(未返回 ID)'}"]
            if info.get("url"):
                lines.append(f"已打开：{info['url']}")
            if info.get("browser_instance_id"):
                lines.append(f"浏览器实例：{info['browser_instance_id']}")
            if info.get("device"):
                lines.append(f"设备模拟：{info['device']}")
            # 如实说明 no_focus 当前无效果 —— 裁决 6：本插件**始终**在后台
            # 打开会话，模型传 false 不会生效。不说明的话它会以为自己控制住了，
            # 于是下次遇到"窗口抢焦点"的问题还会继续传这个参数。
            lines.append(
                "浏览器窗口是在后台打开的（本插件始终不抢焦点，"
                "no_focus 这一项目前无效果）。"
            )
            lines.append("接下来可以直接用 bsk_page / bsk_inspect 等工具操作这一页。")
            return "\n".join(lines)

        if action == "stop":
            info = await self.service.stop_session(args.get("session", ""))
            if info.get("stopped"):
                return (
                    f"已停止浏览器会话 {info.get('session_id') or ''}。".rstrip()
                    + "（借用的标签页会一并归还。）"
                )
            return "这条会话此前已经不在运行了，没有需要停止的东西。"

        # list
        return self._render_sessions(self.service.list_sessions())

    @staticmethod
    def _render_sessions(info: dict) -> str:
        """把 ``service.list_sessions()`` 渲染成给模型看的中文。

        ``list`` 走的是**本地注册表**（不访问 daemon），所以它列出的是本插件
        自己建的会话，字段与 ``bsk_status`` 的诊断信息不是一回事。
        """
        sessions = info.get("sessions") or []
        if not sessions:
            return (
                "本插件当前没有任何浏览器会话。\n"
                "第一次操作浏览器前，请先用 bsk_session(action=\"start\") 建一个。"
            )

        state_labels = {
            "active": "可用",
            "stopped": "已停止",
            "pending_cleanup": "待清理（上个进程遗留，不要再操作它）",
        }
        lines = [f"本插件创建的浏览器会话（{len(sessions)} 个）："]
        for item in sessions:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "")
            marks = []
            if item.get("current"):
                marks.append("当前")
            suffix = f"［{', '.join(marks)}］" if marks else ""
            lines.append(
                f"  - session={item.get('session_id') or '(无 ID)'}"
                f"，key={item.get('key') or '(无)'}"
                f"，状态：{state_labels.get(state, state or '未知')}{suffix}"
            )
        pending = info.get("pending_cleanup") or 0
        if pending:
            lines.append(
                f"另有 {pending} 个上个进程遗留、尚未清理的会话"
                "（插件下次启动时会自动清掉）。"
            )
        lines.append(
            "要用某个会话，把它的 session 值传给对应工具的 session 参数；"
            "省略就用当前那一个。"
        )
        return "\n".join(lines)

    # --- bsk_page ------------------------------------------------------

    async def _run_page(self, key: str, action: str, args: dict) -> str:
        """bsk_page 的 navigate / back / forward / reload / wait 五个分支。"""
        if action == "navigate":
            nav = await self.service.navigate(
                key,
                args["url"],
                wait_until=args.get("wait_until", "load"),
                timeout_ms=args.get("timeout_ms"),
                tab_id=args.get("tab_id"),
            )
            return self._render_nav("已导航", nav)

        if action in ("back", "forward"):
            nav = await self.service.history(
                key,
                action,
                wait_until=args.get("wait_until", "load"),
                timeout_ms=args.get("timeout_ms"),
                tab_id=args.get("tab_id"),
            )
            return self._render_nav("已后退" if action == "back" else "已前进", nav)

        if action == "reload":
            nav = await self.service.reload_page(
                key,
                hard=args.get("hard", False),
                wait_until=args.get("wait_until", "load"),
                timeout_ms=args.get("timeout_ms"),
                tab_id=args.get("tab_id"),
            )
            return self._render_nav(
                "已强制刷新（绕过缓存）" if args.get("hard") else "已刷新", nav
            )

        # wait
        nav = await self.service.wait_for(
            key,
            wait_until=args.get("wait_until", "load"),
            timeout_ms=args.get("timeout_ms") or 30000,
            tab_id=args.get("tab_id"),
        )
        return self._render_nav("已等待页面加载完成", nav)

    @staticmethod
    def _render_nav(verb: str, nav: dict) -> str:
        """渲染导航/等待的结果。

        ``reached`` 可以是 ``"timeout"`` —— 那是**结果不是错误**
        （TOOL-SPEC §1.2 明说），所以这里必须如实转达，并且**不能**写成
        失败语气：模型看到"失败"会去重试，而重试往往只是再等一次同样的时间。
        """
        reached = str(nav.get("reached") or "")
        url = nav.get("final_url") or nav.get("url") or ""
        session = nav.get("session") or ""

        if reached == "timeout":
            head = (
                f"{verb}，但页面在超时时间内没有到达指定的加载阶段"
                "（这是等待结果，不是命令失败：页面很可能还在加载）。"
            )
        elif reached:
            head = f"{verb}（页面已到达 {reached} 阶段）。"
        else:
            head = f"{verb}。"

        lines = [head]
        if url:
            lines.append(f"当前地址：{url}")
        if session:
            lines.append(f"会话：{session}")
        lines.append(
            "页面内容可能已经变化，元素编号也会变；"
            "接着请用 bsk_inspect(action=\"observe\") 重新读一次页面。"
        )
        return "\n".join(lines)

    # --- bsk_inspect ---------------------------------------------------

    async def _run_inspect(self, key: str, action: str, args: dict) -> str:
        """bsk_inspect 的 6 个分支（observe / snapshot / html / screenshot /
        console / network）。

        ``debug`` 已拆成独立的 :meth:`_run_debug`（工具 ``bsk_debug``）。
        """
        if action == "observe":
            return await self._run_observe(key, args)

        if action == "snapshot":
            snap = await self.service.snapshot(
                key,
                max_depth=args.get("max_depth"),
                max_tokens=args.get("max_tokens"),
                tab_id=args.get("tab_id"),
            )
            lines = []
            if snap.get("truncated"):
                lines.append("（快照被截断，只显示了部分内容。）")
            lines.append(str(snap.get("text") or "（页面没有可读内容。）"))
            lines.append(
                f"（会话：{snap.get('session') or '?'}，"
                f"元素引用 {snap.get('ref_count') or 0} 个，"
                f"所在标签页 id={snap.get('tab_id') or 0}。）"
            )
            return "\n".join(lines)

        if action == "html":
            html = await self.service.get_html(
                key,
                ref=args.get("ref", ""),
                max_bytes=args.get("max_bytes") or 524288,
                tab_id=args.get("tab_id"),
            )
            head = f"HTML 共 {html.get('byte_size') or 0} 字节"
            if html.get("ref"):
                head += f"（已限定到元素 {html['ref']}）"
            return f"{head}：\n{html.get('html') or '（空）'}"

        if action == "screenshot":
            return await self._run_screenshot(key, args)

        if action == "console":
            return await self._run_logs(
                key,
                "console",
                args,
                include_stack=args.get("include_stack", False),
            )

        if action == "network":
            return await self._run_logs(key, "network", args, include_stack=False)

        # 走不到这里：validate 只放行 TOOL_ACTIONS["bsk_inspect"] 的六个 action，
        # 上面已经把六个全部分派完（debug 已拆成独立工具 bsk_debug）。
        raise AssertionError(f"bsk_inspect 未处理的 action：{action!r}")

    # --- bsk_debug -----------------------------------------------------

    async def _run_debug(self, key: str, args: dict) -> str:
        """``bsk_debug`` 的唯一分支：把调试参数转给 ``service.debug``。

        这里是**整条 debug 路径的唯一实现** —— ``bsk_inspect`` 不再有 debug 分支，
        所以不存在"两个工具各走一套逻辑"的风险。

        子动作取自 ``action``：校验层已保证它与 ``debug_action`` 一致
        （不一致会直接报错），两者本就是同一个值。
        """
        debug_action = str(args["action"])
        # debug：bsk 的原始 JSON 直通（TOOL-SPEC §2 的硬要求：不重映射字段）。
        # 这里只做一件事 —— 把非 JSON 的返回值也变成字符串。
        result = await self.service.debug(
            key, debug_action, **self._debug_opts(args)
        )
        return self._render_debug(debug_action, result)

    @staticmethod
    def _debug_opts(args: dict) -> dict:
        """摘出要传给 ``service.debug`` 的调试参数。

        这里有两处**必须**处理的形参冲突，都属于"Python 形参绑定"的硬约束
        （不是设计选择），弄错就是 ``TypeError`` 或静默丢参：

        1. ``action`` 要去掉 —— 子动作已经作为 ``debug_action`` 位置参数传进
           ``service.debug`` 了；``action`` 再留在 ``opts`` 里会被
           DEBUG_VALUE_FLAGS 静默忽略（表里没有这一项），也就是**不报错也不生效**。
        2. ``debug_action`` 要去掉 —— ``service.debug`` 的签名是
           ``debug(self, key, debug_action, **opts)``，``debug_action`` 已经是
           **位置参数**；如果它还留在 ``opts`` 里，调用就变成

               TypeError: debug() got multiple values for argument 'debug_action'

           （与裁决 1 的 ``press``/``key`` 是同一类冲突，实测确认。）

        ``tab_id`` 则**必须留着**：``service.debug`` 内部是
        ``opts.pop("tab_id", None)`` 取它的，也就是说 ``tab_id`` 走的就是
        ``opts`` 这条路。把它滤掉会让 ``bsk_debug(tab_id=N)``
        静默打在别的标签上（校验层明明收下了它）——正是 REVIEW-ROUND2 的
        P0-1 那一类错误。
        """
        return {
            name: value
            for name, value in args.items()
            if name not in ("action", "debug_action")
        }

    @staticmethod
    def _render_debug(debug_action: str, result: Any) -> str:
        """把 debug 的原始载荷渲染成给模型看的文本。

        刻意**不做字段重映射**：``debug`` 的输出形态由 action 决定
        （列表、body 切片、导出路径、等待结果……），任何"统一化"都会丢掉
        模型真正要看的东西。所以这里只是套一层中文抬头 + 序列化。
        """
        import json

        if isinstance(result, str):
            body = result
        else:
            try:
                body = json.dumps(result, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body = repr(result)
        if not body.strip():
            body = "（bsk 没有返回任何内容。）"
        return f"debug {debug_action} 的原始返回：\n{body}"

    async def _run_observe(self, key: str, args: dict) -> str:
        """``bsk_inspect(action="observe")``。

        为什么单独一个方法而不是直接 ``await service.observe(key)``：
        ``bsk observe`` 的 CLI **确实**接受 ``--cursor`` / ``--max-depth`` /
        ``--max-tokens`` / ``--tab-id``（已用 ``bsk observe --help`` 实测确认），
        而 ``service.observe(key)`` 的签名只有 ``key`` —— 走它会把校验层
        收下的这四个参数**静默丢掉**，模型以为翻页/限深生效了，实际没有。
        这正是 REVIEW-ROUND2 的 P0-1 那一类缺陷。

        所以这里按 ``service`` 的**实际能力**分两条路（用签名探测，不写死）：

        - service 支持某个参数 → 正常转发；
        - 不支持 → 明确告诉模型"这个参数当前不生效"，并且**在能给替代方案时
          给出替代**（``max_depth``/``max_tokens`` 可以改用 snapshot）。

        绝不静默：宁可让模型知道"这个旋钮没接上"，也不要它拿着一个
        没生效的参数继续往下推理。

        Note:
            这是 **main.py 单方面的兼容处理**，``bsk/service.py`` 一行没动
            （本次作业范围只允许改 main.py 与 _conf_schema.json）。
            ``service.observe`` 补上这些形参之后，本方法会自动走"支持"那条
            分支，无需再改代码 —— 探测是运行期做的。
        """
        supported = self._observe_supported_params()
        forward: dict[str, Any] = {}
        ignored: list[str] = []
        for name in ("cursor", "max_depth", "max_tokens", "tab_id"):
            value = args.get(name)
            if value is None:
                continue
            if name in supported:
                forward[name] = value
            else:
                ignored.append(name)

        # 只有 cursor 是 observe 独有的：它没有等价替代，被忽略时必须说清楚，
        # 否则模型会以为"下一页拿到了"，而实际上拿到的是同一页。
        if "cursor" in ignored:
            return self._fail(
                "observe 的 cursor（继续读取被省略的内容）当前不可用："
                "本插件的服务层还没接上它。\n"
                "请不要传 cursor，改为重新 observe 一次拿到当前完整内容；"
                "如果内容被截断，请用 bsk_inspect(action=\"snapshot\") 配合 "
                "max_depth / max_tokens 控制返回量。"
            )

        observation = await self.service.observe(key, **forward)
        text = self.service.render_page(observation)
        if ignored:
            text += (
                "\n（注意：这次传入的 "
                + "、".join(ignored)
                + " 当前不生效，返回的是完整内容。"
                "要控制返回量请改用 bsk_inspect(action=\"snapshot\")。）"
            )
        # 分页闭环：把 next_cursor 回显给模型，否则它只知道"被截断了"却无从续读。
        # 这是 DSH 的 browser_inspect(observe) 返回 nextCursor 的等价物。
        cursor_next = getattr(observation, "next_cursor", "") or ""
        if cursor_next:
            text += (
                f"\n（内容还没读完。把 cursor=\"{cursor_next}\" 传给下一次 "
                'bsk_inspect(action="observe") 就能接着读。'
                "注意中间不要穿插别的 observe/snapshot，那会让游标失效。）"
            )
        return text

    @staticmethod
    def _observe_supported_params() -> frozenset[str]:
        """探测 ``service.observe`` 实际接受哪些可选参数。

        运行期反射而不是写死常量：``bsk/service.py`` 是另一个代理的作业面，
        它随时可能补上 ``cursor``/``max_depth``/``max_tokens``/``tab_id``。
        写死的话，那边一补齐，这里就会变成"新增的能力被我挡住"——比现在的
        静默丢弃好，但仍然是错的。反射让两边自动对齐。

        探测失败（拿不到签名）时返回空集合：退化成"只调 observe(key)"，
        与改造前的行为一致，绝不因为一次反射失败就把工具打崩。
        """
        import inspect

        try:
            sig = inspect.signature(BskService.observe)
        except (TypeError, ValueError):  # pragma: no cover - 内建/装饰器异常
            return frozenset()

        allowed_kinds = (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        supported: set[str] = set()
        for name, param in sig.parameters.items():
            if name in ("self", "key"):
                continue
            # 有 **kwargs 时，任何名字都能传进去。
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return frozenset({"cursor", "max_depth", "max_tokens", "tab_id"})
            if param.kind in allowed_kinds:
                supported.add(name)
        return frozenset(supported)

    async def _run_screenshot(self, key: str, args: dict) -> str:
        """``bsk_inspect(action="screenshot")``：截图并回一段文字描述。

        ⚠️ 与旧工具 ``bsk_screenshot`` 的**唯一行为差异**，必须说清楚：
        这里的 handler 返回 ``str``（schema 覆写后框架按普通函数调用它），
        **不是** async generator，所以拿不到 ``yield event.image_result(...)``
        那条路 —— 新工具不会把图片发给用户，只回一段描述文字。

        这是本次改造里唯一的功能回退点，已在交付报告里如实列出。
        要真的把图片发出去，目前只能用兼容保留的 ``bsk_screenshot``
        （它还能整页截图，新工具也没有这个能力）。
        """
        payload = await self.service.screenshot_ex(
            key,
            ref=args.get("ref", ""),
            tab_id=args.get("tab_id"),
        )
        message = payload.for_llm
        if payload.warning:
            message = f"{message}\n注意：{payload.warning}"
        return (
            f"{message}\n"
            "（说明：新工具只返回这段文字描述，不会把图片作为消息发出去；"
            f"截图文件在 {payload.path}。"
            "需要把图片直接发给用户时，请改用 bsk_screenshot。）"
        )

    async def _run_logs(
        self, key: str, kind: str, args: dict, *, include_stack: bool
    ) -> str:
        """console / network 两个分支，共用 ``bsk_logs`` 那套游标与渲染。

        复用既有实现（TOOL-SPEC H4）：``_next_since`` 的游标推进规则、
        ``LOGS_RENDER_LIMIT`` 的展示上限、``render_console`` 的双重截断，
        全部走旧工具同一条路径，新工具只多做一次参数传递。
        """
        cursor = int(args.get("since") or 0)
        label = LOGS_LABELS[kind]
        if kind == "console":
            log = await self.service.read_console_ex(
                key,
                since=cursor,
                limit=args.get("limit"),
                max_text_chars=args.get("max_text_chars"),
                include_stack=include_stack,
                tab_id=args.get("tab_id"),
            )
        else:
            log = await self.service.read_network_ex(
                key,
                since=cursor,
                limit=args.get("limit"),
                max_text_chars=args.get("max_text_chars"),
                tab_id=args.get("tab_id"),
            )
        return self._render_logs(label, cursor, log)

    @staticmethod
    def _render_logs(label: str, cursor: int, log: ConsoleLog) -> str:
        """渲染日志结果（与 ``bsk_logs`` 同一套文案与游标约定）。"""
        next_cursor = _next_since(cursor, log)
        if not log.entries:
            return (
                f"从第 {cursor} 条之后到现在，这段时间内没有捕获到{label}。\n"
                "常见原因：页面本来就没输出、还没有新动作发生，或者浏览器会话"
                "是刚重建的（重建后日志会从头开始）。\n"
                f"可以保持 since={next_cursor} 再查一次，或先做点页面操作再看。"
            )

        body = BskService.render_console(log, limit=LOGS_RENDER_LIMIT)
        shown = min(len(log.entries), LOGS_RENDER_LIMIT)
        hidden = len(log.entries) - shown
        lines = [
            f"{label}（本次返回 {len(log.entries)} 条，展示 {shown} 条）：",
            body,
        ]
        if hidden > 0:
            lines.append(
                f"（还有 {hidden} 条这次没展示，把 since 设成下面这个值再读一次"
                "就能接着看。）"
            )
        lines.append(
            f"下次只看新增的{label}，请传 since={next_cursor}"
            f"（只返回序号大于 {next_cursor} 的日志；传 0 则从头读）。"
        )
        return "\n".join(lines)

    # --- bsk_interact --------------------------------------------------

    async def _run_interact(self, key: str, action: str, args: dict) -> str:
        """bsk_interact 的 9 个分支，全部走 ``service.interact`` 一个入口。"""
        opts = {
            name: value
            for name, value in args.items()
            if name not in ("action", "tab_id")
        }
        # 【裁决 1】**必须**在这里改名，且只能是这里。
        #
        # 校验层输出的键是 ``key``（对齐 DSH 的公共参数名，模型看到的就是它），
        # 而 ``service.interact(self, key, action, ...)`` 的 ``key`` 已经是
        # **会话键**。Python 的形参绑定不允许两个 ``key`` 共存：
        #
        #     interact(k, "press", key="Enter")
        #     -> TypeError: got multiple values for argument 'key'
        #
        # 把会话键改成传关键字也一样撞（换成 action 那一侧）。所以改名只能
        # 发生在**调用之前**，service 内部救不了自己。
        if action == "press":
            opts["press_key"] = opts.pop("key")
        result = await self.service.interact(
            key, action, tab_id=args.get("tab_id"), **opts
        )
        return self._render_interact(action, result)

    @staticmethod
    def _render_interact(action: str, result: dict) -> str:
        """渲染交互结果。

        ``service.interact`` 的返回是"通用字段 + 该 action 的结果字段"的
        并集，字段名原样来自 bsk 载荷（不重映射）。这里只挑几个通用字段说
        人话，其余原样附上，避免模型因为看不到回执而怀疑操作没生效。
        """
        lines = [f"已执行 {action}。"]
        session = result.get("session")
        if session:
            lines.append(f"会话：{session}")
        target = result.get("target")
        if target:
            lines.append(f"目标元素：{target}")
        if result.get("changed"):
            lines.append(
                "这个动作会改变页面，接下来请用 bsk_inspect(action=\"observe\") "
                "确认结果（页面变化后元素编号会变，不要沿用旧的 @eN）。"
            )

        extra = {
            name: value
            for name, value in result.items()
            if name not in ("session", "action", "target", "changed", "tab_id")
            and value not in (None, "", [], {})
        }
        if extra:
            rendered = "、".join(f"{name}={value!r}" for name, value in extra.items())
            lines.append(f"浏览器返回的其他信息：{rendered}")
        return "\n".join(lines)

    # --- bsk_tabs ------------------------------------------------------

    async def _run_tabs(self, key: str, action: str, args: dict) -> str:
        """bsk_tabs 的 6 个分支，全部走 ``service.tabs``。"""
        opts = {
            name: value
            for name, value in args.items()
            if name not in ("action", "tab_id")
        }
        # ``service.tabs`` 的 ``tab_id`` 关键字参数与 ``opts["tab_id"]``
        # 是同一个东西的两个入口（它内部就是 ``tab_id if tab_id is not None
        # else opts.get("tab_id")``）。这里统一走关键字参数，避免同一个值
        # 出现两份。list/create 的校验器只在模型显式传了 tab_id 时才写回它，
        # 所以 ``args.get("tab_id")`` 为 None 时就等于"没传"。
        result = await self.service.tabs(
            key, action, tab_id=args.get("tab_id"), **opts
        )
        return self._render_tabs(action, result)

    @staticmethod
    def _render_tabs(action: str, result: dict) -> str:
        """渲染标签页管理的结果。"""
        if action == "list":
            tabs = result.get("tabs") or []
            if not tabs:
                return (
                    "没有列出任何标签页。可能是当前会话还没有可用标签页，"
                    "或 scope 过滤掉了它们（scope=user 列用户的、agent 列"
                    "Agent Window 的、all 列全部）。"
                )
            lines = [f"标签页（{len(tabs)} 个）："]
            for tab in tabs:
                if not isinstance(tab, dict):
                    continue
                tab_id = tab.get("tab_id", tab.get("id", "?"))
                title = tab.get("title") or "（无标题）"
                url = tab.get("url") or ""
                active = "［当前活动］" if tab.get("active") else ""
                lines.append(f"  - id={tab_id} {title}{active}")
                if url:
                    lines.append(f"      {url}")
            lines.append("要操作其中某一页，把它的 id 传给 tab_id。")
            return "\n".join(lines)

        lines = [f"已执行 tab {action}。"]
        if result.get("tab_id") is not None:
            lines.append(f"标签页 id：{result['tab_id']}")
        extra = {
            name: value
            for name, value in result.items()
            if name not in ("session", "action", "tab_id", "tabs")
            and value not in (None, "", [], {})
        }
        if extra:
            rendered = "、".join(f"{name}={value!r}" for name, value in extra.items())
            lines.append(f"浏览器返回的其他信息：{rendered}")
        if action == "borrow":
            lines.append(
                "这一页原本是用户自己的标签页，现在被移进了 Agent Window。"
                "用完请尽快用 bsk_tabs(action=\"return\") 还回去"
                "（停止会话时也会自动归还）。"
            )
        return "\n".join(lines)

    # --- bsk_assist ----------------------------------------------------

    async def _run_assist(self, key: str, action: str, args: dict) -> str:
        """bsk_assist 的 resize / emulate / request-help 三个分支。"""
        if action == "resize":
            result = await self.service.resize_window(
                key,
                args["width"],
                args["height"],
                tab_id=args.get("tab_id"),
            )
            return (
                f"已把窗口调整为 {result.get('width')}x{result.get('height')}。"
                "（窗口尺寸是会话级的，与具体标签页无关。）"
            )

        if action == "emulate":
            result = await self.service.emulate(
                key,
                device=args.get("device", ""),
                width=args.get("width"),
                height=args.get("height"),
                mobile=args.get("mobile", False),
                off=args.get("off", False),
                tab_id=args.get("tab_id"),
            )
            if result.get("off"):
                return "已清除这个会话上的全部设备模拟设置。"
            parts = []
            if result.get("device"):
                parts.append(f"设备预设 {result['device']}")
            if result.get("width") and result.get("height"):
                parts.append(f"视口 {result['width']}x{result['height']}")
            detail = "，".join(parts) if parts else "（bsk 未回显具体参数）"
            return (
                f"已应用设备模拟：{detail}。\n"
                "模拟只作用于会话当前的活动标签页；要恢复请用 off=true。"
            )

        # request-help
        result = await self.service.request_help(
            key,
            args["prompt"],
            title=args.get("title", ""),
            targets=args.get("targets") or [],
            timeout_ms=args.get("timeout_ms") or 300000,
            completion_criteria=args.get("completion_criteria"),
            tab_id=args.get("tab_id"),
        )
        return self._render_request_help(result)

    @staticmethod
    def _render_request_help(result: dict) -> str:
        """渲染 request-help 的结果。

        核心是**如实转达 outcome 的语义**：只有 ``continued`` 与 ``completed``
        表示用户已经做完（TOOL-SPEC §1.6）。其余取值（``cancelled`` /
        ``timed_out`` / ``navigated`` / ``disabled``）都**不是**继续信号 ——
        如果模型把它们当成"用户可以继续了"，它会接着去操作一个真人根本
        没碰过的页面，然后照着错误的前提回答用户。
        """
        outcome = str(result.get("outcome") or "")
        # 这里刻意不用 Markdown 加粗（**...**）：这段文字会被原样发到
        # QQ / 微信等不渲染 Markdown 的平台，星号会直接显示给用户。
        # 强调靠中文措辞本身（"没有完成"、"不能当成"）。
        meanings = {
            "continued": "用户已经完成了他该做的事，可以继续。",
            "completed": "用户完成了全部步骤，可以继续。",
            "cancelled": "用户主动取消了这次求助，没有完成。",
            "timed_out": "等到超时用户也没有完成，没有完成。",
            "navigated": "用户把页面导航到了别处，请求被中断，不能当成已完成。",
            "disabled": "这个部署里的求助功能被禁用了，用户根本没看到提示。",
        }
        if outcome in ("continued", "completed"):
            head = f"求助结果：{outcome} —— {meanings[outcome]}"
        elif outcome:
            head = (
                f"求助结果：{outcome} —— {meanings.get(outcome, '这不是继续信号。')}"
                "不要把它当成用户已完成。"
            )
        else:
            head = (
                "求助结果：bsk 没有返回可识别的 outcome。"
                "不能假定用户已经完成 —— 请先看一眼页面实际状态再决定下一步。"
            )
        return (
            f"{head}\n"
            "（本工具只是显示一个提示浮层给用户看，它自己不会代替用户做任何操作。）"
        )

    # ------------------------------------------------------------------
    # 六个新工具（bsk_session / bsk_page / bsk_inspect / bsk_interact /
    #            bsk_tabs / bsk_assist）
    #
    # 签名一律是 (self, event, **kwargs)：框架按**形参名**注入参数
    # （astr_agent_tool_exec.py:763 的 `handler(event, *args, **kwargs)`），
    # 签名里没有的名字会 TypeError → 框架抛 "Tool handler parameter mismatch"。
    # 用 **kwargs 接住全部参数，多传的不会导致调用失败，真正的校验在
    # bsk/tools.py 的纯函数里做（与 DSH 的 open object root 同构）。
    #
    # docstring 只写描述、**不写 Args: 段** —— schema 由 bsk/tools.py 覆写，
    # Args 段不再是 schema 来源。描述里必须写清 action 取值：覆写失败时
    # 它是模型唯一能看到的说明。每个 docstring 控制在 300 字符以内（token 成本）。
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_session")
    async def bsk_session(self, event: AstrMessageEvent, **kwargs):
        """管理浏览器会话：启动、停止、列出。

        action 取 start / stop / list：
        - start：启动一个新的浏览器会话（可用 url 直接打开网页）
        - stop：停止当前会话
        - list：列出本插件创建的会话（谁都能用，便于自助排查）

        首次使用浏览器功能前必须先 start 一个会话；后续操作会自动复用它。
        """
        return await self._dispatch("bsk_session", event, kwargs)

    @filter.llm_tool("bsk_page")
    async def bsk_page(self, event: AstrMessageEvent, **kwargs):
        """在浏览器里导航，并等待页面加载。

        action 取 navigate / back / forward / reload / wait：
        - navigate 打开网址；back / forward 走历史；reload 刷新（hard 绕缓存）
        - wait 只等页面自己加载完，**不做任何导航**（点了会跳转的链接之后用它）

        页面变化后元素编号会失效，请重新 observe 再操作。
        """
        return await self._dispatch("bsk_page", event, kwargs)

    @filter.llm_tool("bsk_inspect")
    async def bsk_inspect(self, event: AstrMessageEvent, **kwargs):
        """读取页面状态：正文、快照、HTML、截图、控制台与网络日志。

        action 取 observe / snapshot / html / screenshot / console / network。
        读页面**优先用 observe**（最常用，给出正文与 @eN 元素编号）；
        snapshot 是无游标的静态可达性树；html 取原始 HTML（有字节上限）；
        screenshot 只回文字描述、**不会把图片发给用户**；console / network 支持
        since 游标增量读。
        要抓包、看接口返回、控制请求流量，请改用 bsk_debug。
        """
        return await self._dispatch("bsk_inspect", event, kwargs)

    @filter.llm_tool("bsk_debug")
    async def bsk_debug(self, event: AstrMessageEvent, **kwargs):
        """调试与网络流量控制：抓包、分析请求、导出证据、拦截或重放请求。

        什么时候用：要查接口返回、慢请求、重复请求，或要拦截/改写/伪造请求时。
        共 24 个 action（performance / aggregate / duplicates / requests /
        request / export / rules / rule_add / replay / start / status 等），
        由 action 指定子动作。典型顺序：先 start 开抓包，再访问页面，读结果。
        replay 会重发请求，rule_add/rule_enable 能改写或伪造真实请求，
        可能改动服务端数据。
        """
        return await self._dispatch("bsk_debug", event, kwargs)

    @filter.llm_tool("bsk_interact")
    async def bsk_interact(self, event: AstrMessageEvent, **kwargs):
        """与页面交互：点击、输入、按键、滚动等。

        action 取 click / hover / wheel / scroll-to / focus / blur / fill /
        select / press。target 支持快照引用（如 @e3）或 CSS 选择器；
        fill 还要 value，select 还要 values，press 必填 key。
        scroll-to 带连字符（不是 scroll_to）。
        操作后请 observe 一次确认结果。
        """
        return await self._dispatch("bsk_interact", event, kwargs)

    @filter.llm_tool("bsk_tabs")
    async def bsk_tabs(self, event: AstrMessageEvent, **kwargs):
        """管理浏览器标签页：列出、新建、切换、关闭、借用、归还。

        action 取 list / create / select / close / borrow / return。
        select / close / borrow / return 必填 tab_id，取自 list 或 create。
        ⚠️ borrow 会把**用户自己**的标签页移进 Agent Window（会动到用户正在看的
        窗口），用完请尽快 return；会话停止时会自动归还。
        """
        return await self._dispatch("bsk_tabs", event, kwargs)

    @filter.llm_tool("bsk_assist")
    async def bsk_assist(self, event: AstrMessageEvent, **kwargs):
        """调整窗口与设备模拟，或请真人帮忙完成页内步骤。

        action 取 resize / emulate / request-help。
        resize 必填 width 与 height（各 100..7680）；emulate 用 device 或
        width+height(+mobile)，或单独用 off 清除；request-help 必填 prompt，
        会显示提示浮层等用户操作。outcome 里**只有 continued 与 completed**
        表示用户已完成，其余取值都不是。
        """
        return await self._dispatch("bsk_assist", event, kwargs)

    # ------------------------------------------------------------------
    # 工具 1：打开网页
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_open")
    async def bsk_open(self, event: AstrMessageEvent, url: str, new_session: bool = False):
        """用浏览器打开一个网页，并返回页面标题和可交互元素。

        （兼容保留，新用法请优先用 `bsk_page`（action="navigate"）。）
        会复用当前聊天已有的浏览器会话；如果没有就新建一个。
        打开后请用 bsk_read 重新读取页面，或用 bsk_act 操作元素。

        Args:
            url(string): 要打开的网址，必须以 http:// 或 https:// 开头
            new_session(boolean): 设为 true 会关掉当前浏览器会话并重新开一个（用于页面卡死或需要重新登录时）
        """
        denied = self._denied(event)
        if denied:
            return self._fail(denied)

        target = (url or "").strip()
        if not target:
            return self._fail("请提供要打开的网址。")
        if not target.lower().startswith(("http://", "https://")):
            return self._fail(
                "网址必须以 http:// 或 https:// 开头。"
                "例如 https://example.com。"
            )

        key = self._key(event)
        try:
            if new_session:
                await self.service.close_session(key)

            nav, observation = await self.service.open_page(key, target)
            page_text = self.service.render_page(observation)

            landed = nav.final_url or target
            header = f"已打开：{landed}"
            if nav.final_url and nav.final_url.rstrip("/") != target.rstrip("/"):
                header = f"已打开：{target}\n实际落点：{nav.final_url}（发生了跳转）"
            return f"{header}\n\n{page_text}"
        except BskError as exc:
            return self._fail(f"打开网页失败：{exc.friendly}")
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常穿到框架
            astrbot_logger.exception("[bsk_browser] bsk_open 未预期错误")
            return self._fail(f"打开网页时出现未预期的错误：{exc}")

    # ------------------------------------------------------------------
    # 工具 2：读取页面
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_read")
    async def bsk_read(self, event: AstrMessageEvent):
        """读取当前浏览器页面的内容和可交互元素编号。

        （兼容保留，新用法请优先用 `bsk_inspect`（action="observe"）。）
        返回页面标题、正文摘要，以及形如 @e1、@e2 的元素编号。
        这些编号可以直接用在 bsk_act 的 target 参数里。
        页面刚变化过时建议先调用本工具，因为编号可能会变。

        Args:
        """
        denied = self._denied(event)
        if denied:
            return self._fail(denied)

        key = self._key(event)
        try:
            observation = await self.service.observe(key)
            return self.service.render_page(observation)
        except BskError as exc:
            return self._fail(f"读取页面失败：{exc.friendly}")
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_read 未预期错误")
            return self._fail(f"读取页面时出现未预期的错误：{exc}")

    # ------------------------------------------------------------------
    # 工具 3：页面交互
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_act")
    async def bsk_act(
        self,
        event: AstrMessageEvent,
        action: str,
        target: str = "",
        value: str = "",
        key_spec: str = "",
        delta_y: int = 0,
        delta_x: int = 0,
    ):
        """在当前浏览器页面上执行一个操作（点击、输入、按键、滚动等）。

        （兼容保留，新用法请优先用 `bsk_interact`：click → 保持不变，
        hover / wheel / focus / blur / fill / select / press 也都在它里面；
        本工具独有的 reload → `bsk_page`（action="reload"）、
        navigate_back → `bsk_page`（action="back"）、
        navigate_forward → `bsk_page`（action="forward"）、
        wait_for_navigation → `bsk_page`（action="wait"）。）
        操作完成后建议调用 bsk_read 确认结果，因为页面变化后元素编号会变。
        如果页面正在加载（点了会跳转的链接、提交表单后），可以先用
        wait_for_navigation 等它加载完，再去读取，否则可能读到旧页面。

        Args:
            action(string): 要执行的动作，可选值：click(点击)、fill(输入文字)、press(按键)、select(下拉框选择)、hover(鼠标悬停)、scroll_to(滚动到元素)、wheel(滚动页面)、focus(聚焦)、blur(失焦)、reload(刷新)、navigate_back(后退)、navigate_forward(前进)、wait_for_navigation(等待页面加载完成)
            target(string): 元素编号（如 @e3）或 CSS 选择器。click/fill/select/hover/scroll_to/focus/blur 必填；press 可选（填了就表示先聚焦该元素再按键）；wait_for_navigation 不需要
            value(string): fill 要输入的文字，或 select 要选中的选项值
            key_spec(string): press 要按的键，例如 Enter、Escape、Tab、Ctrl+A、ArrowDown
            delta_y(number): wheel 的垂直滚动量，正数向下、负数向上，单位像素，例如 500
            delta_x(number): wheel 的水平滚动量，正数向右、负数向左
        """
        denied = self._denied(event)
        if denied:
            return self._fail(denied)

        key = self._key(event)
        action_norm = (action or "").strip().lower().replace("-", "_")
        try:
            result = await self.service.act(
                key,
                action_norm,
                target=(target or "").strip(),
                value=value or "",
                key_spec=(key_spec or "").strip(),
                delta_y=int(delta_y or 0),
                delta_x=int(delta_x or 0),
            )
            note = f"（{result.note}）" if result.note else ""
            # bsk 的回执里带了 url / changed 时才算得出"页面变了没有"；
            # 说不准就不说 —— 不要用一个猜出来的结论误导模型。
            if result.page_changed:
                hint = "页面已变化，建议接着用 bsk_read 确认新内容。"
            else:
                hint = "建议接着用 bsk_read 确认页面变化。"
            return f"已执行 {action_norm}{note}。{hint}"
        except BskError as exc:
            return self._fail(f"操作失败：{exc.friendly}")
        except ValueError as exc:
            return self._fail(f"参数格式不对：{exc}")
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_act 未预期错误")
            return self._fail(f"执行操作时出现未预期的错误：{exc}")

    # ------------------------------------------------------------------
    # 工具 4：截图
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_screenshot")
    async def bsk_screenshot(self, event: AstrMessageEvent, full_page: bool = False):
        """给当前浏览器页面截图，并直接把图片发给用户。

        （兼容保留，新用法请优先用 `bsk_inspect`（action="screenshot"）。
        注意：新工具目前**只返回一段文字描述，不会把图片发给用户**；
        要真的把图片发出去、或要整页截图（full_page），请继续用本工具
        —— 这两件事目前只有本工具做得到。）
        图片会以消息形式发送，你不需要描述图片内容，除非用户要求。

        Args:
            full_page(boolean): 设为 true 截取整个长页面（较慢，可能需要几十秒）；默认只截可见区域
        """
        denied = self._denied(event)
        if denied:
            yield self._fail(denied)
            return

        key = self._key(event)
        try:
            payload = await self.service.screenshot(key, full_page=bool(full_page))
        except BskError as exc:
            yield self._fail(f"截图失败：{exc.friendly}")
            return
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_screenshot 未预期错误")
            yield self._fail(f"截图时出现未预期的错误：{exc}")
            return

        message = payload.for_llm
        if payload.warning:
            message = f"{message}\n注意：{payload.warning}"

        # 先把图片 yield 给用户，再 yield 文本回灌给模型。
        #   注意 async generator 里不能写 `return <值>`（会是 SyntaxError），
        #   要把文本也 yield 出去。
        try:
            yield event.image_result(payload.path)
        except Exception as exc:  # noqa: BLE001 - 发图失败不应吞掉文本回执
            astrbot_logger.warning("[bsk_browser] 发送截图失败：%r", exc)
            message = f"{message}\n（图片发送失败了，截图文件在：{payload.path}）"

        yield message

    # ------------------------------------------------------------------
    # 工具 5：关闭会话
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_close")
    async def bsk_close(self, event: AstrMessageEvent):
        """关闭当前聊天正在使用的浏览器会话（会关掉那个 Agent Window）。

        （兼容保留，新用法请优先用 `bsk_session`（action="stop"）。）
        用户说"关掉浏览器""不用了""结束"时调用这个。
        长时间不用的会话也会自动回收，但显式关闭更干净。

        Args:
        """
        denied = self._denied(event)
        if denied:
            return self._fail(denied)

        key = self._key(event)
        try:
            closed = await self.service.close_session(key)
            if closed:
                return "已关闭浏览器会话。"
            return "当前没有正在使用的浏览器会话。"
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_close 未预期错误")
            return self._fail(f"关闭浏览器会话时出现未预期的错误：{exc}")

    # ------------------------------------------------------------------
    # 工具 6：诊断
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_status")
    async def bsk_status(self, event: AstrMessageEvent):
        """检查浏览器环境是否正常（bsk 是否安装、浏览器扩展是否连上、有多少会话）。

        当浏览器操作失败、或用户问"为什么用不了"时调用这个来排查。
        它额外给出 bsk 版本与运行时长、已连接的浏览器实例（含"无响应"与
        "版本不匹配"标记）、本对话那条会话的忙碌/不确定状态 —— 这些是
        bsk_session（action="list"）看不到的，所以排查环境问题请用本工具
        （它不带"请优先用新工具"的引导：它没有等价替代品）。

        Args:
        """
        # 诊断不涉及浏览器操作，但仍受总开关约束；且允许非管理员查看（便于自助排查）。
        if not self.settings.enabled:
            return self._fail("浏览器操作已在插件配置中停用。")

        try:
            info = await self.service.status(self._key(event))
            return self._render_status(info)
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_status 未预期错误")
            return self._fail(f"检查环境时出现未预期的错误：{exc}")

    @staticmethod
    def _render_status(info: dict[str, Any]) -> str:
        """把诊断信息渲染成给模型/用户看的中文。"""
        lines: list[str] = []

        resolved = info.get("bsk_resolved")
        if resolved:
            lines.append(f"bsk 可执行文件：{resolved}")
        else:
            lines.append("bsk 可执行文件：没找到")
            if info.get("error"):
                lines.append(f"  原因：{info['error']}")
            return "\n".join(lines)

        daemon = info.get("daemon")
        if isinstance(daemon, dict) and daemon.get("version"):
            uptime = daemon.get("uptime_secs")
            up = f"，已运行 {int(uptime)} 秒" if isinstance(uptime, (int, float)) else ""
            lines.append(f"后台服务：版本 {daemon['version']}{up}")
        else:
            lines.append("后台服务：未运行（第一次使用浏览器功能时会自动启动）")

        browsers = info.get("browsers")
        if isinstance(browsers, list) and browsers:
            lines.append(f"已连接的浏览器（{len(browsers)} 个）：")
            for b in browsers:
                name = b.get("name") or "未知浏览器"
                label = b.get("label") or "（未命名）"
                flags = []
                if b.get("unresponsive"):
                    flags.append("无响应")
                if b.get("version_skew"):
                    flags.append("版本不匹配")
                suffix = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"  - {name} / {label} / id={b.get('instance_id')}{suffix}")
        else:
            lines.append(
                "已连接的浏览器：没有\n"
                "  请确认浏览器已打开、bsk 扩展已安装并显示「已连接」。"
            )

        sessions = info.get("sessions")
        if isinstance(sessions, dict):
            counters = sessions.get("counters") or {}
            lines.append(
                f"当前会话数：{sessions.get('active', sessions.get('sessions', '?'))}"
                f"（累计创建 {counters.get('started', 0)}、重建 {counters.get('rebuilt', 0)}）"
            )

        # 当前这个聊天自己那一条 —— 全局计数回答不了"我这边还能用吗"。
        if info.get("current_session_active"):
            cur = info.get("current_session") or {}
            session_id = cur.get("session_id") or "(未建立)"
            idle = cur.get("idle_sec")
            idle_text = f"，空闲 {idle} 秒" if isinstance(idle, (int, float)) else ""
            flags = []
            if cur.get("busy"):
                flags.append("正在执行命令")
            if cur.get("uncertain"):
                flags.append("上一步结果不确定")
            flag_text = f"（{', '.join(flags)}）" if flags else ""
            lines.append(
                f"本对话的浏览器会话：{session_id}{idle_text}{flag_text}"
            )
        else:
            lines.append("本对话的浏览器会话：尚无（下次操作时自动创建）")

        if info.get("error"):
            lines.append(f"错误：{info['error']}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具 7：执行任意 JavaScript（高风险，默认关闭 + 强制管理员）
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_evaluate")
    async def bsk_evaluate(self, event: AstrMessageEvent, expression: str):
        """在当前页面里执行一段 JavaScript 表达式，并返回它的值。

        （兼容保留。本插件独有的能力，新工具里**没有**对应替代
        —— 它是超出 BrowserSkill 的部分，请继续用本工具。）
        这是高风险能力，默认关闭，且默认仅管理员可用（两项都在插件配置里，
        需要管理员先去打开）。只在 bsk_read 读不到需要的东西时才用它，例如：
        - 读取页面上没有直接显示的数据（元素属性、输入框里已有的内容）；
        - 精确取出某个元素的文字，而不是靠 bsk_read 的摘要。

        脚本在用户已登录的页面里运行，因此它能接触到页面上的全部内容。
        返回文本过长时会被截断，需要完整内容时请在表达式里先做筛选。

        Args:
            expression(string): 要执行的 JavaScript 表达式，例如 document.title。求值成 undefined 时返回"没有值"
        """
        # 三条权限分支都在 _evaluate_denied 里，顺序即设计（见它的 docstring）。
        denied = self._evaluate_denied(event)
        if denied:
            return self._fail(denied)

        script = (expression or "").strip()
        if not script:
            return self._fail(
                "请提供要执行的 JavaScript 表达式，例如 document.title。"
            )

        key = self._key(event)
        try:
            result = await self.service.evaluate(key, script)
            return self.service.render_evaluate(result, script)
        except BskError as exc:
            return self._fail(f"执行脚本失败：{exc.friendly}")
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_evaluate 未预期错误")
            return self._fail(f"执行脚本时出现未预期的错误：{exc}")

    # ------------------------------------------------------------------
    # 工具 8：读取控制台消息与网络请求（只读，与 bsk_read 同级）
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_logs")
    async def bsk_logs(
        self, event: AstrMessageEvent, kind: str = "console", since: float = 0
    ):
        """读取浏览器页面上捕获的控制台消息或网络请求，用于排查页面问题。

        （兼容保留，新用法请优先用 `bsk_inspect`：读控制台用
        action="console"，读网络请求用 action="network"。）
        什么时候用它：
        - 页面看起来没反应、报错、按钮点了没效果，想知道背后发生了什么；
        - 页面数据没显示出来，想确认某个接口是不是请求失败或返回了错误状态码；
        - 用户在页面上提交了表单后，想确认请求发出去了没有。

        它只读日志，不会改动页面，也不会执行任何代码。
        日志是累积的：同一个浏览器会话里，每次调用都从上次读到的位置往后接着读，
        所以返回里会告诉你下次该传什么 since 值，你只要把它原样传回来即可。
        一次最多展示 50 条，很长的网址会被截断。

        Args:
            kind(string): 要读哪种日志，填 console 读浏览器控制台消息（页面的报错、警告、脚本输出），填 network 读网络请求（每个请求的方法、状态码、网址）。默认 console
            since(number): 增量游标，只返回序号大于它的新日志；第一次调用填 0（或不填）从头读，之后把上一次返回里的「下次请传 since=...」那个数字填进来。默认 0
        """
        denied = self._denied(event)
        if denied:
            return self._fail(denied)

        kind_norm = (kind or "").strip().lower()
        if kind_norm not in LOGS_LABELS:
            return self._fail(
                "kind 只能填 console（浏览器控制台消息）或 network（网络请求），"
                f"收到的是 {kind!r}。"
            )

        cursor = _coerce_since(since)
        label = LOGS_LABELS[kind_norm]
        key = self._key(event)
        try:
            if kind_norm == "console":
                log = await self.service.read_console(key, since=cursor)
            else:
                log = await self.service.read_network(key, since=cursor)
        except BskError as exc:
            return self._fail(f"读取{label}失败：{exc.friendly}")
        except Exception as exc:  # noqa: BLE001
            astrbot_logger.exception("[bsk_browser] bsk_logs 未预期错误")
            return self._fail(f"读取{label}时出现未预期的错误：{exc}")

        next_cursor = _next_since(cursor, log)
        if not log.entries:
            # 空结果必须给一句明确的中文说明：空白字符串或 {} 会让模型以为
            # 工具坏了，甚至去编造内容。这里连"下次传什么"一起说清楚。
            return (
                f"从第 {cursor} 条之后到现在，这段时间内没有捕获到{label}。\n"
                "常见原因：页面本来就没输出、还没有新动作发生，或者浏览器会话"
                "是刚重建的（重建后日志会从头开始）。\n"
                f"可以保持 since={next_cursor} 再查一次，或先做点页面操作再看。"
            )

        body = self.service.render_console(log, limit=LOGS_RENDER_LIMIT)
        shown = min(len(log.entries), LOGS_RENDER_LIMIT)
        hidden = len(log.entries) - shown
        lines = [
            f"{label}（本次返回 {len(log.entries)} 条，展示 {shown} 条）：",
            body,
        ]
        if hidden > 0:
            lines.append(
                f"（还有 {hidden} 条这次没展示，把 since 设成下面这个值再读一次"
                "就能接着看。）"
            )
        lines.append(
            f"下次只看新增的{label}，请传 since={next_cursor}"
            f"（只返回序号大于 {next_cursor} 的日志；传 0 则从头读）。"
        )
        return "\n".join(lines)
