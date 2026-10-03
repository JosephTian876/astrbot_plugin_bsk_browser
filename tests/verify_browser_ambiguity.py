"""聚焦验证：多浏览器歧义检测的行为是否符合设计。

重点验证三件事（这些是真实用户会遇到的分支）：
1. **恰好 1 个浏览器 → 仍然免配置自动选中**（这是最常见的场景，
   绝不能因为新增歧义检查而被破坏）
2. **≥2 个且未配置 → 报出可操作的错误**（列出每个 instance_id）
3. **≥2 个但已配置 → 尊重配置，不报歧义**

## 测试方式说明
``probe_browser()`` 内部用的是**同步** ``subprocess.run`` 调真实 bsk，
所以不能靠注入假 runner 来喂数据。好在判定逻辑被拆成了纯函数
``_pick_browser_from_probe(data)``，本脚本**直接测它** —— 覆盖面相同，
且不依赖 subprocess。

``probe_browser`` 与 ``SessionManager`` 的**异常穿透**行为另行验证
（用注入的 ``browser_probe``），见第 4 节。

用法：
    python tests/verify_browser_ambiguity.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from bsk.errors import BskBrowserAmbiguous  # noqa: E402
from bsk.service import BskService  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def browser(instance_id: str, name: str = "edge", label: str = "") -> dict:
    return {
        "instance_id": instance_id,
        "browser_name": name,
        "browser_version": "154.0.0.0",
        "extension_version": "0.3.2",
        "label": label,
        "session_count": 0,
        "unresponsive": False,
        "version_skew": False,
    }


def pick(data) -> str:
    """直接调用纯函数形式的判定逻辑。"""
    return BskService._pick_browser_from_probe(data)


async def main() -> int:
    print("=" * 72)
    print("多浏览器歧义检测行为验证")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 1. 恰好 1 个 → 免配置自动选中（必须不被破坏）
    # ------------------------------------------------------------------
    print("\n--- 1. 只有 1 个浏览器：仍然免配置自动选中 ---")
    try:
        picked = pick([browser("c900a3da")])
        check("自动选中唯一浏览器", picked == "c900a3da", f"picked={picked!r}")
    except Exception as exc:  # noqa: BLE001
        check("自动选中唯一浏览器", False, repr(exc))

    # ------------------------------------------------------------------
    # 2. 多个 → 报歧义，且文案可操作
    # ------------------------------------------------------------------
    print("\n--- 2. 多个浏览器且未配置：报可操作的歧义错误 ---")
    raised: BskBrowserAmbiguous | None = None
    try:
        pick([browser("c900a3da", "edge"), browser("ab12cd34", "chrome")])
        check("报出歧义错误", False, "没有抛异常（会退化成静默随机选）")
    except BskBrowserAmbiguous as exc:
        raised = exc
        check("报出歧义错误", True, f"code={exc.code}")
    except Exception as exc:  # noqa: BLE001
        check("报出歧义错误", False, f"抛了预期外的类型：{type(exc).__name__}")

    if raised is not None:
        text = raised.friendly
        print(f"      实际文案：\n{text}")
        check("文案列出第 1 个 instance_id", "c900a3da" in text, "")
        check("文案列出第 2 个 instance_id", "ab12cd34" in text, "")
        check(
            "文案告诉用户去改哪个配置项",
            "browser_instance_id" in text or "浏览器" in text,
            "模型需要据此告诉用户怎么做",
        )
        check(
            "文案给出了查看方式（bsk browsers）",
            "bsk browsers" in text,
            "",
        )
        check("code 正确", raised.code == "browser_ambiguous", f"{raised.code}")

    # ------------------------------------------------------------------
    # 3. 已配置 → 尊重配置（不依赖 probe）
    # ------------------------------------------------------------------
    print("\n--- 3. 已显式配置时：尊重配置，完全不调用 probe ---")
    from bsk.session import SessionManager

    settings3 = types.SimpleNamespace(
        max_sessions=2,
        idle_release_sec=0,
        browser_instance_id="ab12cd34",
        command_timeout_sec=30.0,
    )
    called = {"n": 0}

    def probe_that_raises() -> str:
        called["n"] += 1
        raise BskBrowserAmbiguous("不该被调用", friendly="不该被调用")

    class _NoopRunner:
        def resolve(self) -> str:
            return r"C:\fake\bsk.exe"

        async def run(self, args, *, timeout=None, expect_json=True):
            from bsk.models import BskResult

            return BskResult(ok=True, exit_code=0, data=[])

        async def run_or_raise(self, args, *, timeout=None, expect_json=True):
            return await self.run(args)

    mgr3 = SessionManager(
        _NoopRunner(), settings3, browser_probe=probe_that_raises
    )
    resolved = await mgr3._resolve_browser_instance()
    check(
        "★ 已配置时完全不调用 probe",
        called["n"] == 0,
        f"probe 被调用 {called['n']} 次（应为 0）",
    )
    check("解析出配置里的 instance_id", resolved == "ab12cd34", f"{resolved!r}")

    # ------------------------------------------------------------------
    # 4. 歧义异常必须能穿过 SessionManager（不能被吞成静默降级）
    # ------------------------------------------------------------------
    print("\n--- 4. ★ 歧义异常能穿过 SessionManager（不被吞掉）---")
    settings4 = types.SimpleNamespace(
        max_sessions=2,
        idle_release_sec=0,
        browser_instance_id="",  # 未配置 → 会走 probe
        command_timeout_sec=30.0,
    )

    def ambiguous_probe() -> str:
        raise BskBrowserAmbiguous(
            "多个浏览器", friendly="检测到 2 个浏览器，请配置 browser_instance_id"
        )

    mgr4 = SessionManager(_NoopRunner(), settings4, browser_probe=ambiguous_probe)
    try:
        await mgr4._resolve_browser_instance()
        check(
            "★ 歧义异常未被吞掉",
            False,
            "被吞掉了 → 会退化成静默随机选浏览器（正是要修的行为）",
        )
    except BskBrowserAmbiguous:
        check("★ 歧义异常未被吞掉", True, "正确抛出")

    # 对照：普通异常仍应被吞掉并降级（原有容错语义不能丢）
    def broken_probe() -> str:
        raise RuntimeError("bsk 崩了")

    mgr5 = SessionManager(_NoopRunner(), settings4, browser_probe=broken_probe)
    try:
        got = await mgr5._resolve_browser_instance()
        check(
            "普通探测失败仍降级为空串（保持原有容错）",
            got == "",
            f"got={got!r}",
        )
    except Exception as exc:  # noqa: BLE001
        check("普通探测失败仍降级为空串（保持原有容错）", False, repr(exc))

    # ------------------------------------------------------------------
    # 5. 边界情况
    # ------------------------------------------------------------------
    print("\n--- 5. 边界情况 ---")
    try:
        picked6 = pick([browser(""), browser("c900a3da")])
        check(
            "空 instance_id 不计入歧义（仍自动选中唯一的有效实例）",
            picked6 == "c900a3da",
            f"picked={picked6!r}",
        )
    except BskBrowserAmbiguous as exc:
        check(
            "空 instance_id 不计入歧义（仍自动选中唯一的有效实例）",
            False,
            f"不该报歧义：{exc.friendly[:80]}",
        )

    try:
        picked7 = pick([browser("c900a3da"), browser("c900a3da")])
        check(
            "重复 instance_id 去重后不算多个",
            picked7 == "c900a3da",
            f"picked={picked7!r}",
        )
    except BskBrowserAmbiguous:
        check("重复 instance_id 去重后不算多个", False, "重复项被误当成歧义")

    picked8 = pick([])
    check(
        "0 个浏览器不报歧义（交给 bsk 自己报错）",
        picked8 == "",
        f"picked={picked8!r}",
    )

    # 非列表输入（外部数据可能是任何类型）
    for bad, desc in ((None, "None"), ({}, "dict"), ("x", "字符串"), ([1, 2], "非字典元素")):
        try:
            got_bad = pick(bad)
            check(f"畸形输入（{desc}）降级为空串不抛异常", got_bad == "", f"got={got_bad!r}")
        except Exception as exc:  # noqa: BLE001
            check(f"畸形输入（{desc}）降级为空串不抛异常", False, repr(exc))

    # label 为空时展示不崩
    try:
        pick([browser("aaaa1111", "edge", ""), browser("bbbb2222", "chrome", "")])
        check("label 为空时展示不崩", False, "没报歧义")
    except BskBrowserAmbiguous as exc:
        check(
            "label 为空时展示不崩（回退到 browser_name）",
            "edge" in exc.friendly and "chrome" in exc.friendly,
            f"文案={exc.friendly[:100]!r}",
        )

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
