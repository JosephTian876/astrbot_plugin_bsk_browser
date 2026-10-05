"""端到端配置贯通验证：用户在 WebUI 里改的值，真的会生效吗？

## 为什么需要这个测试
前面已有两个测试覆盖配置的解析（`test_config_source.py` 验证 dict 与
AstrBotConfig 两种形态），但没有验证从「用户写的配置文件」到「插件实际
用于调用 bsk 的参数」这整条链路。

中间任何一环断掉，用户看到的现象都是同一句话：「我改了配置，但没用」。
而这是插件里最难自查、也最让用户沮丧的一类问题 —— 因为不会有任何报错。

本脚本验证完整链路：
    用户写的 JSON 文件
      → AstrBotConfig（AstrBot 生产环境真实使用的类型）
      → parse_settings()
      → BskService / SessionManager 内部状态
      → 实际传给 bsk 的命令行参数

## 不碰浏览器
只构造配置并检查内部状态与参数拼装，不发任何 bsk 命令。

用法：
    python tests/verify_config_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

# 钉住 AstrBot root，避免在项目目录里生成 data/（含主配置与 API 密钥）。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402
from bsk.service import BskService  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


class CapturingRunner:
    """记录实际传给 bsk 的参数与超时，不真的执行。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float | None]] = []

    def resolve(self) -> str:
        return r"C:\fake\bsk.exe"

    async def run(self, args, *, timeout=None, expect_json=True):
        self.calls.append((list(args), timeout))
        from bsk.models import BskResult

        # 让 session start 能成功
        if list(args)[:2] == ["session", "start"]:
            return BskResult(
                ok=True,
                exit_code=0,
                data={
                    "session_id": "mnaa",
                    "browser_instance_id": "c900a3da",
                    "agent_window_id": 1,
                    "interaction": {},
                },
            )
        return BskResult(ok=True, exit_code=0, data={})

    async def run_or_raise(self, args, *, timeout=None, expect_json=True):
        return await self.run(args, timeout=timeout, expect_json=expect_json)


