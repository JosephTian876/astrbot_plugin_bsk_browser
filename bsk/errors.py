"""bsk 的退出码与错误分类。

bsk 的退出码有 6 档（实测确认），错误 JSON 走 **stdout** 而不是 stderr，
结构是扁平的 ``{code, message, hint, exit_code, data?}``：

    {
      "code": "not_found",
      "message": "session not registered or already stopped",
      "hint": "run `bsk session list` to see current state",
      "exit_code": 1
    }

但有三种情况**拿不到 JSON**，调用方必须能容错：

1. clap 参数错误（例如写了不存在的 flag）→ stderr 纯文本，退出码 1；
2. 进程被我们杀掉（超时）→ 可能没有任何输出；
3. 极少数命令不带 ``--json`` 时输出人类可读文本。

因此判成败**只看退出码**，JSON 解析只是锦上添花。
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

EXIT_CODE_NAMES: dict[int, str] = {
    EXIT_OK: "成功",
    EXIT_USER_ERROR: "参数或实体错误",
    EXIT_PROTOCOL: "通信错误",
    EXIT_BROWSER: "浏览器错误",
    EXIT_TIMEOUT: "超时",
    EXIT_VERSION: "版本不匹配",
}

# --- bsk 返回的 error code 字符串 ---

CODE_NOT_FOUND = "not_found"
"""session/tab/browser 不存在或已停止。**可以重建会话后重试。**"""

CODE_SESSION_BUSY = "session_busy"
"""同一 session 上有 in-flight 命令。**可以短暂等待后重试一次。**"""

CODE_PERMISSION_DENIED = "permission_denied"
"""权限不足（例如 evaluate 试图跑在非 Agent Window 标签里）。**重试无用。**"""

# 这些 reason 出现在错误 JSON 的 data.reason 里，表示"动作可能已经生效"，
# 此时**绝对禁止重试**，否则可能重复点击/重复提交表单。
OUTCOME_UNKNOWN_REASONS: frozenset[str] = frozenset(
    {
        "extension_reconnected",
        "extension_disconnected",
        "input_outcome_unknown",
        "result_outcome_unknown",
    }
)


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
        """这个错误是否**安全**重试。"""
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
    """命令超时。重试**可能**有意义（页面慢），但需谨慎。"""

    @property
    def retryable(self) -> bool:
        return True


class BskSessionGone(BskError):
    """会话不存在或已停止（``not_found``）。

    这是**最常见的可恢复错误**：bsk 会话空闲 5 分钟会被回收。
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

    **绝对不能重试**：动作可能已经执行了。重试会造成重复点击、重复提交。
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


# --- 构造错误的统一入口 ---


def classify(
    *,
    exit_code: int,
    code: str = "",
    message: str = "",
    hint: str = "",
    reason: str = "",
    stderr: str = "",
) -> BskError:
    """把一次失败的 bsk 调用归类成具体的异常。

    这是唯一的分类入口，保证 ``main.py`` 与 ``bsk/`` 拿到一致的错误语义。

    Args:
        exit_code: 进程退出码。
        code: 错误 JSON 里的 ``code``，没有则为空串。
        message: 错误 JSON 里的 ``message``。
        hint: 错误 JSON 里的 ``hint``。
        reason: 错误 JSON 里 ``data.reason``，用于判定"结果未知"。
        stderr: 进程 stderr 原文，clap 参数错误时只有它可用。

    Returns:
        对应的 BskError 子类实例。
    """
    detail = message or stderr.strip() or "bsk 命令执行失败。"
    if hint:
        detail = f"{detail}（提示：{hint}）"

    # 优先级 1：动作结果未知 —— 必须最先判断，否则会被当成可重试错误。
    if reason in OUTCOME_UNKNOWN_REASONS:
        return BskOutcomeUnknown(
            detail,
            friendly=(
                "浏览器连接中断，上一步操作的结果无法确认。"
                "为避免重复操作，我不会自动重试。"
                "请先看一眼浏览器里的实际状态，再决定是否继续。"
            ),
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
            friendly="浏览器拒绝了这次操作：该动作只允许在 bsk 自己的窗口里执行。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    # 优先级 4：按退出码兜底。
    if exit_code == EXIT_TIMEOUT:
        return BskTimeout(
            detail,
            friendly="网页响应太慢，操作超时了。可以稍后重试，或换一个更快的页面。",
            code=code,
            exit_code=exit_code,
            reason=reason,
        )

    if exit_code == EXIT_BROWSER:
        return BskBrowserError(
            detail,
            friendly=(
                "浏览器层面出错了。请确认浏览器和 bsk 扩展还在正常运行"
                "（可以看浏览器工具栏里的扩展图标是否为已连接）。"
            ),
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
