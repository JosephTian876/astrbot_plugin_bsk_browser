"""业务编排层 —— ``main.py`` 的每个工具函数都转调这里。

这一层把 ``runner`` / ``session`` / ``pages`` / ``shots`` 拼成完整用例，
并负责所有面向模型的文案（成功摘要、错误提示、下一步建议）。

设计原则：

- 不 import astrbot：可以脱离框架单测；参数是普通 Python 值，
  不是 ``AstrMessageEvent``。事件相关的处理（权限、发送图片）留在 ``main.py``。
- 面向模型返回字符串：工具调用的返回值最终会进 LLM 上下文，
  所以必须简洁、结构化、且长度可控（绝不能把整棵 VOM 树塞进去）。
- 错误不抛给模型看：内部捕获 ``BskError``，转成"发生了什么 + 该怎么办"，
  让模型能自我纠正（例如会话过期后自动重建），而不是看到一段堆栈。

bsk 命令形式全部对照 `_raw-bsk-help.txt` 的真实帮助文本写，不凭记忆。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from .config import (
    FRAMEWORK_TIMEOUT_FLOOR_SEC,
    FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC,
    Settings,
    as_timeout_seconds,
)
from .errors import (
    CODE_BROWSER_AMBIGUOUS,
    BskBrowserAmbiguous,
    BskError,
    BskProtocolError,
)
from .journal import SessionJournal
from .logger import NULL_LOGGER, LoggerLike
from .models import (
    BrowserInstance,
    BskSession,
    ConsoleLog,
    EvaluateResult,
    NavigateResult,
    PageObservation,
    Screenshot,
)
from .pages import parse_observation, summarize
from .paths import default_journal_path, default_shot_dir
from .runner import BskRunner
from .session import SessionManager
from .shots import (
    cleanup_shots,
    make_shot_path,
    verify_shot,
)

__all__ = ["BskService", "ActionResult", "ShotPayload"]


# --- 各命令的内置下限建议值（秒）---
#
# 语义：这些值回答的是"这条命令至少需要多久"，它们是下限，不是最终超时。
# 最终超时只有一个来源 —— ``BskService._timeout()``，规则只有两条：
#
#     最终超时 = min( max(内置下限, 用户配置的 command_timeout_sec),
#                     框架上限 - 安全余量 )
#
# 第一条是"我们愿意等多久"，第二条是"别被框架从外面掐断"（框架上限读不到时
# 第二条整个不生效，行为与加它之前完全一致）。
#
# 依据：ARCHITECTURE §5 D6 与实机耗时（observe 0.06-0.09s、navigate 0.62-1.44s）。
# 下限的第一条原则是外层超时必须大于 bsk 自身的 --timeout，否则我们会先把它
# 掐掉，而它其实正要成功返回。

TIMEOUT_QUICK = 5.0
"""status / browsers / session list 这类只读命令的下限（它们本身几乎是瞬时的）。"""

TIMEOUT_OBSERVE = 15.0
"""observe 的下限。实测极快，但复杂页面会慢，留足余量。"""

TIMEOUT_ACTION = 30.0
"""click / fill / press 等交互的下限。与 bsk 自身默认 --timeout 30s 对齐。"""

TIMEOUT_EVALUATE = 45.0
"""``evaluate`` 的下限。必须大于 bsk 自身默认的 ``--timeout`` 30s。

与 ``TIMEOUT_NAVIGATE`` 同样的理由（见本模块顶部"下限的第一条原则"）：
``bsk evaluate`` 自己的 ``--timeout`` 默认就是 30s（实测帮助文本
``--timeout <TIMEOUT>  Hard timeout (default 30s)``）。我们外层只给 30s 的话，
会在它正要成功返回的瞬间把它掐掉 —— 而超时表现为"这段 JS 跑了太久"，
用户完全看不出其实是我们的超时预算算错了。所以下限取 45s：比 bsk 自己的
超时大 15s，留足它把结果写回 stdout 的时间。

Note:
    这里不给 bsk 传 ``--timeout``：让它保持自己的 30s 默认值。
    这样"bsk 内部超时"与"我们的外层超时"有明确的先后关系（30s 先到，
    报 ``exit=4`` 并带友好提示；45s 只是防止 bsk 卡死的兜底），
    比把两个超时设成同一个值更容易推理。
"""

TIMEOUT_NAVIGATE = 45.0
"""navigate 的下限。必须 大于 bsk 自身默认的 30s。"""

TIMEOUT_SCREENSHOT = 30.0
"""视口截图的下限。"""

TIMEOUT_FULLPAGE = 120.0
"""全页截图的默认内置下限（用户没配 ``fullpage_timeout_sec`` 时用它）。

⚠️ 真正的取值走 ``settings.fullpage_timeout_sec``（默认值就是这里这个数，
见 :data:`bsk.config.DEFAULT_FULLPAGE_TIMEOUT_SEC`）。本常量现在只承担两个角色：

1. 配置缺失/畸形时的兜底；
2. 启动告警的阈值（"框架上限是否小于全页截图的默认预算"）。

数值从 180 改成 120 是用户拍的板：180 秒对实测的 11.72 秒过于宽松
（15 倍余量），而 120 秒既贴着"实测的约 10 倍"这个合理余量，又与 AstrBot
框架的默认 ``tool_call_timeout``（120 秒）对齐 —— 用户想表达的就是
"整页截图最多等 2 分钟"。

先看清实测数据再决定要不要动配置（本机实测，长页面是 Wikipedia 长条目）::

    短页面 example.com   视口截图  0.12s
    短页面 example.com   全页截图  2.91s
    长页面 Wikipedia     全页截图  11.72s / 11.11s / 10.91s（1820x11741，4.5MB）

最慢的一次只用了 11.72 秒，距 120 秒上限约 1/10。所以"必须两处一起调大才能用
全页截图"是多余的警告，默认配置下通常什么都不用改（此前的文档说法就是错的，
已更正）。

真正需要处理的是另一件事：别让框架从外面把我们掐断。框架上限一旦小于我们的
最终超时，用户看到的会是框架抛的英文 ``tool <name> execution timeout after N
seconds.``，而不是我们写的中文提示，而且"会话可能留下未完成状态"会被完全掩盖。
所以 ``BskService._timeout`` 会把最终值钳到"框架上限 − 5 秒"之下
（见 :data:`bsk.config.FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC`）。框架上限读不到时
不钳制 —— 那说明我们拿不到事实，就不该凭猜测改行为。

