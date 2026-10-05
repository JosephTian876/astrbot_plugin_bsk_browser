"""真实环境测量：session stop 的实际耗时分布。

## 为什么测这个
`session stop` 加了有界重试后，最坏情况的耗时上界从 20s 变成约 42s
（`STOP_TOTAL_BUDGET_SEC`）。这个上界只在"重试也用尽"时才触及，
但需要数据来说明常见路径到底多快 —— 否则无法判断这个上界是否可接受。

本脚本对真实 bsk daemon 反复建/停会话，测量：
1. 正常 stop 的实际耗时（中位数 / 最大值）
2. 重试是否会被触发（正常情况下不应触发）
3. `terminate()` 风格的整体清理耗时

安全边界：只建/停本脚本自己的会话，绝不用 `--all`，只开空白页不导航。

用法：
    python tests/verify_stop_timing_real.py
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
from bsk.runner import BskRunner  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

ROUNDS = 8
"""建/停轮数。真实 stop 很快，8 轮足够看出分布，又不至于让用户等太久。"""


async def main() -> int:
    print("=" * 72)
    print(f"真实环境测量：session stop 耗时（{ROUNDS} 轮）")
    print("=" * 72)

    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 5})
    runner = BskRunner(settings.bsk_path, default_timeout=60.0)
    manager = SessionManager(runner, settings)

    created: list[str] = []
    stop_times: list[float] = []
    start_times: list[float] = []

    try:
        for i in range(ROUNDS):
            t0 = time.monotonic()
            session = await manager.acquire(f"timing:{i}")
            start_times.append(time.monotonic() - t0)
            created.append(session.session_id)

            t1 = time.monotonic()
            ok = await manager.release(f"timing:{i}")
            elapsed = time.monotonic() - t1
            stop_times.append(elapsed)
            print(
                f"  第 {i + 1} 轮: start {start_times[-1]:.3f}s  "
                f"stop {elapsed:.3f}s  {'成功' if ok else '失败'}"
            )
    finally:
        # 保险：把自己建的会话都停掉
        live = await runner.run(["session", "list", "--json"], timeout=15)
        live_ids = {
            str(s.get("session_id"))
            for s in (live.data or [])
            if isinstance(s, dict)
        }
        for sid in [c for c in created if c in live_ids]:
            await runner.run(["session", "stop", sid, "--json"], timeout=20)

    stats = manager.stats()
    print()
    print("-" * 72)
    print("统计")
    print("-" * 72)
    print(f"  session start  中位数 {statistics.median(start_times):.3f}s  "
          f"最大 {max(start_times):.3f}s")
    print(f"  session stop   中位数 {statistics.median(stop_times):.3f}s  "
          f"最大 {max(stop_times):.3f}s")
    counters = stats["counters"]
    print(f"  stop 重试次数      : {counters.get('stop_retries', 0)}"
          f"  （正常路径应为 0）")
    print(f"  靠重试救回         : {counters.get('stop_recovered', 0)}")
    print(f"  预算不足跳过重试   : {counters.get('stop_retry_budget_skips', 0)}")
    print(f"  stop 失败          : {counters.get('stop_failed', 0)}")

    # 清理证明
    final = await runner.run(["session", "list", "--json"], timeout=15)
    remaining = {
        str(s.get("session_id")) for s in (final.data or []) if isinstance(s, dict)
    }
    leaked = [c for c in created if c in remaining]
    print()
    print(f"  本脚本残留会话     : {leaked or '无'}")

    ok_all = not leaked and counters.get("stop_failed", 0) == 0
    print()
    print("=" * 72)
    print("结果：" + ("全部正常" if ok_all else "有问题，见上"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
