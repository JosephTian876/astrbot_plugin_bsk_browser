"""bsk 0.4.x / 0.5.0 协作控制协议（``cooperate sync`` / ``--context`` / ``--observation``）纯逻辑层。

背景（GitHub Issue #7）：
从 ``bsk 0.4.0`` 起，所有新建的会话及其任务标签页默认启用人机协作控制；
``0.4.1`` / ``0.4.2`` / ``0.5.0`` 进一步引入 ``target`` 策略，把控制凭证
（``--context``）与严格观察基线（``--observation``）拆开：

- **DOM 目标动作**（``click`` / ``fill`` / ``select`` / ``hover`` / ``scroll-to`` /
  ``focus`` / ``blur`` / 带 ``--ref`` 的 ``press`` / 带目标的 ``wheel``）：
  在 ``target`` 策略下只需携带 ``--context <context_id>``，且控制凭证可跨多次
  DOM 动作与补充读取复用；
- **非目标 / 严格观察动作**（``screenshot --full-page`` / ``evaluate`` /
  无目标 ``press`` 或 ``wheel`` / Canvas 视觉点击 ``click --capture`` /
  ``navigate back/forward`` / ``reload`` / 写类 ``debug`` 等）：
  除 ``--context`` 外，在 ``target`` 策略下还强制要求来自 ``cooperate sync``
  顶层的未使用 ``--observation <observation_id>``（绝不能用嵌套的
  ``observation.observation_id``），且执行后会消耗该严格观察凭证；
- **首屏引导动作**（``navigate <url>`` / ``window resize`` / ``emulate`` /
  ``tab close``）：在未碰过的初始空白页上可免凭证执行一次，离开空白页后
  同严格观察动作处理；
- **只读与管理命令**（``observe`` / ``snapshot`` / ``get-html`` / 视口或元素
  ``screenshot`` / ``console`` / ``network`` / ``wait-for-navigation`` /
  ``tab list/create/select/borrow/return`` / ``request-help`` / 只读 ``debug``）：
  不携带也不消耗控制凭证。

老版本 ``bsk <= 0.3.3`` 不存在 ``cooperate`` 子命令，也不会发送任何协作参数。

本模块是纯函数 + 纯数据类集合：零第三方依赖、零 IO、不 import ``astrbot``
也不 import 内置 ``logging``，可独立单测。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "COOP_MODE_NONE",
    "COOP_MODE_STRICT",
    "COOP_MODE_STRICT_BOOTSTRAP",
    "COOP_MODE_TARGETED",
    "COOP_PROBE_TIMEOUT_SEC",
    "COOP_SYNC_TIMEOUT_SEC",
    "CooperationCapability",
    "CoopReceipt",
    "attach_coop_flags",
    "classify_command_coop",
    "command_invalidates_tab_context",
    "command_invalidates_window_context",
    "command_leaves_blank_tab",
    "command_switches_default_tab",
    "extract_sync_observation",
    "extract_target_tab_id",
    "has_snapshot_ref_target",
    "is_readonly_strict_command",
]

# --- 协作模式分类常量 ---

COOP_MODE_NONE = "none"
"""只读或会话/标签管理命令：不需要 ``--context`` / ``--observation``。"""

COOP_MODE_TARGETED = "targeted"
"""带 DOM 目标的交互动作：``target`` 策略下只需 ``--context`` 且可复用。"""

COOP_MODE_STRICT = "strict"
"""无目标/视觉/脚本/整页截图等严格动作：需要 ``--context`` 与未使用的顶层 ``--observation``。"""

COOP_MODE_STRICT_BOOTSTRAP = "strict_bootstrap"
"""初始空白页上允许免凭证执行一次，之后与 ``strict`` 相同。"""

# --- 超时常量 ---

COOP_PROBE_TIMEOUT_SEC = 10.0
"""能力探测（``bsk cooperate --help``）的超时（秒），每进程只跑一次。"""

COOP_SYNC_TIMEOUT_SEC = 20.0
"""单次 ``bsk cooperate sync`` 的超时上限（秒）。"""

# --- 命令分类表（对照 Tencent/BrowserSkill 0.5.0 的 tools/policy.ts 与 tools/debug.rs） ---

_TARGETED_COMMANDS: frozenset[str] = frozenset(
    {
        "hover",
        "scroll-to",
        "focus",
        "blur",
        "fill",
        "select",
    }
)

_STRICT_COMMANDS: frozenset[str] = frozenset(
    {
        "navigate-back",
        "navigate-forward",
        "reload",
    }
)

_COOP_GUARDED_DEBUG_ACTIONS: frozenset[str] = frozenset(
    {
        "rule_add",
        "rule_enable",
        "rule_disable",
        "rule_remove",
        "replay",
    }
)

# 跨子命令通用且带一个参数值的选项，用于跳过选项提取位置参数。
_COMMON_VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "--session",
        "--scope",
        "--tab-id",
        "--context",
        "--observation",
        "--expect",
        "--depends-on",
        "--timeout",
        "--ref",
        "--selector",
    }
)

# 各子命令特有的带值选项（只列会进入本模块位置参数解析的命令）。
_COMMAND_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "click": frozenset({"--button", "--click-count", "--modifiers", "--capture", "--image-x", "--image-y"}),
    "hover": frozenset({"--modifiers", "--settle"}),
    "wheel": frozenset({"--delta-x", "--delta-y", "--modifiers"}),
    "fill": frozenset({"--value"}),
    "select": frozenset({"--value"}),
    "press": frozenset({"--hold-ms"}),
    "navigate": frozenset({"--wait-until"}),
    "tab": frozenset({"--url", "--index"}),
    "debug": frozenset(
        {
            "--run-id",
            "--name",
            "--since",
            "--limit",
            "--part",
            "--offset",
            "--max-chars",
            "--pointer",
            "--rule",
            "--rule-file",
            "--replay",
            "--replay-file",
            "--budget",
            "--slow-ms",
            "--window-ms",
            "--url",
            "--method",
            "--resource-type",
            "--status",
            "--state",
            "--kind",
            "--fields",
            "--wait-ms",
            "--command-id",
            "--output",
        }
    ),
}

_REF_TOKEN_RE = re.compile(r"^@?e\d+$")


@dataclass(slots=True, frozen=True)
class CooperationCapability:
    """探测到的 bsk 协作控制协议能力。

    版本对应关系（实测与上游源码确认）：

    - ``bsk <= 0.3.3``：``supported=False``；
    - ``bsk 0.4.0``：``supported=True, supports_observation=False, supports_policy=False``；
    - ``bsk >= 0.4.1``（含 ``0.4.2`` / ``0.5.0``）：四项均为 ``True``。
    """

    supported: bool = False
    supports_observation: bool = False
    supports_settle: bool = False
    supports_policy: bool = False

    @classmethod
    def from_help_text(cls, stdout: str) -> CooperationCapability:
        """从 ``bsk cooperate --help`` 的输出解析协作能力。

        故意要求同时出现 ``sync`` 与 ``--context``：防止空输出、假测试桩或
        不相关的帮助文本被误判为支持协作协议。
        """
        text = stdout or ""
        if "sync" not in text or "--context" not in text:
            return cls()
        return cls(
            supported=True,
            supports_observation="--observation" in text,
            supports_settle="--settle" in text,
            supports_policy="--policy" in text,
        )


@dataclass(slots=True)
class CoopReceipt:
    """一次 ``bsk cooperate sync --json`` 颁发的控制凭证。

    ⚠️ 严格遵守 bsk 契约：``observation_id`` 只能取响应**顶层**的
    ``observation_id``，绝不能取嵌套的 ``observation.observation_id``
    （后者是渲染后的 ref map 标识，传给 ``--observation`` 会被拒绝）。
    """

    context_id: str = ""
    observation_id: str = ""
    control_policy: str = ""
    tab_id: int | None = None
    kind: str = ""
    context_consumed: bool = False
    observation_consumed: bool = False

    @classmethod
    def from_sync_json(cls, data: Any) -> CoopReceipt | None:
        """从 ``cooperate sync --json`` 载荷提取凭证；无有效 ``context_id`` 返回 None。"""
        if not isinstance(data, dict):
            return None
        raw_ctx = data.get("context_id")
        if not isinstance(raw_ctx, str) or not raw_ctx.strip():
            return None

        raw_obs = data.get("observation_id")
        observation_id = raw_obs.strip() if isinstance(raw_obs, str) else ""

        raw_policy = data.get("control_policy")
        control_policy = raw_policy.strip().lower() if isinstance(raw_policy, str) else ""

        raw_kind = data.get("kind")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""

        raw_tab = data.get("tab_id")
        tab_id = raw_tab if isinstance(raw_tab, int) and not isinstance(raw_tab, bool) else None

        return cls(
            context_id=raw_ctx.strip(),
            observation_id=observation_id,
            control_policy=control_policy,
            tab_id=tab_id,
            kind=kind,
        )

    @property
    def is_target_policy(self) -> bool:
        """该会话是否已协商为 ``target`` 控制策略。"""
        return self.control_policy == "target"

    def has_valid_context(self) -> bool:
        """是否存在尚未消耗的 ``context_id``。"""
        return bool(self.context_id) and not self.context_consumed

    def has_valid_observation(self) -> bool:
        """是否存在尚未消耗的顶层 ``observation_id``。"""
        return bool(self.observation_id) and not self.observation_consumed


def _has_flag(args: Sequence[str], flag: str) -> bool:
    """检查命令行列表里是否已出现指定长选项（支持 ``--flag`` 与 ``--flag=...``）。"""
    prefix = f"{flag}="
    return any(tok == flag or tok.startswith(prefix) for tok in args)


def _flag_value(args: Sequence[str], flag: str) -> str:
    """取出指定长选项的值；找不到返回空串。"""
    prefix = f"{flag}="
    for idx, tok in enumerate(args):
        if tok == flag and idx + 1 < len(args):
            return str(args[idx + 1])
        if tok.startswith(prefix):
            return str(tok[len(prefix) :])
    return ""


def _positional_args(args: Sequence[str], *, cmd: str) -> list[str]:
    """跳过选项与选项值，返回子命令 ``cmd`` 之后的位置参数列表。"""
    value_flags = _COMMON_VALUE_FLAGS | _COMMAND_VALUE_FLAGS.get(cmd, frozenset())
    positionals: list[str] = []
    i = 1
    n = len(args)
    after_double_dash = False
    while i < n:
        tok = str(args[i])
        if after_double_dash:
            positionals.append(tok)
            i += 1
            continue
        if tok == "--":
            after_double_dash = True
            i += 1
            continue
        if tok in value_flags:
            i += 2
            continue
        if tok.startswith("-"):
            # 含 `--flag=val` 或布尔开关（`--json` / `--full-page` / `--no-clear` 等）。
            i += 1
            continue
        positionals.append(tok)
        i += 1
    return positionals


def classify_command_coop(args: Sequence[str]) -> str:
    """按命令形态判定它对协作凭证的需求级别。

    Returns:
        ``COOP_MODE_NONE`` / ``COOP_MODE_TARGETED`` /
        ``COOP_MODE_STRICT`` / ``COOP_MODE_STRICT_BOOTSTRAP`` 之一。
    """
    if not args:
        return COOP_MODE_NONE

    cmd = str(args[0])

    if cmd in _TARGETED_COMMANDS:
        return COOP_MODE_TARGETED

    if cmd in _STRICT_COMMANDS:
        return COOP_MODE_STRICT

    if cmd == "click":
        # Canvas 视觉点击没有 DOM 接收者，bsk 强制要求严格观察凭证。
        if (
            _has_flag(args, "--capture")
            or _has_flag(args, "--image-x")
            or _has_flag(args, "--image-y")
        ):
            return COOP_MODE_STRICT
        return COOP_MODE_TARGETED

    if cmd == "press":
        if _has_flag(args, "--ref") or _has_flag(args, "--selector"):
            return COOP_MODE_TARGETED
        return COOP_MODE_STRICT

    if cmd == "wheel":
        if _has_flag(args, "--ref") or _has_flag(args, "--selector"):
            return COOP_MODE_TARGETED
        if _positional_args(args, cmd="wheel"):
            return COOP_MODE_TARGETED
        return COOP_MODE_STRICT

    if cmd == "screenshot":
        return COOP_MODE_STRICT if _has_flag(args, "--full-page") else COOP_MODE_NONE

    if cmd == "evaluate":
        if _has_flag(args, "--unverified"):
            return COOP_MODE_TARGETED
        if _has_flag(args, "--expect") and not _has_flag(args, "--observation"):
            return COOP_MODE_TARGETED
        return COOP_MODE_STRICT

    if cmd == "navigate":
        pos = _positional_args(args, cmd="navigate")
        if pos and pos[0] in ("back", "forward"):
            return COOP_MODE_STRICT
        return COOP_MODE_STRICT_BOOTSTRAP

    if cmd == "window":
        pos = _positional_args(args, cmd="window")
        if pos and pos[0] == "resize":
            return COOP_MODE_STRICT_BOOTSTRAP
        return COOP_MODE_NONE

    if cmd == "emulate":
        return COOP_MODE_STRICT_BOOTSTRAP

    if cmd == "tab":
        pos = _positional_args(args, cmd="tab")
        if pos and pos[0] == "close":
            return COOP_MODE_STRICT_BOOTSTRAP
        return COOP_MODE_NONE

    if cmd == "debug":
        pos = _positional_args(args, cmd="debug")
        if pos and pos[0] in _COOP_GUARDED_DEBUG_ACTIONS:
            return COOP_MODE_STRICT
        return COOP_MODE_NONE

    return COOP_MODE_NONE


def extract_target_tab_id(args: Sequence[str]) -> int | None:
    """从命令参数里提取目标标签页 ID（支持 ``--tab-id`` 与 ``tab close <ID>``）。"""
    if not args:
        return None
    raw = _flag_value(args, "--tab-id")
    if not raw and str(args[0]) == "tab":
        pos = _positional_args(args, cmd="tab")
        if len(pos) >= 2 and pos[0] == "close":
            raw = pos[1]
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def has_snapshot_ref_target(args: Sequence[str]) -> bool:
    """命令是否以快照元素编号（``@eN`` / ``eN``）作为操作目标。

    用于安全防线：如果自动 ``cooperate sync`` 触发了新的 ``baseline``（意味着
    页面结构或文档已变化、ref 编号可能重排），绝不能拿旧的 ``@eN`` 继续点击或填写。
    """
    if not args:
        return False
    ref_flag = _flag_value(args, "--ref").strip()
    if ref_flag and _REF_TOKEN_RE.match(ref_flag):
        return True
    cmd = str(args[0])
    if cmd in _TARGETED_COMMANDS or cmd in ("click", "wheel"):
        pos = _positional_args(args, cmd=cmd)
        if pos and _REF_TOKEN_RE.match(pos[0].strip()):
            return True
    return False


def command_invalidates_tab_context(args: Sequence[str]) -> bool:
    """该命令成功后是否会替换当前文档，从而使该标签页的旧凭证全部失效。"""
    if not args:
        return False
    return str(args[0]) in ("navigate", "navigate-back", "navigate-forward", "reload")


def command_invalidates_window_context(args: Sequence[str]) -> bool:
    """该命令成功后是否会清空整个会话窗口的观察基线（``window resize``）。"""
    if not args or str(args[0]) != "window":
        return False
    pos = _positional_args(args, cmd="window")
    return bool(pos and pos[0] == "resize")


def command_switches_default_tab(args: Sequence[str]) -> bool:
    """该命令成功后是否会改变会话的默认任务标签页。"""
    if not args or str(args[0]) != "tab":
        return False
    pos = _positional_args(args, cmd="tab")
    if not pos:
        return False
    if pos[0] == "create":
        return not _has_flag(args, "--no-active")
    return pos[0] in ("select", "borrow", "return")


def command_leaves_blank_tab(args: Sequence[str]) -> bool:
    """该命令成功后是否意味着当前活动标签页不再保证是初始空白页。"""
    return command_invalidates_tab_context(args) or command_switches_default_tab(args)


def is_readonly_strict_command(args: Sequence[str]) -> bool:
    """是否属于只读型严格动作（无页面写副作用，安全支持一次自动重同步重试）。

    目前只有 ``screenshot --full-page``：它只滚动截屏并恢复滚动条位置，
    不会点击按钮或提交表单。动态页面上若旧的 ``observation_id`` 在截屏前
    刚好过期，重新 ``cooperate sync`` 一次再截是完全安全的。
    """
    return bool(args) and str(args[0]) == "screenshot" and _has_flag(args, "--full-page")


def attach_coop_flags(
    args: Sequence[str],
    receipt: CoopReceipt,
    *,
    mode: str,
    capability: CooperationCapability,
) -> list[str]:
    """按需把 ``--context`` 与 ``--observation`` 拼到命令行参数末尾（不改原列表）。"""
    out = list(args)
    if mode == COOP_MODE_NONE:
        return out
    if receipt.context_id and not _has_flag(out, "--context"):
        out += ["--context", receipt.context_id]
    if (
        mode in (COOP_MODE_STRICT, COOP_MODE_STRICT_BOOTSTRAP)
        and capability.supports_observation
        and receipt.observation_id
        and not _has_flag(out, "--observation")
    ):
        out += ["--observation", receipt.observation_id]
    return out


def extract_sync_observation(data: Any) -> dict[str, Any] | None:
    """从 ``cooperate sync --json`` 响应里取出内嵌的 VOM 观察载荷（仅 baseline 形态有）。

    ``kind == "baseline"`` 时响应里自带完整 ``observation``（``{text, ref_count, tab_id, ...}``），
    可直接交给 ``pages.parse_observation``，省掉一次额外的 ``bsk observe`` 往返；
    ``kind == "unchanged"`` / ``"delta"`` 时没有该字段，返回 None。
    """
    if not isinstance(data, dict):
        return None
    obs = data.get("observation")
    if not isinstance(obs, dict):
        return None
    if not isinstance(obs.get("text"), str):
        return None
    # 顶层有 tab_id 而嵌套块缺失时顺手补上，不修改原字典。
    if "tab_id" not in obs and isinstance(data.get("tab_id"), int):
        merged = dict(obs)
        merged["tab_id"] = data["tab_id"]
        return merged
    return obs
