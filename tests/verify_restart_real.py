"""真实环境验证：模拟"AstrBot 被强杀后重启"，验证遗留会话被自动清理。

## 为什么这是最有价值的端到端验证
`recover_orphans` 的**唯一**设计目的就是应对这个场景，但此前只用假 runner
或"手工构造 journal"验证过。本脚本走**完整真实路径**：

    进程 A：真实建会话 → 写入真实 journal → **不 stop 直接退出**（模拟被强杀）
    进程 B：读同一份 journal → recover_orphans() → 只清掉自己的遗留会话

这验证了 journal 的**跨进程可用性**（时间戳、原子写、文件位置、格式），
而这些是单元测试覆盖不到的。

同时验证**反向安全性**：进程 B 启动时 daemon 里若还有**别人的**会话
（模拟用户的 DSH），绝不能被误停。

安全边界：
- 进程 A 只建**自己**的会话，只开空白页不导航；
- 绝不用 `session stop --all`；
- 进程 B 只清理 journal 里记录且**窗口号匹配**的会话；
- 结束后清理本脚本创建的一切。

用法：
    python tests/verify_restart_real.py          # 主控，自动跑 A、B 两个阶段
    python tests/verify_restart_real.py --phase-a # 内部使用
    python tests/verify_restart_real.py --phase-b # 内部使用
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402
from bsk.journal import SessionJournal  # noqa: E402
from bsk.runner import BskRunner  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


async def _live_ids(runner: BskRunner) -> set[str]:
    result = await runner.run(["session", "list", "--json"], timeout=15)
    data = result.data
    if not isinstance(data, list):
        return set()
    return {str(s.get("session_id")) for s in data if isinstance(s, dict)}


# ----------------------------------------------------------------------
# 阶段 A：建会话 + 写 journal，然后**故意不 stop**就退出
# ----------------------------------------------------------------------
async def phase_a(journal_path: Path, out_path: Path) -> int:
    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 5})
    runner = BskRunner(settings.bsk_path, default_timeout=60.0)
    journal = SessionJournal(journal_path)
    manager = SessionManager(runner, settings, journal=journal)

    # 记录启动前已存在的会话（模拟"别人的会话"，稍后要验证没被动）
    pre_existing = await _live_ids(runner)

    s1 = await manager.acquire("restart-test:orphan-1")
    s2 = await manager.acquire("restart-test:orphan-2")

    payload = {
        "orphans": [s1.session_id, s2.session_id],
        "pre_existing": sorted(pre_existing),
        "journal_path": str(journal_path),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ★ 关键：**不调用 release / terminate**，直接结束进程，
    #   模拟 AstrBot 被强杀。会话仍然活在 daemon 里，journal 里留着记录。
    print(f"  阶段 A：已建会话 {payload['orphans']}，故意不 stop 就退出")
    print(f"          journal 已落盘：{journal_path}")
    print(f"          启动前已存在的（别人的）会话：{payload['pre_existing']}")
    return 0


# ----------------------------------------------------------------------
# 阶段 B：新进程读同一份 journal，清理遗留
# ----------------------------------------------------------------------
async def phase_b(state_path: Path) -> int:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    orphans = set(state["orphans"])
    pre_existing = set(state["pre_existing"])
    journal_path = Path(state["journal_path"])

    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 5})
    runner = BskRunner(settings.bsk_path, default_timeout=60.0)
    journal = SessionJournal(journal_path)

    before = await _live_ids(runner)
    print(f"  阶段 B：恢复前 daemon 里有 {len(before)} 个会话：{sorted(before)}")

    manager = SessionManager(runner, settings, journal=journal)
    cleaned = await manager.recover_orphans()
    after = await _live_ids(runner)

    print(f"          recover_orphans 报告清理 {cleaned} 个")
    print(f"          恢复后 daemon：{sorted(after)}")

    result = {
        "cleaned": cleaned,
        "before": sorted(before),
        "after": sorted(after),
        "orphans": sorted(orphans),
        "pre_existing": sorted(pre_existing),
        "journal_left": len(journal.load()),
    }
    (state_path.parent / "phase_b_result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )

    # 保险：如果孤儿没被清掉，这里补刀（精确 id）
    for sid in orphans & after:
        await runner.run(["session", "stop", sid, "--json"], timeout=20)
    return 0


async def main() -> int:
    if "--phase-a" in sys.argv:
        idx = sys.argv.index("--phase-a")
        return await phase_a(Path(sys.argv[idx + 1]), Path(sys.argv[idx + 2]))
    if "--phase-b" in sys.argv:
        idx = sys.argv.index("--phase-b")
        return await phase_b(Path(sys.argv[idx + 1]))

    print("=" * 72)
    print("真实环境验证：模拟 AstrBot 被强杀后重启，遗留会话能否自动清理")
    print("=" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="bsk-restart-"))
    journal_path = tmp / "sessions.json"
    state_path = tmp / "phase_a.json"
    py = sys.executable
    this = str(Path(__file__).resolve())

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)

    def run_phase(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [py, this, *args],
            cwd=str(PROJECT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )

    # --- 阶段 A ---
    print("\n--- 阶段 A：建会话后模拟强杀（不 stop）---")
    pa = run_phase("--phase-a", str(journal_path), str(state_path))
    print(pa.stdout.strip() or "(无输出)")
    if pa.returncode != 0:
        check("阶段 A 成功", False, f"exit={pa.returncode}\n{pa.stderr[-500:]}")
        return 1
    check("阶段 A 成功建会话并落盘 journal", state_path.is_file(), "")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    orphans = state["orphans"]
    check("journal 文件真实存在", journal_path.is_file(), str(journal_path))

    # 验证 journal 内容可跨进程读取（时间戳是墙钟、格式正确）
    j = SessionJournal(journal_path)
    loaded = j.load()
    check(
        "★ 新进程能读出 journal 记录",
        len(loaded) == len(orphans),
        f"记录数={len(loaded)}，期望={len(orphans)}",
    )
    if loaded:
        rec = loaded[0]
        check(
            "记录里含 agent_window_id（碰撞防护的前提）",
            rec.agent_window_id > 0,
            f"agent_window_id={rec.agent_window_id}",
        )
        check(
            "created_at 是墙钟时间戳（可跨进程比较）",
            rec.created_at > 1_600_000_000,
            f"created_at={rec.created_at}",
        )

    # --- 阶段 B ---
    print("\n--- 阶段 B：新进程启动，自动清理遗留 ---")
    pb = run_phase("--phase-b", str(state_path))
    print(pb.stdout.strip() or "(无输出)")
    if pb.returncode != 0:
        check("阶段 B 成功", False, f"exit={pb.returncode}\n{pb.stderr[-500:]}")

    res_path = tmp / "phase_b_result.json"
    if not res_path.is_file():
        check("阶段 B 产出结果", False, "没有结果文件")
        return 1
    res = json.loads(res_path.read_text(encoding="utf-8"))

    # --- 断言 ---
    print("\n--- 断言 ---")
    check(
        "★ 自己的遗留会话被清理干净",
        not (set(orphans) & set(res["after"])),
        f"孤儿={orphans}，恢复后仍在={sorted(set(orphans) & set(res['after']))}",
    )
    check(
        "清理数量与遗留数量一致",
        res["cleaned"] == len(orphans),
        f"cleaned={res['cleaned']}，孤儿={len(orphans)}",
    )
    # 反向安全：别人的会话不能被误停
    if res["pre_existing"]:
        check(
            "★ 别人的既有会话未被误停",
            set(res["pre_existing"]).issubset(set(res["after"])),
            f"既有={res['pre_existing']}，恢复后={res['after']}",
        )
    else:
        print("      （运行前无他人会话，跳过反向安全断言）")
    check("journal 已被清空（避免下次重复处理）", res["journal_left"] == 0, "")

    print()
    print("=" * 72)
    failed = [r for r in RESULTS if not r[1]]
    print(f"检查项：{len(RESULTS)}，失败：{len(failed)}")
    for name, _, detail in failed:
        print(f"  [FAIL] {name}: {detail}")
    print("结果：" + ("全部通过" if not failed else "有失败"))

    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
