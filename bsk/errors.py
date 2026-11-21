"""bsk 的退出码与错误分类。

bsk 的退出码有 6 档（实测确认），错误 JSON 走 stdout 而不是 stderr，
结构是扁平的 ``{code, message, hint, exit_code, data?}``：

    {
      "code": "not_found",
      "message": "session not registered or already stopped",
      "hint": "run `bsk session list` to see current state",
      "exit_code": 1
    }

但有三种情况拿不到 JSON，调用方必须能容错：

1. clap 参数错误（例如写了不存在的 flag）→ stderr 纯文本，退出码 1；
2. 进程被我们杀掉（超时）→ 可能没有任何输出；
3. 极少数命令不带 ``--json`` 时输出人类可读文本。

因此判成败只看退出码，JSON 解析只是锦上添花。
"""

from __future__ import annotations

# --- 退出码（来自 bsk 源码 render_error.rs，实测确认） ---

EXIT_OK = 0
"""成功。"""

EXIT_USER_ERROR = 1
"""用户错误：参数错误、沙箱限制、实体不存在（session 没了等）。

注意：clap 的参数解析失败也归到这一档。
"""

EXIT_PROTOCOL = 2
"""协议/传输层错误，包含 cancelled 与本地未预期的错误。"""

EXIT_BROWSER = 3
"""浏览器或 CDP 层错误（扩展断了、页面崩了等）。"""

EXIT_TIMEOUT = 4
"""命令超时。"""

EXIT_VERSION = 5
"""版本不匹配（CLI 与扩展协议版本对不上）。"""

EXIT_DIALOG_PENDING = 6
"""页面弹出原生对话框（alert/confirm/prompt），原操作被挂起（bsk >= 0.4.0）。"""

# --- bsk 返回的 error code 字符串 ---

CODE_NOT_FOUND = "not_found"
"""session/tab/browser 不存在或已停止。可以重建会话后重试。"""

CODE_SESSION_BUSY = "session_busy"
"""同一 session 上有 in-flight 命令。可以短暂等待后重试一次。"""

CODE_PERMISSION_DENIED = "permission_denied"
"""权限或协作策略拒绝（bsk >= 0.4.0 下按 data.reason 细分为窗口归属、协作同步、人工接管、目标不可操作等）。"""

CODE_BROWSER_AMBIGUOUS = "browser_ambiguous"
"""同时连着多个浏览器，而用户没有指定用哪一个。必须改配置才能继续，重试无用。"""

CODE_USER_ABORTED = "user_aborted"
"""用户在浏览器浮层里主动点击了「停止任务」（bsk >= 0.4.0）。禁止自动恢复或重试。"""

CODE_DIALOG_PENDING = "dialog_pending"
"""原生 JS 对话框挂起了当前操作（bsk >= 0.4.0）。禁止重放提交。"""

CODE_UNSUPPORTED = "unsupported"
"""对端组件（daemon 或扩展）不支持该特性或协议版本过旧。"""

CODE_CANCELLED = "cancelled"
"""可恢复启动的请求已被取消。

实测（bsk 0.3.2）：对已取消的令牌调 ``session start --request-id`` 返回
``exit=2`` + ``{"code": "cancelled", "message": "start request cancelled..."}``；
对 ``prepared`` 状态调 ``--claim`` 返回同一形状（``start request cannot be claimed``）。

**它是"已按调用方的要求抑制"的成功语义，不是可重试的失败** ——
绝不能因此回退到不带 ``--request-id`` 的普通 start（那会开出一个
没有任何所有权凭据保护的窗口，正是这次要修的 bug）。
"""

# 这些 reason 出现在错误 JSON 的 data.reason 里，表示"动作可能已经生效"，
# 此时绝对禁止重试，否则可能重复点击/重复提交表单。
OUTCOME_UNKNOWN_REASONS: frozenset[str] = frozenset(
    {
        "extension_reconnected",
        "extension_disconnected",
        "input_outcome_unknown",
        "result_outcome_unknown",
        "transfer_outcome_unknown",
        "borrow_outcome_unknown",
    }
)

