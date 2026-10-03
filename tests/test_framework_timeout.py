"""``bsk.config.read_framework_tool_timeout`` 的单元测试。

## 为什么需要这个函数

本插件给全页截图准备的最终超时是 180 秒，而 **AstrBot 自己的单次工具调用上限**
（``tool_call_timeout``，默认 120 秒，见 ``core/agent/run_context.py:19``）比它小。
后果是插件允许自己等 180 秒，框架却在 120 秒时把它掐断 —— 用户看到的是框架抛的英文
``tool <name> execution timeout after 120 seconds.``，而**不是插件精心写的中文提示**。

要解决它就得知道框架的上限是多少。``Context.get_config()`` 能拿到 AstrBot 主配置
（``core/star/context.py:597``），路径已用真实配置文件确认：

    agent_runner → config → misc → tool_call_timeout   （本机实测 = 120）

## 这个文件在测什么

**只有一件事：任何输入都不能让插件加载失败。**

它跑在插件加载路径上（``main.py.__init__`` → ``BskService``），一抛异常整个插件就
起不来。而它读的是**外部配置**：用户手改过、版本不同、结构被挪动、值被写成字符串……
全部可能。所以约定与 ``parse_settings`` 完全一致：

    读不到就返回 None（= "未知"），**绝不抛异常**；未知时调用方保持原有行为，
    不因为"读不到"就缩短用户愿意等待的时间。

覆盖的降级情形（用户明确要求的四类 + 补强）：

- ``None`` / 空 dict / 完全不是配置的东西（字符串、数字、对象）；
- 路径上任何一层缺失，或某一层取值 ``None``；
- 叶子值是字符串（``"120"`` 可用，``"abc"`` 不可用）；
- 值是 ``0`` / 负数 / ``nan`` / ``inf`` / 布尔；
- 畸形类型（``.get()`` 自己抛异常的映射、属性访问抛异常的对象）。

另外还钉住一条**分层约束**：``bsk/`` 包不得 import astrbot。这里用一个"会爆炸的
astrbot 哨兵模块"来证明——如果被测代码偷偷 import 它，测试就会失败。

pytest 在本机不可用（实测 ``ModuleNotFoundError``），所以用标准库 ``unittest``。

运行：``python -m unittest tests.test_framework_timeout -v``
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any

# 让测试能 import 到项目的 bsk 包（与 tests/test_service.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.config import (  # noqa: E402
    FRAMEWORK_TOOL_TIMEOUT_PATH,
    as_timeout_seconds,
    read_framework_tool_timeout,
)

# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


def real_shaped_config(limit: Any = 120) -> dict[str, Any]:
    """造一份与 AstrBot 真实结构一致的配置（路径与层级相同）。"""
    return {
        "agent_runner": {
            "config": {
                "misc": {
                    "max_steps": 30,
                    "tool_schema_mode": "full",
                    "tool_call_timeout": limit,
                    "sanitize_context_by_modalities": False,
                }
            }
        }
    }


class AttrConfig(dict):
    """模仿 ``AstrBotConfig``：既是 dict，又支持 ``.键名`` 属性访问。

    真实的 ``AstrBotConfig``（``core/config/astrbot_config.py:33``）就是
    ``dict`` 的子类 + ``__getattr__`` 转发到 ``self[item]``，两种访问方式都要能用。
    """

    def __getattr__(self, item: str) -> Any:
        return self.get(item)


class HostileMapping:
    """``.get()`` 自己抛异常的映射 —— 用来验证"任何异常都降级"。"""

    def get(self, key: str) -> Any:  # noqa: D401
        raise RuntimeError("这个配置对象坏掉了")

    def __getattr__(self, item: str) -> Any:
        raise RuntimeError("连属性访问都坏掉了")


class HostileLeaf:
    """最后一层的值本身会抛异常（例如某个自定义对象）。"""

    def __float__(self) -> float:
        raise ValueError("不能转成浮点数")


# ----------------------------------------------------------------------
# 1. 正常路径
# ----------------------------------------------------------------------


class TestReaderHappyPath(unittest.TestCase):
    """能读到时必须给出正确的浮点秒数。"""

    def test_real_shaped_dict(self) -> None:
        """★ 与真实配置文件同构的 dict —— 这条是"路径没写错"的基准。"""
        self.assertEqual(read_framework_tool_timeout(real_shaped_config()), 120.0)

    def test_attr_style_object(self) -> None:
        """★ ``AstrBotConfig`` 那种"dict + 属性访问"的对象也要能读。"""
        self.assertEqual(
            read_framework_tool_timeout(AttrConfig(real_shaped_config())), 120.0
        )

    def test_custom_value(self) -> None:
        """用户改过这一项时读到的是用户值（不是写死的 120）。"""
        self.assertEqual(
            read_framework_tool_timeout(real_shaped_config(90)), 90.0
        )

    def test_numeric_string_is_accepted(self) -> None:
        """JSON 手改成 ``"120"`` 时仍然可用（引号很容易多打一个）。"""
        for raw in ("120", " 120 ", "120.5", 120.5):
            with self.subTest(raw=repr(raw)):
                result = read_framework_tool_timeout(real_shaped_config(raw))
                self.assertIsInstance(result, float)
                self.assertGreater(result, 0)

    def test_returns_float_type(self) -> None:
        """返回值必须是 float —— 它会被直接拿去做减法与比较。"""
        self.assertIsInstance(read_framework_tool_timeout(real_shaped_config()), float)

    def test_documented_path_matches_reality(self) -> None:
        """路径常量本身也要钉住：改了它就得同步改文档与这里。"""
        self.assertEqual(
            FRAMEWORK_TOOL_TIMEOUT_PATH,
            ("agent_runner", "config", "misc", "tool_call_timeout"),
        )


# ----------------------------------------------------------------------
# 2. 降级：一切都返回 None，且绝不抛异常
# ----------------------------------------------------------------------


class TestReaderDegradesToNone(unittest.TestCase):
    """★ 用户要求的核心契约：覆盖各类畸形输入，全部返回 None。"""

    def test_none(self) -> None:
        self.assertIsNone(read_framework_tool_timeout(None))

    def test_empty_dict(self) -> None:
        self.assertIsNone(read_framework_tool_timeout({}))

    def test_not_a_config_at_all(self) -> None:
        """字符串、数字、列表、对象 —— 都不是配置，都当"未知"。"""
        for junk in ("config", 120, 12.5, [], [1, 2], object(), True, b"{}"):
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(read_framework_tool_timeout(junk))

    def test_missing_top_level_key(self) -> None:
        """旧版 AstrBot 没有 ``agent_runner`` 这一层。"""
        self.assertIsNone(read_framework_tool_timeout({"other": {"config": {}}}))

    def test_missing_each_level(self) -> None:
        """逐层删除，每一层缺失都要安全返回 None。"""
        full = real_shaped_config()
        levels = [
            {},
            {"agent_runner": {}},
            {"agent_runner": {"config": {}}},
            {"agent_runner": {"config": {"misc": {}}}},
        ]
        for partial in levels:
            with self.subTest(partial=partial):
                self.assertIsNone(read_framework_tool_timeout(partial))
        # 完整结构作为对照，确认这份数据本身是好的。
        self.assertEqual(read_framework_tool_timeout(full), 120.0)

    def test_level_value_is_none(self) -> None:
        """某一层的取值是 ``None``（配置文件里被手改成 null）。"""
        for key in ("agent_runner", "config", "misc", "tool_call_timeout"):
            cfg = real_shaped_config()
            if key == "agent_runner":
                cfg["agent_runner"] = None
            elif key == "config":
                cfg["agent_runner"]["config"] = None
            elif key == "misc":
                cfg["agent_runner"]["config"]["misc"] = None
            else:
                cfg["agent_runner"]["config"]["misc"]["tool_call_timeout"] = None
            with self.subTest(key=key):
                self.assertIsNone(read_framework_tool_timeout(cfg))

    def test_leaf_is_a_bad_string(self) -> None:
        """叶子值是字符串但不是数字（``""`` / ``"abc"`` / ``"120s"``）。"""
        for raw in ("", "  ", "abc", "120s", "1e", "None", "null", "--"):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(
                    read_framework_tool_timeout(real_shaped_config(raw))
                )

    def test_leaf_is_zero_or_negative(self) -> None:
        """★ ``0`` 与负数当"未知"：超时 0 会让每条命令一启动就被掐死。"""
        for raw in (0, 0.0, -1, -120.0, "-5"):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(
                    read_framework_tool_timeout(real_shaped_config(raw))
                )

    def test_leaf_is_bool(self) -> None:
        """``True`` 是 ``int`` 的子类，很容易被 ``isinstance`` 放过。"""
        for raw in (True, False):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(
                    read_framework_tool_timeout(real_shaped_config(raw))
                )

    def test_leaf_is_nan_or_inf(self) -> None:
        """``nan`` / ``inf`` 参与比较的结果不可靠（inf 等于"永不超时"）。"""
        for raw in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(
                    read_framework_tool_timeout(real_shaped_config(raw))
                )

    def test_leaf_is_a_container(self) -> None:
        """叶子被填成 list / dict —— 结构完全不对。"""
        for raw in ([], {}, [120], {"value": 120}):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(
                    read_framework_tool_timeout(real_shaped_config(raw))
                )

    def test_level_is_a_scalar_so_digging_cannot_continue(self) -> None:
        """中间层是数字/字符串时无法继续下探，返回 None 而不是崩。"""
        for raw in (120, "120", True):
            cfg = {"agent_runner": {"config": {"misc": raw}}}
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(read_framework_tool_timeout(cfg))

    def test_hostile_mapping_does_not_raise(self) -> None:
        """★ 畸形映射：``.get()`` 与属性访问都抛异常，也必须降级。"""
        self.assertIsNone(read_framework_tool_timeout(HostileMapping()))
        self.assertIsNone(
            read_framework_tool_timeout({"agent_runner": HostileMapping()})
        )

    def test_hostile_leaf_does_not_raise(self) -> None:
        """叶子对象自己会抛异常（自定义 ``__float__``）。"""
        cfg = real_shaped_config(HostileLeaf())
        self.assertIsNone(read_framework_tool_timeout(cfg))

    def test_never_raises_for_a_wide_junk_matrix(self) -> None:
        """兜底：任意畸形组合都不许抛异常（它跑在插件加载路径上）。"""
        junk_values: tuple[Any, ...] = (
            None,
            "",
            "abc",
            0,
            -1,
            True,
            False,
            float("nan"),
            float("inf"),
            [],
            {},
            object(),
            HostileLeaf(),
        )
        for junk in junk_values:
            for cfg in (
                junk,
                {"agent_runner": junk},
                {"agent_runner": {"config": junk}},
                {"agent_runner": {"config": {"misc": junk}}},
                real_shaped_config(junk),
            ):
                with self.subTest(junk=repr(junk), cfg=repr(cfg)):
                    self.assertIsNone(read_framework_tool_timeout(cfg))

    def test_deep_nesting_is_not_confused(self) -> None:
        """同名 key 出现在别处（例如另一个插件的配置）时不许误读。"""
        cfg = {
            "tool_call_timeout": 999,  # 顶层同名：不是我们要的那一项
            "agent_runner": {
                "tool_call_timeout": 888,  # 少了一层 config
                "config": {"tool_call_timeout": 777},  # 少了一层 misc
            },
        }
        self.assertIsNone(read_framework_tool_timeout(cfg))


# ----------------------------------------------------------------------
# 3. 取值归一化函数本身
# ----------------------------------------------------------------------


class TestAsTimeoutSeconds(unittest.TestCase):
    """``as_timeout_seconds`` 是上面那个函数的取值入口，单独钉一遍。"""

    def test_accepts_positive_numbers(self) -> None:
        for raw, expected in ((120, 120.0), (120.0, 120.0), (0.5, 0.5), ("90", 90.0)):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(as_timeout_seconds(raw), expected)

    def test_rejects_everything_else(self) -> None:
        for raw in (None, "", "abc", 0, -1, True, False, [], {}, object(),
                    float("nan"), float("inf")):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(as_timeout_seconds(raw))

    def test_never_raises(self) -> None:
        for raw in (HostileLeaf(), HostileMapping(), b"120"):
            with self.subTest(raw=repr(raw)):
                self.assertIsNone(as_timeout_seconds(raw))


# ----------------------------------------------------------------------
# 4. 分层约束：bsk/ 包绝不 import astrbot
# ----------------------------------------------------------------------


class TestNoAstrbotImport(unittest.TestCase):
    """★ ``bsk/`` 包绝不能 import astrbot（本函数只接受鸭子类型对象的根本原因）。"""

    def test_astrbot_is_not_imported_by_bsk_config(self) -> None:
        """把 ``astrbot`` 换成会爆炸的哨兵模块，再重新 import 被测模块。

        如果 ``bsk/config.py``（或它拉进来的任何模块）偷偷 import 了 astrbot，
        这里会以异常形式立刻暴露，而不是等到部署到没有 astrbot 的环境才炸。
        """
        import importlib

        sentinel = types.ModuleType("astrbot")
        original = sys.modules.get("astrbot")
        sys.modules["astrbot"] = sentinel
        try:
            importlib.reload(importlib.import_module("bsk.config"))
            # 重新取一次函数引用（reload 后是新对象）。
            reloaded = importlib.import_module("bsk.config")
            self.assertEqual(
                reloaded.read_framework_tool_timeout(real_shaped_config()), 120.0
            )
        finally:
            if original is not None:
                sys.modules["astrbot"] = original
            else:
                sys.modules.pop("astrbot", None)

    def test_source_has_no_astrbot_import(self) -> None:
        """静态复核：``bsk/config.py`` 源码里没有任何 import astrbot 的语句。"""
        import ast

        source = (
            Path(__file__).resolve().parent.parent / "bsk" / "config.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "astrbot")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotEqual(module.split(".")[0], "astrbot")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