⚠️ 注意这个默认值恰好等于框架的默认上限（都是 120），所以默认配置下钳制就会
生效：实际拿到的是 115 秒。这是正确行为，不是缺陷 —— 它保证超时由插件先报出
中文提示。启动时会打印一条解释性告警（见 ``BskService.startup_warnings``）。
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
    （不要把图片二进制塞给模型，除非用户显式要求看图）。
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
        framework_tool_timeout: 框架（AstrBot）自己的单次工具调用超时（秒），
            由 ``main.py`` 读 ``context.get_config()`` 后传入（见
            :func:`bsk.config.read_framework_tool_timeout`）。``None`` / 非法值
            都表示"未知"，此时不做任何钳制，行为与没有这个参数时完全一致。
        logger: 可选的日志接口（见 ``bsk/logger.py`` 的 ``LoggerLike``）。
            由 ``main.py`` 从 ``astrbot.api`` 取来后注入，并**向下传给**
            ``SessionManager`` 与 ``SessionJournal`` —— 它们各自的日志也要
            走到插件专属 logger 上。不传（或传 ``None``）时全部走
            ``NULL_LOGGER``：不产生输出，也绝不抛异常。本层不 import astrbot，
            也不 import 内置 ``logging``（分层约束，有静态测试守着）。
    """

    def __init__(
        self,
        settings: Settings,
        runner: BskRunner | None = None,
        sessions: SessionManager | None = None,
        journal: SessionJournal | None = None,
        framework_tool_timeout: float | None = None,
        logger: LoggerLike | None = None,
    ) -> None:
        self.settings = settings
        self._logger = logger or NULL_LOGGER
        # 先落框架上限再建 runner/sessions：它是纯数据，不产生任何副作用。
        self.framework_tool_timeout = framework_tool_timeout
        self.runner = runner or BskRunner(
            settings.bsk_path,
            default_timeout=settings.command_timeout_sec,
        )
        # journal 是"尽力而为"的辅助机制：解析默认位置时会试着建一次数据目录
        # （判定"能不能用"的唯一可靠办法），但失败就降级到临时目录，
        # 绝不抛异常、也绝不影响插件加载。真正的读写发生在建/停会话和
        # recover_orphans 里，且那些路径全部吞异常。
        self.journal = journal if journal is not None else self._make_journal()
        self.sessions = sessions or SessionManager(
            self.runner,
            settings,
            browser_probe=self.probe_browser,
            journal=self.journal,
            logger=self._logger,
        )

    def _make_journal(self) -> SessionJournal:
        """按配置构造 journal；没配就用插件数据目录下的默认位置。

        用户显式填的 ``journal_path`` 优先级最高，注入的 ``data_dir`` 只在它为空
        时才起作用（默认位置与降级顺序见 ``bsk/paths.py``）。日志同样注入下去，
        让 journal 的读写失败能报在插件自己的 logger 上。
        """
        configured = getattr(self.settings, "journal_path", "") or ""
        path = configured.strip() if isinstance(configured, str) else ""
        if path:
            return SessionJournal(path, logger=self._logger)
        data_dir = getattr(self.settings, "data_dir", "") or ""
        return SessionJournal(
            default_journal_path(data_dir, self._logger), logger=self._logger
        )

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _timeout(self, builtin: float) -> float:
        """把内置建议值、用户配置与框架上限合成最终超时。

        规则只有两条（见模块顶部 ``TIMEOUT_*`` 的说明）::

            最终超时 = min( max(builtin, settings.command_timeout_sec),
                            框架上限 - FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC )

        - ``builtin`` 是"这个命令至少需要多久"（下限），例如 ``navigate`` 是 45 秒，
          必须大于 bsk 自身的 ``--timeout`` 30 秒，否则我们会先把它掐掉。
        - ``settings.command_timeout_sec`` 是"用户愿意等多久"，已经由
          :mod:`bsk.config` 夹取到 ``[5, 110]``。
        - 取较大值，两者都不会被违背。
        - 最后再与"框架上限 − 安全余量"取较小值：插件要在框架动手之前自己超时，
          这样用户看到的是我们写的中文提示（含"该改哪个配置"），而不是框架抛的
          英文 ``tool <name> execution timeout after N seconds.``。

        框架上限读不到时（``None``）第二条整个不生效，结果与加这条规则之前
        逐字节相同。理由：读不到说明我们没有事实依据，凭猜测缩短用户愿意等待的
        时间比"可能被框架掐断"更糟。

        余量的作用与取值理由见 :data:`bsk.config.FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC`：
        框架从它开始等的那一刻计时，而 bsk 返回后我们还要校验截图、渲染中文、
        序列化结果、交给框架发图片 —— 这些都算在同一个窗口里。留 5 秒就不会出现
        "bsk 刚好返回、框架同时掐断"的临界情况。

        钳制用的那个"上限"本身不会低于 :data:`bsk.config.FRAMEWORK_TIMEOUT_FLOOR_SEC`：
        超时太小比超时更糟（每条命令都在起点被掐死，现象是"插件完全不能用"）。
        用户把框架上限设得极小时，我们宁可自己多等几秒、让它去报那句英文错误 ——
        也不会把命令压成"秒失败"。注意下限加在上限上，所以它只会在真正发生
        钳制时起作用，不会把本来就短的命令（例如 5 秒的只读命令）抬高。

        Args:
            builtin: 该命令的内置下限建议值（``TIMEOUT_*`` 之一）。

        Returns:
            实际传给子进程的超时秒数。

        Note:
            这里用 ``getattr`` 而不是 ``self.settings.command_timeout_sec``：
            配置对象可能是测试桩、旧版本的 ``Settings``、或任何"同名属性"的
            鸭子类型对象（``SessionManager`` 出于同样的理由也这么做）。
            缺属性时回退到内置下限，绝不让超时变成 0 或抛 ``AttributeError``
            ——那会把一条本来能成功的命令直接掐死在起点。
        """
        base = self._base_timeout(builtin)
        ceiling = self.framework_timeout_ceiling()
        if ceiling is None:
            return base
        # 框架上限已知：把最终值压到它之下（上限自己已含下限保护）。
        return min(base, ceiling)

    def _base_timeout(self, builtin: float) -> float:
        """只按"内置下限 vs 用户配置"算出的基础超时（钳制之前的值）。"""
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

    def fullpage_budget(self) -> float:
        """全页截图的"用户愿意等多久"（秒），来自 ``settings.fullpage_timeout_sec``。

        这是用户可调的那一项（WebUI → 插件配置 → 整页截图超时，默认 120，
        可填 30–600）。它只作用于 ``screenshot --full-page`` 那一条命令；
        视口截图不读它，而是走 :meth:`_timeout` 的常规规则（内置下限
        :data:`TIMEOUT_SCREENSHOT` 与 ``command_timeout_sec`` 取较大者）。

        Returns:
            用户配置的秒数；配置对象缺这个字段、或取值非法（None / 字符串 /
            bool / nan / inf / ≤0）时回退到 :data:`TIMEOUT_FULLPAGE`（默认值）。
            与 :meth:`_timeout` 一样用 ``getattr``：配置对象可能是测试桩或旧版本的
            ``Settings``，"缺字段"必须是安全的降级而不是 ``AttributeError``。

        Note:
            这里只读配置，不做钳制 —— 钳制统一在 :meth:`_timeout` 里做，
            保证"最终值永远低于框架上限"这条不变量只有一个实现点。
        """
        configured = getattr(self.settings, "fullpage_timeout_sec", None)
        if isinstance(configured, bool) or not isinstance(configured, (int, float)):
            return float(TIMEOUT_FULLPAGE)
        configured = float(configured)
        if not math.isfinite(configured) or configured <= 0:
            return float(TIMEOUT_FULLPAGE)
        return configured

    def framework_timeout_ceiling(self) -> float | None:
        """框架允许我们等多久 —— 已经扣掉安全余量；未知时返回 ``None``。

        返回值直接可以当作"最终超时的上界"用（见 :meth:`_timeout`）。

        Returns:
            ``框架上限 - FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC``，且不低于
            :data:`FRAMEWORK_TIMEOUT_FLOOR_SEC`；框架上限未知时 ``None``。
        """
        limit = self.framework_tool_timeout
        if limit is None:
            return None
        return max(FRAMEWORK_TIMEOUT_FLOOR_SEC, limit - FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC)

    @property
    def framework_tool_timeout(self) -> float | None:
        """框架（AstrBot）自己的单次工具调用超时（秒），读不到时 ``None``。

        由 ``main.py`` 在构造时通过 ``framework_tool_timeout=`` 注入
        （它把 ``self.context.get_config()`` 的结果交给
        :func:`bsk.config.read_framework_tool_timeout` 解析）。
        本层不 import astrbot，只认这个已经归一化好的数值。

        所有"长得像配置"的东西都可能被塞进来（测试桩、旧对象、字符串），
        所以这里再过一遍 :func:`bsk.config.as_timeout_seconds`：拿不准就是
        ``None`` = 未知 = 不钳制，绝不让它把插件搞崩。
        """
        return as_timeout_seconds(getattr(self, "_framework_tool_timeout", None))

    @framework_tool_timeout.setter
    def framework_tool_timeout(self, value: Any) -> None:
        """注入框架上限（``None`` / 非法值 = 未知）。"""
        self._framework_tool_timeout = as_timeout_seconds(value)

    def startup_warnings(self) -> list[str]:
        """插件启动时要打印的告警（中文，包含"该去改哪个配置项"）。

        只在真有风险时才有内容：框架上限已知、且小于或等于全页截图的预算
        （``fullpage_budget()``，默认 :data:`TIMEOUT_FULLPAGE` = 120 秒）时提示。

        默认配置下这条必然出现（本机实测框架上限 = 120，与默认预算相等），
        这是正确的：它如实说明"整页截图的预算顶到了框架上限，会被钳成 115 秒"。
        措辞上刻意不写成报错：默认值对实测的 11.72 秒已有约 10 倍余量，
        绝大多数用户什么都不用做。只有极慢的页面/网络才需要同时调大两处。

        为什么阈值取"≤ 预算"而不是"≤ 用户配的 command_timeout_sec"：全页截图走的是
        自己那一项（``fullpage_timeout_sec``），与 ``command_timeout_sec`` 的上界
        （110 秒）无关。反过来，框架上限大于预算时（或读不到时）一条都不打印。

        Returns:
            告警文案列表（可能为空）。
        """
        limit = self.framework_tool_timeout
        budget = self.fullpage_budget()
        if limit is None or limit > budget:
            return []
        ceiling = self.framework_timeout_ceiling()
        clamped = "未知" if ceiling is None else f"{ceiling:g} 秒"
        return [
            "整页截图的超时预算（"
            f"{budget:g} 秒）已经顶到 AstrBot 单次工具调用的上限（{limit:g} 秒），"
            f"所以实际生效的是 {clamped}（框架上限减 "
            f"{FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC:g} 秒的安全余量）。"
            "这不是错误，也不影响正常使用：实测长页面整页截图约 11 秒、"
            "短页面约 3 秒（默认 120 秒的预算就有约 10 倍余量），"
            "默认配置下什么都不用改。"
            "这样安排的好处是：万一真的超时，会由插件先报出中文提示"
            "（「网页响应太慢……」），而不是被框架从外面掐断成一句英文的 "
            "execution timeout。"
            "只有在页面/网络特别慢时才需要放宽 —— 那时两处都要动：把 AstrBot 主配置里的 "
            "`agent_runner.config.misc.tool_call_timeout` 调大（当前 "
            f"{limit:g} 秒，改完重启 AstrBot），插件这边再按需调大"
            "「整页截图超时」（fullpage_timeout_sec）。"
        ]

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

        三条分支必须分清楚（这是本方法存在的意义）：

        - 探测本身失败（bsk 没装、命令报错、输出不是 JSON）→ 返回空串。
          探测是优化，不该阻止会话创建，所以这里静默降级。
        - 恰好 1 个浏览器 → 返回它的 ``instance_id``，替用户省掉一步配置。
          这是最常见的场景（实测本机就是这一种），必须保持免配置可用。
        - ≥2 个浏览器 → 抛 :class:`~bsk.errors.BskBrowserAmbiguous`。
          绝不能返回空串：那等于让 bsk 自己随便挑一个，用户明明连着
          Edge + Chrome，插件却静默操作其中一个 —— 现象是"有时候对这个、
          有时候对那个"，无从排查。宁可明确报错，把每个实例的 ``instance_id``
          列出来让用户去配置。

        Raises:
            BskBrowserAmbiguous: 同时连着多个浏览器且用户没有指定用哪一个。

        Note:
            抛"歧义"异常的部分刻意写在 try 之外（见 ``_pick_browser_from_probe``）：
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
            self._logger.debug("浏览器探测失败，交给 bsk 选默认：%r", exc)
            return ""
        # 走到这里说明探测成功了，于是"多浏览器歧义"是一条确定的结论，
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
            只有带非空 ``instance_id`` 的实例才算"可用"：``instance_id``
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
        变成给 LLM 的字符串）：必须让模型知道去哪个配置项填哪个值，
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

        只调 ``observe``，不调 ``snapshot`` —— 实机验证两者输出逐字节相同，
        且都不带截图，同时调用纯属浪费一倍时间。

        Note:
            这是只读操作，所以 ``allow_uncertain=True``：即使上一次操作结果未知，
            看一眼页面也是安全的，而且正是用户判断"到底发生了什么"所需要的。

        Note:
            如果这条命令触发了会话重建（原会话被 bsk 空闲回收），
            返回的页面会是空白页 —— 实测确认：重建后 ``RootWebArea``
            没有标题、``text`` 只剩几十个字符、``ref_count=0``。

            这一点必须让模型知道，否则它会把"空白页"当成"这个网页本来就
            没内容"来回答用户。所以重建时往 ``text`` 前面插一句明确提示
            （``text`` 是模型唯一会读到的字段）。
        """
        counters_before = self.sessions.stats().get("counters", {})
        rebuilds_before = counters_before.get("not_found_rebuilds", 0)

        result = await self.sessions.execute(
            key,
            lambda sid: ["observe", "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_OBSERVE),
            allow_uncertain=True,
        )
        observation = parse_observation(
            result.data if isinstance(result.data, dict) else {}
        )

        counters_after = self.sessions.stats().get("counters", {})
        rebuilds_after = counters_after.get("not_found_rebuilds", 0)
        if rebuilds_after > rebuilds_before:
            # 会话是刚刚重建的，页面状态没有恢复 —— 补一句让模型别误判。
            observation.text = (
                "（注意：浏览器会话刚刚因空闲过久被回收并自动重建，"
                "当前是新开的空白页，之前打开的网页和填过的内容已经丢失。"
                "如果用户之前在浏览某个页面，需要重新用 bsk_open 打开那个网址。）\n"
                + observation.text
            )
            self._logger.info("会话 %s 在读取时被重建，已在返回内容里标注页面已重置", key)

        return observation

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
          target 是位置参数，可以是 ``@e3``、``e3`` 或 CSS 选择器。
        - ``fill``：需要 ``--value``，target 是位置参数。
        - ``press``：按键是位置参数（不是 target），可选 ``--ref`` 指定先聚焦的元素。
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
            BskError: 动作不支持或缺少必需参数。在调用 bsk 前就报错，
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
            # press 的按键是位置参数；target 通过 --ref 传入（可选）。
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
        所以这里只提取确定存在的字段，其余交给模型去看下一次 observe。
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
    # 用例 6：执行任意 JavaScript（高风险，需显式开启）
    # ------------------------------------------------------------------

    async def evaluate(self, key: str, expression: str) -> EvaluateResult:
        """在页面里执行一段 JavaScript 表达式，返回它的值。

        ⚠️ 这是本插件风险最高的能力：它在用户已登录的页面里跑任意脚本。
        ``main.py`` 那边有三条权限分支挡着（独立开关 + 强制管理员），本方法
        只负责"把命令发出去并正确判读结果"。

        命令形式严格对照 bsk 真实帮助文本::

            bsk evaluate [OPTIONS] --session <SESSION> <EXPRESSION>

        - ``EXPRESSION`` 是位置参数（不是 ``--expression``）；
        - ``--await-promise`` 默认 true（Promise 会被 await，实测
          ``Promise.resolve('resolved-value')`` 直接返回值本身）；
        - ``--return-by-value`` 默认 true（拿到的就是普通 JSON 值）；
        - ``--timeout`` 默认 30s —— 我们不覆盖它，理由见 ``TIMEOUT_EVALUATE``。

        本方法存在的最重要理由：JS 抛异常时 bsk 的退出码仍然是 0。

        实测（原文见 ``models.EvaluateError`` 的 docstring）：:

            $ bsk evaluate "throw new Error('boom')" --session ycvt --json
            { "ok": false, "tab_id": ..., "error": {"text": "Error: boom", ...} }
            exit=0

        所以只看退出码会把失败当成功，然后拿着一个 ``None`` 或错误的
        ``value`` 去回答用户。判成败必须读返回 JSON 里的 ``ok`` 字段。
        ``SessionManager.execute`` 不会替我们做这件事（它按退出码判成败，
        对 bsk 的其他命令都是对的），所以这一步必须在这里做。

        Args:
            key: 会话键（umo 等）。
            expression: JavaScript 表达式。调用方应已做过"非空"校验。

        Returns:
            :class:`~bsk.models.EvaluateResult`。``ok=True`` 才代表 JS 真的
            跑成功了；``ok=False`` 时本方法抛异常而不是返回，见下。

        Raises:
            BskError: 分两种情况，``friendly`` 都是给模型看的中文。

                1. JS 自己抛异常（进程退出码 0、``ok: false``）—— 这是
                   "表达式写错了"，属于用户/模型可以自我纠正的问题，
                   ``friendly`` 里带上 JS 报错原文与行列，便于直接定位；
                2. bsk 进程本身的失败（超时、会话没了、扩展断了等），
                   由 ``SessionManager.execute`` 按退出码抛出，原样冒泡。

        Note:
            ``allow_uncertain=True``：求值本身可能是只读的，但不是
            必然只读（模型可以传一段会改页面的 JS）。这里持保守立场 ——
            上一次操作结果未知时，宁愿拒绝执行任意脚本，也不要在一个状态
            不明的页面上跑一段"不知道会做什么"的 JS。理由：不确定态下的
            脚本很可能是上一次失败操作的重复（例如重复提交）。所以这个
            参数刻意传 False（默认值），与 click/fill 同一档。
        """
        result = await self.sessions.execute(
            key,
            # EXPRESSION 是位置参数；--json 让输出可机器解析。
            lambda sid: ["evaluate", expression, "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_EVALUATE),
            # 写类动作的待遇：不确定态下不执行任意 JS（见 docstring 的 Note）。
            allow_uncertain=False,
        )
        return self._check_evaluate_result(result.data, expression, self._logger)

    @staticmethod
    def _check_evaluate_result(
        data: Any, expression: str, logger: LoggerLike | None = None
    ) -> EvaluateResult:
        """把 ``evaluate`` 的 JSON 载荷收敛成结果，失败时抛异常。

        抽成静态方法是为了能脱离会话管理单独测试这条判读逻辑 —— 它是整个
        evaluate 功能最容易出错、也最要命的一环。

        Args:
            data: 已解析的 JSON 载荷（外部输入，可能是任何类型）。
            expression: 原始表达式，只用于错误文案里回显"是哪段脚本失败了"。
            logger: 可选的日志接口（见 ``bsk/logger.py``）。静态方法没有 ``self``
                可挂，所以日志对象显式传进来；不传时走 ``NULL_LOGGER``，
                既不影响既有调用方，也保持"任何情况下都不抛异常"。

        Returns:
            ``ok=True`` 的 :class:`~bsk.models.EvaluateResult`。

        Raises:
            BskError: 载荷里 ``ok`` 不为真，或结构完全不认识。
        """
        # 载荷不是 dict：说明输出被截断、或 bsk 改了什么。按失败处理
        # ——绝不能猜成成功，那会把一次未定义的求值当成有返回值。
        if not isinstance(data, dict):
            raise BskError(
                f"evaluate 的返回不是预期结构：{data!r:.200}",
                friendly=(
                    "执行脚本后拿到的返回格式不认识，无法确认是不是成功了。"
                    "请重试一次；若反复出现，可以在终端执行 `bsk --version` "
                    "核对版本。"
                ),
                code="evaluate_bad_payload",
            )

        evaluation = EvaluateResult.from_json(data)
        if evaluation.ok:
            return evaluation

        # --- 走这里 = JS 抛异常了，但进程退出码是 0 ---
        error = evaluation.error
        detail = error.text.strip() if error and error.text.strip() else ""
        location = error.location() if error else ""

        if detail:
            friendly = (
                f"脚本执行出错了{location}：{detail}\n"
                "这不是浏览器或网络的问题，而是这段 JavaScript 本身报错了。"
                "请检查表达式（常见原因：拼错了变量名、调用了页面上不存在的"
                "函数、或者取属性的对象是 null/undefined），改好后再试。"
            )
            message = f"evaluate 里 JS 抛异常：{detail}{location}"
            # 回显失败的那段脚本，便于用户定位是哪一个表达式出的问题。
            # 表达式可能很长（甚至多行），所以截断后再拼。
            shown = expression.strip()
            if shown:
                if len(shown) > 200:
                    shown = shown[:200] + "…"
                message += f"\n表达式：{shown}"
        else:
            # ok=false 但没有 error 结构：bsk 的失败形态不止一种，别让模型
            # 看到一句空话。
            friendly = (
                "脚本执行失败了，但 bsk 没有给出具体的错误信息。"
                "请重试一次；如果一直这样，请把表达式拆简单一些再试"
                "（例如先只求值 `document.title`）。"
            )
            message = f"evaluate 失败且无错误详情：{data!r:.200}"

        (logger or NULL_LOGGER).info("evaluate 里 JS 执行失败：%s", detail or "(无详情)")
        raise BskError(
            message,
            friendly=friendly,
            code="evaluate_js_error",
            # 退出码是 0（实测），这里显式写成 0 而不是 -1：它就是进程层面
            # 成功、业务层面失败的最好证据，日志里一眼能看出来。
            exit_code=0,
        )

    def render_evaluate(self, result: EvaluateResult, expression: str) -> str:
        """把求值结果渲染成给模型看的文本，并做长度截断。

        为什么必须截断：``value`` 是任意 JSON，可以非常大。实测
        ``Array.from({length:2000},(_,i)=>'item-'+i)`` 的命令输出有
        32947 个字符；如果模型写 ``document.body.innerHTML`` 或
        ``JSON.stringify(localStorage)``，拿到几十万字符也是常事。
        不截断会直接撑爆上下文（与 ``max_page_chars`` 同一个先例）。

        截断上限复用 ``settings.max_page_chars``：不再新增一个配置项，
        因为用户对这一项的理解（"一次给模型多少字"）正好适用于这里，
        多一个旋钮只会让配置更难理解。

        Args:
            result: 已确认 ``ok=True`` 的求值结果。
            expression: 原始表达式，回显在开头，让模型对得上"这是哪次的返回"。

        Returns:
            给模型看的文本，长度受 ``max_page_chars`` 约束。
        """
        limit = self._evaluate_char_limit()
        lines = [f"脚本已执行：{expression}"]

        # --- 值本身 ---
        if not result.has_value:
            # 实测：求值成 undefined 时 bsk 会把 value 字段整个省掉。
            lines.append("返回值：undefined（这段脚本没有产生值）")
        else:
            rendered = self._format_evaluate_value(result.value)
            if len(rendered) > limit:
                truncated = rendered[:limit]
                lines.append(
                    f"返回值（内容太长，只显示前 {limit} 个字符，"
                    f"完整长度 {len(rendered)} 个字符）：\n{truncated}\n"
                    "……（已截断。需要完整内容的话，请在脚本里先做筛选或"
                    "只取需要的字段，例如 `document.title` 而不是整个页面 HTML。）"
                )
            else:
                lines.append(f"返回值：{rendered}")

        # --- 弹窗：必须显式告知，因为它意味着"人工确认被绕过了" ---
        if result.dialogs:
            lines.append(self._render_dialogs(result.dialogs))

        return "\n".join(lines)

    @staticmethod
    def _render_dialogs(dialogs: list[Any]) -> str:
        """渲染被自动处理的弹窗。

        这段文案是安全提示，不是装饰：实测 ``confirm`` 会被 bsk 自动
        确认为「确定」（``handled: "accepted"``），也就是说页面本来能让人
        亲自拦一下的那个确认框，在 evaluate 路径上直接消失了。模型必须知道
        这件事，才能在回执里如实告诉用户"这个确认框是被自动点的，不是你点的"。
        """
        parts = [
            f"⚠️ 执行期间页面弹出了 {len(dialogs)} 个对话框，"
            "已被 bsk 自动处理（不是用户点的）："
        ]
        for dialog in dialogs[:10]:
            kind = getattr(dialog, "type", "") or "未知类型"
            message = (getattr(dialog, "message", "") or "").strip() or "（无内容）"
            handled = getattr(dialog, "handled", "") or "未知处理方式"
            parts.append(f"  - {kind}：「{message}」→ {handled}")
        parts.append(
            "  请如实告诉用户：这个对话框是被自动确认/关闭的，用户本人并没有点过它。"
        )
        return "\n".join(parts)

    @staticmethod
    def _format_evaluate_value(value: Any) -> str:
        """把 JS 的返回值渲染成文本。

        分三种情况：字符串原样返回（不加引号，因为模型读起来最自然）、
        其余 JSON 用 ``json.dumps`` 美化、无法序列化的对象退回 ``repr``。

        Note:
            ``ensure_ascii=False`` 是刻意的：中文内容被转成 ``\\uXXXX`` 之后
            既费 token 又难读。这依赖 stdout 已经按 UTF-8 解码
            （``runner.py`` 已显式处理，见 ARCHITECTURE C9）。
        """
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            # 循环引用等极端情况：repr 一定不会抛。
            return repr(value)

    def _evaluate_char_limit(self) -> int:
        """取渲染截断上限，非法配置时回退到 ``DEFAULT_MAX_PAGE_CHARS``。

        与 ``_timeout`` 一样用 ``getattr`` 兜底：配置对象可能是测试桩或
        旧版本 ``Settings``，缺字段不该让一个已经执行成功的脚本白跑。
        """
        from .config import DEFAULT_MAX_PAGE_CHARS

        configured = getattr(self.settings, "max_page_chars", None)
        if isinstance(configured, bool) or not isinstance(configured, int):
            return DEFAULT_MAX_PAGE_CHARS
        return configured if configured > 0 else DEFAULT_MAX_PAGE_CHARS

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
            ``--out`` 会覆盖已有文件，所以路径必须唯一。

        Note:
            两条分支的超时来源不同，不要合并：

            - 全页截图（``--full-page``）用用户配置的 ``fullpage_timeout_sec``
              （默认 :data:`TIMEOUT_FULLPAGE`，可调到 600 秒）；
            - 视口截图走常规规则 :meth:`_timeout`：内置下限
              :data:`TIMEOUT_SCREENSHOT`（30 秒）与 ``command_timeout_sec``
              取较大者，默认配置下实际是 60 秒。

            视口截图不需要用户单独配置：实测只要 0.12 秒，30 秒下限已有
            250 倍余量；它跟着 ``command_timeout_sec`` 走是刻意的 ——
            那条本来就表达"你愿意为一条命令最多等多久"，截图没有理由例外。
        """
        directory = self.settings.screenshot_dir or self._default_shot_dir()
        out_path = make_shot_path(directory, key)

        if full_page:
            timeout = self._timeout(self.fullpage_budget())
        else:
            timeout = self._timeout(TIMEOUT_SCREENSHOT)
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
                self._logger.debug("清理了 %d 张旧截图", removed)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("截图清理失败（忽略）：%r", exc)

        return ShotPayload(
            path=shot.path,
            width=shot.width,
            height=shot.height,
            byte_size=shot.byte_size,
            for_llm=self._describe_shot(shot, full_page),
            warning=warning,
        )

    def _describe_shot(self, shot: Screenshot, full_page: bool) -> str:
        """给模型的截图描述。不返回图片本身，只描述它。"""
        kind = "全页截图" if full_page else "视口截图"
        parts = [f"已生成{kind}"]
        if shot.width and shot.height:
            parts.append(f"{shot.width}x{shot.height} 像素")
        if shot.byte_size:
            parts.append(f"{shot.byte_size / 1024:.0f} KB")
        text = "，".join(parts) + "。图片已直接发给你。"
        if full_page:
            # 刻意不在这里喊"必须同时调大两处"：实测长页面全页截图约 11 秒，
            # 远低于默认预算（见 TIMEOUT_FULLPAGE），正常情况下什么都不用改。
            text += "（全页截图比视口截图慢，长页面通常需要几秒到十几秒。）"
        return text

    def _default_shot_dir(self) -> str:
        """未配置截图目录时的默认位置：插件数据目录的父目录。

        返回的是 ``shots`` 子目录的**父目录**，不是 ``shots`` 本身 ——
        ``make_shot_path`` 会自己拼 ``<本目录>/shots/<session_id>/<文件名>``。
        实际落点是 ``<插件数据目录>/shots/<session_id>/xxx.png``。

        用户显式配的 ``screenshot_dir`` 走同一个契约（也是父目录）。

        数据目录（``settings.data_dir``，由 ``main.py`` 注入）拿不到时由
        ``bsk/paths.py`` 降级到系统临时目录 —— 这里不重复实现那套判断，
        以保证"降级到哪"只有一处定义。

        为什么不用当前工作目录：AstrBot 的工作目录可能是只读的，或随启动
        方式变化；数据目录与临时目录都是明确的、可写的落点。
        """
        data_dir = getattr(self.settings, "data_dir", "") or ""
        return str(default_shot_dir(data_dir, self._logger))

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

        stats = self.sessions.stats()
        info["sessions"] = stats
        # 当前这个聊天会话自己有没有活跃的浏览器会话。
        # stats() 给的是全局计数；用户问"我这边还能用吗"时，真正相关的是
        # 自己这一条。key 就是本插件的会话键（见 main.py 的 _key）。
        current = next(
            (d for d in stats.get("details", []) if d.get("key") == key),
            None,
        )
        info["current_key"] = key
        info["current_session_active"] = current is not None
        if current is not None:
            info["current_session"] = current
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
            本方法绝不抛异常（转发对象的实现已经保证），所以调用方不必
            再套一层 try/except 来防止插件加载失败。
        """
        return await self.sessions.recover_orphans()

    # ------------------------------------------------------------------
    # 页面上报（给模型一段紧凑的环境描述）
    # ------------------------------------------------------------------

    async def read_console(self, key: str, since: int = 0) -> ConsoleLog:
        """读取控制台日志。

        Note:
            返回里 ``entries`` 字段可能整个不存在（实测），
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
        渲染给模型时必须截断，否则会瞬间吃光上下文。
        """
        result = await self.sessions.execute(
            key,
            lambda sid: ["network", "--since", str(int(since)), "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_QUICK),
            allow_uncertain=True,
        )
        return ConsoleLog.from_json(result.data if isinstance(result.data, dict) else {})

    @staticmethod
    def _format_entry_time(raw: Any) -> str:
        """把 bsk 日志条目的时间戳渲染成 ``HH:MM:SS ``（带尾空格），拿不准就返回空串。

        为什么需要"拿不准就空串"：**bsk 两种日志的时间戳量纲不同**，
        这是实测出来的，不是猜的：

        - ``console`` 条目给的是 **Unix 毫秒**（实测 ``1791230905814.595``）；
        - ``network`` 条目给的是 **daemon 启动以来的毫秒**（实测 ``106552.928851``）。

        早先这里直接 ``time.localtime(entry.timestamp)``，对 console 的
        13 位毫秒值会抛 ``OSError: [Errno 22] Invalid argument``
        —— 整条 ``render_console`` 崩掉，用户看到的是"未预期的错误"。

        修法按**量级**判别量纲（三种都实测过）：

        - ``>= 1e11``：Unix **毫秒**（2001-09-09 之后），除以 1000 当秒用 ——
          这是 ``console`` 的常见形态，必须支持，否则时间戳等于白加；
        - ``1e9 ~ 1e11``：Unix **秒**；
        - 其余（相对毫秒、0、负数、非数值、超出范围）：**不显示**。

        宁可少一个时间戳，也不能让渲染抛异常。
        """
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(value) or value <= 0:
            return ""

        if value >= 1e11:
            seconds = value / 1000.0
        elif value >= 1e9:
            seconds = value
        else:
            return ""

        # 再兜一次界：2286 年之前才格式化（time_t 在 32 位平台上会溢出）。
        if not (1e9 <= seconds < 1e10):
            return ""
        try:
            return f"{time.strftime('%H:%M:%S', time.localtime(seconds))} "
        except (OSError, ValueError, OverflowError):
            return ""

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
            # 有 timestamp 就带上时刻 —— 排查时序问题（"报错发生在跳转前还是后"）
            # 时这是唯一的时间锚点。bsk 没给就整段省略，不显示 0。
            when = BskService._format_entry_time(entry.timestamp)
            if entry.kind == "failure":
                # failure 条目没有 status 字段，只有 error_text。
                # 失败原因是这一条唯一有用的信息（如 net::ERR_FAILED），
                # 不打印它就只剩下"失败了"三个字。
                reason = (entry.error_text or "").strip()
                suffix = f" —— {reason}" if reason else ""
                lines.append(
                    f"[{entry.sequence}] {when}失败 {entry.method} {url}{suffix}"
                )
            elif entry.level:
                lines.append(
                    f"[{entry.sequence}] {when}{entry.level}: {entry.text[:300]}"
                )
            else:
                lines.append(
                    f"[{entry.sequence}] {when}{entry.method} {entry.status} {url}"
                )
        if len(log.entries) > limit:
            lines.append(f"……还有 {len(log.entries) - limit} 条未显示")
        if log.truncated:
            lines.append("（bsk 报告日志被截断）")
        return "\n".join(lines)

    # ==================================================================
    # 用例 6-11：新工具（bsk_session / bsk_page / bsk_inspect /
    # bsk_interact / bsk_tabs / bsk_assist）的后端能力
    # ==================================================================
    #
    # 这一整段的命令形式**只**以 ``bsk-0.3.2-cli-capability-report.md``
    # 的实测帮助文本为唯一依据（下称"CLI 报告"）。每条 ``--xxx`` 后面都
    # 标了它在报告里的章节；**报告里查不到的参数一律不发，也绝不臆造**。
    #
    # 既有方法一律不动：新工具走新方法，旧工具继续走旧方法，
    # 共用的是更下面那批基础设施（``_timeout`` / ``sessions.execute`` /
    # ``verify_shot`` / ``cleanup_shots`` …）。

    # ------------------------------------------------------------------
    # 会话注册表：谁建的、哪个是 current
    # ------------------------------------------------------------------

    # 会话键 -> session_id 的本地映射。它同时承担两个职责：
    #
    # 1. ``owns(key)`` 判定"这个会话是不是本插件建的"；
    # 2. ``list_sessions()`` 在不访问 daemon 的前提下报告 pending_cleanup。
    #
    # 为什么必须自己记：``SessionManager.stats()`` 只暴露"槽位里当前有没有
    # 活会话"，而 bsk 的 ``session list`` 既是**异步**的（本方法被设计成
    # 同步），又会把**别的程序**（例如用户自己的 DSH）建出来的会话一起列出来
    # —— 那正是本插件从头到尾在避免的混淆（见 session.py 模块文档第 3 条）。
    #
    # ⚠️ 它是"尽力而为"的台账，不是事实源：``sessions.release`` 走的是
    # SessionManager 自己的槽位，台账只在正常路径上被同步。所以
    # ``list_sessions`` 的 ``state`` 字段以 SessionManager 为准，台账只补
    # session_id 与"上次看到它是什么时候"。

    def _owned(self) -> dict[str, str]:
        """取会话台账，首次访问时按需创建。

        为什么不用 ``__init__`` 里赋值：本文件有大量测试会绕过
        ``__init__``（``object.__new__`` / 假对象 / 直接挂属性），
        把状态初始化放在访问点上可以让它们全部继续工作。
        """
        owned = getattr(self, "_owned_sessions", None)
        if owned is None:
            owned = {}
            self._owned_sessions = owned
        return owned

    def _current_key_slot(self) -> list[str]:
        """取 current key 的容器（单元素列表，便于原地改写）。

        - 从未 start 过 → current 是空串，表示"没有 current 会话"；
        - ``None`` 表示"用户还没做过任何选择"：此时 :meth:`current_key`
          会回退到会话管理器里最近活跃的那个会话（既有行为，向后兼容）。
        """
        slot = getattr(self, "_current_session_key", None)
        if not isinstance(slot, list) or len(slot) != 1:
            slot = [None]
            self._current_session_key = slot
        return slot

    def owns(self, key: str) -> bool:
        """该 key 是否是本插件创建的会话。

        判定依据有两处，任一处命中即为真：

        1. 本地台账里有它的 ``session_id``；
        2. ``SessionManager`` 当前确实持有这个 key 的槽位。

        对外来 id（用户随口编的、或别的程序建的会话键）返回 ``False``
        —— 调用方据此拒绝操作，且**不访问 daemon**（TOOL-SPEC §3 第 1 条）。
        """
        if not isinstance(key, str):
            return False
        clean = key.strip()
        if not clean:
            return False
        if clean in self._owned():
            return True
        entries = getattr(self.sessions, "_entries", None)
        if isinstance(entries, dict) and clean in entries:
            return True
        # 最后一条退路：本会话管理器自己的活跃会话清单。它让"只注入了
        # 一个假 sessions 对象"的调用方也能得到正确的所有权判定。
        try:
            stats = self.sessions.stats()
        except Exception:  # noqa: BLE001 - stats 是诊断接口，坏了不该影响判定
            return False
        details = stats.get("details") if isinstance(stats, dict) else None
        if isinstance(details, list):
            return any(
                isinstance(d, dict) and d.get("key") == clean for d in details
            )
        return False

    def set_current(self, key: str) -> None:
        """把某个 key 设为 current 会话。未知 key 不做任何事。

        "未知"= 空串，或不是本插件创建的键（见 :meth:`owns`）。
        静默忽略是刻意的：``set_current`` 常被当作"顺手激活一下"来调用，
        让一个拼错的 key 把 current 清掉，比什么都不做更糟。
        """
        if not isinstance(key, str):
            return
        clean = key.strip()
        if not clean or not self.owns(clean):
            return
        self._current_key_slot()[0] = clean

    def current_key(self) -> str:
        """当前 current 会话的 key，没有则返回空串。

        三态语义（与 DSH 对齐）：

        - 调用方显式选过（``start`` 或带 ``session`` 参数激活）→ 就返回它；
        - 选过、但那个会话已经关了 → 返回空串（**不**悄悄回退到别的会话：
          "current 已经没了"是模型必须知道的事实，替它挑一个只会让后续
          操作打在一个它没预期的会话上）；
        - 从来没选过 → 回退到会话管理器里最近活跃的会话，拿不到就空串。
          这是保持向后兼容的那一支（既有代码没有"current"这个概念，
          一直用的是"这个 key 自己的会话"）。
        """
        slot = self._current_key_slot()
        chosen = slot[0]
        if chosen is None:
            return self._most_recent_session_key()
        return chosen if chosen in self._owned() else ""

    def _most_recent_session_key(self) -> str:
        """会话管理器里最近活跃的 key（拿不到时返回空串）。"""
        entries = getattr(self.sessions, "_entries", None)
        if isinstance(entries, dict) and entries:
            alive = [e for e in entries.values() if not getattr(e, "closed", False)]
            if alive:
                try:
                    return max(alive, key=lambda e: getattr(e, "last_used", 0.0)).key
                except Exception:  # noqa: BLE001 - 诊断路径，退化成"没有 current"
                    return ""
        try:
            stats = self.sessions.stats()
        except Exception:  # noqa: BLE001
            return ""
        details = stats.get("details") if isinstance(stats, dict) else None
        if isinstance(details, list):
            for item in reversed(details):
                if isinstance(item, dict) and item.get("key"):
                    return str(item["key"])
        return ""

    @staticmethod
    def _coerce_device(raw: Any) -> str | None:
        """把 ``--device`` 的取值收敛成 CLI 报告第 9 节列出的预设之一。

        Returns:
            合法的 device 名（小写）；``None`` 表示**不是**那 7 个预设之一，
            调用方必须整体放弃 ``--device`` 而不是原样转发。

        Note:
            这个白名单不是猜的：CLI 报告 §9（``bsk emulate``）逐字列出了全部
            内置预设 —— ``iphone-14``, ``iphone-14-pro-max``, ``iphone-se``,
            ``pixel-7``, ``galaxy-s23``, ``ipad-mini``, ``galaxy-tab-s8``。
            TOOL-SPEC §1.1 的 ``device`` 枚举也是同样这 7 个。

        Note:
            为什么宁可不发也不原样转发：转发一个 bsk 不认识的值，换来的是
            底层报错，模型完全看不出该怎么办；而从 ``--browser`` 上我们已经有
            实证 —— 用不可核实的值去选目标会**静默选错**。两条路都比
            "只开窗口、不模拟设备、并如实说明"更差。
        """
        if not isinstance(raw, str):
            return None
        clean = raw.strip().lower()
        return clean if clean in EMULATE_DEVICES else None

    async def start_session(
        self,
        *,
        url: str = "",
        width: int | None = None,
        height: int | None = None,
        no_focus: bool = False,
        browser: str = "",
        device: str = "",
        key: str = "",
    ) -> dict:
        """启动一个新的浏览器会话并使其成为 current。

        key 是调用方给的会话键（main.py 的 _key 算出来的），用于在 SessionManager
        里登记。返回 {"session_id":..., "browser_instance_id":..., "url":..., "device":...}
        失败抛 BskError。

        命令形式（CLI 报告 §5 ``session start``）：:

            bsk session start --no-focus --json [--browser <ID>] [--width N --height N]

        Note:
            ``--no-focus`` 是本插件从第一天起的硬要求（不能抢用户焦点）：
            实测会抢焦点的那条路径会让用户正在输入的东西失焦。
            CLI 报告 §5 把它列为不带值的开关，所以这里默认就发；
            模型即使显式传 ``no_focus=False`` 也不会取消它（见下）。

        Note:
            ``--width``/``--height`` 必须**同时**给出（CLI 报告 §5 原文：
            "必须与 ``--height`` 同时给出才生效"）。只给一个时：
            两个都不发（而不是发一个）—— 发一个只会被 bsk 忽略，
            却会让模型以为尺寸已经生效。

        Note:
            ``--width``/``--height``/``--browser`` 走
            ``SessionManager.acquire(key, start_args=[...])`` 这个正规接缝
            透传到 ``session start`` 的 argv 上，其余环节一律照常
            （``acquire`` 的并发去重与占位 → ``_evict_for_capacity`` 的 LRU
            淘汰 → ``_start_into`` 的 journal 落盘与 ``closed`` 竞态处理 →
            ``_start`` 里"配置 → 探测 → bsk 默认"的浏览器选择链路）。

            因为它是**按调用栈传递**的参数，不同 key 并发建会话时互不干扰；
            而且已有会话时 ``start_args`` 被完全忽略，会话失效后的自动重建
            也不带它 —— 尺寸只在"这一次真的新建会话"时有意义。
        """
        del no_focus  # 会话永远后台打开，见上面的 Note。
        target_key = (key or "").strip()

        # --width/--height 必须同时给（报告 §5）；browser 为空时**刻意不发
        # --browser**：让 SessionManager 走它自己那条"配置 → 探测 → 默认"的
        # 链路 —— 那条链路上有硬性守护："探测到多个浏览器却让 bsk 自己挑"
        # 必须报错，绝不能静默随机选一个。
        extra: list[str] = []
        if width is not None and height is not None:
            extra += ["--width", str(int(width)), "--height", str(int(height))]
        if browser:
            extra += ["--browser", browser]

        session = await self.sessions.acquire(
            self._start_key(target_key), start_args=extra
        )

        wanted_device = (device or "").strip()
        applied_device = ""
        if wanted_device:
            # 设备模拟必须打在标签页上，所以要在会话建好之后再做（CLI 报告 §9：
            # ``emulate`` 的可用范围是当前活动标签）。
            try:
                await self.emulate(session.session_id, device=wanted_device)
                applied_device = wanted_device
            except BskError as exc:
                self._logger.warning(
                    "会话 %s 建好了，但设备模拟（device=%s）失败：%s",
                    session.session_id,
                    wanted_device,
                    exc.message,
                )

        final_url = ""
        if url:
            nav = await self.navigate(session.session_id, url)
            final_url = nav.get("final_url") or nav.get("url") or ""

        if target_key:
            self._owned()[target_key] = session.session_id
            self._current_key_slot()[0] = target_key

        return {
            "session_id": session.session_id,
            "browser_instance_id": session.browser_instance_id,
            "url": final_url,
            "device": applied_device,
        }

    def _start_key(self, key: str) -> str:
        """给会话管理器用的 key：调用方没给就沿用 current 的 key。

        不能传空串 —— 会话管理器是按 key 分槽位的，空 key 会让所有
        "没传 session"的调用方共用同一个槽位。
        """
        if key:
            return key
        return self.current_key() or "default"

    async def stop_session(self, key: str) -> dict:
        """停止当前会话。返回 {"stopped": bool, "session_id": str}。

        命令形式（CLI 报告 §5 ``session stop``）：``bsk session stop <ID>``。
        id 是**位置参数**（bsk 里唯一的例外），且**绝不使用** ``--all``
        —— 那会连带停掉别的程序（例如用户自己的 DSH）创建的会话。

        key 为空时按 current 解析（这是 ``bsk_session(action="stop")``
        省略 ``session`` 参数的那条路径）。
        """
        target = (key or "").strip() or self.current_key()
        if not target:
            raise self._no_current_session_error("停止")

        session_id = self._owned().get(target, "")
        stopped = await self.sessions.release(target)

        # 台账要跟着走：不管 stop 成不成功，本地都**不再**持有这个会话
        # （``release`` 已经把它从槽位摘掉了）。留着记录会让
        # ``list_sessions`` 永远报一个已经没了的会话。
        if target in self._owned():
            if not session_id:
                session_id = self._owned().get(target, "")
            del self._owned()[target]
        slot = self._current_key_slot()
        if slot[0] == target:
            slot[0] = ""

        return {"stopped": bool(stopped), "session_id": session_id}

    def list_sessions(self) -> dict:
        """列出本插件创建的全部会话（不访问 daemon）。

        返回 {"pending_cleanup": int, "sessions": [{"key","session_id",
        "browser_instance_id","current","state"}]}

        Note:
            本方法是**同步**的（接口冻结如此），所以它绝不能去跑
            ``bsk session list``（那是子进程调用，只能异步）。这也正是它
            存在的理由：模型想知道"我现在有哪些会话"时，不该为此付一次
            daemon 往返，更不该看到**别人的**会话 —— 那正是本插件从头到尾
            在避免的混淆。

        ``state`` 取值：

        - ``active``：会话管理器槽位里有一个活的会话；
        - ``stopped``：管理器已经不持有它了（刚才被 stop / 被 LRU 淘汰 /
          空闲回收），但台账说明它曾由本插件创建；
        - ``pending_cleanup``：本地已经没有它了，但 journal 里还留着记录
          —— 说明上一次进程没来得及正常 stop。**不要**再往这个 id 上发命令，
          它属于一个已经退出的进程；清理走 :meth:`recover_orphans`。
        """
        pending = self._pending_cleanup_ids()
        current = self.current_key()

        entries = getattr(self.sessions, "_entries", None)
        entries = entries if isinstance(entries, dict) else {}

        sessions: list[dict[str, Any]] = []
        for key, session_id in self._owned().items():
            entry = entries.get(key)
            if entry is not None and not getattr(entry, "closed", False):
                state = "active"
            elif session_id and session_id in pending:
                state = "pending_cleanup"
            else:
                state = "stopped"
            sessions.append(
                {
                    "key": key,
                    "session_id": session_id,
                    # 活跃会话以管理器里的对象为准（它才是事实源），
                    # 已停会话只能报台账里的历史值。
                    "browser_instance_id": (
                        getattr(getattr(entry, "session", None), "browser_instance_id", "")
                        if state == "active"
                        else ""
                    ),
                    "current": bool(current) and key == current,
                    "state": state,
                }
            )

        return {"pending_cleanup": len(pending), "sessions": sessions}

    def _pending_cleanup_ids(self) -> set[str]:
        """journal 里记着、但本地已不再持有的 session_id 集合。

        journal 是"尽力而为"的辅助机制（见 ``bsk/journal.py``），所以这里
        任何异常都退化成"没有待清理项"—— 报不出来只是少一条提示，
        绝不该让 ``list_sessions`` 整个失败。
        """
        journal = getattr(self, "journal", None)
        if journal is None:
            return set()
        try:
            recorded = journal.load()
        except Exception:  # noqa: BLE001
            return set()
        if not recorded:
            return set()
        alive = {
            sid for sid in self._owned().values() if sid
        }
        if isinstance(getattr(self.sessions, "_entries", None), dict):
            for entry in self.sessions._entries.values():
                sid = getattr(getattr(entry, "session", None), "session_id", "")
                if sid:
                    alive.add(sid)
        return {
            str(item.session_id)
            for item in recorded
            if getattr(item, "session_id", "") and item.session_id not in alive
        }

    def _no_current_session_error(self, what: str) -> BskError:
        """构造"没有 current 会话，而这次操作又需要它"的可操作错误。

        面向模型，所以必须说清两件事：现在没有会话，以及下一步该调什么。
        """
        return BskError(
            f"没有 current 会话，无法{what}",
            friendly=(
                f"当前没有浏览器会话，所以无法{what}。\n"
                "请先调用 bsk_session(action=\"start\") 打开一个会话，"
                "或在本次调用里显式给出要操作的 session。"
            ),
            code="no_current_session",
        )

    # ------------------------------------------------------------------
    # 会话解析：session 参数 → 实际要操作的 key
    # ------------------------------------------------------------------

    def _resolve(self, session: str = "", *, param: str = "session") -> str:
        """把调用方给的会话标识解析成 SessionManager 用的 key。

        三条规则（TOOL-SPEC §3 第 1 条 + 接口冻结）：

        1. 省略/空白 → 用 current；没有 current 就报可操作错误；
        2. 显式给出，且 :meth:`owns` 认它 → **激活它**（它成为 current），
           返回它；
        3. 显式给出，但不是本插件建的 → 报错，且**不访问 daemon**
           （所有权判定全在本地，见 :meth:`owns`）。

        Args:
            session: 调用方给的会话键或 session_id。
            param: 出错的参数名，只用于文案。

        Note:
            为什么按 **key** 而不是按 session_id 解析：SessionManager 的
            锁、LRU、空闲回收全部以 key 为单位（同一个 key 严格串行）。
            拿 session_id 去凑一个 key，就会绕开那把锁 —— 两条命令同时
            打在同一个会话上，bsk 会回 ``session_busy``。所以 session_id
            只作为"key 的别名"来解析，解析完仍然回到 key 上执行。
        """
        raw = (session or "").strip() if isinstance(session, str) else ""
        if raw:
            key = self._key_for(raw)
            if key:
                self.set_current(key)
                return key
            raise BskError(
                f"{param} 指定的会话不属于本插件：{raw}",
                friendly=(
                    f"「{raw}」不是本插件创建的会话，已拒绝操作"
                    "（我不会去动别的程序或用户自己的浏览器会话）。\n"
                    "请改用 bsk_session(action=\"list\") 看当前有哪些会话，"
                    "或调用 bsk_session(action=\"start\") 新建一个。"
                ),
                code="foreign_session",
            )
        current = self.current_key()
        if current:
            return current
        raise self._no_current_session_error("执行这次操作")

    def _key_for(self, session: str) -> str:
        """把 session 键或 session_id 解析成本插件持有的 key（认不出返回空串）。"""
        if not session:
            return ""
        if self.owns(session):
            return session
        # 允许用 session_id 指代（用户从 bsk_session(list) 里抄下来的就是它）。
        session_id = session.strip()
        for key, sid in self._owned().items():
            if sid and sid == session_id:
                return key
        entries = getattr(self.sessions, "_entries", None)
        if isinstance(entries, dict):
            for key, entry in entries.items():
                sid = getattr(getattr(entry, "session", None), "session_id", "")
                if sid and sid == session_id:
                    return key
        return ""

    @staticmethod
    def _session_field(key: str) -> str:
        """回执里回显的 ``session`` 字段值。

        约定（TOOL-SPEC §4）：每个结果都要回显实际生效的会话，模型靠它确认
        操作对象。这里给的是**会话键**：它正是模型下次该传进 ``session``
        参数的值（传 session_id 也行，但键更直接，且不会因为会话重建而过期）。
        """
        return key

    # ------------------------------------------------------------------
    # 新方法共用的三个小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _with_tab_id(args: list[str], tab_id: int | None) -> list[str]:
        """把 ``--tab-id`` 追加到 argv 上。tab_id 为 None 时原样返回。

        依据：DSH 的 ``appendTabId``（``phase-one-runtime.ts:47-49``）只在传了
        ``tabId`` 时才追加 ``--tab-id``；未传 = Agent Window 的当前活动标签。

        为什么单独一个辅助而不是每处手写：CLI 报告里 ``--tab-id`` 出现在十几条
        子命令的选项表里（§7/§9/§10/§11/§12/§13/§14/§15/§16/§17/§20…），
        十处各写一遍 trim/类型转换，迟早有一处写成 ``str(tab_id)`` 把
        ``"@e3"`` 这种脏值原样发出去。这里统一收敛成整数。

        Note:
            位置无关：clap 不在意 ``--tab-id`` 与 ``--session``/``--json`` 的
            先后。本文件统一把它放在命令自带参数之后、``--session`` 之前
            （除 debug 外，那里为了不打断筛选参数的分组，放在最前）。
        """
        if tab_id is None:
            return args
        return [*args, *BskService._tab_args(tab_id)]

    @staticmethod
    def _tab_args(tab_id: int | None) -> list[str]:
        """``--tab-id`` 参数（CLI 报告：几乎每条会话命令都有，且都可省略）。"""
        if tab_id is None:
            return []
        return ["--tab-id", str(int(tab_id))]

    def _size_args(self, width: int | None, height: int | None) -> list[str]:
        """``--width`` / ``--height``：**必须同时给**，否则两个都不发。

        依据：CLI 报告 §5（``session start``）"必须与 ``--height`` 同时给出
        才生效"、§9（``emulate``）"无 ``--device`` 时要求同时给"。
        只给一个时发出去只会被 bsk 忽略，却会让模型以为尺寸已生效 ——
        那不是"容错"，是骗人。
        """
        if width is None or height is None:
            return []
        return ["--width", str(int(width)), "--height", str(int(height))]

    # ------------------------------------------------------------------
    # bsk_page：导航与等待
    # ------------------------------------------------------------------

    async def navigate(
        self,
        key: str,
        url: str,
        *,
        wait_until: str = "load",
        timeout_ms: int | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """导航到一个网址。返回 {"url","final_url","reached"}。

        命令形式（CLI 报告 §17）::

            bsk navigate [OPTIONS] [URL]
              --wait-until <WAIT_UNTIL>   默认 load；load/domcontentloaded/networkidle/commit
              --timeout <TIMEOUT>         默认 30s

        ``reached`` **可以**是 ``"timeout"``，那是结果不是错误
        （TOOL-SPEC §1.2 明说），照原样回传让模型自己判断。
        """
        session_key = self._resolve(key)
        args: list[str] = []
        if wait_until and wait_until != "load":
            args += ["--wait-until", str(wait_until)]
        if timeout_ms is not None:
            args += ["--timeout", self._bsk_duration(timeout_ms)]
        nav = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(
                ["navigate", url, *args], tab_id
            )
            + ["--session", sid, "--json"],
            # 超时下限复用 navigate 那一档（45s，必须大于 bsk 自己的 30s）。
            timeout=self._timeout(TIMEOUT_NAVIGATE),
            allow_uncertain=False,
        )
        parsed = NavigateResult.from_json(nav.data)
        return {
            "session": self._session_field(session_key),
            "url": parsed.url or url,
            "final_url": parsed.final_url or parsed.url or url,
            "reached": parsed.reached,
        }

    async def history(
        self,
        key: str,
        direction: str,
        *,
        wait_until: str = "load",
        timeout_ms: int | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """前进/后退。direction 是 "back" 或 "forward"。

        命令形式（CLI 报告 §17/§18/§19）：``bsk navigate back`` /
        ``bsk navigate forward`` 与顶层 ``navigate-back`` / ``navigate-forward``
        等价。这里用 ``navigate <direction>`` 的子命令形式：
        它和既有的 ``service.act(action="navigate_back")`` 发的是同一条命令，
        不引入第二种写法。
        """
        session_key = self._resolve(key)
        if direction not in ("back", "forward"):
            raise BskError(
                f"history 的方向非法：{direction}",
                friendly="只能后退（back）或前进（forward），请检查 direction 参数。",
                code="bad_direction",
            )
        args: list[str] = []
        if wait_until and wait_until != "load":
            args += ["--wait-until", str(wait_until)]
        if timeout_ms is not None:
            args += ["--timeout", self._bsk_duration(timeout_ms)]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(
                ["navigate", direction, *args], tab_id
            )
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_NAVIGATE),
            allow_uncertain=False,
        )
        parsed = NavigateResult.from_json(result.data)
        return {
            "session": self._session_field(session_key),
            "direction": direction,
            "url": parsed.url,
            "final_url": parsed.final_url or parsed.url,
            "reached": parsed.reached,
        }

    async def reload_page(
        self,
        key: str,
        *,
        hard: bool = False,
        wait_until: str = "load",
        timeout_ms: int | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """刷新当前页。hard=True 绕缓存。

        命令形式（CLI 报告 §20）::

            bsk reload --hard --wait-until <W> --timeout <T> --session <ID> --json
        """
        session_key = self._resolve(key)
        args: list[str] = []
        if hard:
            args.append("--hard")
        if wait_until and wait_until != "load":
            args += ["--wait-until", str(wait_until)]
        if timeout_ms is not None:
            args += ["--timeout", self._bsk_duration(timeout_ms)]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["reload", *args], tab_id)
            + ["--session", sid, "--json"],
            # CLI 报告 §20：reload 自己的 --timeout 默认 15s，比 navigate 短一档。
            timeout=self._timeout(TIMEOUT_HISTORY),
            allow_uncertain=False,
        )
        parsed = NavigateResult.from_json(result.data)
        return {
            "session": self._session_field(session_key),
            "hard": bool(hard),
            "url": parsed.url,
            "final_url": parsed.final_url or parsed.url,
            "reached": parsed.reached,
        }

    async def wait_for(
        self,
        key: str,
        *,
        wait_until: str = "load",
        timeout_ms: int = 30000,
        tab_id: int | None = None,
    ) -> dict:
        """只等待页面生命周期事件，不做任何导航。

        命令形式（CLI 报告 §33）::

            bsk wait-for-navigation --wait-until <W> --timeout <T> --session <ID> --json

        Note:
            这是**只读**的（不改页面状态），所以 ``allow_uncertain=True``：
            上一次操作结果未知时，正是最需要"等一等看页面有没有自己稳定下来"
            的时候。
        """
        session_key = self._resolve(key)
        args: list[str] = []
        if wait_until and wait_until != "load":
            args += ["--wait-until", str(wait_until)]
        args += ["--timeout", self._bsk_duration(timeout_ms)]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(
                ["wait-for-navigation", *args], tab_id
            )
            + ["--session", sid, "--json"],
            # 纯等待：bsk 自己的预算是 timeout_ms，外层必须比它大（见模块顶部
            # "下限的第一条原则"），否则我们会先把它掐掉。
            timeout=self._wait_timeout(timeout_ms),
            allow_uncertain=True,
        )
        parsed = NavigateResult.from_json(result.data)
        return {
            "session": self._session_field(session_key),
            "final_url": parsed.final_url or parsed.url,
            "reached": parsed.reached,
        }

    @staticmethod
    def _bsk_duration(milliseconds: int | float) -> str:
        """把毫秒数渲染成 bsk 接受的时间字面量（``30s`` / ``1500ms`` / ``1m``）。

        依据：CLI 报告 §17/§18/§20/§33/§32 反复写明 ``--timeout`` 接受
        ``30s``, ``1m``, ``1500ms`` 这类带单位的字面量。

        Note:
            刻意不写裸数字：只有 ``wait-ms``（§34）的**位置参数**才把裸整数
            解释成毫秒，``--timeout`` 没有这条约定。带单位是唯一确定安全的写法。
            ``ms`` 不是 ``s`` 的整倍数时保留毫秒，避免把 1500ms 悄悄说成 1s。
        """
        value = float(milliseconds)
        if value <= 0:
            value = 1.0
        if value % 1000 == 0:
            return f"{int(value) // 1000}s"
        return f"{int(value)}ms"

    def _wait_timeout(self, timeout_ms: int) -> float:
        """纯等待类命令的外层超时：比它自己的预算多留一点余量。

        ``wait_for`` 与 ``request_help`` 都会把预算交给 bsk 自己控制，
        我们的外层超时只是"防止它卡死"的兜底。两个约束：

        - 必须 **大于** bsk 自己的预算（否则我们会先把它掐掉，
          而它正要成功返回 —— 见模块顶部"下限的第一条原则"）；
        - 不能超过框架上限（否则用户看到的是框架抛的英文
          ``execution timeout``，而不是我们写的中文提示）。
        """
        requested = float(timeout_ms) / 1000.0
        wanted = requested + WAIT_TIMEOUT_MARGIN_SEC
        ceiling = self.framework_timeout_ceiling()
        if ceiling is None:
            return wanted
        return max(min(wanted, ceiling), min(requested, ceiling))

    # ------------------------------------------------------------------
    # bsk_inspect：读取与调试
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        key: str,
        *,
        max_depth: int | None = None,
        max_tokens: int | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """aria 快照（与 observe 的区别：无 cursor，静态可访问性树）。

        命令形式（CLI 报告 §11）::

            bsk snapshot --max-depth <N> --max-tokens <N> --session <ID> --json

        只读，所以 ``allow_uncertain=True``（与 ``observe`` 同一条理由）。
        """
        session_key = self._resolve(key)
        args: list[str] = []
        if max_depth is not None:
            args += ["--max-depth", str(int(max_depth))]
        if max_tokens is not None:
            args += ["--max-tokens", str(int(max_tokens))]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["snapshot", *args], tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_OBSERVE),
            allow_uncertain=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        raw_text = data.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        ref_count = data.get("ref_count")
        return {
            "session": self._session_field(session_key),
            "text": text,
            "ref_count": ref_count if isinstance(ref_count, int) else 0,
            "tab_id": data.get("tab_id") if isinstance(data.get("tab_id"), int) else 0,
            "truncated": data.get("truncated") is True,
        }

    async def get_html(
        self,
        key: str,
        *,
        ref: str = "",
        max_bytes: int = 524288,
        tab_id: int | None = None,
    ) -> dict:
        """导出原始 HTML。ref 非空时限定到该子树。

        命令形式（CLI 报告 §16）::

            bsk get-html --ref <REF> --max-bytes <N> --session <ID> --json

        Note:
            刻意**不发** ``--out``（§16 有它，但那是"把 HTML 写到文件而不是
            stdout"）：本工具要把 HTML 交给模型看，写进一个临时文件只会多出
            一件需要清理的东西，而且那份文件的内容我们还得再读回来。
            ``--max-bytes`` 就是长度控制手段（TOOL-SPEC §1.3 默认 524288）。
        """
        session_key = self._resolve(key)
        args: list[str] = ["--max-bytes", str(int(max_bytes))]
        if ref:
            args += ["--ref", ref]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["get-html", *args], tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_OBSERVE),
            allow_uncertain=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        html = data.get("html")
        if not isinstance(html, str):
            # 认不出的结构不能悄悄返回空串：那会被模型当成"页面没有 HTML"。
            html = ""
        size = data.get("byte_size") or data.get("bytes")
        return {
            "session": self._session_field(session_key),
            "ref": ref,
            "max_bytes": int(max_bytes),
            "html": html,
            "byte_size": size if isinstance(size, int) else len(html.encode("utf-8")),
        }

    async def read_console_ex(
        self,
        key: str,
        *,
        since: int = 0,
        limit: int | None = None,
        max_text_chars: int | None = None,
        include_stack: bool = False,
        tab_id: int | None = None,
    ) -> ConsoleLog:
        """read_console 的扩展版（多 limit/max_text_chars/include_stack）。
        既有的 read_console(key, since) 保持不变并委托到本方法。

        命令形式（CLI 报告 §13）::

            bsk console --since <N> --limit <N> --max-text-chars <N>
                        --include-stack --session <ID> --json

        ``--include-stack`` 只有 ``console`` 有（§14 明确 network 没有）。
        """
        session_key = self._resolve(key)
        args: list[str] = ["--since", str(int(since))]
        if limit is not None:
            args += ["--limit", str(int(limit))]
        if max_text_chars is not None:
            args += ["--max-text-chars", str(int(max_text_chars))]
        if include_stack:
            args.append("--include-stack")
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["console", *args], tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_QUICK),
            allow_uncertain=True,
        )
        return ConsoleLog.from_json(result.data if isinstance(result.data, dict) else {})

    async def read_network_ex(
        self,
        key: str,
        *,
        since: int = 0,
        limit: int | None = None,
        max_text_chars: int | None = None,
        tab_id: int | None = None,
    ) -> ConsoleLog:
        """read_network 的扩展版。

        命令形式（CLI 报告 §14）::

            bsk network --since <N> --limit <N> --max-text-chars <N> --session <ID> --json

        ``network`` **没有** ``--include-stack``（§14 的括号注），
        所以本方法也不接受这个参数。
        """
        session_key = self._resolve(key)
        args: list[str] = ["--since", str(int(since))]
        if limit is not None:
            args += ["--limit", str(int(limit))]
        if max_text_chars is not None:
            args += ["--max-text-chars", str(int(max_text_chars))]
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["network", *args], tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_QUICK),
            allow_uncertain=True,
        )
        return ConsoleLog.from_json(result.data if isinstance(result.data, dict) else {})

    async def debug(self, key: str, debug_action: str, **opts: Any) -> Any:
        """bsk debug 直通。返回原始解析后的 JSON（不做字段重映射）。
        opts 是校验过的调试参数（snake_case）。

        命令形式（CLI 报告 §15）::

            bsk debug [OPTIONS] <ACTION> [ID] ... --session <ID> --json

        ``<ACTION>`` 是位置参数；``[ID]`` 也是位置参数（request / operation /
        rule_enable / rule_disable / rule_remove / replay / pin / unpin 需要它）。

        Note:
            本方法**照原样返回** bsk 的载荷，一个字段都不重映射
            （TOOL-SPEC §2 的硬要求）。理由：debug 的输出形态由 action 决定
            （列表、单条 body 切片、导出路径、等待结果……），任何"统一化"
            都会丢掉模型真正要看的东西。

        Note:
            **``allow_uncertain`` 按 action 分级**（安全设计，不能一刀切）：

            - :data:`MUTATING_DEBUG_ACTIONS` 里的 5 个 —— ``replay`` /
              ``rule_add`` / ``rule_enable`` / ``rule_disable`` /
              ``rule_remove`` —— 一律 ``allow_uncertain=False``。
              ``replay`` 会**带着用户的 cookie 重新发送**抓到的请求
              （DSH 自己的描述就是 "may change server data"），
              规则类 action 能改写/伪造真实流量。它们和 click/fill 同一档：
              上一次结果未知时重复执行，可能造成重复提交或伪造流量。
            - 其余（``performance`` / ``requests`` / ``console`` /
              ``export`` / ``capabilities`` / ``wait`` …）只读证据或等待，
              ``allow_uncertain=True`` —— 页面状态不明时恰恰最需要看这些。

            （早先这里写过"replay 不属于写操作"的论证，那是错的：判断标准
            不是"BSK 服务端会不会自己校验"，而是"重放一次会不会改变外部
            可见的状态"。会变的就必须挡住。）

        Note:
            ``wait`` 的超时不能走常规规则。``wait_ms`` 的合法上限是
            60000ms（CLI 报告 §15 的 0..60000），而 ``_timeout(TIMEOUT_DEBUG)``
            在默认配置下是 60 秒 —— 两者相等，意味着一次**合法的最长等待**
            会被我们自己的外层超时同时掐死，用户看到的是超时错误而不是
            等到的结果。所以 ``wait`` 改用 :meth:`_wait_timeout`：
            它保证外层超时 > bsk 自己的预算（+5 秒余量），同时不越过框架上限。
            DSH 对同一处也是专门放宽的（``debug-tool.ts:238-240``：
            ``Math.max(defaultTimeoutMs, (waitMs ?? 10000) + 15000)``）。

        Args:
            key: 会话键（或 session 参数的原值）。
            debug_action: 24 个 debug action 之一（TOOL-SPEC §2）。
            **opts: 该 action 的参数，见 :data:`DEBUG_VALUE_FLAGS`。
                其中 ``tab_id`` 可以只放在 ``opts`` 里（``bsk debug`` 本来
                就有 ``--tab-id``，见 CLI 报告 §15）；本方法还额外接受
                显式的 ``tab_id=`` 关键字参数，两者等价。
        """
        session_key = self._resolve(key)
        action = str(debug_action)

        args: list[str] = []
        explicit_tab_id = opts.pop("tab_id", None)
        for name, flag in DEBUG_VALUE_FLAGS.items():
            value = opts.get(name)
            if value is None or value == "" or value is False:
                continue
            args += [flag, self._render_debug_value(name, value)]
        for name, flag in DEBUG_BOOL_FLAGS.items():
            if opts.get(name) is True:
                args.append(flag)

        tail: list[str] = []
        id_arg = opts.get("id")
        if id_arg is not None and id_arg != "":
            tail.append(str(id_arg))

        readonly = action not in MUTATING_DEBUG_ACTIONS
        timeout = (
            self._wait_timeout(self._coerce_wait_ms(opts.get("wait_ms")))
            if action == "wait"
            else self._timeout(TIMEOUT_DEBUG)
        )

        result = await self.sessions.execute(
            session_key,
            # --tab-id 放在最前面：DEBUG_VALUE_FLAGS 里它本来也会被渲染，
            #   这里显式再传一次是为了让"只给 tab_id 关键字参数"也能生效；
            #   两者同时给出时以关键字参数为准（上面的 pop 已把它从 opts 摘走，
            #   所以不会出现两个 --tab-id）。
            lambda sid: [
                "debug",
                *self._tab_args(
                    int(explicit_tab_id) if explicit_tab_id is not None else None
                ),
                *args,
                action,
                *tail,
                "--session",
                sid,
                "--json",
            ],
            timeout=timeout,
            allow_uncertain=readonly,
        )
        return result.data

    @staticmethod
    def _coerce_wait_ms(raw: Any) -> int:
        """把 ``wait_ms`` 收敛成非负整数毫秒（非法值用 bsk 自己的默认 10000）。

        依据：CLI 报告 §15 写着 ``--wait-ms <WAIT_MS>`` 默认 10000、范围
        0..60000。``wait`` 动作没给 ``wait_ms`` 时，bsk 会等 10 秒；
        我们的外层超时必须按**它实际会等多久**来算，所以这里回退到同一个
        10000，而不是 0。
        """
        if isinstance(raw, bool) or raw is None:
            return 10000
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 10000
        if value < 0:
            return 10000
        return value

    @staticmethod
    def _render_debug_value(name: str, value: Any) -> str:
        """把 debug 的一个带值参数渲染成命令行字面量。

        - ``rule`` / ``replay`` / ``completion_criteria`` 这类 JSON 参数：
          用紧凑 JSON（``separators``）序列化。依据是 CLI 报告的示例原文
          —— ``--completion-criteria '{"any":[{"url_contains":"/dashboard"}],"stable_for_ms":1000}'``
          是**一个** argv 元素，不是 shell 拼出来的；我们直接过 argv 列表，
          所以只要内容是合法 JSON 即可（不需要 shell 引号）。
        - 布尔：小写 ``true``/``false``，与 CLI 报告里 ``--await-promise <bool>``
          的默认值写法一致。
        - 其余（含 path 等）转字符串。
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if name in ("rule", "replay", "completion_criteria"):
            if isinstance(value, str):
                # 已经是字符串：原样透传（校验层负责确认它是合法 JSON）。
                return value
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    # ------------------------------------------------------------------
    # bsk_interact：统一交互入口
    # ------------------------------------------------------------------

    async def interact(
        self, key: str, action: str, *, tab_id: int | None = None, **opts: Any
    ) -> dict:
        """统一的页面交互入口。

        action 取值见 TOOL-SPEC §1.4。
        opts 是校验过的参数（target/value/values/key/button/click_count/
        modifiers/capture_id/image_x/image_y/settle_ms/hold_ms/
        delta_x/delta_y/timeout_ms/no_clear）。

        Returns:
            {"session": str, "tab_id": int|None, ...该 action 的结果字段}

        Note:
            写类动作必须传 allow_uncertain=False 给 sessions.execute
            （安全设计，见 TOOL-SPEC H4）。

        Note:
            与既有 :meth:`act` 的分工：``act`` 只覆盖 13 个"动作 + 按键/滚动"
            的组合，且没有 canvas 点击、hold、settle、no-clear 这些参数。
            ``act`` 的签名与行为**一个字都不改**（旧工具依赖它），
            两条路径共用下面同一张 argv 构造表 :data:`INTERACT_SPECS`，
            所以同一个 action 在两边发出的命令行逐字节相同。

        Note:
            ⚠️ **接口冻结里的一处矛盾**（已在交付报告里列出，没有自行改签名）：
            本方法的第一个参数（会话键）叫 ``key``，而 ``press`` 动作要按的键
            在 TOOL-SPEC §1.4 里同样叫 ``key``。在 Python 里这两个名字
            无法共存 —— ``interact(k, "press", key="Enter")`` 会直接

                TypeError: interact() got multiple values for argument 'key'

            （已验证：位置参数填满形参 ``key`` 之后，同名关键字进不了
            ``**opts``。把会话键改传关键字也一样撞，只是换成
            "multiple values for argument 'action'" 那一侧。）

            本方法因此在 ``opts`` 里**同时接受** ``key`` 与 ``press_key``：

            .. code-block:: python

                await service.interact(k, "press", press_key="Enter")   # 推荐
                await service.interact(k, "press", **{"key": "Enter"})  # 等价

            ``press_key`` 是我为这个动作选的别名，理由有两条：

            1. 它是 ``**opts`` 里的一个普通键，不占形参名，所以能真正传进来；
            2. 它不与工具层的校验结果冲突 —— 调用方只要做一次
               ``opts["press_key"] = opts.pop("key")``，其余 8 个动作的
               opts 原样透传即可。

            **如果最终决定统一用别的名字，请改这一处别名表**
            （``_PRESS_KEY_ALIASES``），签名本身不用动。
        """
        for alias in _PRESS_KEY_ALIASES:
            if alias in opts and "key" not in opts:
                opts["key"] = opts.pop(alias)
        session_key = self._resolve(key)
        argv, readonly = self._build_interact_args(action, opts)
        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(argv, tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(self._interact_timeout_builtin(action)),
            # 写类动作（click/fill/press/select/hover）绝不允许在不确定态下执行
            # —— 那可能造成重复点击、重复提交。
            allow_uncertain=readonly,
        )
        out: dict[str, Any] = {
            "session": self._session_field(session_key),
            "action": action,
            "tab_id": self._payload_tab_id(result.data),
        }
        out.update(self._interact_result(action, opts, result.data, readonly))
        return out

    @staticmethod
    def _payload_tab_id(data: Any) -> int | None:
        """从载荷里取 ``tab_id``；没有就给 ``None``（不编造 0）。"""
        if isinstance(data, dict):
            value = data.get("tab_id")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    @staticmethod
    def _interact_result(
        action: str, opts: dict[str, Any], data: Any, readonly: bool
    ) -> dict[str, Any]:
        """把交互结果摊平成给模型看的字段。

        不重映射 bsk 的字段名：直接把载荷里的标量与短列表并进来。
        截断策略交给上层渲染（这里只保证不把二进制/巨型结构带出去）。
        """
        out: dict[str, Any] = {
            "target": str(opts.get("target") or ""),
            "changed": readonly is False,
        }
        if isinstance(data, dict):
            for name, value in data.items():
                if name == "tab_id":
                    continue  # 已在 interact 里单独放过
                if isinstance(value, (str, int, float, bool)) or value is None:
                    out[name] = value
        if action == "wheel":
            out["delta_x"] = int(opts.get("delta_x") or 0)
            out["delta_y"] = int(opts.get("delta_y") or 0)
        if action == "press":
            out["key"] = str(opts.get("key") or "")
        return out

    @staticmethod
    def _interact_timeout_builtin(action: str) -> float:
        """交互动作的超时下限（全部按 ``TIMEOUT_ACTION`` 那一档算）。

        CLI 报告里 click/hover/wheel/scroll-to/focus/blur/fill/press/select
        的 ``--timeout`` 默认值**全都是 30s**，与 ``TIMEOUT_ACTION`` 的定义
        完全一致（那个常量的注释就写着"与 bsk 自身默认 --timeout 30s 对齐"）。
        """
        del action
        return TIMEOUT_ACTION

    @staticmethod
    def _build_interact_args(action: str, opts: dict[str, Any]) -> tuple[list[str], bool]:
        """把交互参数翻译成 bsk 命令行参数（不含 ``--session``）。

        Returns:
            ``(argv, readonly)``。``readonly=True`` 表示允许在会话不确定态下
            执行（只有 ``wheel``/``scroll_to``/``focus``/``blur`` 这四种
            "不改变页面数据"的动作）。

        Raises:
            BskError: 该 action **在 bsk CLI 里没有对应子命令**时。
                绝不臆造参数（本次实现的硬约束）。

        Note:
            ``target`` 一律走**位置参数**，不发 ``--ref`` / ``--selector``：
            CLI 报告 §21-§29 明确写了位置参数"使用 --ref/--selector 时可省略"，
            即三选一。位置参数同时接受 ``@e3``/``e3``/CSS 选择器，交给 CLI
            自己判别（TOOL-SPEC §1.4 的"唯一特例"只在 ``press`` 上，
            而 ``press`` 的 ref 是 ``--ref``，见下）。
        """
        # 兼容两种写法：新工具（bsk_interact）用连字符的 "scroll-to"（对齐 DSH
        # 与 bsk CLI 的子命令名），旧工具（bsk_act）用下划线的 "scroll_to"。
        # 归一化放在这一处，两条路径都能用，也避免将来再出现"键对不上"的静默失败。
        # 注意方向：表的键是**连字符**形式，所以要把下划线换成连字符。
        spec = INTERACT_SPECS.get(action) or INTERACT_SPECS.get(
            action.replace("_", "-")
        )
        target = str(opts.get("target") or "")
        if spec is None:
            from .errors import BskError as _Err

            raise _Err(
                f"bsk CLI 没有对应 {action} 的子命令",
                friendly=(
                    f"「{action}」不是可用的交互动作；可选值："
                    + " / ".join(INTERACT_SPECS)
                    + "。"
                ),
                code="unsupported_action",
            )

        if spec.requires_target and not target:
            from .errors import BskError as _Err

            raise _Err(
                f"{action} 需要 target",
                friendly=(
                    f"「{action}」必须指定要操作的元素："
                    "给 target 传元素编号（如 @e3）或 CSS 选择器。"
                ),
                code="missing_target",
            )

        argv: list[str] = [spec.argv[0]]

        # ID / KEY 这类"必填的位置参数"（press 的按键、tab 的 id）。
        for placeholder in spec.leading_positionals:
            value = opts.get(placeholder)
            if value is None or value == "":
                from .errors import BskError as _Err

                raise _Err(
                    f"{action} 缺少 {placeholder}",
                    friendly=f"「{action}」必须提供 {placeholder} 参数。",
                    code="missing_argument",
                )
            argv.append(str(value))

        # 目标位置参数。
        if spec.allows_target and target:
            argv.append(target)

        # 选项（顺序固定，便于测试与人工核对）。
        for name, flag in spec.value_flags:
            value = opts.get(name)
            if value is None or value is False:
                continue
            if value == "" and (action, name) not in _EMPTY_VALUE_OK:
                continue
            if name == "values":
                for item in value if isinstance(value, (list, tuple)) else [value]:
                    argv += [flag, str(item)]
                continue
            if name == "modifiers":
                argv += [flag, ",".join(str(m) for m in value)]
                continue
            if name == "click_count":
                argv += [flag, str(int(value))]
                continue
            argv += [flag, str(value)]
        for name, flag in spec.bool_flags:
            if opts.get(name) is True:
                argv.append(flag)
        for name, flag in spec.ms_flags:
            value = opts.get(name)
            if value is None:
                continue
            argv += [flag, ServiceHelper.ms_value(value)]

        return argv, spec.readonly

    # ------------------------------------------------------------------
    # bsk_tabs：标签管理
    # ------------------------------------------------------------------

    async def tabs(
        self, key: str, action: str, *, tab_id: int | None = None, **opts: Any
    ) -> dict:
        """标签管理。

        action: list / create / select / close / borrow / return
        opts: tab_id / scope / url / active / index

        命令形式（CLI 报告 §7）：

        - ``tab list --scope <SCOPE>``（默认 ``all``，可选 user/agent/all）
        - ``tab create --url <URL> --index <N> --no-active``（默认聚焦新标签）
        - ``tab close <TAB_ID>`` / ``tab select <TAB_ID>``（位置参数）
        - ``tab borrow <TAB_ID> --timeout <T>``（§7 的 ``--no-confirm`` 是
          已废弃兼容标志，**不发** —— 是否确认由扩展决定，发了也改变不了行为）
        - ``tab return <TAB_ID>``

        ``tab_id`` 对 select/close/borrow/return 是**必填的位置参数**
        （§7 里这四个的表格都标了必填），对 list/create 不使用。

        Note:
            ``tab_id`` 这个名字在本方法里有**两个不同的含义**，必须分清：

            - **位置参数**（``tab select <TAB_ID>`` / ``close`` / ``borrow`` /
              ``return``）—— 要操作的那一个标签，就是下面的 ``tab_id`` 参数；
            - **``--tab-id`` 选项** —— "这条命令打在哪个标签上"。

            CLI 报告 §7 里，前四个子命令的表格**只列了 ``--session``**，
            没有 ``--tab-id``；而 ``--tab-id`` 在报告里是其它命令（§9-§21）
            的选项。所以对 ``tabs`` 而言：``tab_id`` 永远走位置参数，
            **不发 ``--tab-id``**。这也是本方法没有跟着其它方法一起接
            ``tab_id=`` 关键字参数的原因（那会把同一个名字指到两个东西上）。
        """
        session_key = self._resolve(key)
        spec = TABS_SPECS.get(action)
        if spec is None:
            raise BskError(
                f"不支持的标签动作：{action}",
                friendly=(
                    f"「{action}」不是可用的标签动作。可用的有："
                    "list / create / select / close / borrow / return。"
                ),
                code="unsupported_action",
            )

        argv: list[str] = ["tab", spec.argv[1]]
        target_tab_id = tab_id if tab_id is not None else opts.get("tab_id")
        if spec.takes_tab_id:
            if target_tab_id is None or target_tab_id == "":
                raise BskError(
                    f"tab {action} 需要 tab_id",
                    friendly=(
                        f"「tab {action}」必须给出要操作的标签 id"
                        "（先用 bsk_tabs(action=\"list\") 查看）。"
                    ),
                    code="missing_tab_id",
                )
            argv.append(str(int(target_tab_id)))

        if action == "list":
            scope = opts.get("scope")
            if scope:
                argv += ["--scope", str(scope)]
        elif action == "create":
            url = opts.get("url")
            if url:
                argv += ["--url", str(url)]
            index = opts.get("index")
            if index is not None:
                argv += ["--index", str(int(index))]
            # --no-active 是"后台打开"，所以 active=True（默认）时不发任何开关。
            if opts.get("active") is False:
                argv.append("--no-active")

        result = await self.sessions.execute(
            session_key,
            lambda sid: [*argv, "--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_ACTION),
            # 标签管理本身是"窗口/标签"操作，不是页面写操作；但它会改变
            # 会话能看到的标签集合，且 borrow/return 涉及用户自己的标签。
            # 保守起见一律不允许在不确定态下执行 —— 与写类动作同一档。
            allow_uncertain=False,
        )
        data = result.data
        out: dict[str, Any] = {
            "session": self._session_field(session_key),
            "action": action,
        }
        if action == "list":
            out["tabs"] = self._render_tabs(data)
            out["tab_id"] = None
        else:
            out["tab_id"] = (
                int(target_tab_id)
                if isinstance(target_tab_id, int)
                and not isinstance(target_tab_id, bool)
                else None
            )
            if isinstance(data, dict):
                for name, value in data.items():
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        out.setdefault(name, value)
        return out

    @staticmethod
    def _render_tabs(data: Any) -> list[dict[str, Any]]:
        """把 ``tab list`` 的载荷收敛成一段稳定的标签清单。

        CLI 报告 §7 明确说元素级 JSON 结构**未取到**（取它需要先建会话，
        那属于状态变更）。所以这里只认最保守的形态：

        - 载荷是列表 → 逐项取 ``id``/``tab_id``、``title``、``url``、
          ``active``、``window_id``，缺的字段就不放进去（**不编造**）；
        - 载荷不是列表 → 返回空列表。

        刻意不猜字段名之外的语义，也不做任何"补齐"：模型看到的就是 bsk
        返回的东西。元素结构将来变了，这里最多是少显示几个字段，
        不会显示错的东西。
        """
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {}
            for src, dst in (
                ("id", "id"),
                ("tab_id", "tab_id"),
                ("title", "title"),
                ("url", "url"),
                ("active", "active"),
                ("window_id", "window_id"),
            ):
                if src in item:
                    row[dst] = item[src]
            if row:
                out.append(row)
        return out

    # ------------------------------------------------------------------
    # bsk_assist：窗口、设备模拟、请人帮忙
    # ------------------------------------------------------------------

    async def resize_window(
        self, key: str, width: int, height: int, *, tab_id: int | None = None
    ) -> dict:
        """调整 Agent Window 大小。

        命令形式（CLI 报告 §8）::

            bsk window resize --width <W> --height <H> --session <ID> --json

        ``--width``/``--height`` 在这里都是**必填**（§8 的 usage 行直接标了），
        合法范围 100..=7680 —— 范围校验在上层纯函数里做（TOOL-SPEC §1.6），
        这一层不重复实现。

        Args:
            tab_id: 接受它只为与其它方法保持**同一个调用形状**（DSH 的
                ``appendTabId`` 是把 ``tabId`` 发给全部六个工具的）。
                但它在这里**不会被转发** —— 见下。

        Note:
            ``tab_id`` 被刻意忽略：CLI 报告 §8 是 ``window resize`` 的**完整**
            选项表，里面**没有** ``--tab-id``（只有 ``--session`` /
            ``--width`` / ``--height``），而它的 usage 行也把它标成必填项。
            这张表与其它命令（§9-§21 都列了 ``--tab-id``）的对比很明确：
            这个子命令不接受 ``--tab-id``。发一个报告里查不到的 flag 会让
            clap 直接报参数错误，整条命令失败 —— 那比"忽略这个尺寸无关的
            参数"糟得多。窗口是会话级的，本就没有"给某个标签 resize"的语义。
        """
        del tab_id  # CLI §8 无此选项，见 docstring 的 Note。
        session_key = self._resolve(key)
        result = await self.sessions.execute(
            session_key,
            lambda sid: [
                "window",
                "resize",
                "--width",
                str(int(width)),
                "--height",
                str(int(height)),
                "--session",
                sid,
                "--json",
            ],
            timeout=self._timeout(TIMEOUT_ACTION),
            # 改窗口尺寸不碰页面数据，不确定态下做它是安全的。
            allow_uncertain=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {
            "session": self._session_field(session_key),
            "width": data.get("width") if isinstance(data.get("width"), int) else int(width),
            "height": data.get("height") if isinstance(data.get("height"), int) else int(height),
        }

    async def emulate(
        self,
        key: str,
        *,
        device: str = "",
        width: int | None = None,
        height: int | None = None,
        mobile: bool = False,
        off: bool = False,
        tab_id: int | None = None,
    ) -> dict:
        """移动设备环境模拟。

        命令形式（CLI 报告 §9）::

            bsk emulate --device <D> --width <W> --height <H> --mobile --off
                        --session <ID> --json

        Note:
            ``--off`` 在 CLI 报告里写着"**与所有其他选项互斥**"，所以它一旦
            为真，其余参数一个都不发（互斥校验在上层做，但这一层也不该
            自己造出非法组合）。

        Note:
            报告 §9 里还有 ``--dpr`` / ``--ua`` / ``--accept-language`` /
            ``--touch`` / ``--no-touch`` / ``--max-touch-points`` /
            ``--no-mobile`` / ``--tab-id``。它们**不在** TOOL-SPEC §1.6 的
            参数表里，所以本方法不接受更细的开关 —— 少一个旋钮只是少一种
            玩法，多一个无人校验的旋钮才会出问题。``--tab-id`` 例外，
            它对所有带标签的命令都有效，但本工具没有"换标签再模拟"的语义，
            故也不发（模拟总是作用于会话当前的活动标签，与 CLI 报告 §9 的
            默认值一致）。
        """
        session_key = self._resolve(key)

        if off:
            args = ["--off"]
        else:
            args = []
            device_name = self._coerce_device(device)
            if device_name:
                args += ["--device", device_name]
            # --width/--height 必须同时给（报告 §9：无 --device 时要求同时给；
            # 有 device 时给出则表示"手动覆盖预设的单个字段"，所以给了就一起给）。
            args += self._size_args(width, height)
            if mobile:
                args.append("--mobile")

        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["emulate", *args], tab_id)
            + ["--session", sid, "--json"],
            timeout=self._timeout(TIMEOUT_ACTION),
            allow_uncertain=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {
            "session": self._session_field(session_key),
            "off": bool(off),
            "device": (data.get("device") if isinstance(data.get("device"), str) else "") or "",
            "width": data.get("width") if isinstance(data.get("width"), int) else 0,
            "height": data.get("height") if isinstance(data.get("height"), int) else 0,
        }

    async def request_help(
        self,
        key: str,
        prompt: str,
        *,
        title: str = "",
        targets: list[str] | None = None,
        timeout_ms: int = 300000,
        completion_criteria: dict | None = None,
        tab_id: int | None = None,
    ) -> dict:
        """请真人完成页内步骤（验证码/登录/确认）。

        返回 {"outcome": str, ...}。outcome 见 TOOL-SPEC §1.6。

        命令形式（CLI 报告 §35）::

            bsk request-help --prompt <P> --title <T> --target <T>...
                             --timeout <T> --completion-criteria <JSON>
                             --session <ID> --json

        Note:
            ``--target`` **可重复**（报告 §35 原文），所以每个目标一个
            ``--target``。

        Note:
            ``--completion-criteria`` 的整体值就是那个 JSON 字符串
            （报告 §35 的示例原文：
            ``'{"any":[{"url_contains":"/dashboard"}],"stable_for_ms":1000}'``）。
            注意示例里的键是 **snake_case**（``url_contains`` /
            ``stable_for_ms``），所以本方法收下调用方给的 snake_case 字典后
            **整体**序列化成**一个** argv 元素，不做任何二次改写 ——
            逐键翻译只会把两个已经对齐的命名空间又拧开一次。
        """
        session_key = self._resolve(key)
        args: list[str] = ["--prompt", str(prompt)]
        if title:
            args += ["--title", str(title)]
        for target in targets or []:
            args += ["--target", str(target)]
        args += ["--timeout", self._bsk_duration(timeout_ms)]
        if completion_criteria:
            args += [
                "--completion-criteria",
                self._render_debug_value("completion_criteria", completion_criteria),
            ]

        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(["request-help", *args], tab_id)
            + ["--session", sid, "--json"],
            # 这是**人在回路**的操作：真人要走到电脑前、看清提示、动手做完。
            # 所以外层超时必须比 bsk 自己的预算（--timeout，默认 5m）更大，
            # 否则我们会在人还没做完的时候先把它掐掉。
            timeout=self._wait_timeout(timeout_ms),
            # 只读：它不改页面，只是显示一个面板并等人。
            allow_uncertain=True,
        )
        data = result.data if isinstance(result.data, dict) else {}
        outcome = data.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            # 认不出 outcome 时**不猜**成 continued（那会被模型当成"用户可以继续了"，
            # 而真人可能根本没动手）。给一个明确的未知值 + 原始载荷。
            outcome = ""
        out: dict[str, Any] = {
            "session": self._session_field(session_key),
            "outcome": outcome,
            "continued": outcome in ("continued", "completed"),
        }
        if isinstance(data, dict):
            for name, value in data.items():
                if name == "outcome":
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None:
                    out.setdefault(name, value)
        return out

    # ------------------------------------------------------------------
    # bsk_inspect(screenshot) / 兼容层：截图的扩展版
    # ------------------------------------------------------------------

    async def screenshot_ex(
        self,
        key: str,
        *,
        ref: str = "",
        full_page: bool = False,
        tab_id: int | None = None,
    ) -> ShotPayload:
        """screenshot 的扩展版（多 ref）。既有的 screenshot(key, full_page=)
        保持不变并委托到本方法。

        命令形式（CLI 报告 §10）::

            bsk screenshot --ref <REF> --full-page --out <PATH> --session <ID> --json

        Note:
            ``ref`` 与 ``full_page`` 在 CLI 报告里是两个独立选项
            （``--ref`` 把截图裁剪到该元素或 Canvas 区域；``--full-page``
            是滚动拼接全页），报告没有说它们互斥，所以本方法两个都照传。
            Canvas 点击（``click --capture/--image-x/--image-y``）依赖
            ``screenshot --ref`` 返回的 capture id，所以 ``ref`` 非空时
            我们**不**加 ``--full-page``：报告明确说 ``--capture`` 绑定的是
            "``screenshot --ref`` 返回的图像"，而全页拼接出来的图不是那个区域。
        """
        session_key = self._resolve(key)
        directory = self.settings.screenshot_dir or self._default_shot_dir()
        out_path = make_shot_path(directory, session_key)

        use_full_page = bool(full_page) and not ref
        timeout = (
            self._timeout(self.fullpage_budget())
            if use_full_page
            else self._timeout(TIMEOUT_SCREENSHOT)
        )

        argv = ["screenshot", "--out", str(out_path)]
        if ref:
            argv += ["--ref", ref]
        if use_full_page:
            argv.append("--full-page")

        result = await self.sessions.execute(
            session_key,
            lambda sid: self._with_tab_id(argv, tab_id)
            + ["--session", sid, "--json"],
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
                self._logger.debug("清理了 %d 张旧截图", removed)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("截图清理失败（忽略）：%r", exc)

        payload = ShotPayload(
            path=shot.path,
            width=shot.width,
            height=shot.height,
            byte_size=shot.byte_size,
            for_llm=self._describe_shot(shot, use_full_page),
            warning=warning,
        )
        if ref:
            payload.for_llm += f"（已裁剪到元素 {ref}。）"
        return payload


# --- 新方法用的表格与常量 ------------------------------------------------
#
# 这些表格是"bsk 子命令 → argv"的**唯一事实源**：``act``（既有）与
# ``interact``（新）都从它们构造命令行，所以同一个动作用哪条路径调用，
# 发出去的命令都逐字节相同。每一条后面标了 CLI 报告的章节。

# ``--timeout`` 的额外余量：纯等待类命令的外层超时 = 它自己的预算 + 这么多。
#
# 为什么需要：``wait_for`` 与 ``request_help`` 把"等多久"交给了 bsk 自己
# （``--timeout``），我们的外层超时只是防它卡死的兜底。取 5 秒与
# ``FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC`` 同一个数量级：足够它把结果写回
# stdout，又不至于让"没完成"的等待多拖很久。
WAIT_TIMEOUT_MARGIN_SEC = 5.0

TIMEOUT_HISTORY = 20.0
"""``reload`` / ``navigate-back`` / ``navigate-forward`` 的下限（秒）。

