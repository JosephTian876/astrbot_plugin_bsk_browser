"""回归测试：配置来源的两种形态都必须被正确解析。

为什么需要这个测试：源码里 AstrBot 传给插件 ``__init__`` 的是
``AstrBotConfig`` 对象（``star_manager.py:1164``），而不是普通 dict。
目前它恰好继承自 ``dict``，所以 ``parse_settings`` 能直接处理 —— 但这是一个
隐式依赖：一旦 AstrBot 改成非 dict 的配置对象，本插件的所有用户配置会
静默退回默认值，而且不会有任何报错（用户只会觉得"改了没用"）。

这个测试把这个依赖钉死，将来 AstrBot 升级导致行为变化时会立刻失败。

同时它也验证"两种形态结果一致"，避免只测一种而漏掉另一种。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

ASTRBOT_APP = os.environ.get("ASTRBOT_APP_PATH", r"D:\AstrBot\backend\app")
if os.path.isdir(ASTRBOT_APP):
    sys.path.insert(0, ASTRBOT_APP)

# 必须钉住 AstrBot 的 root，且要在任何 astrbot import 之前生效。
#
# AstrBot 解析数据路径时优先读 ASTRBOT_ROOT，否则普通模式用当前工作目录
# （core/utils/astrbot_path.py:29-35）。本测试要 import AstrBotConfig，
# 不设置就会在项目目录里生成 data/cmd_config.json —— AstrBot 的主配置，
# 含 provider API 密钥与管理员 QQ 号，而本仓库是要公开发布的。
#
# 注意：这个设置必须在 unittest 收集阶段就生效，所以放在模块顶层
# （不能挪进 setUpClass）。
os.environ.setdefault(
    "ASTRBOT_ROOT", os.path.join(os.path.expanduser("~"), ".astrbot")
)

from bsk.config import parse_settings  # noqa: E402

SCHEMA_PATH = PROJECT / "_conf_schema.json"


class TestConfigSourceShapes(unittest.TestCase):
    """配置可能以 dict 或 AstrBotConfig 对象传入，两者必须等价。"""

    def setUp(self) -> None:
        self.user_values = {
            "enabled": True,
            "bsk_path": r"C:\custom\bsk.exe",
            "browser_instance_id": "abcd1234",
            "command_timeout_sec": 99,
            "max_sessions": 6,
            "admin_only": False,
            "allowed_users": ["1001", "1002"],
            "session_scope": "user",
            "idle_release_sec": 120,
            "screenshot_dir": r"C:\shots",
            "max_page_chars": 5000,
        }

    def test_plain_dict(self) -> None:
        """普通 dict（单元测试与旧版 AstrBot 的形态）。"""
        s = parse_settings(self.user_values)
        self.assertEqual(s.bsk_path, r"C:\custom\bsk.exe")
        self.assertEqual(s.command_timeout_sec, 99.0)
        self.assertEqual(s.max_sessions, 6)
        self.assertFalse(s.admin_only)
        self.assertEqual(s.allowed_users, ("1001", "1002"))
        self.assertEqual(s.session_scope, "user")
        self.assertEqual(s.idle_release_sec, 120.0)
        self.assertEqual(s.max_page_chars, 5000)

    def test_astrbot_config_object(self) -> None:
        """AstrBotConfig 对象 —— 生产环境的真实形态。

        若 AstrBot 未来改成非 dict 的配置对象，这个测试会失败，
        提醒我们需要在 parse_settings 里加适配层。
        """
        try:
            from astrbot.core.config.astrbot_config import AstrBotConfig
        except ImportError:  # pragma: no cover - 非 AstrBot 环境下跳过
            self.skipTest("当前环境没有 AstrBot，跳过生产形态测试")

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            Path(cfg_path).write_text(
                json.dumps(self.user_values, ensure_ascii=False), encoding="utf-8"
            )
            cfg = AstrBotConfig(config_path=cfg_path, schema=schema)

            # 显式确认它仍是 dict 的子类（parse_settings 依赖这一点）
            self.assertIsInstance(
                cfg,
                dict,
                "AstrBotConfig 不再是 dict 的子类！parse_settings 需要加适配层，"
                "否则用户配置会静默失效。",
            )

            s = parse_settings(cfg)

        self.assertEqual(s.bsk_path, r"C:\custom\bsk.exe")
        self.assertEqual(s.command_timeout_sec, 99.0)
        self.assertEqual(s.max_sessions, 6)
        self.assertFalse(s.admin_only)
        self.assertEqual(s.session_scope, "user")
        self.assertEqual(s.browser_instance_id, "abcd1234")

    def test_both_shapes_are_equivalent(self) -> None:
        """两种形态解析出的配置必须逐字段相同。"""
        try:
            from astrbot.core.config.astrbot_config import AstrBotConfig
        except ImportError:  # pragma: no cover
            self.skipTest("当前环境没有 AstrBot")

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        from_dict = parse_settings(self.user_values)

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            Path(cfg_path).write_text(
                json.dumps(self.user_values, ensure_ascii=False), encoding="utf-8"
            )
            from_obj = parse_settings(
                AstrBotConfig(config_path=cfg_path, schema=schema)
            )

        self.assertEqual(
            from_dict,
            from_obj,
            "两种配置形态解析结果不一致 —— 说明其中一条路径有字段被漏读",
        )

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        """AstrBotConfig 会补齐 schema 里缺失的键，缺失项应当是默认值。"""
        try:
            from astrbot.core.config.astrbot_config import AstrBotConfig
        except ImportError:  # pragma: no cover
            self.skipTest("当前环境没有 AstrBot")

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "cfg.json")
            # 故意只写一个字段，其余让 AstrBot 补默认
            Path(cfg_path).write_text(
                json.dumps({"max_sessions": 9}), encoding="utf-8"
            )
            s = parse_settings(AstrBotConfig(config_path=cfg_path, schema=schema))
            defaults = parse_settings({})

        self.assertEqual(s.max_sessions, 9)
        # 未指定的字段应当等于默认值
        self.assertEqual(s.command_timeout_sec, defaults.command_timeout_sec)
        self.assertEqual(s.admin_only, defaults.admin_only)
        self.assertEqual(s.session_scope, defaults.session_scope)


if __name__ == "__main__":
    unittest.main(verbosity=2)
