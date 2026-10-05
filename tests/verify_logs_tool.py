"""``bsk_logs`` 工具的专项验证 —— 用假 runner + 假 event，不需要真实浏览器。

## 为什么需要这个脚本

``bsk/service.py`` 里的 ``read_console`` / ``read_network`` / ``render_console``
早就写好了，但 ``main.py`` 一直没有调用点，模型根本拿不到控制台与网络日志。
把工具接出来时有三类问题只有真实调用链才暴露：

1. 空结果不是错误。实测 bsk 在"没有日志"时会把 ``entries`` 字段整个省掉
   （不是返回空数组），``ConsoleLog.from_json`` 已容错成空列表。若工具把空结果
   渲染成空白串或 ``{}``，模型会以为工具坏了，甚至去编造日志内容。
2. URL 会内联 base64。实测 ``network`` 会把整段
   ``data:image/png;base64,...``（本机一次实测单条上千字符）塞进 ``url`` 字段。
   不截断就会瞬间吃光模型上下文 —— 所以这里必须有一条回归测试钉住截断。
3. ``failure`` 条目没有 ``status`` 字段（实测只有 ``error_text``），
   而 ``render_console`` 会去读 ``method`` / ``status``。字段对不上就是
   ``AttributeError``，用户看到的会是一句英文报错。

## 为什么用假 runner 而不是假 service

用真实的 ``BskService`` + ``SessionManager``，只把最底层的子进程换成假 runner，
才能断言"``since`` 真的被拼进了 bsk 命令行"。若连 service 一起换掉，这条断言
就退化成"我自己传给自己"，测不出任何东西。

## 安全边界

本脚本不创建任何真实 bsk 会话、不启动浏览器、不执行任何 JavaScript：
假 runner 只记录参数并返回构造好的 JSON。

用法：
    python tests/verify_logs_tool.py

退出码：全部通过 0，有任何失败 1。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

RESULTS: list[tuple[str, bool, str]] = []

# 超长 URL 的构造基元：与实测形态一致（network 会把图片内联进 url）。
BASE64_PREFIX = "data:image/png;base64,"
HUGE_URL = BASE64_PREFIX + "iVBORw0KGgoAAAANSUhEUg" * 200
HUGE_URL_LEN = len(HUGE_URL)


def record(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条用例并立即打印。"""
    RESULTS.append((name, ok, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def _ensure_paths() -> None:
    """把必要路径放进 ``sys.path``，并钉住 ``ASTRBOT_ROOT``。

    本脚本要 import ``main.py``（它会 import astrbot），所以必须先把
    ``ASTRBOT_ROOT`` 钉到用户真实目录：AstrBot 解析数据路径时优先读该变量，
    否则会用当前工作目录，在仓库里生成 ``data/cmd_config.json``
    （AstrBot 主配置，含 API key 与管理员 QQ 号）—— 而本仓库是要公开发布的。
    """
    for path in (str(HERE), str(PROJECT), str(PROJECT.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)

    astrbot_app = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
    if os.path.isdir(astrbot_app) and astrbot_app not in sys.path:
        sys.path.insert(0, astrbot_app)

    astrbot_root = os.environ.get(
        "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
    )
    os.environ.setdefault("ASTRBOT_ROOT", astrbot_root)
    if os.path.isdir(astrbot_root) and astrbot_root not in sys.path:
        sys.path.insert(0, astrbot_root)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class FakeRunner:
    """假的 ``BskRunner``：记录每次调用的参数，返回编排好的 JSON。

    Attributes:
        calls: 每次调用的参数列表（原样记录，用来断言 ``--since`` 是否透传）。
        payloads: ``命令名 -> 该命令要返回的 JSON``。命令名形如 ``console``、
            ``network``、``session start``。
    """

    def __init__(self, payloads: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.payloads: dict[str, Any] = dict(payloads or {})

    @staticmethod
    def _command_of(call: list[str]) -> str:
        """把 ``["session", "start", ...]`` 归一成 ``"session start"``。"""
        if len(call) > 1 and call[0] == "session":
            return f"session {call[1]}"
        return call[0] if call else ""

    def resolve(self) -> str:
        """冒充"找到 bsk 可执行文件"这一步。"""
        return r"C:\fake\bsk.exe"

    def calls_for(self, command: str) -> list[list[str]]:
        """取出某个命令的所有调用参数。"""
        return [c for c in self.calls if self._command_of(c) == command]

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        """假装执行一条 bsk 命令，返回构造好的载荷。"""
        from bsk.models import BskResult

        call = list(args)
        self.calls.append(call)
        command = self._command_of(call)

        if command == "session start":
            data: Any = {"session_id": "logsT1", "browser_instance_id": "fakebrw"}
        else:
            data = self.payloads.get(command, {})
            # 载荷允许写成可调用对象，便于按"第几次调用"返回不同内容。
            if callable(data):
                data = data(call)
        return BskResult(ok=True, exit_code=0, data=data, elapsed=0.0)


def console_payload(entries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """构造一份 console/network 的成功载荷。"""
    payload: dict[str, Any] = {"tab_id": 77, "truncated": False}
    if entries is not None:
        payload["entries"] = entries
    payload.update(extra)
    return payload


def make_plugin(fake: FakeRunner, tmp_dir: str, **config: Any):
    """构造插件实例，并把 ``service`` 换成"真实 service + 假 runner"。"""
    from astrbot_plugin_bsk_browser.bsk.service import BskService
    from astrbot_plugin_bsk_browser.main import BskBrowserPlugin
    from astrbot_test_doubles import make_context

    # 显式指定浏览器，避免触发 probe_browser 里的同步子进程探测。
    full: dict[str, Any] = {
        "browser_instance_id": "fakebrw",
        "screenshot_dir": tmp_dir,
        "enabled": True,
        "admin_only": True,
    }
    full.update(config)
    plugin = BskBrowserPlugin(make_context(), config=full)
    plugin.service = BskService(plugin.settings, runner=fake)
    return plugin


async def call_logs(plugin, event, **kwargs: Any) -> str:
    """await ``bsk_logs`` 并把结果转成字符串（异常转成可识别的标记）。"""
    try:
        result = await plugin.bsk_logs(event, **kwargs)
        return str(result)
    except BaseException as exc:  # noqa: BLE001 - 工具绝不该抛，这里兜住以便报错
        return f"<RAISED {type(exc).__name__}: {exc}>"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def run() -> int:
    from astrbot_test_doubles import FakeEvent

    admin = FakeEvent(is_admin=True, sender_id="1", umo="logs:umo:admin")
    stranger = FakeEvent(is_admin=False, sender_id="10001", umo="logs:umo:user")

    print("=" * 72)
    print("bsk_logs 工具验证（假 runner + 假 event，不碰真实浏览器）")
    print("=" * 72)

    with tempfile.TemporaryDirectory(prefix="bsk_logs_test_") as tmp_dir:
        # ------------------------------------------------------------------
        print("\n--- 1. kind=console：正常返回 ---")
        # ------------------------------------------------------------------
        try:
            fake = FakeRunner(
                {
                    "console": console_payload(
                        [
                            {
                                "sequence": 1,
                                "kind": "log",
                                "level": "error",
                                "text": "Failed to load resource",
                                "url": "https://example.com/x.js",
                            }
                        ],
                        next_since=1,
                    )
                }
            )
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="console")

            record(
                "1.1 console 返回内容而不是报错",
                "RAISED" not in text and "Failed to load resource" in text,
                text[:70].replace("\n", " "),
            )
            record(
                "1.2 返回里回显了序号，模型能对上增量位置",
                "[1]" in text,
                "",
            )
            record(
                "1.3 返回里给出下次该传的 since（增量拉取）",
                "since=1" in text,
                text.splitlines()[-1][:80],
            )
        except Exception:
            record("1. kind=console", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 2. kind=network：正常返回 + since 透传到命令行 ---")
        # ------------------------------------------------------------------
        try:
            fake = FakeRunner(
                {
                    "network": console_payload(
                        [
                            {
                                "sequence": 4,
                                "kind": "response",
                                "method": "GET",
                                "url": "https://example.com/",
                                "status": 200,
                            }
                        ],
                        next_since=4,
                    )
                }
            )
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="network", since=3)

            record(
                "2.1 network 返回方法、状态码与网址",
                "GET" in text and "200" in text and "example.com" in text,
                text[:70].replace("\n", " "),
            )

            net_calls = fake.calls_for("network")
            argv = net_calls[0] if net_calls else []
            record(
                "2.2 走的是 bsk network 子命令（不是 console）",
                bool(net_calls) and not fake.calls_for("console"),
                f"network 调用 {len(net_calls)} 次",
            )
            record(
                "2.3 --since 被透传到 bsk 命令行且值正确",
                "--since" in argv and argv[argv.index("--since") + 1] == "3",
                f"argv={argv}",
            )
            record(
                "2.4 命令行带 --session 与 --json（会话隔离与机器可读输出）",
                "--session" in argv and "--json" in argv,
                f"argv={argv}",
            )
        except Exception:
            record("2. kind=network", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 3. since 的取值形态（模型可能传 number 的多种写法）---")
        # ------------------------------------------------------------------
        try:
            for given, want in ((7, "7"), (7.0, "7"), ("7", "7"), (None, "0")):
                fake = FakeRunner({"console": console_payload([], next_since=0)})
                plugin = make_plugin(fake, tmp_dir)
                await call_logs(plugin, admin, kind="console", since=given)
                argv = fake.calls_for("console")[0]
                got = argv[argv.index("--since") + 1]
                with_position = got == want
                record(
                    f"3.{given!r} 归一成 --since {want}",
                    with_position,
                    f"实际 {got!r}（argv={argv}）",
                )
        except Exception:
            record("3. since 取值形态", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 4. 空结果：必须给明确中文说明，不能是空串或 {} ---")
        # ------------------------------------------------------------------
        try:
            # 实测形态：没有日志时 entries 字段整个消失（不是空数组）。
            fake = FakeRunner({"console": {"tab_id": 77, "next_since": 0, "truncated": False}})
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="console")

            record(
                "4.1 空结果不报错、不抛异常",
                "RAISED" not in text and "失败" not in text,
                text[:60].replace("\n", " "),
            )
            record(
                "4.2 空结果给出明确中文说明（不是空串）",
                bool(text.strip()) and "没有捕获到" in text,
                repr(text[:60]),
            )
            record(
                "4.3 空结果不返回 {} 或裸 JSON",
                "{}" not in text and not text.strip().startswith("{"),
                "",
            )
            record(
                "4.4 空结果也告诉模型下次可以传什么 since",
                "since=" in text,
                text.splitlines()[-1][:80],
            )

            # network 那条路径同样要成立（两份 read_* 是分开的代码）。
            fake_net = FakeRunner({"network": {"tab_id": 77, "next_since": 0}})
            plugin_net = make_plugin(fake_net, tmp_dir)
            text_net = await call_logs(plugin_net, admin, kind="network")
            record(
                "4.5 network 空结果同样有明确说明",
                "没有捕获到" in text_net and "网络请求" in text_net,
                text_net[:60].replace("\n", " "),
            )
            record(
                "4.6 两种日志的说明文案能区分开（不会都叫「控制台消息」）",
                "控制台消息" in text and "网络请求" in text_net,
                "",
            )
        except Exception:
            record("4. 空结果", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 5. URL 截断回归（防 base64 内联撑爆模型上下文）---")
        # ------------------------------------------------------------------
        try:
            fake = FakeRunner(
                {
                    "network": console_payload(
                        [
                            {
                                "sequence": 1,
                                "kind": "response",
                                "method": "GET",
                                "url": HUGE_URL,
                                "status": 200,
                            }
                        ],
                        next_since=1,
                    )
                }
            )
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="network")

            record(
                "5.1 超长 URL 不会让工具崩溃",
                "RAISED" not in text,
                text[:60].replace("\n", " "),
            )
            record(
                "5.2 超长 URL 被截断（整段 base64 不在输出里）",
                HUGE_URL not in text,
                f"原始 URL {HUGE_URL_LEN} 字符，输出共 {len(text)} 字符",
            )
            record(
                "5.3 截断后仍保留前 200 个字符（信息没被砍没）",
                BASE64_PREFIX in text and "iVBORw0KGgoAAAANSUhEUg" in text,
                "",
            )
            record(
                "5.4 URL 恰好保留 200 字符（第 201 个字符起被砍掉）",
                HUGE_URL[:200] in text and HUGE_URL[:201] not in text,
                f"含前 200 字符={HUGE_URL[:200] in text}，"
                f"含前 201 字符={HUGE_URL[:201] not in text}",
            )
        except Exception:
            record("5. URL 截断", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 6. failure 条目（无 status 字段）不崩溃，且给出失败原因 ---")
        # ------------------------------------------------------------------
        try:
            # 实测形态：failure 条目没有 status，只有 error_text。
            fake = FakeRunner(
                {
                    "network": console_payload(
                        [
                            {
                                "sequence": 3,
                                "kind": "failure",
                                "method": "GET",
                                "url": "https://example.com/missing.js",
                                "error_text": "net::ERR_FAILED",
                            },
                            {
                                "sequence": 4,
                                "kind": "response",
                                "method": "GET",
                                "url": "https://example.com/ok.js",
                                "status": 200,
                            },
                        ],
                        next_since=4,
                    )
                }
            )
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="network")

            record(
                "6.1 failure 条目不会导致 AttributeError",
                "RAISED" not in text,
                text[:70].replace("\n", " "),
            )
            record(
                "6.2 failure 条目被标记成「失败」",
                "失败" in text and "missing.js" in text,
                "",
            )
            record(
                "6.3 failure 条目带上失败原因（否则只剩「失败了」三个字）",
                "net::ERR_FAILED" in text,
                "",
            )
            record(
                "6.4 failure 之后仍能渲染正常条目（循环没被中断）",
                "ok.js" in text and "200" in text,
                "",
            )
        except Exception:
            record("6. failure 条目", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 7. 输出长度可控 + 条数上限 ---")
        # ------------------------------------------------------------------
        try:
            entries = [
                {
                    "sequence": i,
                    "kind": "response",
                    "method": "GET",
                    "url": HUGE_URL,
                    "status": 200,
                }
                for i in range(1, 61)
            ]
            fake = FakeRunner(
                {"network": console_payload(entries, next_since=60)}
            )
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="network")

            record(
                "7.1 只展示上限条数（60 条里只渲染 50 条）",
                text.count("GET") == 50,
                f"实际渲染 {text.count('GET')} 条",
            )
            record(
                "7.2 明确告知还有多少条没展示",
                "还有 10 条" in text,
                "",
            )
            record(
                "7.3 整体输出长度有上限（60 条超长 URL 也没撑爆上下文）",
                len(text) < 20000,
                f"输出 {len(text)} 字符（不截断会是 {60 * HUGE_URL_LEN} 字符以上）",
            )
        except Exception:
            record("7. 长度可控", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 8. kind 非法 / 权限门 ---")
        # ------------------------------------------------------------------
        try:
            fake = FakeRunner({})
            plugin = make_plugin(fake, tmp_dir)
            text = await call_logs(plugin, admin, kind="javascript")

            record(
                "8.1 非法 kind 被拒绝且不调用 bsk",
                fake.calls_for("console") == [] and "console" in text,
                text[:60].replace("\n", " "),
            )
            record(
                "8.2 非法 kind 的提示说明了两个合法取值",
                "network" in text and "RAISED" not in text,
                "",
            )
        except Exception:
            record("8. kind 校验", False, traceback.format_exc())

        try:
            fake = FakeRunner({"console": console_payload([], next_since=0)})
            plugin = make_plugin(fake, tmp_dir, admin_only=True)
            text = await call_logs(plugin, stranger, kind="console")

            record(
                "8.3 admin_only=true 时非管理员被 _denied 拒绝",
                "仅限管理员" in text,
                text[:60].replace("\n", " "),
            )
            record(
                "8.4 被拒绝时一条 bsk 命令都没发出去",
                fake.calls == [],
                f"实际发出 {len(fake.calls)} 条命令",
            )

            # 总开关关闭时同样拒绝（_denied 的第一条分支）。
            fake_off = FakeRunner({})
            plugin_off = make_plugin(fake_off, tmp_dir, enabled=False)
            text_off = await call_logs(plugin_off, admin, kind="console")
            record(
                "8.5 enabled=false 时管理员也被拒绝",
                "停用" in text_off and fake_off.calls == [],
                text_off[:50].replace("\n", " "),
            )
        except Exception:
            record("8. 权限门", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 9. 静态检查：只读、不执行代码、复用既有渲染 ---")
        # ------------------------------------------------------------------
        try:
            main_src = (PROJECT / "main.py").read_text(encoding="utf-8")
            start = main_src.index('@filter.llm_tool("bsk_logs")')
            # 下一个工具定义（或文件末尾）作为本工具函数体的结束位置。
            rest = main_src[start + 10 :]
            nxt = rest.find("@filter.llm_tool(")
            body = rest[:nxt] if nxt != -1 else rest

            record(
                "9.1 bsk_logs 复用 render_console（没有另写一份渲染）",
                "render_console" in body,
                "",
            )
            record(
                "9.2 走 _denied（只读操作），不走 _evaluate_denied",
                "_denied(event)" in body and "_evaluate_denied" not in body,
                "",
            )
            record(
                "9.3 只读：不调用 service.act / evaluate",
                "service.act" not in body and "service.evaluate" not in body,
                "",
            )
            record(
                "9.4 同时暴露了 console 与 network 两条路径",
                "read_console" in body and "read_network" in body,
                "",
            )
        except Exception:
            record("9. 静态检查", False, traceback.format_exc())

        # ------------------------------------------------------------------
        print("\n--- 10. models：ConsoleEntry 必须带上渲染所需字段 ---")
        # ------------------------------------------------------------------
        try:
            from bsk.models import ConsoleEntry

            # 这一条是 6.1 的根因护栏：render_console 会读 method/status，
            # 模型里没有这两个字段就是 AttributeError。
            entry = ConsoleEntry.from_json(
                {
                    "sequence": 3,
                    "kind": "failure",
                    "method": "GET",
                    "url": "https://example.com/x",
                    "error_text": "net::ERR_FAILED",
                }
            )
            record(
                "10.1 ConsoleEntry 解析出 method",
                entry.method == "GET",
                f"method={entry.method!r}",
            )
            record(
                "10.2 failure 条目缺 status 时降级为 0 而不是报错",
                entry.status == 0,
                f"status={entry.status}",
            )
            record(
                "10.3 ConsoleEntry 解析出 error_text",
                entry.error_text == "net::ERR_FAILED",
                f"error_text={entry.error_text!r}",
            )

            # 缺字段的条目仍要能构造（宽松解析是既有约定）。
            blank = ConsoleEntry.from_json({})
            record(
                "10.4 空 JSON 能构造出条目且不抛异常",
                blank.method == "" and blank.status == 0,
                f"{blank.method!r}/{blank.status}",
            )
        except Exception:
            record("10. models 字段", False, traceback.format_exc())

    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"用例：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


def main() -> int:
    _ensure_paths()
    # 输出里有中文：Windows 下 stdout 默认可能是 cp936，
    # 显式转 UTF-8，避免打印时抛 UnicodeEncodeError（见 ARCHITECTURE C9）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