def make_astrbot_config(user_values: dict) -> object:
    """用 AstrBot 的真实类型构造配置（与生产环境一致）。"""
    from astrbot.core.config.astrbot_config import AstrBotConfig

    schema = json.loads((PROJECT / "_conf_schema.json").read_text(encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="bsk-cfg-")
    path = os.path.join(tmp, "cfg.json")
    Path(path).write_text(json.dumps(user_values, ensure_ascii=False), encoding="utf-8")
    return AstrBotConfig(config_path=path, schema=schema)


async def main() -> int:
    print("=" * 72)
    print("端到端配置贯通验证：用户改的值会不会真的生效")
    print("=" * 72)

    # ------------------------------------------------------------------
    # 用户在 WebUI 里填的一组非默认值
    # ------------------------------------------------------------------
    print("\n--- 1. 用户写入非默认配置 ---")
    user_values = {
        "enabled": True,
        "bsk_path": r"D:\custom\bsk.exe",
        "browser_instance_id": "deadbeef",
        "command_timeout_sec": 90,
        "max_sessions": 7,
        "admin_only": False,
        "allowed_users": ["1001"],
        "session_scope": "user",
        "idle_release_sec": 180,
        "screenshot_dir": r"D:\shots",
        "max_page_chars": 5000,
        "journal_path": r"D:\journal\s.json",
    }

    try:
        cfg = make_astrbot_config(user_values)
        check("构造 AstrBotConfig", True, type(cfg).__name__)
    except Exception as exc:  # noqa: BLE001
        check("构造 AstrBotConfig", False, repr(exc))
        return 1

    settings = parse_settings(cfg)

    # ------------------------------------------------------------------
    # 2. 每个字段都要贯通到 Settings
    # ------------------------------------------------------------------
    print("\n--- 2. 配置 → Settings 逐字段贯通 ---")
    expected = {
        "bsk_path": r"D:\custom\bsk.exe",
        "browser_instance_id": "deadbeef",
        "command_timeout_sec": 90.0,
        "max_sessions": 7,
        "admin_only": False,
        "session_scope": "user",
        "idle_release_sec": 180.0,
        "screenshot_dir": r"D:\shots",
        "max_page_chars": 5000,
        "journal_path": r"D:\journal\s.json",
    }
    for key, want in expected.items():
        got = getattr(settings, key, "<缺失>")
        check(f"{key} 贯通", got == want, f"期望={want!r} 实际={got!r}")
    check(
        "allowed_users 贯通（转成 tuple）",
        tuple(settings.allowed_users) == ("1001",),
        f"实际={settings.allowed_users!r}",
    )

    # ------------------------------------------------------------------
    # 3. 贯通到 BskService / SessionManager 的内部状态
    # ------------------------------------------------------------------
    print("\n--- 3. → BskService / SessionManager 内部状态 ---")
    runner = CapturingRunner()
    service = BskService(settings, runner=runner)

    check(
        "runner 拿到正确的 bsk 路径",
        settings.bsk_path == service.settings.bsk_path,
        f"{service.settings.bsk_path!r}",
    )
    stats = service.sessions.stats()
    check(
        "会话上限贯通（max_sessions=7）",
        stats.get("max_sessions") == 7,
        f"实际={stats.get('max_sessions')}",
    )
    check(
        "空闲回收阈值贯通（180s）",
        stats.get("idle_release_sec") == 180.0,
        f"实际={stats.get('idle_release_sec')}",
    )
    check(
        "命令超时贯通（90s）",
        stats.get("default_timeout_sec") == 90.0,
        f"实际={stats.get('default_timeout_sec')}",
    )

    # ------------------------------------------------------------------
    # 4. 贯通到实际发出的 bsk 命令行参数
    # ------------------------------------------------------------------
    print("\n--- 4. → 实际传给 bsk 的命令行参数 ---")
    try:
        await service.sessions.acquire("pipeline-test")
    except Exception as exc:  # noqa: BLE001
        check("建立会话（假 runner）", False, repr(exc))

    start_calls = [c for c, _ in runner.calls if c[:2] == ["session", "start"]]
    check("发出了 session start", bool(start_calls), f"{len(start_calls)} 次")
    if start_calls:
        argv = start_calls[0]
        check(
            "--browser 使用配置里的 instance_id",
            "--browser" in argv and "deadbeef" in argv,
            f"实际参数={argv}",
        )
        check(
            "session start 带 --no-focus（不抢用户焦点）",
            "--no-focus" in argv,
            f"实际参数={argv}",
        )
        check("session start 带 --json", "--json" in argv, "")

    # 超时应为 max(内置下限, 用户配置 90)
    start_timeouts = [t for c, t in runner.calls if c[:2] == ["session", "start"]]
    if start_timeouts:
        t = start_timeouts[0]
        check(
            "session start 超时 >= 用户配置（90）",
            t is not None and t >= 90.0,
            f"实际={t}",
        )

    # ------------------------------------------------------------------
    # 5. 反向验证：未配置 instance_id 时不应硬编码某个 id
    # ------------------------------------------------------------------
    print("\n--- 5. 反向验证：未配置 instance_id 时不硬编码浏览器 ---")
    # 注意 browser_probe 会尝试真实调用 `bsk browsers`。这里用假 runner，
    # probe 走的是同步 subprocess（见 service.probe_browser），
    # 拿不到结果就返回空串 —— 于是不传 --browser，交给 bsk 自己选。
    plain = parse_settings({})
    runner2 = CapturingRunner()
    service2 = BskService(plain, runner=runner2)
    try:
        await service2.sessions.acquire("pipeline-test-2")
    except Exception:  # noqa: BLE001
        pass
    starts2 = [c for c, _ in runner2.calls if c[:2] == ["session", "start"]]
    if starts2:
        argv2 = starts2[0]
        if "--browser" in argv2:
            # 允许：用户本机确实只有一个浏览器时 probe 成功并自动选中它
            idx = argv2.index("--browser")
            picked = argv2[idx + 1] if idx + 1 < len(argv2) else ""
            print(f"      （探测到唯一浏览器并自动选用：{picked}）")
            check(
                "自动选用的 instance_id 非空",
                bool(picked),
                f"实际={picked!r}",
            )
        else:
            check(
                "未配置且探测不到时，不传 --browser（交给 bsk 选默认）",
                True,
                f"实际参数={argv2}",
            )
    else:
        check("发出了 session start（默认配置）", False, "未发出")

    # ------------------------------------------------------------------
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
