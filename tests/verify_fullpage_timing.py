"""实测：全页截图到底要多久？AstrBot 的 120 秒工具超时是真问题吗？

## 为什么测
一直有人说"全页截图需要同时调大插件的 command_timeout_sec 和 AstrBot 的
tool_call_timeout"，但从没人量过**它实际要多久**。如果只要几秒，那所谓
"框架限制"根本不存在，文档里那段话就是在吓唬用户。

并且这决定了一件更重要的事：**能不能在插件侧解决**。
AstrBot 的超时是包在 `asyncio.wait_for(anext(wrapper), ...)` 上的
（`astr_agent_tool_exec.py:691`）—— 是**每一步**的超时，不是整个工具的超时。
如果工具是 async generator 且能中途 yield，理论上可以重置这个计时器。

安全边界：
- 只访问公开的、无副作用的长页面（Wikipedia 条目）
- 绝不借用用户标签页；不用 `session stop --all`
- 只截图，不做任何交互
- 结束按精确 id 清理自己的会话

用法：
    python tests/verify_fullpage_timing.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402
from bsk.service import BskService  # noqa: E402

# 一个内容较长、结构稳定的公开页面，适合衡量全页截图成本。
LONG_PAGE = "https://en.wikipedia.org/wiki/Web_browser"
SHORT_PAGE = "https://example.com"

KEY = "fullpage-timing:local"
FRAMEWORK_LIMIT_SEC = 120.0
"""AstrBot 的 tool_call_timeout 默认值（源码 run_context.py:19）。"""


async def _live(service: BskService) -> set[str]:
    r = await service.runner.run(["session", "list", "--json"], timeout=15)
    return {str(s.get("session_id")) for s in (r.data or []) if isinstance(s, dict)}


async def main() -> int:
    print("=" * 72)
    print("实测：全页截图耗时 vs AstrBot 的 120 秒工具超时")
    print("=" * 72)

    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 3})
    service = BskService(settings)
    before = await _live(service)

    results: list[tuple[str, float, str]] = []

    try:
        # --- 短页面：视口截图与全页截图对比 ---
        print(f"\n--- 短页面 {SHORT_PAGE} ---")
        nav, obs = await service.open_page(KEY, SHORT_PAGE)
        session = await service.sessions.acquire(KEY)
        print(f"    已打开（session={session.session_id!r}, title={obs.title!r}）")

        t0 = time.monotonic()
        shot_vp = await service.screenshot(KEY, full_page=False)
        dt_vp = time.monotonic() - t0
        results.append(("短页面-视口截图", dt_vp, f"{shot_vp.width}x{shot_vp.height}"))
        print(f"    视口截图 : {dt_vp:.2f}s  {shot_vp.width}x{shot_vp.height}  "
              f"{shot_vp.byte_size / 1024:.0f}KB")

        t0 = time.monotonic()
        shot_fp = await service.screenshot(KEY, full_page=True)
        dt_fp = time.monotonic() - t0
        results.append(("短页面-全页截图", dt_fp, f"{shot_fp.width}x{shot_fp.height}"))
        print(f"    全页截图 : {dt_fp:.2f}s  {shot_fp.width}x{shot_fp.height}  "
              f"{shot_fp.byte_size / 1024:.0f}KB")

        # --- 长页面：这才是全页截图的真实成本 ---
        print(f"\n--- 长页面（真实成本所在）---")
        print(f"    {LONG_PAGE}")
        t0 = time.monotonic()
        nav2, obs2 = await service.open_page(KEY, LONG_PAGE)
        dt_nav = time.monotonic() - t0
        print(f"    打开页面 : {dt_nav:.2f}s  title={obs2.title!r}")

        # 连测 3 次全页截图，看稳定性
        for i in range(3):
            t0 = time.monotonic()
            shot = await service.screenshot(KEY, full_page=True)
            dt = time.monotonic() - t0
            results.append((f"长页面-全页截图#{i + 1}", dt, f"{shot.width}x{shot.height}"))
            print(f"    全页截图 #{i + 1}: {dt:.2f}s  {shot.width}x{shot.height}  "
                  f"{shot.byte_size / 1024:.0f}KB")

    finally:
        await service.shutdown()
        after = await _live(service)
        for sid in (after - before):
            await service.runner.run(["session", "stop", sid, "--json"], timeout=20)

    # --- 结论 ---
    print()
    print("=" * 72)
    print("结论")
    print("=" * 72)
    for name, dt, extra in results:
        flag = "  ← 超过框架限制！" if dt > FRAMEWORK_LIMIT_SEC else ""
        print(f"  {name:26s} {dt:6.2f}s   {extra}{flag}")

    fullpage = [dt for name, dt, _ in results if "全页" in name]
    if fullpage:
        print()
        print(f"  全页截图：最长 {max(fullpage):.2f}s，中位数 {statistics.median(fullpage):.2f}s")
        worst = max(fullpage)
        if worst > FRAMEWORK_LIMIT_SEC:
            print(f"  ★ 确实会撞上 AstrBot 的 {FRAMEWORK_LIMIT_SEC:.0f}s 限制 —— 是真问题")
        else:
            margin = FRAMEWORK_LIMIT_SEC / worst
            print(f"  ★ 最长只用了框架限制的 1/{margin:.1f} —— **不撞限制**，"
                  "文档里那段警告是多余的")
            print(f"    （除非页面远端更慢。留了 {FRAMEWORK_LIMIT_SEC - worst:.0f}s 余量）")

    print()
    print(f"  daemon 清理后: {sorted(await _live(service))}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
