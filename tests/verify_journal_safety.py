"""会话 journal 恢复行为验证（含最关键的"碰撞防护"）。

为什么单独验证这个：``recover_orphans`` 会在插件启动时自动停掉一批会话。
如果它的归属判断有漏洞，插件就会误停别的程序的会话 —— 例如用户正在用的
DSH 的浏览器会话。这是本插件里后果最严重的一类 bug，值得独立验证，
而不是只依赖单元测试。

bsk 的 session_id 只有 4 个小写字母（26^4 ≈ 45 万），存在碰撞可能：
我们记录的 ``mnaa`` 早已过期，之后另一个程序也建了一个 ``mnaa``。
所以恢复时必须同时比对 ``agent_window_id``。

本脚本用真实的 ``SessionJournal`` + 真实的 ``SessionManager``（假 runner）
构造这些场景，验证只停自己的。

用法：
    python tests/verify_journal_safety.py
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from bsk.errors import BskBrowserError  # noqa: E402
from bsk.journal import JournalEntry, SessionJournal  # noqa: E402
from bsk.models import BskResult  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


class RecordingRunner:
    """记录所有 ``session stop`` 调用，并可预设 daemon 里的活跃会话。"""

    def __init__(self, live: list[dict]) -> None:
        self.live = live
        self.stopped: list[str] = []
        self.list_calls = 0

    async def run_or_raise(self, args, *, timeout=None, expect_json=True):
        cmd = list(args)
        if cmd[:2] == ["session", "list"]:
            self.list_calls += 1
            return BskResult(ok=True, exit_code=0, data=list(self.live))
        if cmd[:2] == ["session", "stop"]:
            # session stop <ID> —— id 是位置参数
            sid = cmd[2] if len(cmd) > 2 else ""
            self.stopped.append(sid)
            # 从活跃集合里移除，模拟真实停止
            self.live = [s for s in self.live if s.get("session_id") != sid]
            return BskResult(ok=True, exit_code=0, data=None)
        if cmd[:2] == ["session", "start"]:
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "session_id": "mnaa",
                    "browser_instance_id": "c900a3da",
                    "agent_window_id": 5000,
                    "interaction": {},
                },
            )
        return BskResult(ok=True, exit_code=0, data={})


def live_entry(sid: str, wid: int | None) -> dict:
    item: dict = {"session_id": sid, "browser_instance_id": "c900a3da"}
    if wid is not None:
        item["agent_window_id"] = wid
    return item


def make_settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        max_sessions=3,
        idle_release_sec=0,
        browser_instance_id="c900a3da",
        command_timeout_sec=30.0,
        journal_path="",
    )


async def main() -> int:
    print("=" * 72)
    print("会话 journal 恢复行为验证（重点：不误停别人的会话）")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 用例 1：正常恢复 —— journal 里的会话仍然活着且窗口号匹配
    # ------------------------------------------------------------------
    print("\n--- 1. 正常恢复：确认是自己的遗留会话，应当停掉 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        journal.add(JournalEntry("abcd", "c900a3da", 111, 1.0, 1234))
        runner = RecordingRunner([live_entry("abcd", 111)])
        manager = SessionManager(runner, make_settings(), journal=journal)

        cleaned = await manager.recover_orphans()
        check("停掉了自己的遗留会话", cleaned == 1, f"cleaned={cleaned}")
        check("stop 用精确 id（位置参数）", runner.stopped == ["abcd"], f"stopped={runner.stopped}")
        check("journal 已清空", journal.load() == [], f"剩余={journal.load()}")
        check("绝未使用 --all", "--all" not in " ".join(runner.stopped), "")

    # ------------------------------------------------------------------
    # 用例 2：碰撞防护 —— id 相同但窗口号不同，说明是别人的
    # ------------------------------------------------------------------
    print("\n--- 2. id 碰撞：窗口号不同 → 必须跳过，一条 stop 都不能发 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        # 我们记录的 abcd 属于窗口 111（早已消失）
        journal.add(JournalEntry("abcd", "c900a3da", 111, 1.0, 1234))
        # daemon 里活着的 abcd 属于窗口 999 —— 是别的程序刚建的
        runner = RecordingRunner([live_entry("abcd", 999)])
        manager = SessionManager(runner, make_settings(), journal=journal)

        cleaned = await manager.recover_orphans()
        check("清理数为 0（认出来不是自己的）", cleaned == 0, f"cleaned={cleaned}")
        check(
            "一条 stop 都没发（没误杀别人的会话）",
            runner.stopped == [],
            f"stopped={runner.stopped} ← 必须是空列表",
        )
        check("journal 仍被清空（避免下次重复尝试）", journal.load() == [], "")
        check("别人的会话还在", any(s["session_id"] == "abcd" for s in runner.live), "")

    # ------------------------------------------------------------------
    # 用例 3：journal 里的会话已经不存在（最常见的正常情况）
    # ------------------------------------------------------------------
    print("\n--- 3. 常见情况：遗留会话早已自己消失 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        journal.add(JournalEntry("abcd", "c900a3da", 111, 1.0, 1234))
        runner = RecordingRunner([])  # daemon 里什么都没有
        manager = SessionManager(runner, make_settings(), journal=journal)

        cleaned = await manager.recover_orphans()
        check("清理数为 0", cleaned == 0, f"cleaned={cleaned}")
        check("没发任何 stop", runner.stopped == [], f"stopped={runner.stopped}")

    # ------------------------------------------------------------------
    # 用例 4：daemon 返回的条目缺 agent_window_id → 无法确认身份，跳过
    # ------------------------------------------------------------------
    print("\n--- 4. daemon 未提供窗口号 → 无法确认归属，宁可漏清不误停 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        journal.add(JournalEntry("abcd", "c900a3da", 111, 1.0, 1234))
        runner = RecordingRunner([live_entry("abcd", None)])  # 缺 window_id
        manager = SessionManager(runner, make_settings(), journal=journal)

        cleaned = await manager.recover_orphans()
        check("清理数为 0", cleaned == 0, f"cleaned={cleaned}")
        check("没发任何 stop", runner.stopped == [], f"stopped={runner.stopped}")

    # ------------------------------------------------------------------
    # 用例 5：journal 为空 → 零开销（连 session list 都不该发）
    # ------------------------------------------------------------------
    print("\n--- 5. journal 为空：应当零额外开销 ---")
    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        runner = RecordingRunner([live_entry("abcd", 111)])
        manager = SessionManager(runner, make_settings(), journal=journal)

        cleaned = await manager.recover_orphans()
        check("清理数为 0", cleaned == 0, f"cleaned={cleaned}")
        check(
            "没有发 session list（零开销）",
            runner.list_calls == 0,
            f"list_calls={runner.list_calls}",
        )

    # ------------------------------------------------------------------
    # 用例 6：daemon 没在跑（session list 报错）→ 不抛异常，journal 清掉
    # ------------------------------------------------------------------
    print("\n--- 6. daemon 没在跑：不能抛异常，且 journal 要清掉 ---")

    class DeadRunner(RecordingRunner):
        async def run_or_raise(self, args, *, timeout=None, expect_json=True):
            if list(args)[:2] == ["session", "list"]:
                raise BskBrowserError(
                    "daemon down", friendly="后台服务没在跑", code="no_daemon", exit_code=2
                )
            return await super().run_or_raise(args, timeout=timeout, expect_json=expect_json)

    with tempfile.TemporaryDirectory() as tmp:
        journal = SessionJournal(Path(tmp) / "s.json")
        journal.add(JournalEntry("abcd", "c900a3da", 111, 1.0, 1234))
        runner = DeadRunner([])
        manager = SessionManager(runner, make_settings(), journal=journal)

        try:
            cleaned = await manager.recover_orphans()
            check("daemon 不可用时不抛异常", True, f"cleaned={cleaned}")
        except Exception as exc:  # noqa: BLE001
            check("daemon 不可用时不抛异常", False, repr(exc))
        check("journal 被清空（daemon 都没了，会话自然也没了）", journal.load() == [], "")

    # ------------------------------------------------------------------
    # 用例 7：journal 文件损坏 → 不影响启动
    # ------------------------------------------------------------------
    print("\n--- 7. journal 文件损坏：不能影响插件启动 ---")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.json"
        p.write_text("{ 这不是合法 JSON", encoding="utf-8")
        journal = SessionJournal(p)
        runner = RecordingRunner([])
        manager = SessionManager(runner, make_settings(), journal=journal)

        try:
            cleaned = await manager.recover_orphans()
            check("损坏 journal 不抛异常", True, f"cleaned={cleaned}")
        except Exception as exc:  # noqa: BLE001
            check("损坏 journal 不抛异常", False, repr(exc))

    # ------------------------------------------------------------------
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
