"""业务编排层 —— ``main.py`` 的每个工具函数都转调这里。

这一层把 ``runner`` / ``session`` / ``pages`` / ``shots`` 拼成完整用例，
并负责**所有面向模型的文案**（成功摘要、错误提示、下一步建议）。

设计原则：

- **不 import astrbot**：可以脱离框架单测；参数是普通 Python 值，
  不是 ``AstrMessageEvent``。事件相关的处理（权限、发送图片）留在 ``main.py``。
- **面向模型返回字符串**：工具调用的返回值最终会进 LLM 上下文，
  所以必须简洁、结构化、且**长度可控**（绝不能把整棵 VOM 树塞进去）。
- **错误不抛给模型看**：内部捕获 ``BskError``，转成"发生了什么 + 该怎么办"，
  让模型能自我纠正（例如会话过期后自动重建），而不是看到一段堆栈。

bsk 命令形式全部对照 `_raw-bsk-help.txt` 的真实帮助文本写，不凭记忆。
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import CODE_BROWSER_AMBIGUOUS, BskBrowserAmbiguous, BskError
from .journal import SessionJournal, default_journal_path
from .models import (
    BrowserInstance,
    ConsoleLog,
    NavigateResult,
    PageObservation,
    Screenshot,
)
from .pages import parse_observation, summarize
from .runner import BskRunner
from .session import SessionManager
from .shots import (
    cleanup_shots,
    encode_for_llm,
    make_shot_path,
    verify_shot,
)

logger = logging.getLogger(__name__)

__all__ = ["BskService", "ActionResult", "ShotPayload"]


# --- 各命令的**内置下限建议值**（秒）---
#
# 语义：这些值回答的是"这条命令**至少**需要多久"，它们是**下限**，不是最终超时。
# 最终超时只有一个来源 —— ``BskService._timeout()``，规则也只有一条：
#
#     最终超时 = max(内置下限, 用户配置的 command_timeout_sec)
#
# 依据：ARCHITECTURE §5 D6 与实机耗时（observe 0.06-0.09s、navigate 0.62-1.44s）。
# 下限的第一条原则是**外层超时必须大于 bsk 自身的 --timeout**，否则我们会先把它
# 掐掉，而它其实正要成功返回。

TIMEOUT_QUICK = 5.0
"""status / browsers / session list 这类只读命令的下限（它们本身几乎是瞬时的）。"""

TIMEOUT_OBSERVE = 15.0
"""observe 的下限。实测极快，但复杂页面会慢，留足余量。"""

TIMEOUT_ACTION = 30.0
"""click / fill / press 等交互的下限。与 bsk 自身默认 --timeout 30s 对齐。"""

TIMEOUT_NAVIGATE = 45.0
"""navigate 的下限。必须 **大于** bsk 自身默认的 30s。"""

TIMEOUT_SCREENSHOT = 30.0
"""视口截图的下限。"""

TIMEOUT_FULLPAGE = 180.0
"""全页截图的下限。bsk 自身默认 2 分钟，外层留余量。

⚠️ **光把本插件的 ``command_timeout_sec`` 调大是不够的**，全页截图要真正跑通，
需要用户**两处一起调大**：

1. 本插件的 ``command_timeout_sec``（WebUI → 插件配置，最多只能填 110 秒）；
2. **AstrBot 的 ``tool_call_timeout``** —— 默认 **120 秒**，见源码
   ``core/agent/run_context.py:19`` 与 ``core/config/agent_runner.py:33``；
   它是可调的，配置项位于 ``agent_runner.config.misc.tool_call_timeout``。

第 2 条才是真正的卡点：本插件给全页截图准备的下限是 180 秒，而
``command_timeout_sec`` 被夹在 110 秒以内（见 ``config.COMMAND_TIMEOUT_MAX_SEC``），
所以框架那 120 秒不放宽的话，bsk 永远没机会跑完 —— 用户看到的会是框架自己抛的
``tool ... execution timeout``，而不是我们那句"网页响应太慢"的友好提示。

