"""``bsk/config.py`` 的单元测试。

重点不是"正常配置能解析"，而是用户把配置填坏之后插件还能不能活：

- 字段缺失、整条配置是 ``None``、甚至传进来一个列表；
- 数字被填成字符串（``"60"``）或写成小数；
- 布尔开关被填成 ``"true"`` / ``"yes"`` / 乱码；
- 数值越界（``timeout=9999`` 必须被夹到 110，而不是原样透传）；
- ``allowed_users`` 的三种写法（list / 逗号分隔字符串 / 单个字符串）；
- ``session_scope`` 填了不认识的值要回退成 ``umo``。

另外有一条跨文件一致性测试：``_conf_schema.json`` 里每一项的 ``default``
必须与 ``config.py`` 的默认值完全相同。这两个文件由不同的人手改，一旦漂移，
用户在 WebUI 看到的和插件实际用的就对不上，而且完全没有任何报错 —— 只能靠这条测试兜住。

用 ``unittest`` 而不是 ``pytest``：AstrBot 自带的 Python 里没有装 pytest。
运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import fields
from pathlib import Path

# 让测试能 import 到项目的 bsk 包（与 tests/test_runner.py 保持一致）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from bsk import config as cfg  # noqa: E402
from bsk.config import Settings, parse_settings, validate_settings  # noqa: E402

SCHEMA_PATH = _PROJECT_ROOT / "_conf_schema.json"
METADATA_PATH = _PROJECT_ROOT / "metadata.yaml"

# AstrBot 的配置类型白名单，来自 core/config/default.py 的 DEFAULT_VALUE_MAP。
# 不在这个集合里的 type 会让 AstrBotConfig 抛 TypeError，插件直接加载失败。
ALLOWED_SCHEMA_TYPES = frozenset(
    {
        "int",
        "float",
        "bool",
        "string",
        "text",
        "list",
        "file",
        "object",
        "template_list",
        "dict",
    }
)


class TestDefaults(unittest.TestCase):
    """默认值本身必须符合设计约束（与 _conf_schema.json 的一致性另测）。"""

    def test_default_values_match_spec(self) -> None:
        s = parse_settings(None)
        self.assertIs(s.enabled, True)
        self.assertEqual(s.bsk_path, "bsk")
        self.assertEqual(s.browser_instance_id, "")
        self.assertEqual(s.command_timeout_sec, 60.0)
        self.assertEqual(s.max_sessions, 3)
        self.assertIs(s.admin_only, True)
        self.assertEqual(s.allowed_users, ())
        self.assertEqual(s.session_scope, "umo")
        self.assertEqual(s.idle_release_sec, 240.0)
        self.assertEqual(s.screenshot_dir, "")
        self.assertEqual(s.max_page_chars, 3000)

    def test_timeout_below_astrbot_limit(self) -> None:
        """默认超时必须明显小于 AstrBot 的 120 秒工具调用上限。"""
        self.assertLess(cfg.DEFAULT_COMMAND_TIMEOUT_SEC, cfg.ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC)

    def test_idle_release_below_bsk_reclaim(self) -> None:
        """默认空闲释放必须早于 bsk 自己的 300 秒回收线。"""
        self.assertLess(cfg.DEFAULT_IDLE_RELEASE_SEC, cfg.BSK_SESSION_RECLAIM_SEC)
        # 夹取上界也必须严格小于回收线，否则"上限"本身就是个陷阱。
        self.assertLess(cfg.IDLE_RELEASE_MAX_SEC, cfg.BSK_SESSION_RECLAIM_SEC)

    def test_command_timeout_max_below_astrbot_limit(self) -> None:
        self.assertLess(cfg.COMMAND_TIMEOUT_MAX_SEC, cfg.ASTRBOT_TOOL_TIMEOUT_LIMIT_SEC)

    def test_settings_is_frozen_and_slotted(self) -> None:
        """Settings 必须不可变且用 __slots__（运行期只读，防止配置漂移）。"""
        s = parse_settings({})
        with self.assertRaises(Exception):
            s.max_sessions = 99  # type: ignore[misc]
        self.assertFalse(hasattr(s, "__dict__"))


class TestEmptyAndJunkInput(unittest.TestCase):
    """空配置与"根本不是 dict"的输入都要能活下来。"""

    def test_none(self) -> None:
        self.assertEqual(parse_settings(None), parse_settings({}))

    def test_empty_dict(self) -> None:
        self.assertEqual(parse_settings({}), parse_settings(None))

    def test_non_dict_inputs_do_not_raise(self) -> None:
        for junk in ([], "abc", 42, 3.14, True, object(), {1, 2}):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(parse_settings(junk), parse_settings(None))  # type: ignore[arg-type]

    def test_unknown_keys_are_ignored(self) -> None:
        s = parse_settings({"totally_unknown": 1, "another": {"nested": True}})
        self.assertEqual(s, parse_settings(None))

    def test_all_fields_none_fall_back(self) -> None:
        """整条配置每一项都是 None —— 手工编辑过的配置文件很容易变成这样。"""
        raw = dict.fromkeys([f.name for f in fields(Settings)])
        self.assertEqual(parse_settings(raw), parse_settings(None))


class TestFullValidConfig(unittest.TestCase):
    """全字段合法时必须原样保留。"""

    def test_every_field_valid(self) -> None:
        raw = {
            "enabled": False,
            "bsk_path": r"C:\Users\me\.local\bin\bsk.exe",
            "browser_instance_id": "c900a3da",
            "command_timeout_sec": 45.5,
            "max_sessions": 4,
            "admin_only": False,
            "allowed_users": ["123", "456"],
            "session_scope": "user",
            "idle_release_sec": 120.0,
            "screenshot_dir": r"D:\shots",
            "max_page_chars": 8000,
        }
        s = parse_settings(raw)
        self.assertIs(s.enabled, False)
        self.assertEqual(s.bsk_path, raw["bsk_path"])
        self.assertEqual(s.browser_instance_id, "c900a3da")
        self.assertEqual(s.command_timeout_sec, 45.5)
        self.assertEqual(s.max_sessions, 4)
        self.assertIs(s.admin_only, False)
        self.assertEqual(s.allowed_users, ("123", "456"))
        self.assertEqual(s.session_scope, "user")
        self.assertEqual(s.idle_release_sec, 120.0)
        self.assertEqual(s.screenshot_dir, r"D:\shots")
        self.assertEqual(s.max_page_chars, 8000)

    def test_legal_config_reports_no_problems(self) -> None:
        """合法配置的校验结果必须是空列表 —— 否则每次启动都在误报。"""
        raw = {
            "enabled": True,
            "bsk_path": "bsk",
            "browser_instance_id": "",
            "command_timeout_sec": 60.0,
            "max_sessions": 3,
            "admin_only": True,
            "allowed_users": [],
            "session_scope": "umo",
            "idle_release_sec": 240.0,
            "screenshot_dir": "",
            "max_page_chars": 3000,
        }
        self.assertEqual(validate_settings(parse_settings(raw)), [])

    def test_parse_result_has_correct_types(self) -> None:
        """强类型化：字符串数字进去，出来必须是 int/float。"""
        s = parse_settings(
            {
                "command_timeout_sec": "60",
                "max_sessions": "5",
                "idle_release_sec": "240",
                "max_page_chars": "4000",
            }
        )
        self.assertIsInstance(s.command_timeout_sec, float)
        self.assertIsInstance(s.idle_release_sec, float)
        self.assertIsInstance(s.max_sessions, int)
        self.assertIsInstance(s.max_page_chars, int)
        self.assertEqual(s.max_sessions, 5)


class TestNumericCoercion(unittest.TestCase):
    """数值字段：接受 int/float/字符串数字，非法回退默认，越界夹取。"""

    def test_string_numbers_are_accepted(self) -> None:
        s = parse_settings({"command_timeout_sec": " 30.5 ", "max_sessions": " 2 "})
        self.assertEqual(s.command_timeout_sec, 30.5)
        self.assertEqual(s.max_sessions, 2)

    def test_int_accepted_for_float_field(self) -> None:
        self.assertEqual(parse_settings({"command_timeout_sec": 30}).command_timeout_sec, 30.0)

    def test_float_truncated_for_int_field(self) -> None:
        """max_sessions 是"个数"，小数向零截断。"""
        self.assertEqual(parse_settings({"max_sessions": 3.9}).max_sessions, 3)
        self.assertEqual(parse_settings({"max_page_chars": 999.9}).max_page_chars, 999)

    def test_invalid_values_fall_back_to_default(self) -> None:
        junk_values = ["abc", "", "  ", "60s", None, [], {}, object(), "NaN", "inf"]
        for field in ("command_timeout_sec", "max_sessions", "idle_release_sec", "max_page_chars"):
            default = getattr(parse_settings(None), field)
            for junk in junk_values:
                with self.subTest(field=field, junk=repr(junk)):
                    self.assertEqual(getattr(parse_settings({field: junk}), field), default)

    def test_bool_is_not_a_number(self) -> None:
        """True/False 填在数值字段上是误操作，应回退默认而不是当成 1/0。"""
        s = parse_settings(
            {
                "command_timeout_sec": True,
                "max_sessions": True,
                "idle_release_sec": False,
                "max_page_chars": False,
            }
        )
        self.assertEqual(s.command_timeout_sec, cfg.DEFAULT_COMMAND_TIMEOUT_SEC)
        self.assertEqual(s.max_sessions, cfg.DEFAULT_MAX_SESSIONS)
        self.assertEqual(s.idle_release_sec, cfg.DEFAULT_IDLE_RELEASE_SEC)
        self.assertEqual(s.max_page_chars, cfg.DEFAULT_MAX_PAGE_CHARS)

    def test_nan_and_inf_fall_back(self) -> None:
        """nan/inf 参与 min/max 夹取结果不可靠，必须回退。"""
        nan = float("nan")
        inf = float("inf")
        self.assertEqual(
            parse_settings({"command_timeout_sec": nan}).command_timeout_sec,
            cfg.DEFAULT_COMMAND_TIMEOUT_SEC,
        )
        self.assertEqual(
            parse_settings({"command_timeout_sec": inf}).command_timeout_sec,
            cfg.DEFAULT_COMMAND_TIMEOUT_SEC,
        )


class TestClampingBounds(unittest.TestCase):
    """越界一律夹到边界 —— 绝不能把 9999 原样透传给 bsk。"""

    def test_command_timeout_clamped(self) -> None:
        # 这是任务点名要求的用例：9999 必须变成 110。
        self.assertEqual(parse_settings({"command_timeout_sec": 9999}).command_timeout_sec, 110.0)
        self.assertEqual(
            parse_settings({"command_timeout_sec": 9999}).command_timeout_sec,
            cfg.COMMAND_TIMEOUT_MAX_SEC,
        )
        self.assertEqual(parse_settings({"command_timeout_sec": 0}).command_timeout_sec, 5.0)
        self.assertEqual(parse_settings({"command_timeout_sec": -100}).command_timeout_sec, 5.0)
        self.assertEqual(parse_settings({"command_timeout_sec": 120}).command_timeout_sec, 110.0)
        # 边界内保持原值。
        self.assertEqual(parse_settings({"command_timeout_sec": 5}).command_timeout_sec, 5.0)
        self.assertEqual(parse_settings({"command_timeout_sec": 110}).command_timeout_sec, 110.0)

    def test_idle_release_clamped(self) -> None:
        self.assertEqual(parse_settings({"idle_release_sec": 99999}).idle_release_sec, 299.0)
        self.assertEqual(parse_settings({"idle_release_sec": 0}).idle_release_sec, 60.0)
        self.assertEqual(parse_settings({"idle_release_sec": -5}).idle_release_sec, 60.0)
        # 夹取上界必须严格小于 bsk 的 300 秒回收线。
        self.assertLess(parse_settings({"idle_release_sec": 1e9}).idle_release_sec, 300.0)
        self.assertEqual(parse_settings({"idle_release_sec": 60}).idle_release_sec, 60.0)
        self.assertEqual(parse_settings({"idle_release_sec": 299}).idle_release_sec, 299.0)

    def test_max_sessions_clamped(self) -> None:
        self.assertEqual(parse_settings({"max_sessions": 999}).max_sessions, 10)
        self.assertEqual(parse_settings({"max_sessions": 0}).max_sessions, 1)
        self.assertEqual(parse_settings({"max_sessions": -3}).max_sessions, 1)
        self.assertEqual(parse_settings({"max_sessions": 10}).max_sessions, 10)
        self.assertEqual(parse_settings({"max_sessions": 1}).max_sessions, 1)

    def test_max_page_chars_clamped(self) -> None:
        self.assertEqual(parse_settings({"max_page_chars": 999999}).max_page_chars, 20000)
        self.assertEqual(parse_settings({"max_page_chars": 0}).max_page_chars, 200)
        self.assertEqual(parse_settings({"max_page_chars": -1}).max_page_chars, 200)
        self.assertEqual(parse_settings({"max_page_chars": 200}).max_page_chars, 200)
        self.assertEqual(parse_settings({"max_page_chars": 20000}).max_page_chars, 20000)


class TestSessionScope(unittest.TestCase):
    """只接受 umo / user，其他回退 umo。"""

    def test_valid_values(self) -> None:
        self.assertEqual(parse_settings({"session_scope": "umo"}).session_scope, "umo")
        self.assertEqual(parse_settings({"session_scope": "user"}).session_scope, "user")

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(parse_settings({"session_scope": " USER "}).session_scope, "user")
        self.assertEqual(parse_settings({"session_scope": "Umo"}).session_scope, "umo")

    def test_invalid_falls_back_to_umo(self) -> None:
        for junk in ["group", "session", "", "  ", "UMO2", "user2", None, 123, [], {}, True]:
            with self.subTest(junk=repr(junk)):
                self.assertEqual(parse_settings({"session_scope": junk}).session_scope, "umo")


class TestAllowedUsers(unittest.TestCase):
    """白名单的三种输入形态必须统一成 tuple[str, ...]。"""

    def test_list_form(self) -> None:
        s = parse_settings({"allowed_users": ["123", "456"]})
        self.assertEqual(s.allowed_users, ("123", "456"))
        self.assertIsInstance(s.allowed_users, tuple)

    def test_comma_separated_string_form(self) -> None:
        s = parse_settings({"allowed_users": "123, 456 ,789"})
        self.assertEqual(s.allowed_users, ("123", "456", "789"))

    def test_single_string_form(self) -> None:
        self.assertEqual(parse_settings({"allowed_users": "123"}).allowed_users, ("123",))

    def test_blank_items_are_dropped(self) -> None:
        self.assertEqual(
            parse_settings({"allowed_users": [" 123 ", "", "   ", "456"]}).allowed_users,
            ("123", "456"),
        )
        self.assertEqual(parse_settings({"allowed_users": "123,,456,"}).allowed_users, ("123", "456"))

    def test_duplicates_are_removed_order_preserved(self) -> None:
        s = parse_settings({"allowed_users": ["456", "123", "456"]})
        self.assertEqual(s.allowed_users, ("456", "123"))

    def test_numeric_ids_become_strings(self) -> None:
        """WebUI 的 list 控件或手写 YAML 可能给出数字 ID。"""
        s = parse_settings({"allowed_users": [123, 456.0]})
        self.assertEqual(s.allowed_users, ("123", "456"))

    def test_invalid_shapes_yield_empty_tuple(self) -> None:
        for junk in [None, {}, 123, True, ""]:
            with self.subTest(junk=repr(junk)):
                self.assertEqual(parse_settings({"allowed_users": junk}).allowed_users, ())

    def test_unusable_items_are_skipped_individually(self) -> None:
        """填错其中一项时丢掉那一项，而不是把整份白名单作废。"""
        s = parse_settings({"allowed_users": ["123", {"oops": 1}, True, ["nested"], "456"]})
        self.assertEqual(s.allowed_users, ("123", "456"))


class TestStringFields(unittest.TestCase):
    """字符串字段：strip 空白，空值回退默认。"""

    def test_bsk_path_stripped(self) -> None:
        self.assertEqual(parse_settings({"bsk_path": "  bsk  "}).bsk_path, "bsk")
        self.assertEqual(
            parse_settings({"bsk_path": r"  C:\Tools\bsk.exe  "}).bsk_path,
            r"C:\Tools\bsk.exe",
        )

    def test_bsk_path_empty_falls_back(self) -> None:
        for junk in ["", "   ", None, 123, [], True]:
            with self.subTest(junk=repr(junk)):
                self.assertEqual(parse_settings({"bsk_path": junk}).bsk_path, "bsk")

    def test_browser_instance_id_empty_means_auto(self) -> None:
        self.assertEqual(parse_settings({"browser_instance_id": ""}).browser_instance_id, "")
        self.assertEqual(parse_settings({"browser_instance_id": "  "}).browser_instance_id, "")
        self.assertEqual(
            parse_settings({"browser_instance_id": " c900a3da "}).browser_instance_id,
            "c900a3da",
        )

    def test_screenshot_dir_empty_means_default(self) -> None:
        """空值在这个字段上是有意义的取值（用插件数据目录），不能被当成错误。"""
        self.assertEqual(parse_settings({"screenshot_dir": ""}).screenshot_dir, "")
        self.assertEqual(parse_settings({"screenshot_dir": "  "}).screenshot_dir, "")
        self.assertEqual(
            parse_settings({"screenshot_dir": r"  D:\shots  "}).screenshot_dir,
            r"D:\shots",
        )
        self.assertEqual(parse_settings({"screenshot_dir": 123}).screenshot_dir, "")


class TestBooleanFields(unittest.TestCase):
    """布尔字段：认字符串写法，认不出来回退默认。"""

    def test_real_bools(self) -> None:
        self.assertIs(parse_settings({"enabled": False}).enabled, False)
        self.assertIs(parse_settings({"admin_only": False}).admin_only, False)

    def test_string_forms(self) -> None:
        for token in ["true", "True", " TRUE ", "1", "yes", "on", "是"]:
            with self.subTest(token=token):
                self.assertIs(parse_settings({"enabled": token}).enabled, True)
        for token in ["false", "False", "0", "no", "off", "否"]:
            with self.subTest(token=token):
                self.assertIs(parse_settings({"enabled": token}).enabled, False)

    def test_unrecognized_falls_back_to_default(self) -> None:
        # enabled/admin_only 的默认都是 True。
        for junk in ["maybe", "", "  ", None, [], {}]:
            with self.subTest(junk=repr(junk)):
                self.assertIs(parse_settings({"enabled": junk}).enabled, True)
                self.assertIs(parse_settings({"admin_only": junk}).admin_only, True)

    def test_numbers_are_coerced(self) -> None:
        self.assertIs(parse_settings({"enabled": 0}).enabled, False)
        self.assertIs(parse_settings({"enabled": 1}).enabled, True)


class TestValidateSettings(unittest.TestCase):
    """validate_settings 报告"合法但值得提醒"的问题。"""

    def test_clean_config_has_no_problems(self) -> None:
        self.assertEqual(validate_settings(parse_settings(None)), [])

    def test_timeout_over_astrbot_limit_warns(self) -> None:
        """夹取后拿不到 ≥120，所以直接构造一个越界的 Settings 来验证报警逻辑。"""
        s = parse_settings({"command_timeout_sec": 60})
        object.__setattr__(s, "command_timeout_sec", 120.0)
        problems = validate_settings(s)
        self.assertTrue(any("command_timeout_sec" in p for p in problems))
        self.assertTrue(any("120" in p for p in problems))

    def test_idle_release_over_bsk_reclaim_warns(self) -> None:
        s = parse_settings(None)
        object.__setattr__(s, "idle_release_sec", 300.0)
        problems = validate_settings(s)
        self.assertTrue(any("idle_release_sec" in p for p in problems))
        self.assertTrue(any("回收" in p or "300" in p for p in problems))

    def test_admin_only_false_raises_security_warning(self) -> None:
        problems = validate_settings(parse_settings({"admin_only": False}))
        self.assertTrue(problems)
        joined = "\n".join(problems)
        self.assertIn("admin_only", joined)
        self.assertIn("安全", joined)
        # 必须说清后果：任何能跟机器人对话的人都能操控浏览器。
        self.assertTrue("任何人" in joined or "任何能给机器人发消息的人" in joined)

    def test_many_sessions_warns(self) -> None:
        problems = validate_settings(parse_settings({"max_sessions": 8}))
        self.assertTrue(any("max_sessions" in p for p in problems))
        # 夹取范围内的 5 个不该报警。
        self.assertEqual(validate_settings(parse_settings({"max_sessions": 5})), [])

    def test_allowed_users_with_admin_only_warns(self) -> None:
        """白名单非空但 admin_only 开启：如实说明实际生效规则是白名单优先。

        真实的权限判定在 ``main.py`` 的 ``_denied()``：白名单命中即放行，
        不再看是否为管理员。因此这两项同时配置时，白名单里的非管理员
        确实能用 —— 这是一个值得提醒用户的安全影响，而不是"白名单不生效"。
        """
        problems = validate_settings(
            parse_settings({"allowed_users": ["123", "456"], "admin_only": True})
        )
        self.assertTrue(any("allowed_users" in p for p in problems))
        joined = "\n".join(problems)
        # 提醒必须说清楚"白名单优先"这个真实行为。
        self.assertIn("白名单优先", joined)
        self.assertIn("admin_only", joined)
        # 并且不能出现与实现相反的"不生效"说法。
        self.assertNotIn("不会生效", joined)

    def test_allowed_users_with_admin_only_off_is_clean(self) -> None:
        """关掉 admin_only 后，白名单不再有"绕过"含义，不该再报这条。"""
        problems = validate_settings(
            parse_settings({"allowed_users": ["123"], "admin_only": False})
        )
        self.assertFalse(any("白名单优先" in p for p in problems))
        # 但仍然会有那条安全提醒（admin_only 关闭本身就该警告）。
        self.assertTrue(any("安全" in p for p in problems))

    def test_multiple_problems_are_all_reported(self) -> None:
        problems = validate_settings(
            parse_settings({"admin_only": False, "max_sessions": 9, "allowed_users": ["1"]})
        )
        self.assertGreaterEqual(len(problems), 2)

    def test_does_not_raise_on_handmade_bad_settings(self) -> None:
        """启动路径上的函数：字段被塞了错误类型也不能抛异常。"""
        s = parse_settings(None)
        for field, junk in [
            ("command_timeout_sec", "abc"),
            ("idle_release_sec", None),
            ("max_sessions", {}),
            ("admin_only", "maybe"),
            ("allowed_users", 12345),
        ]:
            with self.subTest(field=field):
                object.__setattr__(s, field, junk)
                self.assertIsInstance(validate_settings(s), list)


class TestConfSchemaFile(unittest.TestCase):
    """``_conf_schema.json`` 必须能被 AstrBot 正确加载。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_all_settings_fields_are_present(self) -> None:
        expected = {f.name for f in fields(Settings)}
        self.assertEqual(set(self.schema), expected)

    def test_all_types_are_in_astrbot_whitelist(self) -> None:
        """非法 type 会让 AstrBotConfig 抛 TypeError，插件加载失败。"""
        for key, meta in self.schema.items():
            with self.subTest(key=key):
                self.assertIn("type", meta)
                self.assertIn(meta["type"], ALLOWED_SCHEMA_TYPES)

    def test_every_item_has_description(self) -> None:
        for key, meta in self.schema.items():
            with self.subTest(key=key):
                self.assertIsInstance(meta.get("description"), str)
                self.assertTrue(meta["description"].strip())

    def test_defaults_match_config_py(self) -> None:
        """跨文件一致性：schema 的 default 必须与 config.py 的默认值完全相同。"""
        s = parse_settings(None)
        for key, meta in self.schema.items():
            with self.subTest(key=key):
                self.assertIn("default", meta)
                actual = getattr(s, key)
                expected = meta["default"]
                if isinstance(actual, tuple):
                    # JSON 没有 tuple，白名单在 schema 里是 list。
                    self.assertEqual(list(actual), expected)
                else:
                    self.assertEqual(actual, expected)
                    # 类型也要一致：3 != 3.0 在 JSON 里看起来一样，但类型不同。
                    self.assertIsInstance(actual, type(expected))

    def test_slider_ranges_match_clamp_bounds(self) -> None:
        """schema 的 slider 范围必须和 config.py 的夹取区间一致。"""
        expected_bounds = {
            "command_timeout_sec": (cfg.COMMAND_TIMEOUT_MIN_SEC, cfg.COMMAND_TIMEOUT_MAX_SEC),
            "idle_release_sec": (cfg.IDLE_RELEASE_MIN_SEC, cfg.IDLE_RELEASE_MAX_SEC),
            "max_sessions": (cfg.MAX_SESSIONS_MIN, cfg.MAX_SESSIONS_MAX),
            "max_page_chars": (cfg.MAX_PAGE_CHARS_MIN, cfg.MAX_PAGE_CHARS_MAX),
        }
        for key, (low, high) in expected_bounds.items():
            with self.subTest(key=key):
                slider = self.schema[key]["slider"]
                self.assertEqual(slider["min"], low)
                self.assertEqual(slider["max"], high)

    def test_session_scope_options_match_config_py(self) -> None:
        self.assertEqual(self.schema["session_scope"]["options"], list(cfg.SESSION_SCOPES))

    def test_list_field_declares_item_type(self) -> None:
        self.assertEqual(self.schema["allowed_users"]["items"], {"type": "string"})


