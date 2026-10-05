"""bsk 子进程调用层 —— 整个插件里唯一与子进程打交道的地方。

把 ``bsk <命令> --json`` 的调用细节全部封在这里，上层的 ``session.py`` /
``service.py`` 只需要 ``await runner.run([...])``，不必关心进程、编码、超时。

设计要点（每一条都对应一个实测踩过的坑）：

1. 列表参数、不过 shell —— 用 ``create_subprocess_exec(*args)``，
   避免命令注入，也避免 Windows 引号转义问题。
2. ``communicate()`` 必须包超时 —— bsk 自动拉起后台 daemon 后，daemon 会继承
   stdout/stderr 管道句柄，导致子进程的管道永远不关闭、``communicate()`` 可能永不返回。
3. 超时后的取消语义 —— 先关 stdin（bsk 会据此发送 cancel RPC 并等浏览器回收），
   给 15 秒宽限期；仍不退出才 ``kill()``。直接 SIGINT/kill 会跳过浏览器的清理逻辑。
4. 显式 UTF-8 解码 —— AstrBot 自带 Python 在 Windows 下 ``sys.stdout.encoding`` 是
   ``gbk``，而网页文本可能是阿拉伯文/俄文/中文。不指定编码会抛 ``UnicodeDecodeError``，
   而且是在 subprocess 的 reader 线程里抛，主线程只看到一个莫名其妙的 ``None``。
5. 判成败只看退出码 —— 错误 JSON 走的是 stdout 而不是 stderr；
   而 clap 参数错误又是 stderr 纯文本。所以 JSON 解析必须容错，不能用来判成败。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from typing import Any

from . import errors
from .errors import BskError, BskNotInstalled, BskProtocolError
from .models import BskResult

# 超时后给 bsk 的优雅取消宽限期（秒）。
#
# 为什么需要宽限期：直接 kill 会跳过 bsk 的清理逻辑（归还借用的标签页、
# 让浏览器端把中断的操作收尾）。bsk 官方的建议值是 15 秒 —— Windows 上
# IPC 可能花 5s 连接 + 2s 取消 + 2s 收尾 + 最多 5s 释放传输。
#
# 为什么做成可配置：宽限期是用户可感知的额外等待。如果一条命令已经
# 等满超时（例如 navigate 的 45 秒），再无条件干等 15 秒体验很差。
# 因此允许调用方按场景调小，默认仍取官方建议值。
DEFAULT_CANCEL_GRACE_SEC = 15.0

# 宽限期的下限：低于这个值就基本等于直接 kill，失去优雅取消的意义。
MIN_CANCEL_GRACE_SEC = 1.0

# 进程退出后，等待 stdout/stderr 抽取任务收尾的上限（秒）。
#
# 为什么需要：Windows 上 bsk 自动拉起的 daemon 会继承 stdout/stderr 的管道句柄，
# 导致子进程退出后管道仍然不关闭，读取端永远等不到 EOF。
# 没有这个上限的话，命令会在已经成功之后卡住不返回。
DRAIN_TIMEOUT_SEC = 2.0


async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
    """把子进程的一个输出管道读到 EOF 或出错为止。

    单独抽出来是因为要并发读 stdout 和 stderr —— 顺序读会在其中一个
    缓冲区写满时死锁（经典管道死锁）。

    Args:
        stream: 管道；为 None 时直接返回。
        sink: 读到的字节块追加到这里。
    """
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            sink.append(chunk)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        # 读取失败不该让整条命令失败：已经读到的部分仍然有效
        # （例如进程被 kill 时管道会先断）。
        return


def _build_env() -> dict[str, str]:
    """构造子进程环境变量。

    - ``BSK_CANCEL_ON_STDIN_CLOSE=1`` —— Windows 上必需。
      没有它，关闭 stdin 不会触发 bsk 的取消逻辑，我们的优雅取消就形同虚设，
      最后只能硬 kill（会跳过浏览器端的收尾）。

      ⚠️ 正因为设了它，执行期间绝不能关 stdin：bsk 会把"stdin 被关"
      理解成"用户按了 Ctrl-C"，从而把正在执行的命令取消掉。
      所以本模块不使用 ``proc.communicate()``（它会立刻关 stdin），
      改为自己并发读取两个管道。详见 ``run()`` 的说明。

    - ``PYTHONIOENCODING`` / ``PYTHONUTF8`` 只是双保险：bsk 是 Rust 程序，
      本身不受影响，但万一它内部调用了 Python 工具链就有用。
    - 不设置 ``BSK_AUTO_START``：保持默认行为（允许 bsk 自动拉起 daemon）。
      AstrBot 是常驻服务，让 bsk 自己管 daemon 生命周期最省事。
    """
    env = dict(os.environ)
    env["BSK_CANCEL_ON_STDIN_CLOSE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def resolve_bsk_path(configured: str) -> str:
    """解析 bsk 可执行文件路径。

    用户可能填：
    - 空串或 ``"bsk"`` → 从 PATH 查找；
    - 绝对路径 → 直接用（但要校验存在，否则给出明确错误）；
    - Windows 下漏写 ``.exe`` → 补上再试。

    Args:
        configured: 用户配置里的 ``bsk_path``。

    Returns:
        可用的可执行文件路径。

    Raises:
        BskNotInstalled: 找不到可执行文件，附带给用户看的修复建议。
    """
    raw = (configured or "").strip() or "bsk"

    # 情况 1：直接是路径（含分隔符）——校验存在性。
    looks_like_path = os.sep in raw or (os.altsep is not None and os.altsep in raw)
    if looks_like_path:
        candidates = [raw]
        if os.name == "nt" and not raw.lower().endswith(".exe"):
            candidates.append(raw + ".exe")
        for cand in candidates:
            if os.path.isfile(cand):
                return cand
        raise BskNotInstalled(
            f"配置的 bsk 路径不存在：{raw}",
            friendly=(
                f"找不到 bsk 可执行文件：{raw}\n"
                "请检查插件配置里的「bsk 可执行文件路径」，"
                "或先按 BrowserSkill 官方文档安装 bsk。"
            ),
            code="bsk_not_installed",
        )

    # 情况 2：裸命令名 —— 走 PATH。
    found = shutil.which(raw)
    if found:
        return found

    # Windows 常见安装位置兜底：用户装了但当前进程的 PATH 没刷新。
    if os.name == "nt":
        guess = os.path.join(
            os.path.expanduser("~"), ".local", "bin", raw + ".exe"
        )
        if os.path.isfile(guess):
            return guess

    raise BskNotInstalled(
        f"PATH 中找不到可执行文件：{raw}",
        friendly=(
            f"在系统 PATH 里找不到 `{raw}`。\n"
            "可能原因：bsk 没装，或者装了但 AstrBot 进程没有继承到新的 PATH。\n"
            "解决办法：在插件配置里把「bsk 可执行文件路径」填成绝对路径，"
            r"例如 C:\Users\<你的用户名>\.local\bin\bsk.exe"
        ),
        code="bsk_not_installed",
    )


def _try_parse_json(text: str) -> Any | None:
    """尽最大努力从输出里解析 JSON。

    bsk 的正常输出是单个 JSON 对象/数组，但为了健壮性也处理"前面有杂音"的情况
    （实测成功时 stderr 可能有 ``waiting for browser extension to connect…`` 之类提示，
    虽然那在 stderr，但 stdout 也可能被未来版本加上前缀）。

    Args:
        text: 进程 stdout。

    Returns:
        解析出的对象；无法解析时返回 None（不抛异常 —— 调用方只看退出码）。
    """
    stripped = (text or "").strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 回退：找出第一个 { 或 [ 到最后一个 } 或 ] 之间的片段再试。
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _extract_error_fields(data: Any) -> tuple[str, str, str, str]:
    """从错误 JSON 里取出 (code, message, hint, reason)。

    bsk 的错误结构是扁平的 ``{code, message, hint, exit_code, data?}``，
    没有 error 包装层。但为了兼容未来可能的嵌套，这里两种都试。

    Args:
        data: 解析后的 JSON，可能是任何类型。

    Returns:
        ``(code, message, hint, reason)``，缺的字段为空串。
    """
    if not isinstance(data, dict):
        return "", "", "", ""

    node: dict[str, Any] = data
    inner = data.get("error")
    if isinstance(inner, dict):
        node = inner

    code = node.get("code")
    message = node.get("message")
    hint = node.get("hint")

    reason = ""
    extra = node.get("data")
    if isinstance(extra, dict):
        reason = str(extra.get("reason") or "")

    return (
        code if isinstance(code, str) else "",
        message if isinstance(message, str) else "",
        hint if isinstance(hint, str) else "",
        reason,
    )


class BskRunner:
    """调用 bsk CLI 的执行器。

    无状态（除了配置），可安全地在多个协程间共享。

    Args:
        bsk_path: bsk 可执行文件路径（可为裸命令名，构造时解析一次）。
        default_timeout: 默认超时（秒）。
        cancel_grace: 超时后等待 bsk 优雅退出的宽限期（秒）。
            这是最坏情况下的额外等待：正常时 bsk 收到 stdin 关闭会很快退出，
            只有它卡死时才需要等满。默认取官方建议的 15 秒。
    """

    def __init__(
        self,
        bsk_path: str,
        default_timeout: float = 60.0,
        cancel_grace: float = DEFAULT_CANCEL_GRACE_SEC,
    ) -> None:
        self._configured_path = bsk_path
        self.default_timeout = max(1.0, float(default_timeout))
        self.cancel_grace = max(MIN_CANCEL_GRACE_SEC, float(cancel_grace))
        self._resolved: str | None = None

    def resolve(self) -> str:
        """解析并缓存 bsk 路径。第一次调用可能抛 ``BskNotInstalled``。

        之所以懒解析而不是构造时就解析：插件加载时用户可能还没装 bsk，
        不应该让整个插件加载失败 —— 应该在真正要用的时候才报错。
        """
        if self._resolved is None:
            self._resolved = resolve_bsk_path(self._configured_path)
        return self._resolved

    def invalidate_cache(self) -> None:
        """清掉路径缓存（用户改了配置或刚装好 bsk 后调用）。"""
        self._resolved = None

    async def run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        """执行一条 bsk 命令。

        Args:
            args: 命令参数列表，不含 bsk 自身路径。
                例如 ``["session", "start", "--no-focus", "--json"]``。
            timeout: 超时秒数；None 时用 ``default_timeout``。
            expect_json: 是否期望 JSON 输出。为 False 时不因解析失败而报错
                （用于 ``--version`` 这类纯文本命令）。

        Returns:
            ``BskResult``。成功与失败都会返回（失败时 ``ok=False``），
            只有"bsk 根本没跑起来"才抛异常。

        Raises:
            BskNotInstalled: 找不到 bsk。
            BskError: 进程启动失败等基础设施错误（非 bsk 业务错误）。
        """
        exe = self.resolve()
        effective_timeout = float(timeout) if timeout else self.default_timeout

        argv = [exe, *args]
        started = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # stdin 保持打开（PIPE）但不写入 —— 见下面关于 communicate() 的说明。
                stdin=asyncio.subprocess.PIPE,
                env=_build_env(),
            )
        except OSError as exc:
            raise BskError(
                f"启动 bsk 失败：{exc}",
                friendly=(
                    "无法启动 bsk 进程。请确认配置文件里的路径正确、"
                    "且该文件有执行权限。"
                ),
                code="bsk_spawn_failed",
            ) from exc

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        drain_out = asyncio.create_task(_drain(proc.stdout, stdout_chunks))
        drain_err = asyncio.create_task(_drain(proc.stderr, stderr_chunks))

        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await self._cancel(proc, self.cancel_grace)

        # 进程已退出（或被我们终止）。给抽取任务一个有界的时间收尾：
        # Windows 上 daemon 可能继承了管道句柄，导致 EOF 永远不来，
        # 所以必须有上限，不能无限等。
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(
                asyncio.gather(drain_out, drain_err, return_exceptions=True),
                timeout=DRAIN_TIMEOUT_SEC,
            )
        for task in (drain_out, drain_err):
            if not task.done():
                task.cancel()

        elapsed = time.monotonic() - started

        if timed_out:
            return BskResult(
                ok=False,
                exit_code=errors.EXIT_TIMEOUT,
                data={
                    "code": "timeout",
                    "message": f"命令超过 {effective_timeout:.0f} 秒未返回",
                    "exit_code": errors.EXIT_TIMEOUT,
                },
                stderr=f"timeout after {effective_timeout:.0f}s",
                elapsed=elapsed,
            )

        # 必须显式指定 utf-8：Windows 中文环境下默认是 gbk，
        #   遇到阿拉伯文/俄文会抛 UnicodeDecodeError。
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        code = proc.returncode if proc.returncode is not None else -1

        parsed = _try_parse_json(stdout)

        if code == errors.EXIT_OK:
            # 命令成功，但可能没有 JSON 输出（例如某些命令在无数据时）。
            if parsed is None and expect_json and stdout:
                # 有输出但不是 JSON —— 不算致命，把原文放到 stderr 供排查。
                return BskResult(
                    ok=True,
                    exit_code=0,
                    data=None,
                    stdout=stdout,
                    stderr=stderr,
                    elapsed=elapsed,
                )
            return BskResult(
                ok=True,
                exit_code=0,
                data=parsed,
                stdout=stdout,
                stderr=stderr,
                elapsed=elapsed,
            )

        return BskResult(
            ok=False,
            exit_code=code,
            data=parsed,
            stdout=stdout,
            stderr=stderr,
            elapsed=elapsed,
        )

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        """同 ``run``，但失败时抛对应的 ``BskError`` 子类。

        这是上层业务代码应该用的入口 —— 它负责把退出码和错误码翻译成
        带中文提示的异常，省去每处调用都写一遍 if not ok。

        Args:
            args: 命令参数列表。
            timeout: 超时秒数。
            expect_json: 是否期望 JSON 输出。

        Returns:
            成功的 ``BskResult``。

        Raises:
            BskError: 其子类，``friendly`` 字段可直接展示给用户/模型。
        """
        result = await self.run(args, timeout=timeout, expect_json=expect_json)
        if result.ok:
            return result

        code, message, hint, reason = _extract_error_fields(result.data)
        raise errors.classify(
            exit_code=result.exit_code,
            code=code,
            message=message,
            hint=hint,
            reason=reason,
            stderr=result.stderr,
        )

    @staticmethod
    async def _cancel(proc: asyncio.subprocess.Process, grace: float) -> None:
        """超时后的三级降级取消。

        1. 关 stdin —— 配合 ``BSK_CANCEL_ON_STDIN_CLOSE=1``，bsk 会据此发送
           cancel RPC 并等浏览器回收（Windows 上比 SIGINT 可靠）；
        2. 等 ``grace`` 秒宽限（正常情况远小于此值就会退出）；
        3. 仍不退就 kill，并回收僵尸进程。

        注意每一步都要吞异常：进程可能已经自己退出了，此时操作会抛
        ``ProcessLookupError`` 之类，不该让清理逻辑反而把原始错误盖掉。
        """
        with contextlib.suppress(Exception):
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()

        # 先看它是否已经因为 stdin 关闭而退出（正常路径，几乎立刻返回）。
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), timeout=grace)

        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