注意这**不代表**这里的 180 会被框架的 120 改小：两者是"谁先到点谁说了算"，
我们只能保证自己不提前掐断，框架侧的上限必须由用户自己放宽。
这一点已写进 README 的已知限制与 ``_conf_schema.json`` 的配置说明。
"""


@dataclass(slots=True)
class ActionResult:
    """一次交互动作的结果，用于生成给模型的回执。"""

    action: str
    target: str = ""
    note: str = ""
    page_changed: bool = False


@dataclass(slots=True)
class ShotPayload:
    """截图的交付物。

    ``path`` 给 AstrBot 发送图片用；``for_llm`` 是给模型看的文本描述
    （**不要把图片二进制塞给模型**，除非用户显式要求看图）。
    """

    path: str
    width: int = 0
    height: int = 0
    byte_size: int = 0
    for_llm: str = ""
    warning: str = ""


class BskService:
    """把 bsk 能力编排成"打开 / 读取 / 操作 / 截图 / 关闭"五个用例。

    Args:
        settings: 已解析的强类型配置。
        runner: 子进程执行器。可注入假实现以便单测。
        sessions: 会话管理器。可注入假实现以便单测。
        journal: 会话所有权 journal（``bsk/journal.py``）。默认按
            ``settings.journal_path`` 构造，空值时用系统临时目录下的默认位置。
            只有显式传了 ``sessions`` 时才不会被用到。
    """

    def __init__(
        self,
        settings: Settings,
        runner: BskRunner | None = None,
        sessions: SessionManager | None = None,
        journal: SessionJournal | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or BskRunner(
            settings.bsk_path,
            default_timeout=settings.command_timeout_sec,
        )
        # journal 是"尽力而为"的辅助机制：构造它本身不做任何 IO（真正的读写
        # 发生在建/停会话和 recover_orphans 里，且那些路径全部吞异常），
        # 所以这里即便路径不可用也不会影响插件加载。
        self.journal = journal if journal is not None else self._make_journal()
        self.sessions = sessions or SessionManager(
            self.runner,
            settings,
            browser_probe=self.probe_browser,
            journal=self.journal,
        )

    def _make_journal(self) -> SessionJournal:
        """按配置构造 journal；拿不到配置时退回默认位置。"""
        configured = getattr(self.settings, "journal_path", "") or ""
        path = configured.strip() if isinstance(configured, str) else ""
        return SessionJournal(path or default_journal_path())

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _timeout(self, builtin: float) -> float:
        """把内置建议值与用户配置合成最终超时。

        规则只有一条（见模块顶部 ``TIMEOUT_*`` 的说明）::

            最终超时 = max(builtin, settings.command_timeout_sec)

        - ``builtin`` 是"这个命令至少需要多久"（下限），例如 ``navigate`` 是 45 秒，
          必须大于 bsk 自身的 ``--timeout`` 30 秒，否则我们会先把它掐掉。
        - ``settings.command_timeout_sec`` 是"用户愿意等多久"，已经由
          :mod:`bsk.config` 夹取到 ``[5, 110]``。
        - 取较大值，两者都不会被违背。

        Args:
            builtin: 该命令的内置下限建议值（``TIMEOUT_*`` 之一）。

        Returns:
            实际传给子进程的超时秒数。

        Note:
            这里用 ``getattr`` 而不是 ``self.settings.command_timeout_sec``：
            配置对象可能是测试桩、旧版本的 ``Settings``、或任何"同名属性"的
            鸭子类型对象（``SessionManager`` 出于同样的理由也这么做）。
            缺属性时**回退到内置下限**，绝不让超时变成 0 或抛 ``AttributeError``
            ——那会把一条本来能成功的命令直接掐死在起点。
        """
        configured = getattr(self.settings, "command_timeout_sec", None)
        if isinstance(configured, bool) or not isinstance(configured, (int, float)):
            # True/False 当超时没有意义；字符串/None 也不可信（正常入口
            # parse_settings 已经把它们收敛成 float 了，这里只是兜底）。
            return float(builtin)
        configured = float(configured)
        if not math.isfinite(configured) or configured <= 0:
            # nan / inf 参与 max() 的结果不可靠（inf 会变成"永不超时"）。
            return float(builtin)
        return max(float(builtin), configured)

    async def list_browsers(self) -> list[BrowserInstance]:
        """列出已连接的浏览器实例。

        用于：配置校验、错误提示里告诉用户有哪些可选实例。
        """
        result = await self.runner.run_or_raise(
            ["browsers", "--json"], timeout=self._timeout(TIMEOUT_QUICK)
        )
        raw = result.data
        if not isinstance(raw, list):
            return []
        return [BrowserInstance.from_json(item) for item in raw]

    def probe_browser(self) -> str:
        """给 ``SessionManager`` 用的同步探测钩子。

        只在用户没有显式配置 ``browser_instance_id`` 时才会被调用。
        返回空串表示"探测不出，让 bsk 自己选默认浏览器"。

        三条分支**必须**分清楚（这是本方法存在的意义）：

        - **探测本身失败**（bsk 没装、命令报错、输出不是 JSON）→ 返回空串。
          探测是优化，不该阻止会话创建，所以这里静默降级。
        - **恰好 1 个浏览器** → 返回它的 ``instance_id``，替用户省掉一步配置。
          这是最常见的场景（实测本机就是这一种），必须保持免配置可用。
        - **≥2 个浏览器** → 抛 :class:`~bsk.errors.BskBrowserAmbiguous`。
          **绝不能**返回空串：那等于让 bsk 自己随便挑一个，用户明明连着
          Edge + Chrome，插件却静默操作其中一个 —— 现象是"有时候对这个、
          有时候对那个"，无从排查。宁可明确报错，把每个实例的 ``instance_id``
          列出来让用户去配置。

        Raises:
            BskBrowserAmbiguous: 同时连着多个浏览器且用户没有指定用哪一个。

        Note:
            抛"歧义"异常的部分**刻意写在 try 之外**（见 ``_pick_browser_from_probe``）：
            上面那个 ``except Exception`` 是给"探测失败"用的，如果歧义异常也被
            它吞掉，就会退化成"静默随机选一个"，正是本次要消灭的行为。
        """
        try:
            # SessionManager 期望的是同步可调用对象，但我们的探测是异步的。
            # 为保持接口简单，这里用一个短超时的同步子进程调用。
            import subprocess

            exe = self.runner.resolve()
            proc = subprocess.run(
                [exe, "browsers", "--json"],
                capture_output=True,
                timeout=self._timeout(TIMEOUT_QUICK),
                check=False,
            )
            if proc.returncode != 0:
                return ""
            data = json.loads(proc.stdout.decode("utf-8", errors="replace") or "[]")
        except Exception as exc:  # noqa: BLE001 - 探测失败必须静默降级
            logger.debug("浏览器探测失败，交给 bsk 选默认：%r", exc)
            return ""
        # ★ 走到这里说明探测**成功**了，于是"多浏览器歧义"是一条确定的结论，
        #   必须让它抛出去（下面这个方法会抛），不能和上面的失败混为一谈。
        return self._pick_browser_from_probe(data)

    @staticmethod
    def _pick_browser_from_probe(data: Any) -> str:
        """把 ``bsk browsers --json`` 的载荷收敛成一个可用的 ``instance_id``。

        Args:
            data: 已解析的 JSON 载荷（任意类型 —— 它是外部输入）。

        Returns:
            选定的 ``instance_id``；没有可用的浏览器（0 个，或结构不认识）时
            返回空串，表示"不传 ``--browser``，交给 bsk 自己报错/选默认"。

        Raises:
            BskBrowserAmbiguous: 有 2 个及以上可用的浏览器实例。

        Note:
            **只有带非空 ``instance_id`` 的实例才算"可用"**：``instance_id``
            正是用户要填进配置的那个值，空 id 既不能选中、也无法让用户填写。
            所以"1 个可用 + N 个空 id"仍按唯一可用实例处理，而不是报一个
            列不出第二个实例的歧义错误（那样的提示会让用户莫名其妙）。
        """
        if not isinstance(data, list):
            return ""

        instances: list[BrowserInstance] = []
        seen: set[str] = set()
        for item in data:
            instance = BrowserInstance.from_json(item)
            if not instance.instance_id or instance.instance_id in seen:
                continue
            seen.add(instance.instance_id)
            instances.append(instance)

        if not instances:
            return ""
        if len(instances) == 1:
            # 只有一个浏览器时自动选中它 —— 这是最常见的情况，替用户省一步配置。
            return instances[0].instance_id
        raise BskService._ambiguous_browser_error(instances)

    @staticmethod
    def _ambiguous_browser_error(
        instances: list[BrowserInstance],
    ) -> BskBrowserAmbiguous:
        """构造"多个浏览器，无法确定用哪一个"的可操作错误。

        文案要求（面向模型，最终会由 ``main.py`` 的 ``except BskError``
        变成给 LLM 的字符串）：必须让模型知道**去哪个配置项填哪个值**，
        所以逐条列出 ``instance_id``，并给出配置项的名字。

        Note:
            展示用 ``BrowserInstance.display_name()``（label 为空时它自己会
            回退到 ``browser_name`` —— 实测 ``label`` 经常是空串），但
            ``instance_id`` 一定会出现在这一行里：label 非空时
            ``display_name()`` 只给 label，那样用户就看不到要填的值了。
        """
        lines = [f"检测到 {len(instances)} 个已连接的浏览器，无法确定用哪一个："]
        for instance in instances:
            lines.append(f"  - {BskService._describe_instance(instance)}")
        lines.append(
            "请在插件配置里把「目标浏览器」（browser_instance_id）填成上面其中一个 "
            "instance_id，然后重载插件再试。"
        )
        lines.append(
            "（在 AstrBot WebUI → 插件 → 本插件 → 配置 里改；"
            "也可以先在终端执行 `bsk browsers --json` 查看这些实例。）"
        )
        return BskBrowserAmbiguous(
            "多个已连接的浏览器且未配置 browser_instance_id："
            + "、".join(i.instance_id for i in instances),
            friendly="\n".join(lines),
            code=CODE_BROWSER_AMBIGUOUS,
        )

    @staticmethod
    def _describe_instance(instance: BrowserInstance) -> str:
        """把实例渲染成一行"名称 + instance_id"，供错误文案使用。"""
        name = instance.display_name().strip() or "未命名浏览器"
        if instance.instance_id not in name:
            # ``display_name()`` 在 label 为空时已经给出 "edge (c900a3da)"；
            # label 非空时只给 label，那样用户就看不到要填的值了，这里补上。
            name = f"{name} ({instance.instance_id})"
        if instance.unresponsive:
            # 只标注、不排除：它确实"连着"，用户需要知道该避开哪一个。
            name += "（此实例当前无响应，不建议选它）"
        return name

    def doctor_hint(self) -> str:
        """环境自检提示，用于错误信息里给用户可操作的下一步。"""
        return (
            "可以按顺序排查：\n"
            "  1. 终端执行 `bsk --version`，确认 bsk 已安装；\n"
            "  2. 终端执行 `bsk browsers --json`，确认浏览器扩展显示已连接；\n"
            "  3. 终端执行 `bsk doctor`，按它的提示修复。"
        )

    # ------------------------------------------------------------------
    # 用例 1：打开网页
    # ------------------------------------------------------------------

    async def open_page(self, key: str, url: str) -> tuple[NavigateResult, PageObservation]:
        """打开一个网址，并顺带读一次页面，让模型立刻看到内容。

        为什么顺带读：模型调用"打开网页"后，几乎总是紧接着要知道页面内容。
        分两次工具调用意味着两轮模型往返；合成一次能显著降低延迟和 token 消耗。

        Args:
            key: 会话键（umo 等）。
            url: 目标网址，必须已由上层校验过协议。

        Returns:
            ``(导航结果, 页面观察)``。
        """
        nav_result = await self.sessions.execute(
            key,
            lambda sid: ["navigate", url, "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_NAVIGATE),
        )
        nav = NavigateResult.from_json(nav_result.data)

        observation = await self.observe(key)
        return nav, observation

    # ------------------------------------------------------------------
    # 用例 2：读取页面
    # ------------------------------------------------------------------

    async def observe(self, key: str) -> PageObservation:
        """读取当前页面的语义结构（VOM）。

        只调 ``observe``，**不调 ``snapshot``** —— 实机验证两者输出逐字节相同，
        且都不带截图，同时调用纯属浪费一倍时间。

        Note:
            这是只读操作，所以 ``allow_uncertain=True``：即使上一次操作结果未知，
            看一眼页面也是安全的，而且正是用户判断"到底发生了什么"所需要的。
        """
        result = await self.sessions.execute(
            key,
            lambda sid: ["observe", "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_OBSERVE),
            allow_uncertain=True,
        )
        return parse_observation(result.data if isinstance(result.data, dict) else {})

    def render_page(self, observation: PageObservation) -> str:
        """把页面观察渲染成给模型看的文本，并做长度限制。"""
        return summarize(
            observation.text,
            observation.title,
            observation.refs,
            max_chars=self.settings.max_page_chars,
        )

    # ------------------------------------------------------------------
    # 用例 3：交互动作
    # ------------------------------------------------------------------

    async def act(
        self,
        key: str,
        action: str,
        *,
        target: str = "",
        value: str = "",
        values: list[str] | None = None,
        key_spec: str = "",
        delta_y: int = 0,
        delta_x: int = 0,
    ) -> ActionResult:
        """执行一次页面交互。

        命令形式严格对照 bsk 真实帮助文本：

        - ``click`` / ``hover`` / ``scroll-to`` / ``focus`` / ``blur``：
          target 是**位置参数**，可以是 ``@e3``、``e3`` 或 CSS 选择器。
        - ``fill``：需要 ``--value``，target 是位置参数。
        - ``press``：**按键是位置参数**（不是 target），可选 ``--ref`` 指定先聚焦的元素。
        - ``select``：需要 ``--value``（可重复），target 是位置参数。
        - ``wheel``：用 ``--delta-x`` / ``--delta-y``。

        Args:
            key: 会话键。
            action: 动作名，见 ``SUPPORTED_ACTIONS``。
            target: 元素引用或选择器。
            value: 单值（fill / select）。
            values: 多值（select 多选）。
            key_spec: 按键（press 专用），如 ``Enter``、``Ctrl+A``。
            delta_y: 垂直滚动量（wheel 专用），正数向下。
            delta_x: 水平滚动量（wheel 专用），正数向右。

        Returns:
            ``ActionResult``。

        Raises:
            BskError: 任何 bsk 层错误（含会话不确定态被拒）。
        """
        args = self._build_action_args(
            action,
            target=target,
            value=value,
            values=values,
            key_spec=key_spec,
            delta_y=delta_y,
            delta_x=delta_x,
        )
        result = await self.sessions.execute(
            key,
            lambda sid: [*args, "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_ACTION),
            # 写类动作绝不允许在不确定态下执行 —— 那可能造成重复点击/重复提交。
            allow_uncertain=False,
        )
        return self._render_action_result(action, target, result.data)

    @staticmethod
    def _build_action_args(
        action: str,
        *,
        target: str,
        value: str,
        values: list[str] | None,
        key_spec: str,
        delta_y: int,
        delta_x: int,
    ) -> list[str]:
        """把动作参数翻译成 bsk 命令行参数（不含 --session，由 session 层补）。

        Raises:
            BskError: 动作不支持或缺少必需参数。**在调用 bsk 前就报错**，
                避免把非法参数传给 bsk 换来一个难懂的 clap 错误。
        """
        from .errors import BskError as _Err

        if action == "click":
            if not target:
                raise _Err("click 需要 target", friendly="请提供要点击的元素编号（如 @e3）或 CSS 选择器。")
            return ["click", target]
        if action == "hover":
            if not target:
                raise _Err("hover 需要 target", friendly="请提供要悬停的元素编号或 CSS 选择器。")
            return ["hover", target]
        if action == "scroll_to":
            if not target:
                raise _Err("scroll_to 需要 target", friendly="请提供要滚动到的元素编号或 CSS 选择器。")
            return ["scroll-to", target]
        if action == "focus":
            if not target:
                raise _Err("focus 需要 target", friendly="请提供要聚焦的元素编号或 CSS 选择器。")
            return ["focus", target]
        if action == "blur":
            if not target:
                raise _Err("blur 需要 target", friendly="请提供要取消聚焦的元素编号或 CSS 选择器。")
            return ["blur", target]
        if action == "fill":
            if not target:
                raise _Err("fill 需要 target", friendly="请提供要输入的元素编号。")
            return ["fill", target, "--value", value]
        if action == "press":
            if not key_spec:
                raise _Err("press 需要 key", friendly="请提供要按下的键，例如 Enter、Escape、Ctrl+A。")
            # ★ press 的按键是**位置参数**；target 通过 --ref 传入（可选）。
            argv = ["press", key_spec]
            if target:
                argv.extend(["--ref", target])
            return argv
        if action == "select":
            if not target:
                raise _Err("select 需要 target", friendly="请提供下拉框的元素编号。")
            vals = values if values else ([value] if value else [])
            if not vals:
                raise _Err("select 需要 value", friendly="请提供要选中的选项值。")
            argv = ["select", target]
            for v in vals:
                argv.extend(["--value", v])
            return argv
        if action == "wheel":
            return ["wheel", "--delta-x", str(int(delta_x)), "--delta-y", str(int(delta_y))]
        if action == "navigate_back":
            return ["navigate", "back"]
        if action == "navigate_forward":
            return ["navigate", "forward"]
        if action == "reload":
            return ["reload"]
        if action == "wait_for_navigation":
            return ["wait-for-navigation"]

        raise _Err(
            f"不支持的动作：{action}",
            friendly=(
                f"不支持的动作「{action}」。可用的有："
                "click / fill / press / select / hover / scroll_to / wheel / "
                "focus / blur / navigate_back / navigate_forward / reload / "
                "wait_for_navigation。"
            ),
        )

    @staticmethod
    def _render_action_result(action: str, target: str, data: Any) -> ActionResult:
        """把 bsk 的返回转成一句人话。

        大多数交互命令的 JSON 结构没有稳定契约（不同命令字段不同），
        所以这里**只提取确定存在的字段**，其余交给模型去看下一次 observe。
        """
        note = ""
        changed = False
        if isinstance(data, dict):
            # 部分命令会带回导航/变化信息；有就利用，没有也不猜。
            if data.get("url"):
                note = f"当前地址：{data['url']}"
                changed = True
            elif data.get("changed") is True:
                changed = True
        return ActionResult(action=action, target=target, note=note, page_changed=changed)

    # ------------------------------------------------------------------
    # 用例 4：截图
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        key: str,
        *,
        full_page: bool = False,
    ) -> ShotPayload:
        """截图并返回可交付的文件路径。

        流程：分配唯一路径 → 调 bsk（``--out`` 明确指定）→ 校验魔数与大小。

        Note:
            必须自己指定 ``--out``：bsk 默认写到系统 TEMP 且文件名含时间戳，
            我们拿不到确定的路径，也管不了清理。
            ``--out`` 会**覆盖**已有文件，所以路径必须唯一。
        """
        directory = self.settings.screenshot_dir or self._default_shot_dir()
        out_path = make_shot_path(directory, key)

        timeout = self._timeout(TIMEOUT_FULLPAGE if full_page else TIMEOUT_SCREENSHOT)
        argv = ["screenshot", "--out", str(out_path)]
        if full_page:
            argv.append("--full-page")

        result = await self.sessions.execute(
            key,
            lambda sid: [*argv, "--session", sid, "--json"],
            timeout=timeout,
            # 截图是只读动作。
            allow_uncertain=True,
        )
        shot = Screenshot.from_json(result.data)
        if not shot.path:
            shot.path = str(out_path)

        ok, reason = verify_shot(shot)
        warning = "" if ok else f"截图可能不完整：{reason}"

        # 顺手清理旧图，避免磁盘无限增长（失败不影响主流程）。
        try:
            removed = cleanup_shots(directory)
            if removed:
                logger.debug("清理了 %d 张旧截图", removed)
        except Exception as exc:  # noqa: BLE001
            logger.debug("截图清理失败（忽略）：%r", exc)

        return ShotPayload(
            path=shot.path,
            width=shot.width,
            height=shot.height,
            byte_size=shot.byte_size,
            for_llm=self._describe_shot(shot, full_page),
            warning=warning,
        )

    def _describe_shot(self, shot: Screenshot, full_page: bool) -> str:
        """给模型的截图描述。**不返回图片本身**，只描述它。"""
        kind = "全页截图" if full_page else "视口截图"
        parts = [f"已生成{kind}"]
        if shot.width and shot.height:
            parts.append(f"{shot.width}x{shot.height} 像素")
        if shot.byte_size:
            parts.append(f"{shot.byte_size / 1024:.0f} KB")
        text = "，".join(parts) + "。图片已直接发给你。"
        if full_page:
            text += (
                "（全页截图较慢。若经常超时，需要同时调大两处：插件配置里的"
                "「单条命令超时」，以及 AstrBot 主配置里的 tool_call_timeout。）"
            )
        return text

    def _default_shot_dir(self) -> str:
        """未配置截图目录时的默认位置。

        用系统临时目录下的固定子目录，而不是当前工作目录 ——
        AstrBot 的工作目录可能是只读的或随启动方式变化。
        """
        import tempfile
        from pathlib import Path

        return str(Path(tempfile.gettempdir()) / "astrbot_bsk_shots")

    def shot_for_llm(self, payload: ShotPayload) -> str | None:
        """可选的降采样版本（data URL）。PIL 不可用时返回 None。

        给模型"看图"用的备选路径。默认不给 —— 图片很贵，
        而且 AstrBot 会把图片直接发给用户，模型通常不需要再看一遍。
        """
        try:
            return encode_for_llm(payload.path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("降采样失败（忽略）：%r", exc)
            return None

    # ------------------------------------------------------------------
    # 用例 5：诊断与关闭
    # ------------------------------------------------------------------

    async def status(self, key: str) -> dict[str, Any]:
        """收集诊断信息，用于 ``bsk_status`` 工具。

        设计目标：用户说"浏览器用不了"时，模型调用一次就能拿到足够信息
        判断问题出在哪一层（bsk 没装 / 扩展没连 / 会话没了）。
        """
        info: dict[str, Any] = {
            "bsk_path": self.settings.bsk_path,
            "admin_only": self.settings.admin_only,
            "max_sessions": self.settings.max_sessions,
        }
        try:
            exe = self.runner.resolve()
            info["bsk_resolved"] = exe
        except BskError as exc:
            info["bsk_resolved"] = None
            info["error"] = exc.friendly
            return info

        try:
            result = await self.runner.run_or_raise(
                ["status", "--json"], timeout=self._timeout(TIMEOUT_QUICK)
            )
            data = result.data if isinstance(result.data, dict) else {}
            info["daemon"] = {
                "version": data.get("daemon_version"),
                "pid": data.get("pid"),
                "uptime_secs": data.get("uptime_secs"),
            }
            browsers = data.get("browsers")
            if isinstance(browsers, list):
                info["browsers"] = [
                    {
                        "instance_id": b.get("instance_id"),
                        "name": b.get("browser_name"),
                        "label": b.get("label"),
                        "unresponsive": b.get("unresponsive"),
                        "version_skew": b.get("version_skew"),
                    }
                    for b in browsers
                    if isinstance(b, dict)
                ]
        except BskError as exc:
            info["error"] = exc.friendly

        info["sessions"] = self.sessions.stats()
        return info

    async def close_session(self, key: str) -> bool:
        """关闭当前会话。返回是否真的关了一个。"""
        return await self.sessions.release(key)

    async def shutdown(self) -> int:
        """关闭全部会话。插件 ``terminate()`` 调用。"""
        return await self.sessions.release_all()

    async def recover_orphans(self) -> int:
        """清理上一次进程遗留的、仍然活着的自己的会话。

        插件 ``initialize()`` 调用。对应场景：AstrBot 被强杀时 ``terminate()``
        不会执行，而 bsk daemon 独立于 AstrBot 继续活着，于是桌面上留下没人管的
        浏览器窗口；下次启动靠 journal 记录把它们按 id 精确停掉。

        Returns:
            实际清理掉的会话数量。

        Note:
            本方法**绝不抛异常**（转发对象的实现已经保证），所以调用方不必
            再套一层 try/except 来防止插件加载失败。
        """
        return await self.sessions.recover_orphans()

    # ------------------------------------------------------------------
    # 页面上报（给模型一段紧凑的环境描述）
    # ------------------------------------------------------------------

    async def read_console(self, key: str, since: int = 0) -> ConsoleLog:
        """读取控制台日志。

        Note:
            返回里 ``entries`` 字段**可能整个不存在**（实测），
            ``ConsoleLog.from_json`` 已经处理成空列表。
        """
        result = await self.sessions.execute(
            key,
            lambda sid: ["console", "--since", str(int(since)), "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_QUICK),
            allow_uncertain=True,
        )
        return ConsoleLog.from_json(result.data if isinstance(result.data, dict) else {})

    async def read_network(self, key: str, since: int = 0) -> ConsoleLog:
        """读取网络请求日志。

        ⚠️ 实测 ``url`` 字段可能内联巨大的 ``data:image/png;base64,...``，
        渲染给模型时**必须截断**，否则会瞬间吃光上下文。
        """
        result = await self.sessions.execute(
            key,
            lambda sid: ["network", "--since", str(int(since)), "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_QUICK),
            allow_uncertain=True,
        )
        return ConsoleLog.from_json(result.data if isinstance(result.data, dict) else {})

    @staticmethod
    def render_console(log: ConsoleLog, *, limit: int = 50, url_max: int = 200) -> str:
        """把控制台/网络日志渲染成给模型的文本，做双重截断。

        Args:
            log: 日志对象。
            limit: 最多展示多少条。
            url_max: 单条 URL 的最大字符数（防 base64 内联把上下文撑爆）。
        """
        if not log.entries:
            return "没有捕获到任何日志。"
        lines: list[str] = []
        for entry in log.entries[:limit]:
            url = (entry.url or "")[:url_max]
            if entry.kind == "failure":
                # failure 条目**没有 status 字段**，只有 error_text。
                lines.append(f"[{entry.sequence}] 失败 {entry.method} {url}")
            elif entry.level:
                lines.append(f"[{entry.sequence}] {entry.level}: {entry.text[:300]}")
            else:
                lines.append(
                    f"[{entry.sequence}] {entry.method} {entry.status} {url}"
                )
        if len(log.entries) > limit:
            lines.append(f"……还有 {len(log.entries) - limit} 条未显示")
        if log.truncated:
            lines.append("（bsk 报告日志被截断）")
        return "\n".join(lines)

    async def sleep_ms(self, ms: int) -> None:
        """等待。用于页面异步加载后重试读取。"""
        import asyncio

        await asyncio.sleep(max(0, ms) / 1000.0)

    def now(self) -> float:
        """单调时钟（测试可替换）。"""
        return time.monotonic()