class TestMetadataFile(unittest.TestCase):
    """``metadata.yaml`` 必须满足 AstrBot 的强制校验。"""

    REQUIRED_FIELDS = ("name", "desc", "version", "author")

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import yaml
        except ImportError:  # pragma: no cover - AstrBot 环境一定有 pyyaml
            raise unittest.SkipTest("未安装 pyyaml，跳过 metadata 校验")
        cls.meta = yaml.safe_load(METADATA_PATH.read_text(encoding="utf-8"))

    def test_required_fields_are_non_empty_strings(self) -> None:
        """updater.py 的 PLUGIN_METADATA_REQUIRED_FIELDS 会强制校验这四项。"""
        for field in self.REQUIRED_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, self.meta)
                self.assertIsInstance(self.meta[field], str)
                self.assertTrue(self.meta[field].strip())

    def test_name_is_importable_and_matches_directory(self) -> None:
        """star_manager 要求 name 是合法 Python 标识符且等于插件目录名。

        AstrBot 用 ``__import__("data.plugins.<目录名>.main")`` 加载插件，
        所以 ``metadata.name`` 必须与插件所在目录名一致，否则插件根本
        不会被发现（``star_manager._get_modules`` 按目录名构造 import 路径）。

        Note:
            当仓库被改名导出时（例如从 git tag 导出成
            ``bsk-release-verify-194540`` 来验证发布产物），目录名与 name
            必然不同 —— 那不是缺陷，只是"还没被放进正确的目录名里"。
            这种情形下跳过该断言，避免在验证发布产物时产生误报。
            真实的安装检查由 ``verify_discovery.py`` 负责：它把插件放进
            AstrBot 真正的插件目录，用 AstrBot 自己的发现函数验证。
        """
        name = self.meta["name"]
        # 这两条与目录名无关，任何情况下都必须成立。
        self.assertTrue(name.isidentifier(), f"name 必须是合法标识符：{name!r}")
        import keyword

        self.assertFalse(keyword.iskeyword(name), f"name 不能是 Python 关键字：{name!r}")
        self.assertEqual(name, "astrbot_plugin_bsk_browser")

        if _PROJECT_ROOT.name != name:
            self.skipTest(
                f"目录名 {_PROJECT_ROOT.name!r} 与 name {name!r} 不同，"
                "说明这是改名导出的副本（如发布产物验证），非安装状态；"
                "真实安装的目录一致性由 verify_discovery.py 验证"
            )

    def test_astrbot_version_is_pep440_specifier(self) -> None:
        try:
            from packaging.specifiers import SpecifierSet
        except ImportError:  # pragma: no cover - AstrBot 环境一定有 packaging
            self.skipTest("未安装 packaging，跳过版本范围校验")
        spec = SpecifierSet(self.meta["astrbot_version"])
        # AstrBot 用 specifier.contains(prereleases=True) 判断。
        self.assertTrue(spec.contains("4.16.0"))
        self.assertTrue(spec.contains("4.28.1"))

    def test_desc_contains_unofficial_disclaimer(self) -> None:
        """desc 必须含非官方声明 —— 商标与合规要求。"""
        desc = self.meta["desc"]
        self.assertIn("非官方", desc)
        self.assertIn("腾讯", desc)

    def test_no_brand_name_as_product_name(self) -> None:
        """"BrowserSkill" 不得出现在 name / display_name 里（商标纪律）。"""
        for field in ("name", "display_name"):
            with self.subTest(field=field):
                self.assertNotIn("BrowserSkill", self.meta[field])
                self.assertNotIn("browserskill", self.meta[field].lower())

    def test_display_name_and_short_desc_exist(self) -> None:
        self.assertTrue(self.meta["display_name"].strip())
        self.assertTrue(self.meta["short_desc"].strip())

    def test_version_is_semver_like(self) -> None:
        self.assertEqual(self.meta["version"], "0.1.0")


if __name__ == "__main__":
    unittest.main()
