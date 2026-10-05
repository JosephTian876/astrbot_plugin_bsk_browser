"""真实环境验证：新增暴露的 wait_for_navigation 动作确实可用。

## 为什么单独验证
`wait_for_navigation` 此前已实现但三处清单都没列（main.py 的 docstring、
service.py 的错误提示、README 的动作表），意味着模型根本不知道它存在 ——
实质是不可达的死代码。本次把它补齐到三处。

补齐之后必须验证两件事：
1. 它能真的工作（对真实浏览器发得出去、bsk 接受这个命令）；
2. 它确实是只读的（不会改变页面状态，因此可以在不确定态下安全使用）。

安全边界：只访问 example.com、不借用用户标签页、不做任何写操作、
不用 `session stop --all`、结束显式清理自己的会话。

用法：
    python tests/verify_wait_navigation_real.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402
from bsk.service import BskService  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


KEY = "wait-nav-test:local"


async def _live(service: BskService) -> set[str]:
    result = await service.runner.run(["session", "list", "--json"], timeout=15)
    data = result.data
    if not isinstance(data, list):
        return set()
    return {str(s.get("session_id")) for s in data if isinstance(s, dict)}


async def main() -> int:
    print("=" * 72)
    print("真实环境验证：wait_for_navigation 动作")
    print("=" * 72)

    settings = parse_settings({"bsk_path": "bsk", "max_sessions": 3})
    service = BskService(settings)

    created: set[str] = set()
    try:
        # --- 打开页面 ---
        print("\n--- 1. 打开 example.com ---")
        nav, obs = await service.open_page(KEY, "https://example.com")
        session = await service.sessions.acquire(KEY)
        created.add(session.session_id)
        check(
            "打开网页成功",
            "Example Domain" in (obs.title or ""),
            f"session={session.session_id!r}, title={obs.title!r}",
        )

        # --- 调用 wait_for_navigation（本次新增暴露的动作）---
        print("\n--- 2. 执行 wait_for_navigation ---")
        try:
            result = await service.act(KEY, "wait_for_navigation")
            check(
                "wait_for_navigation 执行成功",
                True,
                f"action={result.action!r}",
            )
        except Exception as exc:  # noqa: BLE001
            check("wait_for_navigation 执行成功", False, f"{type(exc).__name__}: {exc}")

        # --- 验证它确实只读：页面内容不变 ---
        print("\n--- 3. 验证只读性：页面内容不变 ---")
        after = await service.observe(KEY)
        check(
            "调用后页面标题不变",
            (after.title or "") == (obs.title or ""),
            f"之前={obs.title!r} 之后={after.title!r}",
        )

        # --- 验证连字符写法也被接受（main.py 会转换，这里直接测 service 层）---
        print("\n--- 4. 连字符写法兼容（service 层归一化由 main.py 负责）---")
        try:
            await service.act(KEY, "wait-for-navigation")
            check("service 层接受连字符写法", False, "应当报错（归一化在 main.py）")
        except Exception as exc:  # noqa: BLE001
            msg = getattr(exc, "friendly", str(exc))
            check(
                "service 层对未归一化的写法给出可读提示",
                "不支持的动作" in msg and "wait_for_navigation" in msg,
                f"{msg[:90]!r}",
            )

    finally:
        print()
        print("-" * 72)
        try:
            closed = await service.shutdown()
            print(f"清理：关闭了 {closed} 个会话")
            remaining = await _live(service)
            leaked = created & remaining
            check(
                "本测试的会话已清理干净",
                not leaked,
                f"泄漏={sorted(leaked)}" if leaked else "无残留",
            )
        except Exception as exc:  # noqa: BLE001
            check("本测试的会话已清理干净", False, repr(exc))

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