# 这些 reason 表示当前标签页缓存的协作控制凭证已经失效，必须清掉本地缓存。
COOPERATION_INVALIDATING_REASONS: frozenset[str] = frozenset(
    {
        "cooperation_sync_required",
        "control_context_invalid",
        "document_changed",
        "human_intervention",
        "control_held",
        "control_authority_required",
        "uncertain_prior_effect",
        "user_moved_tab",
    }
)

# 只读型严格命令（如 screenshot --full-page）遇到这些 reason（且 effect_state != unknown）时，
# 可以安全地重新执行一次 cooperate sync 再重试一次。
COOPERATION_READONLY_RETRY_REASONS: frozenset[str] = frozenset(
    {
        "cooperation_sync_required",
        "control_context_invalid",
        "observation_required",
        "observation_changed",
        "document_changed",
    }
)

# 这些 reason 表示目标页面当前无法建立协作采集（例如初始 about:blank 或受限页），
# 只读 observe/snapshot 遇到它们时应优雅降级为被动读取，不阻断页面查看。
COOPERATION_UNOBSERVABLE_REASONS: frozenset[str] = frozenset(
    {
        "frame_unobservable",
        "restricted_tab_url",
        "page_load_failed",
    }
)

# 这些 reason 表示人类正在操作该标签页（人工接管中）。
HUMAN_CONTROL_REASONS: frozenset[str] = frozenset(
    {
        "human_intervention",
        "control_held",
        "control_authority_required",
    }
)

