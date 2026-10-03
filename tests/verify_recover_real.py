"""真实环境验证：recover_orphans 对**真实 bsk daemon** 的行为。

为什么必须单独做这一步：journal 的恢复逻辑此前只用**假 runner** 验证过。
假 runner 的行为是我假设的，而真实 daemon 的返回结构、错误语义可能不同 ——
一旦假设错了，"清理孤儿会话"可能会变成"不清理"甚至"误停别人的会话"。

本脚本刻意制造一个**真实的孤儿会话**，然后验证恢复逻辑能正确识别并清理它。

安全边界：
- 只创建/停止**本脚本自己**的会话，绝不用 `session stop --all`；
- 不访问任何网站（只开会话、不导航）；
- 结束前无论成败都清理干净。

用法：
    python tests/verify_recover_real.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# 参考其他脚本：钉住 AstrBot root（本脚本其实不 import astrbot，但保持一致）
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402
from bsk.journal import JournalEntry, SessionJournal  # noqa: E402
from bsk.runner import BskRunner  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


async def live_sessions(runner: BskRunner) -> dict[str, dict]:
    """真实查询 daemon 当前会话：{session_id: 原始条目}。"""
    result = await runner.run(["session", "list", "--json"], timeout=15)
    data = result.data
    if not isinstance(data, list):
        return {}
    return {
        str(s.get("session_id")): s
        for s in data
        if isinstance(s, dict) and s.get("session_id")
    }


async def main() -> int:
    print("=" * 72)
    print("真实环境验证：recover_orphans 对真实 daemon 的行为")
    print("=" * 72)

    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 5})
    runner = BskRunner(settings.bsk_path, default_timeout=60.0)

    before = await live_sessions(runner)
    print(f"\n开始前 daemon 里的会话：{sorted(before)}")

    # ------------------------------------------------------------------
    # 1. 真实创建两个会话（模拟"上次进程遗留"）
    # ------------------------------------------------------------------
    print("\n--- 1. 真实创建两个会话（模拟遗留）---")
    live_manager = SessionManager(runner, settings)  # 不带 journal，纯创建
    created: list[str] = []
    try:
        s1 = await live_manager.acquire("recover-test:a")
        s2 = await live_manager.acquire("recover-test:b")
        created = [s1.session_id, s2.session_id]
        check("真实创建了 2 个会话", len(set(created)) == 2, f"{created}")
    except Exception as exc:  # noqa: BLE001
        check("真实创建会话", False, repr(exc))
        await live_manager.release_all()
        return 1

    after_create = await live_sessions(runner)
    check(
        "daemon 里能看到它们",
        all(c in after_create for c in created),
        f"当前={sorted(after_create)}",
    )

    # 关键：真实 daemon 返回的字段里有没有 agent_window_id？
    # journal 的碰撞防护完全依赖这个字段。
    sample = after_create.get(created[0], {})
    has_wid = "agent_window_id" in sample
    check(
        "★ 真实 session list 带 agent_window_id（碰撞防护的前提）",
        has_wid,
        f"字段={sorted(sample)}" if sample else "没取到样本",
    )

    # ------------------------------------------------------------------
    # 2. 用 journal 模拟"新进程启动"：只把 s1 当作自己的遗留
    # ------------------------------------------------------------------
    print("\n--- 2. 模拟新进程：journal 里只记 s1，验证只清 s1 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "sessions.json")
        journal.add(
            JournalEntry(
                session_id=created[0],
                browser_instance_id=str(sample.get("browser_instance_id") or ""),
                agent_window_id=int(sample.get("agent_window_id") or 0),
                created_at=1.0,
                pid=0,
            )
        )

        fresh = SessionManager(runner, settings, journal=journal)
        cleaned = await fresh.recover_orphans()
        check("recover_orphans 报告清理了 1 个", cleaned == 1, f"cleaned={cleaned}")

        after_recover = await live_sessions(runner)
        check(
            "s1（journal 记录的）已被真实停止",
            created[0] not in after_recover,
            f"当前={sorted(after_recover)}",
        )
        check(
            "★ s2（不在 journal 里的，模拟别人的会话）没被动",
            created[1] in after_recover,
            f"s2 是否还在={created[1] in after_recover}",
        )
        check("journal 已清空", journal.load() == [], "")

    # ------------------------------------------------------------------
    # 3. 碰撞防护的真实版：journal 里的窗口号故意写错
    # ------------------------------------------------------------------
    print("\n--- 3. ★ 真实碰撞场景：窗口号不匹配时必须跳过 ---")
    still = await live_sessions(runner)
    target = created[1]
    if target in still:
        real_wid = int(still[target].get("agent_window_id") or 0)
        with tempfile.TemporaryDirectory() as tmp:
            journal = SessionJournal(Path(tmp) / "sessions.json")
            journal.add(
                JournalEntry(
                    session_id=target,
                    browser_instance_id=str(
                        still[target].get("browser_instance_id") or ""
                    ),
                    # 故意写一个**不同**的窗口号，模拟"id 撞车但不是我们的"
                    agent_window_id=real_wid + 999999,
                    created_at=1.0,
                    pid=0,
                )
            )
            fresh = SessionManager(runner, settings, journal=journal)
            cleaned = await fresh.recover_orphans()
            after = await live_sessions(runner)
            check(
                "窗口号不匹配 → 清理数为 0",
                cleaned == 0,
                f"cleaned={cleaned}",
            )
            check(
                "★ 真实会话未被误停（没误杀别人的会话）",
                target in after,
                f"{target} 是否还在={target in after}",
            )
    else:
        print("      （跳过：目标会话已不在，无法构造真实碰撞场景）")

    # ------------------------------------------------------------------
    # 4. 清理：把自己建的全部停掉（精确 id）
    # ------------------------------------------------------------------
    print("\n--- 4. 清理自己创建的会话 ---")
    final_live = await live_sessions(runner)
    leftover = [c for c in created if c in final_live]
    for sid in leftover:
        r = await runner.run(["session", "stop", sid, "--json"], timeout=20)
        print(f"      stop {sid} -> rc={r.exit_code}")

    end = await live_sessions(runner)
    mine_left = [c for c in created if c in end]
    check("本脚本创建的会话已全部清理", not mine_left, f"残留={mine_left}")
    check(
        "未干扰开始前就存在的会话",
        all(s in end for s in before),
        f"原有={sorted(before)}，现在={sorted(end)}",
    )

    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"检查项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))
    return 1 if failed else 0


import asyncio  # noqa: E402

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
