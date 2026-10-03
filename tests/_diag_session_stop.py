"""聚焦诊断：会话 stop 到底有没有生效。

不猜，直接观察：用本插件的 SessionManager 建会话 → 立刻 stop → 查 daemon
是否还认得它。这样能把"stop 没生效"和"会话被遗漏没 stop"两种情况分开。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from bsk.config import parse_settings  # noqa: E402
from bsk.runner import BskRunner  # noqa: E402
from bsk.session import SessionManager  # noqa: E402


async def daemon_session_ids(runner: BskRunner) -> set[str]:
    """直接问 daemon：现在有哪些会话。"""
    result = await runner.run(["session", "list", "--json"], timeout=10)
    data = result.data or []
    if not isinstance(data, list):
        return set()
    return {str(s.get("session_id")) for s in data if isinstance(s, dict)}


async def main() -> int:
    settings = parse_settings(
        {"bsk_path": "bsk", "max_sessions": 5, "command_timeout_sec": 60}
    )
    runner = BskRunner(settings.bsk_path, default_timeout=settings.command_timeout_sec)
    manager = SessionManager(runner, settings)

    print("=" * 70)
    print("诊断：会话 stop 是否真的生效")
    print("=" * 70)

    before = await daemon_session_ids(runner)
    print(f"开始前 daemon 里的会话: {sorted(before)}")

    # --- 用例 A：建会话后立即 release ---
    key_a = "diag:release"
    session = await manager.acquire(key_a)
    sid_a = session.session_id
    print(f"\n[A] 创建会话 {sid_a!r}")
    after_create = await daemon_session_ids(runner)
    print(f"    daemon 现在: {sorted(after_create)}")
    print(f"    会话确实存在: {sid_a in after_create}")

    released = await manager.release(key_a)
    print(f"    release() 返回: {released}")
    after_release = await daemon_session_ids(runner)
    print(f"    release 后 daemon: {sorted(after_release)}")
    still_there = sid_a in after_release
    print(f"    ★ 会话 {sid_a!r} 是否还在: {still_there}")

    # --- 用例 B：建两个会话，用 shutdown 一起清 ---
    key_b1, key_b2 = "diag:b1", "diag:b2"
    s1 = await manager.acquire(key_b1)
    s2 = await manager.acquire(key_b2)
    print(f"\n[B] 创建两个会话 {s1.session_id!r}, {s2.session_id!r}")
    ids_now = await daemon_session_ids(runner)
    print(f"    daemon 现在: {sorted(ids_now)}")

    closed = await manager.release_all()
    print(f"    release_all() 报告关闭了 {closed} 个")
    after_shutdown = await daemon_session_ids(runner)
    print(f"    release_all 后 daemon: {sorted(after_shutdown)}")
    leaked = {s1.session_id, s2.session_id} & after_shutdown
    print(f"    ★ 泄漏的: {sorted(leaked)}")

    # --- 统计 ---
    stats = manager.stats()
    print(f"\n统计: {stats.get('counters')}")
    errs = stats.get("recent_stop_errors")
    if errs:
        print(f"最近的 stop 错误: {errs}")

    print()
    print("=" * 70)
    ok_a = not still_there
    ok_b = not leaked
    print(f"用例 A（release 生效）: {'通过' if ok_a else '失败'}")
    print(f"用例 B（shutdown 生效）: {'通过' if ok_b else '失败'}")

    # 清理诊断自己可能泄漏的
    for sid in sorted(({sid_a} | {s1.session_id, s2.session_id}) & await daemon_session_ids(runner)):
        r = await runner.run(["session", "stop", sid, "--json"], timeout=15)
        print(f"补救 stop {sid}: rc={r.exit_code}")

    final = await daemon_session_ids(runner)
    print(f"最终 daemon 里的会话: {sorted(final)}（原本就有的是 {sorted(before)}）")
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