# bsk >= 0.4.0 / 0.5.0 下 code="permission_denied" 对应的细分 reason 文案。
# 空白或 agent_window_scope 沿用既有文案；未知的非空 reason 不再冒充窗口归属错误。
_PERMISSION_DENIED_FRIENDLY: dict[str, str] = {
    "agent_window_scope": "浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行。",
    "cooperation_sync_required": (
        "当前标签页的协作控制状态尚未同步或已失效。"
        '请先用 bsk_inspect(action="observe") 重新读取页面状态后再试。'
    ),
    "control_context_invalid": (
        "当前标签页的控制上下文已失效（页面可能发生了切换或人工操作）。"
        '请先用 bsk_inspect(action="observe") 重新读取页面状态后再试。'
    ),
    "observation_required": (
        "该操作需要来自最新协作同步（cooperate sync）的显式观察凭证（--observation）。"
        '请先用 bsk_inspect(action="observe") 刷新页面状态后再试。'
    ),
    "observation_changed": (
        "页面内容在执行前发生了变化，浏览器已拦截本次操作以防误点。"
        '请先用 bsk_inspect(action="observe") 确认页面最新状态后再决定下一步。'
    ),
    "document_changed": (
        "页面主文档已跳转或重新加载，原有的控制上下文与元素编号均已失效。"
        '请先用 bsk_inspect(action="observe") 重新读取页面后再操作。'
    ),
    "human_intervention": (
        "用户正在亲自操作这个标签页，浏览器已暂停自动操作。"
        '请等用户操作完成后，再用 bsk_inspect(action="observe") 确认当前页面状态。'
    ),
    "control_held": (
        "用户当前正持有该标签页的控制权，浏览器已暂停自动操作。"
        '请等用户交回控制权后，再用 bsk_inspect(action="observe") 同步状态。'
    ),
    "control_authority_required": (
        "当前未持有该标签页的控制权（可能处于人工接管或共享控制已结束）。"
        "请等用户交回控制权后检查当前页面状态，不要盲目重试。"
    ),
    "uncertain_prior_effect": (
        "上一步操作的结果尚未确认，bsk 已拦截后续写操作以防重复提交。"
        '请先用 bsk_inspect(action="observe") 检查页面或业务状态，确认后再继续。'
    ),
    "frame_unobservable": (
        "当前页面无法建立交互监测（例如空白页 about:blank、受限内部页面或子框架无响应）。"
        "请先导航到正常网页或刷新页面；只读查看可改用 snapshot 或 html。"
    ),
    "page_load_failed": (
        "目标主页面加载失败，暂时无法交互。请检查网址与网络连接后重新打开或刷新。"
    ),
    "element_not_visible": (
        "目标元素当前不在可见视口范围内，无法直接操作。"
        "请先滚动页面让元素进入视野，或重新读取页面确认元素位置后再试。"
    ),
    "target_hidden": (
        "目标元素当前处于隐藏状态，无法操作。"
        '请确认页面状态或先用 bsk_inspect(action="observe") 查看最新元素。'
    ),
    "target_detached": (
        "目标元素已从页面中移除。"
        '请先用 bsk_inspect(action="observe") 重新获取最新的元素编号后再试。'
    ),
    "target_unavailable": (
        "目标元素当前不可用或已失效。"
        '请先用 bsk_inspect(action="observe") 重新读取页面并选择有效目标。'
    ),
    "target_disabled": "目标元素当前处于禁用（disabled）状态，暂时无法操作。",
    "target_readonly": "目标输入框当前为只读（readonly）状态，无法写入内容。",
    "target_covered": (
        "目标元素被页面上的弹窗、浮层或其他元素遮挡，无法直接点击。"
        "请先关闭遮挡层或滚动页面后再试。"
    ),
    "target_moved": (
        "目标元素在点击前发生了位置移动，本次输入已取消以防点偏。"
        "请重新确认页面元素后再试。"
    ),
    "target_focus_lost": "键盘焦点已从目标元素移走，请先聚焦该元素后再按键。",
    "virtual_row_recycled": (
        "目标所在的列表行已被虚拟列表复用到另一条记录，浏览器已拦截操作以防点错。"
        '请先用 bsk_inspect(action="observe") 刷新列表编号后再试。'
    ),
    "precondition_failed": (
        "操作前校验的页面条件不再匹配（目标文字或状态已变化）。"
        '请先用 bsk_inspect(action="observe") 核对最新内容后再决定是否操作。'
    ),
    "restricted_tab_url": (
        "当前标签页属于浏览器受限内部页面（如 chrome:// 或扩展页），不允许执行此操作。"
    ),
    "user_denied": "用户拒绝了借用标签页的授权请求，请不要重复发起同一请求。",
    "confirmation_timeout": "等待用户确认借用标签页超时，本次操作未执行。",
    "confirmation_ui_unavailable": (
        "浏览器当前无法弹出借用确认界面，请检查浏览器窗口状态或扩展的自动化设置。"
    ),
    "file_drop_target_unavailable": "无法安全定位文件拖放目标，本次上传未发出。",
    "local_file_error": "本地文件路径无效或无读写权限，请检查目标路径。",
    "user_moved_tab": (
        "用户把受控标签页移出了任务窗口，该浏览器会话已结束且无法恢复。"
        "请向用户确认是否需要新建会话。"
    ),
}


