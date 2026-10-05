"""真实调用链验证：走 AstrBot 自己的工具执行器，而不是直接调函数。

## 与已有测试的区别
`verify_tools_e2e.py` 是直接 await 工具函数（`plugin.bsk_read(event)`）。
但那不是生产路径 —— 真实调用要经过 AstrBot 的工具执行层，它会：

1. 用 ``functools.partial`` 把插件实例绑上去（``star_manager.py:1300``）；
2. 经 ``call_local_llm_tool`` 按 ``method_name`` 分派（``decorator_handler``）；
3. 包一层 ``asyncio.wait_for(tool_call_timeout)``（默认 120 秒）；
4. 消费 async generator，并把 yield 出来的图片走
   ``tool_direct_result`` 通道发给用户（``astr_agent_tool_exec.py:708-716``）。

任何一环与我们的假设不符，真实使用就会失败，而直接调函数的测试
完全看不出来。例如：如果工具签名不被 ``decorator_handler`` 接受，
或者 yield 的对象不是 AstrBot 认识的类型，图片就发不出去。

## 覆盖范围
不启动浏览器：只验证执行链本身能用，以及拒绝路径（权限/参数）
经真实执行器返回的结果形态正确。真实浏览器交互已由其他脚本覆盖。

用法：
    python tests/verify_tool_execution_chain.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
TESTS = PROJECT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)
_ASTRBOT_ROOT = os.environ["ASTRBOT_ROOT"]
if os.path.isdir(_ASTRBOT_ROOT) and _ASTRBOT_ROOT not in sys.path:
    sys.path.insert(0, _ASTRBOT_ROOT)

from astrbot_test_doubles import FakeEvent, make_context  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


async def main() -> int:
    print("=" * 72)
    print("真实调用链验证：经 AstrBot 的工具执行器调用插件工具")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 1. 加载插件，取到框架注册的工具对象
    # ------------------------------------------------------------------
    print("\n--- 1. 加载插件并取到注册的工具 ---")
    try:
        module = __import__(
            "data.plugins.astrbot_plugin_bsk_browser.main", fromlist=["main"]
        )
    except Exception as exc:  # noqa: BLE001
        check("import 插件", False, repr(exc))
        return 1

    from astrbot.core.provider.register import llm_tools

    plugin = module.BskBrowserPlugin(make_context(), config={"admin_only": True})
    await plugin.initialize()

    # 框架在加载时已经用 functools.partial 把插件实例绑上去了；
    # 这里手动绑定，模拟 star_manager 的行为。
    import functools

    bound: dict[str, object] = {}
    for tool in llm_tools.func_list:
        if not tool.name.startswith("bsk_"):
            continue
        raw = tool.handler
        if raw is None:
            continue
        fn = raw.func if isinstance(raw, functools.partial) else raw
        bound[tool.name] = functools.partial(fn, plugin)

    expected = {
        "bsk_open",
        "bsk_read",
        "bsk_act",
        "bsk_screenshot",
        "bsk_close",
        "bsk_status",
    }
    check(
        "6 个工具都拿到了可调用句柄",
        expected.issubset(set(bound)),
        f"拿到：{sorted(bound)}",
    )

    # ------------------------------------------------------------------
    # 2. 经真实执行器调用：拒绝路径（非管理员）
    # ------------------------------------------------------------------
    print("\n--- 2. 经 call_local_llm_tool 执行（非管理员应被拒）---")
    try:
        from astrbot.core.astr_agent_tool_exec import call_local_llm_tool
    except Exception as exc:  # noqa: BLE001
        check("import call_local_llm_tool", False, repr(exc))
        await plugin.terminate()
        return 1

    stranger = FakeEvent(is_admin=False, sender_id="88888", umo="chain:stranger")
    wrapper_ctx = types.SimpleNamespace(
        context=types.SimpleNamespace(event=stranger)
    )

    handler = bound["bsk_open"]
    collected: list[object] = []
    try:
        gen = call_local_llm_tool(
            context=wrapper_ctx,
            handler=handler,
            method_name="decorator_handler",
            url="https://example.com",
            new_session=False,
        )
        async for item in gen:
            collected.append(item)
        check("经真实执行器调用成功（未抛异常）", True, f"yield 了 {len(collected)} 项")
    except Exception as exc:  # noqa: BLE001
        import traceback

        check(
            "经真实执行器调用成功（未抛异常）",
            False,
            traceback.format_exc()[-400:],
        )

    text_out = " ".join(str(c) for c in collected if c is not None)
    check(
        "非管理员经真实执行器被拒绝",
        "管理员" in text_out or "权限" in text_out,
        f"输出={text_out[:160]!r}",
    )
    check(
        "拒绝结果不是空（模型能拿到可读文本）",
        bool(text_out.strip()),
        "",
    )

    # ------------------------------------------------------------------
    # 3. 参数错误经真实执行器也应返回文本而非抛异常
    # ------------------------------------------------------------------
    print("\n--- 3. 参数错误经真实执行器（应返回文本）---")
    admin = FakeEvent(is_admin=True, umo="chain:admin")
    wrapper_ctx2 = types.SimpleNamespace(context=types.SimpleNamespace(event=admin))

    for tool_name, kwargs, expect_kw in (
        ("bsk_open", {"url": "ftp://x", "new_session": False}, "http"),
        ("bsk_act", {"action": "click", "target": ""}, "元素"),
    ):
        collected2: list[object] = []
        try:
            gen2 = call_local_llm_tool(
                context=wrapper_ctx2,
                handler=bound[tool_name],
                method_name="decorator_handler",
                **kwargs,
            )
            async for item in gen2:
                collected2.append(item)
            text2 = " ".join(str(c) for c in collected2 if c is not None)
            check(
                f"{tool_name} 参数错误返回可读文本",
                expect_kw in text2,
                f"输出={text2[:140]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            check(f"{tool_name} 参数错误返回可读文本", False, repr(exc))

    # ------------------------------------------------------------------
    # 4. bsk_status 经真实执行器应返回诊断文本
    # ------------------------------------------------------------------
    print("\n--- 4. bsk_status 经真实执行器 ---")
    try:
        collected3: list[object] = []
        gen3 = call_local_llm_tool(
            context=wrapper_ctx2,
            handler=bound["bsk_status"],
            method_name="decorator_handler",
        )
        async for item in gen3:
            collected3.append(item)
        text3 = " ".join(str(c) for c in collected3 if c is not None)
        # 本机环境正常时应当能看到 bsk 路径；环境异常时应当给出可操作提示。
        # 两者都算"有意义的诊断输出"。
        has_meaning = ("bsk" in text3) or ("没找到" in text3) or ("浏览器" in text3)
        check(
            "bsk_status 经执行器返回诊断文本",
            bool(text3.strip()) and has_meaning,
            f"{text3[:160]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("bsk_status 经执行器返回诊断文本", False, traceback.format_exc()[-300:])

    await plugin.terminate()

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