依据 CLI 报告：这三条命令自己的 ``--timeout`` 默认值都是 **15s**
（§18/§19/§20），比 ``navigate`` 的 30s 低一档。所以它们的下限取 20 秒 ——
比 bsk 自己的默认值大，满足"外层超时必须大于 bsk 自身的 --timeout"这条
原则（见模块顶部），又不必像 navigate 那样留到 45 秒。
"""

TIMEOUT_DEBUG = 20.0
"""``debug`` 的下限（秒）。

CLI 报告 §15 里 ``debug`` 的各个选项没有统一的自带超时（只有 ``--wait-ms``
默认 10000）。取 20 秒 = ``wait-ms`` 默认值的两倍，足够覆盖"等一下再读证据"
的常见用法，又不会在 daemon 不响应时把用户挂太久。
"""

EMULATE_DEVICES: frozenset[str] = frozenset(
    {
        "iphone-14",
        "iphone-14-pro-max",
        "iphone-se",
        "pixel-7",
        "galaxy-s23",
        "ipad-mini",
        "galaxy-tab-s8",
    }
)
"""``bsk emulate --device`` 的 7 个内置预设（CLI 报告 §9 逐字列出）。

这是**白名单**而不是"格式校验"：转发一个 bsk 不认识的设备名换来的是底层
报错（模型看不出该怎么办），而用不可核实的值去选目标在 ``--browser`` 上
已经有"静默选错"的实证。发不出去比发错好。
"""

MUTATING_DEBUG_ACTIONS: frozenset[str] = frozenset(
    {
        "rule_add",
        "rule_enable",
        "rule_disable",
        "rule_remove",
        "replay",
    }
)
"""``bsk debug`` 里**会改变服务端数据/真实流量**的 action。

