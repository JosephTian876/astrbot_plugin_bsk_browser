"""astrbot_plugin_bsk_browser —— 框架适配层。

**这是整个插件里唯一允许 import astrbot 的文件**（其余逻辑全在 ``bsk/`` 包里，
可以脱离框架独立单测）。它只做四件事：

1. 持有 ``BskService``（业务编排）与配置；
2. 把 ``@filter.llm_tool`` 的调用参数解包后转给 service；
3. 做**权限校验**（默认仅管理员）；
4. 把图片结果 yield 给用户。

为什么不把逻辑写在这里：AstrBot 要求 ``@filter.llm_tool`` 装饰的函数必须定义
在插件主模块（``main.py``）里 —— 框架用 ``handler.__module__ == metadata.module_path``
**精确匹配**来决定是否给工具绑定 ``self`` 实例，定义在子模块里的工具会被框架
认为存在、却永远拿不到 self，调用时静默失败。所以这个约束无法绕过，
但可以通过"这里只做薄适配"来控制它的体积。

几个必须遵守的框架约束（均来自 AstrBot 4.28.1 源码实证）：

- **绝不定义 ``__del__``**：``star_manager._terminate_plugin`` 是
  ``if "__del__" in cls.__dict__: ... elif "terminate" in cls.__dict__: ...``，
  定义了 ``__del__`` 会让 ``terminate()`` **永不执行**，导致浏览器会话泄漏。
- ``terminate`` / ``initialize`` 必须定义在**本类**上（框架用 ``cls.__dict__`` 判断）。
- ``__init__`` 的 ``config`` 必须有默认值（框架在无配置时只传 ``context``）。
- docstring 的 ``Args:`` 段是工具参数 schema 的**唯一来源**，不读函数类型注解。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .bsk.config import Settings, parse_settings, validate_settings
from .bsk.errors import BskError
from .bsk.service import BskService
from .bsk.session import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["BskBrowserPlugin"]

# 后台空闲回收的检查间隔（秒）。
# 不需要很频繁：会话空闲阈值是分钟级的，30 秒粒度足够，且开销可忽略。
IDLE_REAP_INTERVAL_SEC = 30.0


@register(
    "astrbot_plugin_bsk_browser",
    "yourname",
    "用 bsk（BrowserSkill）操作你自己已登录的浏览器：打开网页、读取内容、点击输入、截图。",
    "0.1.0",
)
class BskBrowserPlugin(Star):
    """把本机 bsk CLI 包装成 LLM 可调用的浏览器操作能力。

    设计要点：

    - **权限默认仅管理员**（``admin_only``，可在插件配置里关闭）；
    - **按聊天会话隔离浏览器会话**（``session_scope``：一个群一条 / 每人一条）；
    - 所有 bsk 细节都在 ``bsk/`` 包里，这里只做参数解包与结果包装。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.settings: Settings = parse_settings(config)

        self.service = BskService(self.settings)

        self._reap_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件加载后调用：提示配置问题，并启动空闲会话回收任务。"""
        # 配置问题只提示、不阻断 —— 用户可能还没装 bsk，不该因此让插件加载失败。
        for problem in validate_settings(self.settings):
            astrbot_logger.warning("[bsk_browser] 配置提醒：%s", problem)

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

        astrbot_logger.info(
            "[bsk_browser] 已加载。bsk 路径=%s，最大会话=%d，仅管理员=%s",
            self.settings.bsk_path,
            self.settings.max_sessions,
            self.settings.admin_only,
        )

    async def terminate(self) -> None:
        """插件卸载/停用时调用：关闭所有浏览器会话。

        ⚠️ 这个方法**必须**能跑完 —— 如果不关会话，用户桌面上会留下无人管的
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

        为什么要自己回收：bsk 自己会在会话空闲 5 分钟后回收，但**回收不保证
        归还借用的标签页**。我们提前一点主动 stop，标签页归还有机会走完整流程。

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

        两套机制**取严**：

        1. 插件自己的 ``admin_only`` / ``allowed_users``（在插件配置页设置）；
        2. AstrBot 原生的 ``tool_permissions``（WebUI → 扩展 → 组件）。

        第 2 条由框架在调用工具前**自动**执行，这里不重复实现；这里只管第 1 条。
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

        Args:
            action(string): 要执行的动作，可选值：click(点击)、fill(输入文字)、press(按键)、select(下拉框选择)、hover(鼠标悬停)、scroll_to(滚动到元素)、wheel(滚动页面)、focus(聚焦)、blur(失焦)、reload(刷新)、navigate_back(后退)、navigate_forward(前进)
            target(string): 元素编号（如 @e3）或 CSS 选择器。click/fill/select/hover/scroll_to/focus/blur 必填；press 可选（填了就表示先聚焦该元素再按键）
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

        # ★ 先把图片 yield 给用户，再 yield 文本回灌给模型。
        #   注意 async generator 里**不能**写 `return <值>`（会是 SyntaxError），
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
            lines.append("bsk 可执行文件：**没找到**")
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
                "已连接的浏览器：**没有**\n"
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