class BskError(Exception):
    """所有 bsk 调用失败的基类。

    Args:
        message: 面向开发者的详细描述（英文或中文均可，进日志）。
        friendly: 面向用户/模型的中文提示。必须包含"下一步怎么做"。
        code: bsk 返回的 error code，拿不到时为 ""。
        exit_code: 进程退出码，非进程失败时为 -1。
        reason: 错误 JSON 里的 data.reason，用于判断能否重试。
    """

    def __init__(
        self,
        message: str,
        *,
        friendly: str = "",
        code: str = "",
        exit_code: int = -1,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.friendly = friendly or message
        self.code = code
        self.exit_code = exit_code
        self.reason = reason

    @property
    def retryable(self) -> bool:
        """这个错误是否安全重试。"""
        return False

    def __str__(self) -> str:  # pragma: no cover - 便于日志排查
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.exit_code >= 0:
            parts.append(f"exit={self.exit_code}")
        return " | ".join(parts)


class BskNotInstalled(BskError):
    """找不到 bsk 可执行文件，或没有执行权限。重试无用，必须改配置。"""

    @property
    def retryable(self) -> bool:
        return False


class BskTimeout(BskError):
    """命令超时。重试可能有意义（页面慢），但需谨慎。"""

    @property
    def retryable(self) -> bool:
        return True


class BskSessionGone(BskError):
    """会话不存在或已停止（``not_found``）。

    这是最常见的可恢复错误：bsk 会话空闲 5 分钟会被回收。
    正确处理是重建会话后重试一次，而不是报错给用户。
    """

    @property
    def retryable(self) -> bool:
        return True


class BskSessionBusy(BskError):
    """同 session 上有命令正在执行（``session_busy``）。

    说明我们自己的锁没生效，或另有程序在操作同一 session。
    等一小会儿重试一次即可。
    """

    @property
    def retryable(self) -> bool:
        return True


class BskOutcomeUnknown(BskError):
    """动作结果未知（扩展断开/重连等）。

    绝对不能重试：动作可能已经执行了。重试会造成重复点击、重复提交。
    调用方应把会话标记为"不确定态"，并要求用户人工确认页面状态。
    """

    @property
    def retryable(self) -> bool:
        return False


class BskProtocolError(BskError):
    """输出不是预期格式（例如 clap 参数错误返回纯文本）。"""

    @property
    def retryable(self) -> bool:
        return False


class BskBrowserError(BskError):
    """浏览器/CDP 层错误（退出码 3）。"""

    @property
    def retryable(self) -> bool:
        return False


class BskVersionError(BskError):
    """bsk CLI 与浏览器扩展版本不匹配（退出码 5）。需用户升级。"""

    @property
    def retryable(self) -> bool:
        return False


class BskStartCancelled(BskError):
    """这次可恢复启动已被取消（``code="cancelled"``）。

    与普通失败的关键区别：重试与回退都是错的 —— 重试会被同一条墓碑再次拒绝，
    回退到普通 start 则会绕开取消。所以 ``retryable`` 恒为 False。

    Note:
        ``classify()`` 刻意没有把它纳入映射（那是共享分类器，改动面太大），
        转换由调用点显式做，判定用 :func:`is_cancelled_error`。
    """

    @property
    def retryable(self) -> bool:
        return False


class BskBrowserAmbiguous(BskError):
    """同时连着多个浏览器，而用户没有指定要用哪一个。

    为什么需要单独一个类型（而不是让 ``probe_browser`` 返回空串了事）：
    返回空串的后果是调用方不传 ``--browser``，于是 bsk 自己随便挑一个 ——
    用户明明连着 Edge 和 Chrome，插件却会静默操作其中一个，而且"有时候对
    有时候不对"，完全无从排查。这里唯一的正确做法是明确报错，把每个
    已连接实例的 ``instance_id`` 都列出来，让用户填进配置。

    Note:
        这是用户配置问题，不是瞬时故障：重试多少次结果都一样，只有
        改配置（或关掉多余的浏览器）才能继续。
        ``friendly`` 里必须带上完整的实例清单与操作步骤 —— 它最终会经
        ``main.py`` 的 ``except BskError`` 变成给模型看的字符串，
        模型要据此告诉用户去哪里改什么。
    """

    @property
    def retryable(self) -> bool:
        return False


# --- 判定辅助 ---


def is_cancelled_error(exc: BaseException) -> bool:
    """这个异常是不是「请求已被取消」（而不是普通失败）。

    为什么不用 ``isinstance``：``cancelled`` 可能在调用链的任意一层被转换成
    别的异常类型（例如包装后的 ``BskError``），但 ``code`` 会一路保留下来。
    因此判定只看 ``code``，不认类型。
    """
    return getattr(exc, "code", "") == CODE_CANCELLED


def _permission_denied_friendly(reason: str, detail: str) -> str:
    """把 ``code="permission_denied"`` 按 ``data.reason`` 翻译成准确的中文提示。"""
    if not reason:
        return _PERMISSION_DENIED_FRIENDLY["agent_window_scope"]
    known = _PERMISSION_DENIED_FRIENDLY.get(reason)
    if known is not None:
        return known
    return f"操作被浏览器策略拒绝（{reason}）：{detail}"


def _browser_error_friendly(reason: str, detail: str) -> str:
    """为退出码 3（浏览器/CDP 层失败）提供按 ``data.reason`` 细分的中文提示。"""
    if reason == "operation_settling":
        return "上一条浏览器操作还在收尾中，本次动作未发出。请稍等一秒后重试。"
    if reason == "screenshot_capture_failed":
        return (
            f"截屏失败：{detail}。"
            "若是动态变化或无限滚动页面，可改用普通视口截图，或等页面稳定后再试。"
        )
    if reason == "page_hidden":
        return "截图失败：目标标签页当前处于隐藏状态，请保持窗口可见后再试。"
    if reason == "stale_frame":
        return "全页截图失败：页面滚动后画面未刷新，请保持目标窗口可见后再试。"
    if reason == "navigation":
        return "截图过程中页面发生了跳转，请重新读取页面后再试。"
    if reason == "cdp_extension_access_denied":
        return (
            "浏览器拦截了当前页面里其他扩展内容的访问权限。"
            "可尝试导航离开该页面，或停用冲突扩展后刷新重试。"
        )
    if reason == "observe_identity_reload_loop":
        return (
            "目标站点检测到调试器附加并不断自动刷新（反调试限制），"
            "请不要反复观察或刷新该页，改用其他页面或由用户自行操作。"
        )
    return (
        "浏览器层面出错了。请确认浏览器和 bsk 扩展还在正常运行"
        "（可以看浏览器工具栏里的扩展图标是否为已连接）。"
    )


def _timeout_friendly(reason: str, detail: str) -> str:
    """为退出码 4（超时）提供按 ``data.reason`` 细分的中文提示。"""
    if reason == "loading_stalled":
        return (
            "全页截图超时：页面底部一直处于加载状态且高度不再增长，"
            "建议改用视口截图而不是继续放大超时。"
        )
    if reason == "watchdog_timeout":
        return "全页截图与页面失去响应，请确认浏览器正常运行后再试。"
    if reason == "screenshot_capture_failed":
        return f"截图超时：{detail}。请确认标签页响应正常后再试。"
    return "网页响应太慢，操作超时了。可以稍后重试，或换一个更快的页面。"


# --- 构造错误的统一入口 ---


def classify(
    *,
    exit_code: int,
    code: str = "",
    message: str = "",
    hint: str = "",
    reason: str = "",
    stderr: str = "",
    effect_state: str = "",
) -> BskError:
    """把一次失败的 bsk 调用归类成具体的异常。

    这是唯一的分类入口，保证 ``main.py`` 与 ``bsk/`` 拿到一致的错误语义。

    Args:
        exit_code: 进程退出码。
        code: 错误 JSON 里的 ``code``，没有则为空串。
        message: 错误 JSON 里的 ``message``。
        hint: 错误 JSON 里的 ``hint``。
        reason: 错误 JSON 里 ``data.reason``，用于判定"结果未知"与细分权限/目标原因。
        stderr: 进程 stderr 原文，clap 参数错误时只有它可用。
        effect_state: 错误 JSON 里 ``data.effect_state``（bsk >= 0.4.2 / 0.5.0），
            为 ``"unknown"`` 时与 ``OUTCOME_UNKNOWN_REASONS`` 同等对待，绝不重试。

    Returns:
        对应的 BskError 子类实例。
    """
    detail = message or stderr.strip() or "bsk 命令执行失败。"
    if hint:
        detail = f"{detail}（提示：{hint}）"

    # 优先级 1：动作结果未知 —— 必须最先判断，否则会被当成可重试错误。
    if reason in OUTCOME_UNKNOWN_REASONS or effect_state == "unknown":
        return BskOutcomeUnknown(
            detail,
            friendly=(
                "上一步操作的结果无法确认（连接中断或浏览器执行状态未知）。"
                "为避免重复操作，我不会自动重试。"
                "请先看一眼浏览器里的实际状态，再决定是否继续。"
            ),
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 优先级 1.5：明确不可自动重建/重试的协作终态或目标不存在（避免误入 BskSessionGone 重建整扇窗口）。
    if code == CODE_USER_ABORTED:
        return BskError(
            detail,
            friendly="用户已主动停止当前浏览器任务。请尊重用户操作，不要自动恢复或重试。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if reason == "user_moved_tab":
        return BskError(
            detail,
            friendly=_PERMISSION_DENIED_FRIENDLY["user_moved_tab"],
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if code == CODE_DIALOG_PENDING or exit_code == EXIT_DIALOG_PENDING:
        return BskError(
            detail,
            friendly=(
                "页面弹出了原生对话框（alert/confirm/prompt），当前操作已挂起。"
                "请先查看页面状态或请用户处理对话框，不要重复提交。"
            ),
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if code == CODE_NOT_FOUND and reason in (
        "ref_not_found",
        "selector_not_found",
        "visual_target_changed",
    ):
        target_hints = {
            "ref_not_found": (
                "找不到对应的元素编号（可能页面已经变化，旧的 @eN 已失效）。"
                '请先用 bsk_inspect(action="observe") 重新读取页面获取最新编号后再试。'
            ),
            "selector_not_found": (
                "页面上找不到匹配的 CSS 选择器。"
                '请检查选择器是否正确，或先用 bsk_inspect(action="observe") 获取元素的 @eN 编号。'
            ),
            "visual_target_changed": (
                "Canvas 截图目标在点击前已变化或失效。"
                '请重新 observe 并对元素截屏（screenshot --ref）后再试。'
            ),
        }
        return BskError(
            detail,
            friendly=target_hints[reason],
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 优先级 2：会话不存在 —— 可自动重建。
    if code == CODE_NOT_FOUND:
        return BskSessionGone(
            detail,
            friendly="浏览器会话已失效（可能空闲太久被回收），正在重新建立。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 优先级 3：会话忙 —— 短暂等待后可重试一次。
    if code == CODE_SESSION_BUSY:
        return BskSessionBusy(
            detail,
            friendly="浏览器正忙（上一条操作还没结束），稍后重试。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if code == CODE_PERMISSION_DENIED:
        return BskError(
            detail,
            friendly=_permission_denied_friendly(reason, detail),
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 优先级 4：按退出码兜底。
    if exit_code == EXIT_TIMEOUT:
        return BskTimeout(
            detail,
            friendly=_timeout_friendly(reason, detail),
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if exit_code == EXIT_BROWSER:
        return BskBrowserError(
            detail,
            friendly=_browser_error_friendly(reason, detail),
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if exit_code == EXIT_VERSION:
        return BskVersionError(
            detail,
            friendly="bsk 命令行与浏览器扩展的版本不一致，请把它们都升级到最新版。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if exit_code == EXIT_PROTOCOL:
        if reason == "user_cancelled":
            return BskProtocolError(
                detail,
                friendly="操作已被用户中断（如全页截图期间发生人工操作），不会自动重试。",
                code=code,
                exit_code=exit_code,
                reason=reason,
            )
        return BskProtocolError(
            detail,
            friendly="与 bsk 后台服务的通信失败了。可以在终端执行 `bsk doctor` 检查环境。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 默认：用户错误（参数写错、实体不存在等）。
    if code:
        friendly = f"操作失败：{detail}"
    elif stderr.strip():
        # clap 参数错误走这里：没有 JSON，只有 stderr 文本。
        friendly = f"命令参数有问题，这是个 bug，请反馈：{stderr.strip()[:200]}"
    else:
        friendly = f"操作失败（退出码 {exit_code}）：{detail}"

    return BskError(
        detail,
        friendly=friendly,
        code=code,
        exit_code=exit_code,
        reason=reason,
    )