它们不能用 ``allow_uncertain=True``（见 :meth:`BskService.debug`）：

- ``replay`` —— 会**带着用户的 cookie 重新发送**抓到的请求，DSH 自己的描述
  就是 "may change server data"；
- ``rule_add`` / ``rule_enable`` / ``rule_disable`` / ``rule_remove`` ——
  能改写/伪造真实请求与响应。

其余 action（``performance`` / ``requests`` / ``console`` / ``export`` /
``capabilities`` / ``wait`` …）都只是读证据或等待，属于只读。
"""


@dataclass(slots=True)
class _InteractSpec:
    """一条交互动作 → bsk 命令的映射。

    Attributes:
        argv: 命令头（例如 ``["scroll-to"]``；``["click"]``）。
        leading_positionals: 必填的**前置位置参数**名（``press`` 的 ``key``）。
        allows_target: ``target`` 是否作为位置参数拼进去。
        requires_target: 是否要求 ``target`` 非空。
        value_flags: ``(参数名, flag)`` 列表，按固定顺序拼接。
        bool_flags: ``(参数名, flag)`` 开关。
        ms_flags: ``(参数名, flag)``，值是毫秒，渲染成 ``30s``/``1500ms``。
        readonly: True 表示允许在会话不确定态下执行。
    """

    argv: tuple[str, ...]
    leading_positionals: tuple[str, ...] = ()
    allows_target: bool = False
    requires_target: bool = False
    value_flags: tuple[tuple[str, str], ...] = ()
    bool_flags: tuple[tuple[str, str], ...] = ()
    ms_flags: tuple[tuple[str, str], ...] = ()
    readonly: bool = False


# 9 个 action（TOOL-SPEC §1.4）。CLI 报告的章节：§21 click、§22 hover、
# §23 wheel、§24 scroll-to、§25 focus、§26 blur、§27 fill、§29 select、
# §28 press。
#
# readonly 的判定标准是"这个动作会不会改变页面数据"：
#   - wheel / scroll_to / focus / blur 只改变视口或焦点，不提交任何东西，
#     不确定态下做它们是安全的（而且常常正是"看清楚刚才到底点了什么"所需）；
#   - click / hover / fill / select / press 会改页面数据（悬停会展开菜单、
#     触发页面自己的 JS），一律按写类动作处理。
#
# ⚠️ ``press`` 的 target 走 ``--ref``（CLI 报告 §28：``--ref`` 是"派发按键前
# 先聚焦的可选 snapshot ref"）—— 这是 TOOL-SPEC §1.4 点名的"唯一特例"。
# 这里只发 ``--ref``，不发 ``--selector``：两个都发是非法组合，而
# "用户到底想要哪个"无从判断（ref 与 selector 的判别规则在上层校验层）。
INTERACT_SPECS: dict[str, _InteractSpec] = {
    "click": _InteractSpec(
        argv=("click",),
        allows_target=True,
        requires_target=True,
        value_flags=(
            ("button", "--button"),
            ("click_count", "--click-count"),
            ("modifiers", "--modifiers"),
            ("capture_id", "--capture"),
            ("image_x", "--image-x"),
            ("image_y", "--image-y"),
        ),
        ms_flags=(("timeout_ms", "--timeout"),),
    ),
    "hover": _InteractSpec(
        argv=("hover",),
        allows_target=True,
        requires_target=True,
        value_flags=(("modifiers", "--modifiers"),),
        ms_flags=(
            ("settle_ms", "--settle"),
            ("timeout_ms", "--timeout"),
        ),
    ),
    "wheel": _InteractSpec(
        argv=("wheel",),
        allows_target=True,
        value_flags=(
            ("delta_x", "--delta-x"),
            ("delta_y", "--delta-y"),
            ("modifiers", "--modifiers"),
        ),
        ms_flags=(("timeout_ms", "--timeout"),),
        readonly=True,
    ),
    # 键用连字符，与 bsk CLI 的子命令名、以及 bsk/tools.py 里
    # TOOL_ACTIONS 的写法逐字一致（DSH 的 action 名也是 "scroll-to"）。
    # 曾经这里写成下划线的 "scroll_to"，而 tools.validate 输出的是连字符，
    # 两边对不上导致该 action 落进"没有对应子命令"分支、完全不可用。
    "scroll-to": _InteractSpec(
        argv=("scroll-to",),
        allows_target=True,
        requires_target=True,
        ms_flags=(("timeout_ms", "--timeout"),),
        readonly=True,
    ),
    "focus": _InteractSpec(
        argv=("focus",),
        allows_target=True,
        requires_target=True,
        ms_flags=(("timeout_ms", "--timeout"),),
        readonly=True,
    ),
    "blur": _InteractSpec(
        argv=("blur",),
        allows_target=True,
        requires_target=True,
        ms_flags=(("timeout_ms", "--timeout"),),
        readonly=True,
    ),
    "fill": _InteractSpec(
        argv=("fill",),
        allows_target=True,
        requires_target=True,
        # fill 的 --value 是必填的（CLI 报告 §27），而且**空串也要发出去**：
        # "把字段清空"是一个正当操作，按"空值就跳过"的通用规则处理会做不到
        # 这件事（还会把一次清空悄悄变成一次不改动）。例外登记在
        # _EMPTY_VALUE_OK 里。
        value_flags=(("value", "--value"),),
        bool_flags=(("no_clear", "--no-clear"),),
        ms_flags=(("timeout_ms", "--timeout"),),
    ),
    "select": _InteractSpec(
        argv=("select",),
        allows_target=True,
        requires_target=True,
        value_flags=(("values", "--value"),),
        ms_flags=(("timeout_ms", "--timeout"),),
    ),
    "press": _InteractSpec(
        argv=("press",),
        leading_positionals=("key",),
        value_flags=(("target", "--ref"),),
        ms_flags=(
            ("hold_ms", "--hold-ms"),
            ("timeout_ms", "--timeout"),
        ),
    ),
}

# ``fill`` 的 ``--value`` 允许是空串（"清空这个字段"）。
# 单独一张表而不是在 spec 里加字段：只有它一条有这个需求，加一个只被用到
# 一次的开关会让 _InteractSpec 变复杂，而复杂度正是要省的东西。
_EMPTY_VALUE_OK: frozenset[tuple[str, str]] = frozenset({("fill", "value")})

# ``press`` 要按的键在 ``interact(**opts)`` 里可以用的名字。
#
# 为什么需要别名：``interact(self, key, action, **opts)`` 的形参 ``key`` 就是
# 会话键，而 TOOL-SPEC §1.4 给 press 的按键参数起的名字也是 ``key`` ——
# 后者因此**永远传不进** ``**opts``（Python 会先报 "multiple values for
# argument 'key'"）。这是接口冻结里的一处矛盾，本文件不擅自改签名，
# 只在 opts 层兼容一个不冲突的名字。保留 "key" 是为了让
# ``interact(k, "press", **{"key": "Enter"})`` 这种写法也说得通
# （技术上它进不了 opts，留着只是让人一眼看出对应关系）。
_PRESS_KEY_ALIASES: tuple[str, ...] = ("press_key", "key")


@dataclass(slots=True)
class _TabSpec:
    """一条标签动作 → bsk 命令的映射。"""

    argv: tuple[str, str]
    takes_tab_id: bool


# 6 个 action（TOOL-SPEC §1.5），全部来自 CLI 报告 §7。
TABS_SPECS: dict[str, _TabSpec] = {
    "list": _TabSpec(argv=("tab", "list"), takes_tab_id=False),
    "create": _TabSpec(argv=("tab", "create"), takes_tab_id=False),
    "select": _TabSpec(argv=("tab", "select"), takes_tab_id=True),
    "close": _TabSpec(argv=("tab", "close"), takes_tab_id=True),
    "borrow": _TabSpec(argv=("tab", "borrow"), takes_tab_id=True),
    "return": _TabSpec(argv=("tab", "return"), takes_tab_id=True),
}

# ``bsk debug`` 的带值选项（CLI 报告 §15 全表）。键是参数名（snake_case），
# 值是命令行 flag。**只包含报告里真实存在的项**，一个都不加。
DEBUG_VALUE_FLAGS: dict[str, str] = {
    "tab_id": "--tab-id",
    "run_id": "--run-id",
    "name": "--name",
    "since": "--since",
    "limit": "--limit",
    "part": "--part",
    "offset": "--offset",
    "max_chars": "--max-chars",
    "pointer": "--pointer",
    "rule": "--rule",
    "rule_file": "--rule-file",
    "replay": "--replay",
    "replay_file": "--replay-file",
    "budget": "--budget",
    "slow_ms": "--slow-ms",
    "window_ms": "--window-ms",
    "url": "--url",
    "method": "--method",
    "resource_type": "--resource-type",
    "status": "--status",
    "state": "--state",
    "kind": "--kind",
    "fields": "--fields",
    "wait_ms": "--wait-ms",
    "command_id": "--command-id",
    "output": "--output",
}

DEBUG_BOOL_FLAGS: dict[str, str] = {
    "include_controlled": "--include-controlled",
}


class ServiceHelper:
    """给上面的表格用的小工具集合（不实例化，纯静态）。"""

    @staticmethod
    def ms_value(value: Any) -> str:
        """毫秒 → bsk 的时间字面量（``200ms`` / ``1s``）。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number <= 0:
            number = 1.0
        if number % 1000 == 0:
            return f"{int(number) // 1000}s"
        return f"{int(number)}ms"
