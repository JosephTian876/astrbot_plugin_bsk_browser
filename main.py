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
import logging
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .bsk.config import (
    Settings,
    parse_settings,
    read_framework_tool_timeout,
    validate_settings,
)
from .bsk.errors import BskError
from .bsk.models import ConsoleLog
from .bsk.service import BskService
from .bsk.session import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["BskBrowserPlugin"]

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


@register(
    "astrbot_plugin_bsk_browser",
    "yourname",
    "用 bsk（BrowserSkill）操作你自己已登录的浏览器：打开网页、读取内容、点击输入、截图。",
    "0.1.0",
)
class BskBrowserPlugin(Star):
    """把本机 bsk CLI 包装成 LLM 可调用的浏览器操作能力。

    设计要点：

    - 权限默认仅管理员（``admin_only``，可在插件配置里关闭）；
    - 按聊天会话隔离浏览器会话（``session_scope``：一个群一条 / 每人一条）；
    - 所有 bsk 细节都在 ``bsk/`` 包里，这里只做参数解包与结果包装。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.settings: Settings = parse_settings(config)

        # 读出框架自己的单次工具调用上限（AstrBot 主配置里的 tool_call_timeout），
        # 交给 bsk/ 层做超时钳制。这个读取永不抛异常（读不到就是 None），
        # 因为它在插件加载路径上 —— 拿不到这个值最多只是少一层保护，
        # 绝不该让插件起不来（见 bsk.config.read_framework_tool_timeout）。
        self.framework_tool_timeout: float | None = self._read_framework_timeout()

        self.service = BskService(
            self.settings, framework_tool_timeout=self.framework_tool_timeout
        )

        self._reap_task: asyncio.Task[None] | None = None
        self._closed = False

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
            logger.debug("读取 AstrBot 主配置失败（按未知处理）：%r", exc)
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

        astrbot_logger.info(
            "[bsk_browser] 已加载。bsk 路径=%s，最大会话=%d，仅管理员=%s",
            self.settings.bsk_path,
            self.settings.max_sessions,
            self.settings.admin_only,
        )

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
            logger.debug("is_admin() 调用失败，按非管理员处理：%r", exc)
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
    # 工具 1：打开网页
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_open")
    async def bsk_open(self, event: AstrMessageEvent, url: str, new_session: bool = False):
        """用浏览器打开一个网页，并返回页面标题和可交互元素。

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
            return (
                f"已执行 {action_norm}{note}。"
                "建议接着用 bsk_read 确认页面变化。"
            )
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

        if info.get("error"):
            lines.append(f"错误：{info['error']}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 工具 7：执行任意 JavaScript（高风险，默认关闭 + 强制管理员）
    # ------------------------------------------------------------------

    @filter.llm_tool("bsk_evaluate")
    async def bsk_evaluate(self, event: AstrMessageEvent, expression: str):
        """在当前页面里执行一段 JavaScript 表达式，并返回它的值。

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
