"""首次使用体验验证：环境没准备好时，用户看到的是什么？

这是**每个新用户都会先撞上的路径**，也是插件最容易给出糟糕体验的地方：
如果 bsk 没装、或浏览器扩展没连上，工具应当返回**能照着做**的中文提示，
而不是 Python 堆栈、空字符串、或一句"操作失败"。

本脚本用真实的插件实例 + 故意配错的环境，验证这些路径。
**不需要浏览器**（正因为环境是坏的，它压根到不了浏览器那一步）。

用法：
    python tests/verify_failure_ux.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
TESTS = PROJECT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

from astrbot_test_doubles import FakeEvent, make_context  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# 这些词出现在提示里，说明用户能照着做（而不是只看到"失败"）
ACTIONABLE_HINTS = ("bsk", "安装", "配置", "路径", "扩展", "连接", "doctor")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def looks_actionable(text: str) -> bool:
    """提示是否包含可操作信息（提到 bsk / 安装 / 配置 等）。"""
    return any(h in text for h in ACTIONABLE_HINTS)


def looks_like_stacktrace(text: str) -> bool:
    """是否是 Python 堆栈（用户完全看不懂，且暴露内部结构）。"""
    markers = ("Traceback (most recent call last)", "File \"", "Error: ", "Exception")
    return any(m in text for m in markers)


async def load_plugin(config: dict):
    """用真实 AstrBot 环境加载插件实例。"""
    module_name = "data.plugins.astrbot_plugin_bsk_browser.main"
    module = __import__(module_name, fromlist=["main"])
    return module.BskBrowserPlugin(make_context(), config=config)


async def main() -> int:
    print("=" * 72)
    print("首次使用体验验证：环境未就绪时的提示质量")
    print("=" * 72)

    # 确保 import 路径就绪（插件已装在 ~/.astrbot/data/plugins）
    home = os.path.expanduser("~")
    astrbot_root = os.path.join(home, ".astrbot")
    if os.path.isdir(astrbot_root) and astrbot_root not in sys.path:
        sys.path.insert(0, astrbot_root)

    admin = FakeEvent(is_admin=True, umo="test:first-run")

    # ------------------------------------------------------------------
    # 场景 1：bsk 路径指向一个不存在的文件
    # ------------------------------------------------------------------
    print("\n--- 场景 1：bsk 没装（路径写错）---")
    plugin = await load_plugin(
        {
            "bsk_path": r"C:\definitely\not\here\bsk.exe",
            "admin_only": True,
            "command_timeout_sec": 10,
        }
    )
    await plugin.initialize()

    out = await plugin.bsk_open(admin, url="https://example.com")
    print(f"      返回：{out[:220]}")
    check("bsk 未安装时返回字符串（不抛异常）", isinstance(out, str), f"类型={type(out).__name__}")
    check("不是 Python 堆栈", not looks_like_stacktrace(out), "堆栈会暴露内部结构且用户看不懂")
    check("提示可照着做", looks_actionable(out), "应提到 bsk / 安装 / 配置 / 路径")

    out_status = await plugin.bsk_status(admin)
    print(f"      status 返回：{out_status[:220]}")
    check("bsk_status 也给出可操作提示", looks_actionable(out_status), "")
    check("bsk_status 不是堆栈", not looks_like_stacktrace(out_status), "")

    out_read = await plugin.bsk_read(admin)
    check(
        "bsk_read 同样优雅降级",
        isinstance(out_read, str) and not looks_like_stacktrace(out_read),
        f"{out_read[:120]}",
    )

    out_close = await plugin.bsk_close(admin)
    check(
        "bsk_close 不会因环境坏而崩",
        isinstance(out_close, str) and not looks_like_stacktrace(out_close),
        f"{out_close[:120]}",
    )

    await plugin.terminate()

    # ------------------------------------------------------------------
    # 场景 2：权限被拒（非管理员）
    # ------------------------------------------------------------------
    print("\n--- 场景 2：非管理员调用（默认 admin_only=True）---")
    plugin2 = await load_plugin({"bsk_path": "bsk", "admin_only": True})
    await plugin2.initialize()

    stranger = FakeEvent(is_admin=False, sender_id="99999", umo="test:stranger")
    out = await plugin2.bsk_open(stranger, url="https://example.com")
    print(f"      返回：{out[:220]}")
    check("非管理员被拒绝", "管理员" in out or "权限" in out, f"{out[:120]}")
    check("拒绝提示说明怎么获得权限", "管理员" in out, "应告诉用户找管理员")

    # 被拒绝时**不应该**产生任何浏览器会话。
    # 注意要比较"调用前后"的差集，而不是断言总数为 0 —— daemon 是共享的，
    # 可能本来就有别的程序（用户的 DSH、其他会话）创建的会话。
    def _ids(data: object) -> set[str]:
        if not isinstance(data, list):
            return set()
        return {str(s.get("session_id")) for s in data if isinstance(s, dict)}

    snap_before = _ids(
        (await plugin2.service.runner.run(["session", "list", "--json"], timeout=10)).data
    )
    # 再试一次被拒的调用，确认它仍然不创建会话
    await plugin2.bsk_open(stranger, url="https://example.com")
    snap_after = _ids(
        (await plugin2.service.runner.run(["session", "list", "--json"], timeout=10)).data
    )
    created = snap_after - snap_before
    check(
        "被拒时不创建浏览器会话",
        not created,
        f"新增会话={sorted(created)}" if created else f"（daemon 中另有 {len(snap_after)} 个不属于本插件的会话）",
    )

    await plugin2.terminate()

    # ------------------------------------------------------------------
    # 场景 3：插件被停用
    # ------------------------------------------------------------------
    print("\n--- 场景 3：插件在配置里被停用 ---")
    plugin3 = await load_plugin({"enabled": False, "bsk_path": "bsk"})
    await plugin3.initialize()
    out = await plugin3.bsk_open(admin, url="https://example.com")
    print(f"      返回：{out[:220]}")
    check("停用后拒绝执行", "停用" in out or "禁用" in out, f"{out[:120]}")
    check("停用提示不是堆栈", not looks_like_stacktrace(out), "")
    out_status = await plugin3.bsk_status(admin)
    check("停用后 status 也说明原因", "停用" in out_status or "禁用" in out_status, f"{out_status[:120]}")
    await plugin3.terminate()

    # ------------------------------------------------------------------
    # 场景 4：URL 参数错误（用户/模型传错）
    # ------------------------------------------------------------------
    print("\n--- 场景 4：URL 参数不合法 ---")
    plugin4 = await load_plugin({"bsk_path": "bsk", "admin_only": True})
    await plugin4.initialize()

    # 空/空白 URL 的正确提示是"请提供要打开的网址"（先检查有没有值），
    # 有值但协议不对才提示"必须以 http(s) 开头"。两者都是好提示，
    # 所以断言分两组，不要用同一个条件硬套。
    empty_cases = (
        ("", "空字符串"),
        ("  ", "纯空白"),
    )
    bad_scheme_cases = (
        ("example.com", "缺协议"),
        ("ftp://example.com", "非 http 协议"),
        ("file:///C:/windows/win.ini", "file 协议"),
    )

    for url, desc in empty_cases:
        out = await plugin4.bsk_open(admin, url=url)
        ok = (
            isinstance(out, str)
            and "网址" in out
            and not looks_like_stacktrace(out)
        )
        check(f"拒绝 {desc}", ok, f"{out[:110]}")

    for url, desc in bad_scheme_cases:
        out = await plugin4.bsk_open(admin, url=url)
        ok = (
            isinstance(out, str)
            and "http" in out
            and not looks_like_stacktrace(out)
        )
        check(f"拒绝 {desc}", ok, f"{out[:110]}")

    await plugin4.terminate()

    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"检查项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
