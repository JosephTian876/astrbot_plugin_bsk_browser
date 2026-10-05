"""``bsk/errors.py`` 里"可恢复启动被取消"这一档的单元测试。

为什么单独一个文件：``cancelled`` 是唯一一个**既不是成功、也不是失败**的
错误码 —— 它表示"这次启动已经按调用方的要求被抑制了"。把它误当成普通失败，
后果比报错严重得多：

- 重试 → 被同一条墓碑再次拒绝，白等一轮；
- 回退到不带 ``--request-id`` 的普通 start → 绕开取消，开出一个没有任何
  所有权凭据保护的窗口，正是这一轮要修的 bug。

所以这里测的不是"字符串比得对"，而是这两条安全语义有没有被钉住。

pytest 在本机不可用，因此用标准库 ``unittest``。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能 import 到项目的 bsk 包（与 test_runner.py / test_journal.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import errors  # noqa: E402
from bsk.errors import (  # noqa: E402
    BskBrowserError,
    BskError,
    BskOutcomeUnknown,
    BskSessionBusy,
    BskSessionGone,
    BskStartCancelled,
    BskTimeout,
    CODE_CANCELLED,
    is_cancelled_error,
)


class TestCancelledCode(unittest.TestCase):
    """``CODE_CANCELLED`` 常量本身。"""

    def test_code_value_matches_bsk_output(self) -> None:
        """实测原文就是 ``"cancelled"`` —— 写错一个字母判定就永远为假。"""
        self.assertEqual(CODE_CANCELLED, "cancelled")

    def test_cancelled_exit_code_is_protocol(self) -> None:
        """它走退出码 2（协议层），与 ``classify`` 的兜底分支一致。"""
        self.assertEqual(errors.EXIT_PROTOCOL, 2)


class TestIsCancelledError(unittest.TestCase):
    """``is_cancelled_error`` 的判定表 —— 认 ``code``，不认类型。"""

    def test_cancelled_code_is_recognized(self) -> None:
        exc = BskStartCancelled(
            "start request cancelled",
            friendly="这次启动已被取消。",
            code=CODE_CANCELLED,
            exit_code=errors.EXIT_PROTOCOL,
        )
        self.assertTrue(is_cancelled_error(exc))

    def test_plain_error_with_cancelled_code_is_recognized(self) -> None:
        """刻意不要求 ``isinstance``：``code`` 会在包装/转换后一路保留下来。

        调用链上任何一层都可能把 ``BskStartCancelled`` 换成别的类型，
        判定若绑死类型，转换一次就漏判一次。
        """
        exc = BskError("cancelled", code=CODE_CANCELLED)
        self.assertTrue(is_cancelled_error(exc))

    def test_other_codes_are_not_cancelled(self) -> None:
        """其它错误码（含可重试的那些）一律不许被判成"已取消"。"""
        for code in (
            "",
            "not_found",
            "session_busy",
            "permission_denied",
            "browser_ambiguous",
            "protocol_error",
            "Cancelled",  # 大小写不同就是不同的 code
            "cancelled_",  # 前缀相似但不是同一个
        ):
            with self.subTest(code=code):
                self.assertFalse(is_cancelled_error(BskError("失败", code=code)))

    def test_exception_without_code_attribute_is_not_cancelled(self) -> None:
        """没有 ``code`` 属性的普通异常不能炸，也不能被判成已取消。"""
        for exc in (
            RuntimeError("普通异常"),
            ValueError("没有 code"),
            TimeoutError("超时"),
            Exception(),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_cancelled_error(exc))

    def test_code_attribute_of_wrong_type_is_not_cancelled(self) -> None:
        """``code`` 不是字符串时同样只返回 False，不做隐式转换。"""

        class Weird(Exception):
            code = None

        self.assertFalse(is_cancelled_error(Weird()))

    def test_non_exception_objects_are_not_cancelled(self) -> None:
        """传进来的东西即使不是异常也不能炸（判定函数常被当成通用谓词用）。"""
        for obj in (None, 0, "", object()):
            with self.subTest(obj=repr(obj)):
                self.assertFalse(is_cancelled_error(obj))  # type: ignore[arg-type]


class TestBskStartCancelled(unittest.TestCase):
    """异常类本身的语义。"""

    def test_retryable_is_false(self) -> None:
        """重试会被同一条墓碑再次拒绝 —— 必须是不可重试。"""
        self.assertFalse(BskStartCancelled("cancelled", code=CODE_CANCELLED).retryable)

    def test_is_a_bsk_error_so_callers_keep_catching_one_type(self) -> None:
        """继承自 ``BskError``：``main.py`` 的 ``except BskError`` 不用改。"""
        exc = BskStartCancelled("cancelled", code=CODE_CANCELLED)
        self.assertIsInstance(exc, BskError)

    def test_friendly_is_chinese_and_says_what_happens_next(self) -> None:
        """面向模型的文案必须中文，且要说明"这次启动没有发生"。"""
        friendly = "这次浏览器启动已被取消，我没有打开任何窗口，也不会自动重试。"
        exc = BskStartCancelled(
            "start request cancelled", friendly=friendly, code=CODE_CANCELLED
        )

        self.assertEqual(exc.friendly, friendly)
        self.assertTrue(
            any("\u4e00" <= ch <= "\u9fff" for ch in exc.friendly),
            f"friendly 里没有中文：{exc.friendly!r}",
        )

    def test_friendly_falls_back_to_message(self) -> None:
        """不传 friendly 时沿用基类行为（退回 message），不能变成空串。"""
        exc = BskStartCancelled("start request cancelled", code=CODE_CANCELLED)
        self.assertEqual(exc.friendly, "start request cancelled")

    def test_str_includes_code_and_exit(self) -> None:
        """诊断串里要能看到 code，排查时才知道是这一档。"""
        exc = BskStartCancelled(
            "start request cancelled",
            code=CODE_CANCELLED,
            exit_code=errors.EXIT_PROTOCOL,
        )
        rendered = str(exc)
        self.assertIn("cancelled", rendered)
        self.assertIn("exit=2", rendered)

    def test_other_errors_keep_their_retryable_semantics(self) -> None:
        """新增一个异常类不该动到既有分类：可重试的仍可重试。"""
        self.assertTrue(BskTimeout("慢").retryable)
        self.assertTrue(BskSessionGone("没了").retryable)
        self.assertTrue(BskSessionBusy("忙").retryable)
        self.assertFalse(BskOutcomeUnknown("未知").retryable)
        self.assertFalse(BskBrowserError("浏览器炸了").retryable)


class TestClassifyIsUnchanged(unittest.TestCase):
    """``classify()`` 是共享分类器，本次扩展**刻意不改**它的映射。

    转换在调用点显式做（先 ``classify``，再用 ``is_cancelled_error`` 判），
    这里把这条边界钉住：``cancelled`` 不会被 ``classify`` 悄悄变成新类型。
    """

    def test_cancelled_still_classifies_as_protocol_error_by_exit_code(self) -> None:
        exc = errors.classify(
            exit_code=errors.EXIT_PROTOCOL,
            code=CODE_CANCELLED,
            message="start request cancelled",
            hint="",
            reason="",
            stderr="",
        )
        # 既有映射的结果不许变 —— 仍是 BskProtocolError。
        self.assertIsInstance(exc, errors.BskProtocolError)
        # 但调用点靠 code 就能认出它，这正是 is_cancelled_error 存在的理由。
        self.assertTrue(is_cancelled_error(exc))

    def test_classify_does_not_emit_start_cancelled(self) -> None:
        """没有哪个既有映射分支会返回 ``BskStartCancelled``。"""
        for exit_code in range(0, 6):
            with self.subTest(exit_code=exit_code):
                exc = errors.classify(
                    exit_code=exit_code,
                    code=CODE_CANCELLED,
                    message="start request cancelled",
                    hint="",
                    reason="",
                    stderr="",
                )
                self.assertNotIsInstance(exc, BskStartCancelled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
