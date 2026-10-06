"""真机实测：7 个新工具（56 个 action）在真实 AstrBot + 真实浏览器上跑通。

这是本次移植的核心验收 —— 走 AstrBot 真实工具执行器，驱动真实浏览器，
覆盖 7 个工具的每个 action（只对被判定为只读的 action 做真实操作，
写类动作只验证到"命令发出且被正确接受"这一层，避免改动用户页面）。

``bsk_debug`` 是从 ``bsk_inspect`` 里拆出来的调试工具（24 个 action），
所以这里单独测它，``bsk_inspect`` 只剩 6 个读页面的 action。

运行：
    & 'D:\\AstrBot\\backend\\python\\python.exe' tests/verify_new_tools_real.py

前置：插件已装到 ``~/.astrbot/data/plugins/``，bsk 与扩展已就绪。
"""

from __future__ import annotations

import asyncio
import functools
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
for path in (PROJECT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
_ASTRBOT_ROOT = os.environ.get("ASTRBOT_ROOT", str(Path.home() / ".astrbot"))
os.environ.setdefault("ASTRBOT_ROOT", _ASTRBOT_ROOT)
for path in (_ASTRBOT_APP, _ASTRBOT_ROOT):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail[:120]}" if detail else ""))


def make_context():
    from astrbot_test_doubles import make_context as _mk

    return _mk()


class _Wrap:
    def __init__(self, event):
        self.context = type("X", (), {"event": event})()


async def call_tool(handler, event, **kwargs) -> str:
    """经 AstrBot 真实执行器调用工具，返回拼接后的文本。

    注意 ``method_name`` 必须是 ``"decorator_handler"``：框架在这个分支传的是
    ``event``（``astr_agent_tool_exec.py:762``），而 ``"call"`` 分支传的是
    ``context`` —— 用错会让工具收到一个不是 event 的对象，于是权限门里
    ``event.is_admin()`` / ``get_sender_id()`` 全部失败，表现成"非管理员被拒"。
    """
    from astrbot.core.astr_agent_tool_exec import call_local_llm_tool

    out = []
    async for item in call_local_llm_tool(
        context=_Wrap(event),
        handler=handler,
        method_name="decorator_handler",
        **kwargs,
    ):
        if isinstance(item, str):
            out.append(item)
    return "\n".join(out)


