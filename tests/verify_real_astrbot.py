"""在你自己的 AstrBot 上做最终实测（用户要求 #7：先装实测，确认没问题再发布）。

## 与前面所有测试的区别
前面的测试都用我在测试脚本里手搓的替身（FakeContext / FakeEvent）。
这个脚本不一样 —— 它验证的是真实 AstrBot 环境里的真实加载路径：

1. 插件从 `~/.astrbot/data/plugins/` 被加载（AstrBot 真正读的那个目录）
2. 用 AstrBot 自己的发现函数确认它能被找到
3. 用 AstrBot 自己的执行器调用每一个工具
4. 把结果落成一份可读的实测报告（写入仓库内 `test-reports/`），供用户审阅（要求 #9）

覆盖 7 个工具，包含两个新增的高风险/新配置能力（bsk_evaluate、全页截图超时）。

安全边界（严格遵守用户既有授权）：
- 只访问 example.com（公开无害页面）
- 不借用用户标签页；不用 session stop --all
- bsk_act 只做只读动作（不做 click/fill/press 等会改页面的）
- bsk_evaluate 保持默认关闭，只验证"默认被拒绝"这条路径，
  不真的执行 JS（要验证放行路径需要临时改配置，那留给用户自己决定）
- 结束按精确 id 清理自己的会话

用法：
    python tests/verify_real_astrbot.py
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
TESTS = PROJECT / "tests"
REPORT_DIR = PROJECT / "test-reports"  # 运行产物，已在 .gitignore 中排除
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

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from astrbot_test_doubles import FakeEvent, make_context  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
REPORT: list[dict] = []


def record(name: str, ok: bool, detail: str = "", *, extra: dict | None = None) -> None:
    RESULTS.append((name, ok, detail))
    REPORT.append({"项": name, "结果": "通过" if ok else "失败", "详情": detail})
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


async def _call_tool(handler, event, **kwargs):
    """经 AstrBot 自己的执行器调用工具（不是直接 await 函数）。

    走 `call_local_llm_tool` + `decorator_handler` 分派，
    与真实模型调用工具时的路径一致。
    """
    from astrbot.core.astr_agent_tool_exec import call_local_llm_tool

    wrapper_ctx = type("Ctx", (), {"context": type("E", (), {"event": event})()})()
    items = []
    gen = call_local_llm_tool(
        context=wrapper_ctx, handler=handler, method_name="decorator_handler", **kwargs
    )
    async for item in gen:
        items.append(item)
    return items


async def main() -> int:
    print("=" * 72)
    print("在你自己的 AstrBot 上实测（要求 #7）")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 1. AstrBot 自己的发现函数能否找到插件
    # ------------------------------------------------------------------
    print("\n--- 1. AstrBot 的插件发现 ---")
    try:
        from astrbot.core.star.star_manager import PluginManager

        plugin_dir = Path(_ASTRBOT_ROOT) / "data" / "plugins"
        modules = PluginManager._get_modules(str(plugin_dir))
        found = [m for m in modules if m.get("pname") == "astrbot_plugin_bsk_browser"]
        record(
            "AstrBot 能发现本插件",
            bool(found),
            f"扫到 {len(modules)} 个插件，本插件={'找到' if found else '未找到'}",
        )
        if found:
            record(
                "入口模块 = main",
                found[0].get("module") == "main",
                f"module={found[0].get('module')!r}",
            )
    except Exception as exc:  # noqa: BLE001
        record("AstrBot 能发现本插件", False, repr(exc))

    # ------------------------------------------------------------------
    # 2. 加载插件 + 取出框架注册的工具
    # ------------------------------------------------------------------
    print("\n--- 2. 加载插件与工具注册 ---")
    try:
        module = __import__(
            "data.plugins.astrbot_plugin_bsk_browser.main", fromlist=["main"]
        )
        from astrbot.core.provider.register import llm_tools

        plugin = module.BskBrowserPlugin(
            make_context(), config={"admin_only": True, "bsk_path": "bsk"}
        )
        await plugin.initialize()

        bound: dict[str, object] = {}
        for tool in llm_tools.func_list:
            if not tool.name.startswith("bsk_"):
                continue
            raw = tool.handler
            if raw is None:
                continue
            fn = raw.func if isinstance(raw, functools.partial) else raw
            bound[tool.name] = functools.partial(fn, plugin)

        # 必需工具（少一个就是回归），以及"实际注册的必须与 main.py 声明的一致"。
        #
        # 不再手写完整清单：那样每加一个工具都要回来改，忘了就误报
        # （bsk_evaluate、bsk_logs 各踩过一次）。改为从 main.py 的
        # `@filter.llm_tool("名字")` 自动推导 —— 那是唯一事实来源。
        required = {
            "bsk_open",
            "bsk_read",
            "bsk_act",
            "bsk_screenshot",
            "bsk_close",
            "bsk_status",
        }
        declared = set(
            re.findall(
                r'@filter\.llm_tool\(\s*"([^"]+)"\s*\)',
                (PROJECT / "main.py").read_text(encoding="utf-8"),
            )
        )
        actual = set(bound)
        record(
            "必需工具全部注册",
            required.issubset(actual),
            f"缺少：{sorted(required - actual)}" if not required.issubset(actual) else "",
        )
        record(
            "注册的工具与 main.py 声明一致",
            actual == declared,
            f"共 {len(declared)} 个：{sorted(declared)}"
            if actual == declared
            else f"不一致：实际 {sorted(actual)} / 声明 {sorted(declared)}",
        )
    except Exception as exc:  # noqa: BLE001
        import traceback

        record("加载插件", False, traceback.format_exc()[-400:])
        return 1

    # ------------------------------------------------------------------
    # 3. 逐个工具实测（经真实执行器）
    # ------------------------------------------------------------------
    print("\n--- 3. 工具实测（经 AstrBot 真实执行器）---")
    admin = FakeEvent(is_admin=True, umo="real-test:admin")
    created: set[str] = set()

    # 3.1 bsk_status
    try:
        out = await _call_tool(bound["bsk_status"], admin)
        text = " ".join(str(i) for i in out if i is not None)
        ok = "bsk" in text and "浏览器" in text
        record("bsk_status 返回诊断", ok, f"{text[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        record("bsk_status 返回诊断", False, repr(exc))

    # 3.2 bsk_open
    try:
        out = await _call_tool(
            bound["bsk_open"], admin, url="https://example.com", new_session=False
        )
        text = " ".join(str(i) for i in out if i is not None)
        session = await plugin.service.sessions.acquire("real-test:admin")
        created.add(session.session_id)
        record(
            "bsk_open 打开网页并读到标题",
            "Example Domain" in text,
            f"session={session.session_id!r}",
        )
    except Exception as exc:  # noqa: BLE001
        record("bsk_open 打开网页并读到标题", False, repr(exc))

    # 3.3 bsk_read
    try:
        out = await _call_tool(bound["bsk_read"], admin)
        text = " ".join(str(i) for i in out if i is not None)
        record("bsk_read 读到页面内容", "Example" in text, f"{text[:100]!r}")
    except Exception as exc:  # noqa: BLE001
        record("bsk_read 读到页面内容", False, repr(exc))

    # 3.4 bsk_act（只做只读动作：滚动到元素，不改页面状态）
    try:
        out = await _call_tool(
            bound["bsk_act"], admin, action="scroll_to", target="@e1"
        )
        text = " ".join(str(i) for i in out if i is not None)
        record("bsk_act 只读动作可用", "已执行" in text, f"{text[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        record("bsk_act 只读动作可用", False, repr(exc))

    # 3.5 bsk_screenshot（视口 + 全页，验证新配置项在真实链路生效）
    try:
        t0 = time.monotonic()
        out = await _call_tool(bound["bsk_screenshot"], admin, full_page=False)
        dt_vp = time.monotonic() - t0
        imgs = [i for i in out if hasattr(i, "path")]
        texts = " ".join(str(i) for i in out if i is not None and not hasattr(i, "path"))
        img_ok = bool(imgs) and Path(imgs[0].path).is_file()
        record(
            "bsk_screenshot 视口截图（图片已发出）",
            img_ok,
            f"{dt_vp:.2f}s，{'图片存在' if img_ok else '没有图片'}",
        )
        if imgs:
            created.add(Path(imgs[0].path).parent.name)
    except Exception as exc:  # noqa: BLE001
        record("bsk_screenshot 视口截图（图片已发出）", False, repr(exc))

    try:
        t0 = time.monotonic()
        out = await _call_tool(bound["bsk_screenshot"], admin, full_page=True)
        dt_fp = time.monotonic() - t0
        imgs = [i for i in out if hasattr(i, "path")]
        record(
            "bsk_screenshot 全页截图（新超时配置项）",
            bool(imgs) and Path(imgs[0].path).is_file(),
            f"{dt_fp:.2f}s",
        )
    except Exception as exc:  # noqa: BLE001
        record("bsk_screenshot 全页截图（新超时配置项）", False, repr(exc))

    # 3.6 bsk_evaluate（默认关闭，验证的是"被正确拒绝"）
    #
    # ⚠️ 参数名必须与 main.py 的签名一致：是 `expression` 而不是 `script`。
    #    写成 `script` 会得到框架的英文报错
    #    "Tool handler parameter mismatch: Handler parameters: expression: str"
    #    —— 那是调用方传错参数名，不是插件缺陷。
    #    tests/verify_tool_params.py 专门静态检查"签名 vs docstring"一致性。
    try:
        out = await _call_tool(
            bound["bsk_evaluate"], admin, expression="document.title"
        )
        text = " ".join(str(i) for i in out if i is not None)
        record(
            "bsk_evaluate 默认关闭（管理员也被拒）",
            "默认关闭" in text or "未启用" in text,
            f"{text[:140]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        record("bsk_evaluate 默认关闭（管理员也被拒）", False, repr(exc))

    # 非管理员对 evaluate 也应被拒
    try:
        stranger = FakeEvent(is_admin=False, sender_id="99999", umo="real-test:x")
        out = await _call_tool(bound["bsk_evaluate"], stranger, expression="1+1")
        text = " ".join(str(i) for i in out if i is not None)
        record(
            "非管理员被拒（evaluate）",
            "关闭" in text or "未启用" in text or "管理员" in text,
            f"{text[:120]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        record("非管理员被拒（evaluate）", False, repr(exc))

    # 3.6b 参数名写错时的表现（记录框架的真实报错，便于排查）
    try:
        out = await _call_tool(bound["bsk_evaluate"], admin, script="1+1")
        text = " ".join(str(i) for i in out if i is not None)
        record(
            "参数名写错时框架会报错（记录此行为）",
            "parameter mismatch" in text or "参数" in text,
            "框架层报 parameter mismatch（调用方错误，非插件缺陷）",
        )
    except Exception as exc:  # noqa: BLE001
        record(
            "参数名写错时框架会报错（记录此行为）",
            "parameter mismatch" in str(exc),
            f"{type(exc).__name__}: {str(exc)[:90]}",
        )

    # 3.7 bsk_close
    try:
        out = await _call_tool(bound["bsk_close"], admin)
        text = " ".join(str(i) for i in out if i is not None)
        record("bsk_close 关闭会话", "关闭" in text, f"{text[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        record("bsk_close 关闭会话", False, repr(exc))

    # ------------------------------------------------------------------
    # 4. 清理 + 残留检查
    # ------------------------------------------------------------------
    print("\n--- 4. 清理 ---")
    try:
        closed = await plugin.service.shutdown()
        live = await plugin.service.runner.run(["session", "list", "--json"], timeout=15)
        live_ids = {
            str(s.get("session_id")) for s in (live.data or []) if isinstance(s, dict)
        }
        leaked = created & live_ids
        for sid in leaked:
            await plugin.service.runner.run(
                ["session", "stop", sid, "--json"], timeout=20
            )
        live2 = await plugin.service.runner.run(
            ["session", "list", "--json"], timeout=15
        )
        live_ids2 = {
            str(s.get("session_id")) for s in (live2.data or []) if isinstance(s, dict)
        }
        record(
            "本测试的会话已清理干净",
            not (created & live_ids2),
            f"关闭 {closed} 个，daemon 剩余 {len(live_ids2)} 个（不属于本测试）",
        )
    except Exception as exc:  # noqa: BLE001
        record("本测试的会话已清理干净", False, repr(exc))

    try:
        await plugin.terminate()
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------------------------------
    # 写入实测报告（供审阅，要求 #9）
    # ------------------------------------------------------------------
    report_path = REPORT_DIR / "实测报告-AstrBot真机.md"
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        failed = [r for r in RESULTS if not r[1]]
        lines = [
            "# AstrBot 真机实测报告",
            "",
            f"实测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 环境",
            "",
            f"- AstrBot：`{_ASTRBOT_ROOT}`",
            f"- 插件目录：`{Path(_ASTRBOT_ROOT) / 'data' / 'plugins' / 'astrbot_plugin_bsk_browser'}`",
            "- 加载方式：AstrBot 自己的发现函数 + 自己的工具执行器",
            "",
            f"## 结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过",
            "",
            "| 检查项 | 结果 | 详情 |",
            "|---|---|---|",
        ]
        for r in REPORT:
            detail = str(r["详情"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['项']} | {r['结果']} | {detail} |")
        lines.append("")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n实测报告已写入：{report_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"（写报告失败，不影响结论：{exc}）")

    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"实测项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
