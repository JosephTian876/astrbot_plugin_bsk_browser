"""生产配置形态验证：AstrBot 实际传给插件的 config 是什么类型？

这是**只有真实运行路径才会暴露**的问题：源码里 AstrBot 传的是
``AstrBotConfig`` 实例（见 star_manager.py:1164），而不是普通 dict。
如果 ``parse_settings`` 只接受 dict，用户在 WebUI 里改的任何配置
都会**静默失效**、全部退回默认值 —— 而且不会有任何报错。

本脚本用真实的 AstrBotConfig 类型构造配置，验证解析结果正确。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

# ★ 钉住 AstrBot 的 root，避免它在项目目录里生成 data/。
# AstrBot 解析数据路径时优先读 ASTRBOT_ROOT，否则普通模式下用当前工作目录
# （core/utils/astrbot_path.py:29-35）。不设置就会在项目里凭空生成
# data/cmd_config.json（含 AstrBot 主配置），曾经因此误提交过整个 data/ 目录。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

import json  # noqa: E402
import tempfile  # noqa: E402

from bsk.config import parse_settings  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("=" * 70)
    print("生产配置形态验证：AstrBotConfig 对象 vs 普通 dict")
    print("=" * 70)

    # --- 1. 普通 dict（测试里一直用的形态）---
    plain = {"command_timeout_sec": 42, "admin_only": False, "max_sessions": 7}
    s_plain = parse_settings(plain)
    check(
        "普通 dict 生效",
        s_plain.command_timeout_sec == 42 and s_plain.admin_only is False,
        f"timeout={s_plain.command_timeout_sec}, admin_only={s_plain.admin_only}",
    )

    # --- 2. 真实 AstrBotConfig 对象 ---
    try:
        from astrbot.core.config.astrbot_config import AstrBotConfig
    except Exception as exc:  # noqa: BLE001
        check("import AstrBotConfig", False, repr(exc))
        print("\n无法 import AstrBotConfig，跳过生产形态验证。")
        return 1

    schema = json.loads((PROJECT / "_conf_schema.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "astrbot_plugin_bsk_browser_config.json")
        # 先写入用户自定义值，模拟用户在 WebUI 里改过配置
        user_values = {
            "command_timeout_sec": 99,
            "admin_only": False,
            "max_sessions": 6,
            "bsk_path": r"C:\custom\path\bsk.exe",
            "session_scope": "user",
        }
        Path(cfg_path).write_text(
            json.dumps(user_values, ensure_ascii=False), encoding="utf-8"
        )

        try:
            cfg = AstrBotConfig(config_path=cfg_path, schema=schema)
            check("构造 AstrBotConfig", True, f"类型={type(cfg).__name__}")
        except Exception as exc:  # noqa: BLE001
            check("构造 AstrBotConfig", False, repr(exc))
            return 1

        # 它是不是 dict 的子类？
        is_dict_subclass = isinstance(cfg, dict)
        check(
            "AstrBotConfig 是 dict 的子类",
            is_dict_subclass,
            f"isinstance(dict)={is_dict_subclass}, MRO={[c.__name__ for c in type(cfg).__mro__[:4]]}",
        )

        # 关键：把对象直接交给 parse_settings，看用户配置是否被读到
        try:
            s_obj = parse_settings(cfg)
            check(
                "★ AstrBotConfig 对象被正确解析（用户配置不丢失）",
                s_obj.command_timeout_sec == 99.0
                and s_obj.admin_only is False
                and s_obj.max_sessions == 6
                and s_obj.session_scope == "user",
                f"timeout={s_obj.command_timeout_sec}, admin_only={s_obj.admin_only}, "
                f"max_sessions={s_obj.max_sessions}, scope={s_obj.session_scope}, "
                f"bsk_path={s_obj.bsk_path!r}",
            )
        except Exception as exc:  # noqa: BLE001
            check("★ AstrBotConfig 对象被正确解析（用户配置不丢失）", False, repr(exc))

        # 对照：如果解析失败，值会等于默认值 —— 这正是"静默失效"的样子
        s_default = parse_settings({})
        if s_obj.command_timeout_sec == s_default.command_timeout_sec:
            print()
            print("  ⚠️  解析结果与默认值相同 —— 用户配置很可能被忽略了！")

    print()
    print("=" * 70)
    if FAILURES:
        print(f"失败：{len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