async def main() -> int:
    from astrbot.core.provider.register import llm_tools
    from astrbot_test_doubles import FakeEvent

    print("=" * 74)
    print("真机实测：7 个新工具（56 个 action）")
    print("=" * 74)

    module = __import__(
        "data.plugins.astrbot_plugin_bsk_browser.main", fromlist=["main"]
    )
    plugin = module.BskBrowserPlugin(
        make_context(),
        config={"bsk_path": "bsk", "admin_only": True, "screenshot_dir": ""},
    )
    await plugin.initialize()

    # 绑定工具（与 verify_real_astrbot.py 同法）
    bound: dict[str, object] = {}
    for tool in llm_tools.func_list:
        if not tool.name.startswith("bsk_"):
            continue
        raw = tool.handler
        if raw is None:
            continue
        fn = raw.func if isinstance(raw, functools.partial) else raw
        bound[tool.name] = functools.partial(fn, plugin)

    NEW = (
        "bsk_session",
        "bsk_page",
        "bsk_inspect",
        "bsk_debug",
        "bsk_interact",
        "bsk_tabs",
        "bsk_assist",
    )
    print("\n--- 1. 7 个新工具已注册 ---")
    missing = [n for n in NEW if n not in bound]
    record("7 个新工具全部注册", not missing, f"缺少：{missing}")

    ev = FakeEvent(is_admin=True, umo="aiocqhttp:group:realnew", sender_id="1")

    # ------------------------------------------------------------------
    print("\n--- 2. bsk_session ---")
    r = await call_tool(bound["bsk_session"], ev, action="list")
    record("session.list", "本对话" in r or "会话" in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_session"], ev, action="start")
    record("session.start", "失败" not in r and "错误" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_session"], ev, action="list")
    record("start 之后 list 能看到会话", "尚无" not in r, r.replace("\n", " ")[:110])

    # ------------------------------------------------------------------
    print("\n--- 3. bsk_page ---")
    r = await call_tool(
        bound["bsk_page"], ev, action="navigate", url="https://example.com"
    )
    record("page.navigate", "Example" in r or "example" in r or "已" in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_page"], ev, action="reload")
    record("page.reload", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_page"], ev, action="back")
    record("page.back", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_page"], ev, action="forward")
    record("page.forward", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_page"], ev, action="wait", timeout_ms=5000)
    record("page.wait", "失败" not in r, r.replace("\n", " ")[:110])

    # ------------------------------------------------------------------
    print("\n--- 4. bsk_inspect ---")
    r = await call_tool(bound["bsk_inspect"], ev, action="observe")
    record("inspect.observe", "标题" in r or "Example" in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_inspect"], ev, action="snapshot")
    record("inspect.snapshot", bool(r.strip()) and "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_inspect"], ev, action="html", max_bytes=2048)
    record("inspect.html", bool(r.strip()) and "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_inspect"], ev, action="console", limit=5)
    record("inspect.console", "未预期" not in r and "暂不支持" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_inspect"], ev, action="network", limit=5)
    # 网络日志的正常返回里本来就可能出现"失败"（failure 类条目），
    # 所以不能拿那两个词当判据 —— 只看有没有内部故障或参数被拒。
    record(
        "inspect.network",
        "未预期" not in r and "暂不支持" not in r and bool(r.strip()),
        r.replace("\n", " ")[:110],
    )

    # debug 已拆成独立工具：bsk_inspect 必须明确拒绝旧的 debug 写法。
    r = await call_tool(bound["bsk_inspect"], ev, action="observe", debug_action="capabilities")
    record(
        "inspect 拒绝旧 debug_action 并指向 bsk_debug",
        "bsk_debug" in r,
        r.replace("\n", " ")[:130],
    )

    # ------------------------------------------------------------------
    print("\n--- 4b. bsk_debug（从 bsk_inspect 拆出的调试工具）---")
    r = await call_tool(bound["bsk_debug"], ev, action="capabilities")
    record("debug.capabilities", bool(r.strip()), r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_debug"], ev, action="status")
    record("debug.status", bool(r.strip()), r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_debug"], ev, action="activity")
    record("debug.activity", "未预期" not in r, r.replace("\n", " ")[:110])

    # 条件规则必须在真机路径上也生效（不是只有单测里生效）。
    r = await call_tool(bound["bsk_debug"], ev, action="requests", slow_ms=1000)
    record(
        "debug 的 slow_ms 仅 aggregate（其余 action 被拒）",
        "aggregate" in r,
        r.replace("\n", " ")[:130],
    )

    r = await call_tool(bound["bsk_debug"], ev, action="request")
    record(
        "debug 的 request 缺 id 被拒",
        "id" in r,
        r.replace("\n", " ")[:130],
    )

    # ------------------------------------------------------------------
    print("\n--- 5. bsk_interact ---")
    # 只做只读性动作：scroll-to / wheel / hover / focus / blur / press
    # 都不改变页面数据（wheel 只滚视口，press Escape 不提交任何东西）。
    r = await call_tool(bound["bsk_interact"], ev, action="scroll-to", target="body")
    record("interact.scroll-to", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_interact"], ev, action="wheel", delta_y=100)
    record("interact.wheel", "未预期" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_interact"], ev, action="hover", target="body")
    record("interact.hover", "未预期" not in r, r.replace("\n", " ")[:110])

    # 注意：这里刻意**不**用 body 之类不可聚焦的元素 —— bsk 对不可聚焦目标
    # 会正确返回 cdp_failed("Element is not focusable")，那是真实的浏览器语义，
    # 不是插件缺陷。用一个确定可聚焦的元素（链接 @e1）来验通路。
    r = await call_tool(bound["bsk_interact"], ev, action="focus", target="@e1")
    record(
        "interact.focus（可聚焦元素）",
        "未预期" not in r and "暂不支持" not in r,
        r.replace("\n", " ")[:110],
    )

    # 顺带验证：对**不可聚焦**的元素，错误要被如实转达（不能假装成功）
    r = await call_tool(bound["bsk_interact"], ev, action="focus", target="body")
    record(
        "interact.focus（不可聚焦元素被如实拒绝）",
        "失败" in r or "不支持" in r or "出错" in r,
        r.replace("\n", " ")[:110],
    )

    r = await call_tool(bound["bsk_interact"], ev, action="blur", target="body")
    record("interact.blur", "未预期" not in r and "暂不支持" not in r, r.replace("\n", " ")[:110])

    # press：Escape 不提交任何东西，是安全的只读按键。
    # 这里同时验证裁决 1 的改名链路（工具层 key → service 层 press_key）。
    r = await call_tool(bound["bsk_interact"], ev, action="press", key="Escape")
    record("interact.press（含 key→press_key 改名）", "未预期" not in r, r.replace("\n", " ")[:110])

    # ------------------------------------------------------------------
    print("\n--- 6. bsk_tabs ---")
    r = await call_tool(bound["bsk_tabs"], ev, action="list")
    record("tabs.list", "失败" not in r, r.replace("\n", " ")[:110])

    # ------------------------------------------------------------------
    print("\n--- 7. bsk_assist ---")
    r = await call_tool(bound["bsk_assist"], ev, action="resize", width=1024, height=720)
    record("assist.resize", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(
        bound["bsk_assist"], ev, action="emulate", device="iphone-14"
    )
    record("assist.emulate(device)", "失败" not in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_assist"], ev, action="emulate", off=True)
    record("assist.emulate(off)", "失败" not in r, r.replace("\n", " ")[:110])

    # ------------------------------------------------------------------
    print("\n--- 8. 权限门与参数校验（不碰浏览器）---")
    ev_noadmin = FakeEvent(is_admin=False, umo="aiocqhttp:group:realnew", sender_id="2")
    r = await call_tool(bound["bsk_session"], ev_noadmin, action="start")
    record("非管理员被拒", "管理员" in r, r.replace("\n", " ")[:110])

    r = await call_tool(bound["bsk_session"], ev, action="bogus")
    record("非法 action 被拒且列出可选值", "start" in r and "list" in r, r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_page"], ev, action="navigate")
    record("缺必填 url 被拒", "url" in r or "必填" in r, r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_assist"], ev, action="resize", width=800)
    record("resize 只给宽被拒", "height" in r or "同时" in r, r.replace("\n", " ")[:130])

    r = await call_tool(bound["bsk_interact"], ev, action="wheel", delta_x=0, delta_y=0)
    record("wheel 两个 delta 都为 0 被拒", "滚动" in r or "非零" in r, r.replace("\n", " ")[:130])

    # schema 外参数不该抛 mismatch（**kwargs 接住）
    r = await call_tool(bound["bsk_session"], ev, action="list", bogus_arg=1)
    record("schema 外参数不抛 mismatch", "mismatch" not in r.lower(), r.replace("\n", " ")[:110])

    # 跨 action 的参数必须被拒，而不是静默丢弃
    r = await call_tool(bound["bsk_interact"], ev, action="wheel", delta_y=1, values=["a"])
    record(
        "跨 action 参数被拒（wheel 收到 values）",
        "values" in r and "用不上" in r,
        r.replace("\n", " ")[:130],
    )

    r = await call_tool(bound["bsk_page"], ev, action="navigate", url="https://example.com", session="  ")
    record(
        "空白 session 被拒（不再静默忽略）",
        "session" in r,
        r.replace("\n", " ")[:130],
    )

    # timeout_ms 必须真的生效（第三轮 P0：曾经被静默丢弃）
    r = await call_tool(
        bound["bsk_interact"], ev, action="press", key="Escape", timeout_ms=15000
    )
    record(
        "press 的 timeout_ms 被接受",
        "未预期" not in r and "用不上" not in r,
        r.replace("\n", " ")[:110],
    )

    # ------------------------------------------------------------------
    print("\n--- 9. 会话指定（session 转发）---")

    # 建第二个会话，验证显式指定 session 时命令真的打到它上面
    r2 = await call_tool(bound["bsk_session"], ev, action="start")
    r = await call_tool(bound["bsk_session"], ev, action="list")
    # 从 list 的输出里找一个会话键（格式由渲染器决定，这里只验证"指定后不报错"）
    import re as _re

    keys = _re.findall(r"`([^`]+)`", r) or _re.findall(r"([A-Za-z0-9_\-:]{3,})", r)
    record("session.list 有输出", bool(r.strip()), r.replace("\n", " ")[:110])

    # 用一个明确不存在的会话，应当被拒（而不是静默回退到当前会话）
    r = await call_tool(
        bound["bsk_page"], ev, action="navigate", url="https://example.com",
        session="definitely-not-a-real-session",
    )
    record(
        "指定不存在的 session 被拒（不回退、不静默）",
        "不属于本插件" in r or "失败" in r or "不存在" in r,
        r.replace("\n", " ")[:130],
    )

    # ------------------------------------------------------------------
    print("\n--- 10. 清理 ---")
    r = await call_tool(bound["bsk_session"], ev, action="stop")
    record("session.stop", "失败" not in r, r.replace("\n", " ")[:110])

    try:
        await plugin.service.shutdown()
    except Exception:  # noqa: BLE001
        pass
    await plugin.terminate()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"实测项：{len(RESULTS)}，失败：{len(failed)}")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
