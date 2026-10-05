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
from .errors import CODE_BROWSER_AMBIGUOUS, BskBrowserAmbiguous, BskError
from .journal import SessionJournal
from .logger import NULL_LOGGER, LoggerLike
from .models import (
    BrowserInstance,
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
        """未配置截图目录时的默认位置：插件数据目录下的 ``shots``。

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
            when = ""
            if entry.timestamp > 0:
                when = f"{time.strftime('%H:%M:%S', time.localtime(entry.timestamp))} "
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
