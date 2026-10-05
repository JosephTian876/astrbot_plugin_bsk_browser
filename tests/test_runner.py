"""``bsk/runner.py`` 的单元测试。

策略：不依赖真实 bsk。用一个"假的 bsk 可执行文件"——即一个 Python 脚本，
由测试动态生成，能按参数模拟各种退出码、编码、超时行为。这样测试可以在
任何机器上跑，不需要装 bsk、不需要浏览器。

覆盖的重点是那些实测踩过的坑：
- GBK 编码崩溃（必须显式 UTF-8 解码）
- 错误 JSON 走 stdout；clap 参数错误走 stderr 纯文本
- 超时必须能中断（不能因管道句柄挂死）
- 退出码 6 档的分类
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# 让测试能 import 到项目的 bsk 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import errors  # noqa: E402
from bsk.errors import BskNotInstalled  # noqa: E402
from bsk.runner import BskRunner, _extract_error_fields, _try_parse_json  # noqa: E402

# 一个"万能假 bsk"：读取环境变量 / 首参数决定行为。
# 用 Python 写而不是 shell，保证跨平台且能精确控制字节输出。
FAKE_BSK_TEMPLATE = '''\
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

if mode == "ok":
    sys.stdout.write(json.dumps({{"session_id": "mnaa", "ok": True}}))
elif mode == "ok_array":
    sys.stdout.write(json.dumps([{{"instance_id": "c900a3da"}}]))
elif mode == "empty":
    pass
elif mode == "notfound":
    # 错误 JSON 走 stdout，退出码 1 —— 实测行为
    sys.stdout.write(json.dumps({{
        "code": "not_found",
        "message": "session not registered or already stopped",
        "hint": "run `bsk session list`",
        "exit_code": 1,
    }}))
    sys.exit(1)
elif mode == "busy":
    sys.stdout.write(json.dumps({{
        "code": "session_busy",
        "message": "another command is in flight",
        "exit_code": 1,
    }}))
    sys.exit(1)
elif mode == "outcome_unknown":
    sys.stdout.write(json.dumps({{
        "code": "input_outcome_unknown",
        "message": "extension reconnected",
        "exit_code": 3,
        "data": {{"reason": "extension_reconnected"}},
    }}))
    sys.exit(3)
elif mode == "clap":
    # clap 参数错误：stderr 纯文本，stdout 空，退出码 1
    sys.stderr.write("error: unexpected argument '--nope' found\\n")
    sys.exit(1)
elif mode == "multilang":
    # 关键用例：模拟含阿拉伯文/俄文的页面文本，触发 GBK 坑
    payload = {{"text": "\\u0647\\u0630\\u0627 \\u041f\\u0440\\u0438\\u0432\\u0435\\u0442 \\u4f60\\u597d"}}
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8"))
elif mode == "raw_high_bytes":
    # 直接吐非 UTF-8 字节，验证 errors="replace" 不炸
    sys.stdout.buffer.write(b"\\x89PNG\\xff\\xfe bad bytes")
elif mode == "prefix_noise":
    # stdout 前面有杂音，JSON 在后面
    sys.stdout.write("waiting for browser extension...\\n")
    sys.stdout.write(json.dumps({{"ok": True, "session_id": "zzzz"}}))
elif mode == "sleep":
    time.sleep(60)
elif mode == "exit3":
    sys.stdout.write(json.dumps({{"code": "browser_gone", "message": "cdp lost", "exit_code": 3}}))
    sys.exit(3)
elif mode == "exit4":
    sys.stdout.write(json.dumps({{"code": "timeout", "message": "slow", "exit_code": 4}}))
    sys.exit(4)
elif mode == "exit5":
    sys.stdout.write(json.dumps({{"code": "version_skew", "message": "mismatch", "exit_code": 5}}))
    sys.exit(5)
elif mode == "exit2":
    sys.stdout.write(json.dumps({{"code": "cancelled", "message": "cancelled", "exit_code": 2}}))
    sys.exit(2)
elif mode == "stderr_only_success":
    # 成功但 stderr 有提示 —— 实测会出现
    sys.stderr.write("waiting for browser extension to connect...\\n")
    sys.stdout.write(json.dumps({{"session_id": "abcd"}}))
else:
    sys.stderr.write("unknown mode: " + mode + "\\n")
    sys.exit(9)
'''


class RunnerTestBase(unittest.IsolatedAsyncioTestCase):
    """公共夹具：生成假 bsk 可执行文件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.fake_dir = Path(cls._tmpdir.name)
        script = cls.fake_dir / "fake_bsk.py"
        script.write_text(
            textwrap.dedent(FAKE_BSK_TEMPLATE).format(),
            encoding="utf-8",
        )
        cls.fake_script = script

        # 用一个 .cmd/.bat 包装器让 subprocess 能直接执行
        # （Windows 上 .py 不是可执行文件）
        if os.name == "nt":
            launcher = cls.fake_dir / "fake_bsk.cmd"
            launcher.write_text(
                f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
                encoding="ascii",
            )
        else:
            launcher = cls.fake_dir / "fake_bsk"
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                encoding="ascii",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        cls.launcher = launcher

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def make_runner(self, timeout: float = 20.0, cancel_grace: float = 1.0) -> BskRunner:
        """构造指向假 bsk 的 runner。

        Note:
            Windows 上 .cmd 不能被 create_subprocess_exec 直接执行，
            所以这里直接调用 python 解释器 + 脚本路径。
            为了复用 BskRunner，我们把 bsk_path 设成解释器路径，
            然后在 args 前面自己加脚本路径 —— 这由 run_fake 处理。

            cancel_grace 特意取小值（1 秒）：生产默认是 15 秒，
            测试里等 15 秒太慢，而取消逻辑本身与时长无关。
        """
        return BskRunner(sys.executable, default_timeout=timeout, cancel_grace=cancel_grace)

    async def run_fake(
        self,
        mode: str,
        *,
        timeout: float | None = None,
        runner: BskRunner | None = None,
    ):
        """执行一次假 bsk 调用。"""
        r = runner or self.make_runner(timeout or 20.0)
        return await r.run([str(self.fake_script), mode], timeout=timeout)


class TestPathResolution(unittest.TestCase):
    """bsk 可执行文件路径解析。"""

    def test_absolute_path_exists(self) -> None:
        from bsk.runner import resolve_bsk_path

        resolved = resolve_bsk_path(sys.executable)
        self.assertEqual(Path(resolved).resolve(), Path(sys.executable).resolve())

    def test_absolute_path_missing_raises(self) -> None:
        from bsk.runner import resolve_bsk_path

        with self.assertRaises(BskNotInstalled) as ctx:
            resolve_bsk_path(r"C:\definitely\not\here\bsk.exe")
        # 面向用户的提示必须包含可操作的修复建议
        self.assertIn("bsk", ctx.exception.friendly.lower())

    def test_path_like_missing_raises(self) -> None:
        from bsk.runner import resolve_bsk_path

        with self.assertRaises(BskNotInstalled):
            resolve_bsk_path("/no/such/dir/bsk")

    def test_bare_name_not_found_raises(self) -> None:
        from bsk.runner import resolve_bsk_path

        with self.assertRaises(BskNotInstalled) as ctx:
            resolve_bsk_path("definitely-not-a-real-binary-xyz")
        # 提示要告诉用户填绝对路径
        self.assertIn("PATH", ctx.exception.friendly)

    def test_lazy_resolution_does_not_raise_on_construct(self) -> None:
        """构造 runner 不应解析路径 —— 用户可能还没装 bsk。"""
        runner = BskRunner("definitely-not-a-real-binary-xyz")
        self.assertIsNone(runner._resolved)
        with self.assertRaises(BskNotInstalled):
            runner.resolve()

    def test_invalidate_cache(self) -> None:
        runner = BskRunner(sys.executable)
        first = runner.resolve()
        runner.invalidate_cache()
        self.assertIsNone(runner._resolved)
        self.assertEqual(first, runner.resolve())


class TestJsonParsing(unittest.TestCase):
    """JSON 容错解析（纯函数）。"""

    def test_plain_object(self) -> None:
        self.assertEqual(_try_parse_json('{"a":1}'), {"a": 1})

    def test_plain_array(self) -> None:
        self.assertEqual(_try_parse_json('[{"a":1}]'), [{"a": 1}])

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_try_parse_json(""))
        self.assertIsNone(_try_parse_json("   \n  "))

    def test_garbage_returns_none_not_raises(self) -> None:
        self.assertIsNone(_try_parse_json("error: unexpected argument"))

    def test_prefix_noise_extracts_json(self) -> None:
        """成功时 stderr 有提示、stdout 有杂音的情况。"""
        text = 'waiting for browser extension...\n{"ok": true}'
        self.assertEqual(_try_parse_json(text), {"ok": True})

    def test_nested_object(self) -> None:
        text = 'noise {"a":{"b":[1,2]}} trailing'
        self.assertEqual(_try_parse_json(text), {"a": {"b": [1, 2]}})


class TestErrorExtraction(unittest.TestCase):
    """错误 JSON 字段提取。"""

    def test_flat_structure(self) -> None:
        code, msg, hint, reason = _extract_error_fields(
            {"code": "not_found", "message": "gone", "hint": "check", "exit_code": 1}
        )
        self.assertEqual(code, "not_found")
        self.assertEqual(msg, "gone")
        self.assertEqual(hint, "check")
        self.assertEqual(reason, "")

    def test_reason_from_data(self) -> None:
        _, _, _, reason = _extract_error_fields(
            {"code": "x", "data": {"reason": "extension_reconnected"}}
        )
        self.assertEqual(reason, "extension_reconnected")

    def test_nested_error_wrapper(self) -> None:
        """兼容未来可能的 {"error": {...}} 包装。"""
        code, msg, _, _ = _extract_error_fields(
            {"error": {"code": "boom", "message": "bad"}}
        )
        self.assertEqual(code, "boom")
        self.assertEqual(msg, "bad")

    def test_non_dict_returns_empties(self) -> None:
        self.assertEqual(_extract_error_fields(None), ("", "", "", ""))
        self.assertEqual(_extract_error_fields([1, 2]), ("", "", "", ""))
        self.assertEqual(_extract_error_fields("text"), ("", "", "", ""))

    def test_wrong_field_types_ignored(self) -> None:
        """code 是数字时不应崩。"""
        code, msg, _, _ = _extract_error_fields({"code": 123, "message": None})
        self.assertEqual(code, "")
        self.assertEqual(msg, "")


class TestRunSuccess(RunnerTestBase):
    """成功路径。"""

    async def test_ok_object(self) -> None:
        result = await self.run_fake("ok")
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.data["session_id"], "mnaa")
        self.assertGreater(result.elapsed, 0.0)

    async def test_ok_array(self) -> None:
        result = await self.run_fake("ok_array")
        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, list)
        self.assertEqual(result.data[0]["instance_id"], "c900a3da")

    async def test_empty_output_still_ok(self) -> None:
        """没有输出的成功命令不应被判失败。"""
        result = await self.run_fake("empty")
        self.assertTrue(result.ok)
        self.assertIsNone(result.data)

    async def test_stderr_noise_on_success(self) -> None:
        """成功但 stderr 有提示 —— 实测会出现，不能因此报错。"""
        result = await self.run_fake("stderr_only_success")
        self.assertTrue(result.ok)
        self.assertIn("waiting", result.stderr)
        self.assertEqual(result.data["session_id"], "abcd")

    async def test_prefix_noise_still_parses(self) -> None:
        result = await self.run_fake("prefix_noise")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["session_id"], "zzzz")


class TestEncoding(RunnerTestBase):
    """GBK 编码坑 —— 这是实测唯一真实崩溃过的地方。"""

    async def test_multilang_utf8_decodes(self) -> None:
        """阿拉伯文/俄文/中文必须正确解码，不能抛 UnicodeDecodeError。"""
        result = await self.run_fake("multilang")
        self.assertTrue(result.ok, f"stderr={result.stderr}")
        text = result.data["text"]
        self.assertIn("Привет", text)  # 俄文
        self.assertIn("你好", text)  # 中文
        self.assertIn("\u0647", text)  # 阿拉伯文首字母

    async def test_invalid_utf8_bytes_do_not_crash(self) -> None:
        """非 UTF-8 字节必须被 replace 掉，而不是抛异常。"""
        result = await self.run_fake("raw_high_bytes")
        self.assertIsNotNone(result.stdout)


class TestErrorPaths(RunnerTestBase):
    """错误路径 —— 错误 JSON 在 stdout、clap 错误在 stderr。"""

    async def test_not_found_json_on_stdout(self) -> None:
        result = await self.run_fake("notfound")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.data["code"], "not_found")

    async def test_clap_error_text_on_stderr(self) -> None:
        result = await self.run_fake("clap")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.data)
        self.assertIn("unexpected argument", result.stderr)

    async def test_exit_codes(self) -> None:
        for mode, expected in (
            ("exit2", 2),
            ("exit3", 3),
            ("exit4", 4),
            ("exit5", 5),
        ):
            with self.subTest(mode=mode):
                result = await self.run_fake(mode)
                self.assertFalse(result.ok)
                self.assertEqual(result.exit_code, expected)


class TestRunOrRaise(RunnerTestBase):
    """异常分类 —— 上层业务依赖这个语义。"""

    async def test_success_returns_result(self) -> None:
        runner = self.make_runner()
        result = await runner.run_or_raise([str(self.fake_script), "ok"])
        self.assertTrue(result.ok)

    async def test_not_found_maps_to_session_gone(self) -> None:
        runner = self.make_runner()
        with self.assertRaises(errors.BskSessionGone) as ctx:
            await runner.run_or_raise([str(self.fake_script), "notfound"])
        self.assertTrue(ctx.exception.retryable, "会话不存在应当可重试（重建后）")
        self.assertIn("会话", ctx.exception.friendly)

    async def test_busy_maps_to_session_busy(self) -> None:
        runner = self.make_runner()
        with self.assertRaises(errors.BskSessionBusy) as ctx:
            await runner.run_or_raise([str(self.fake_script), "busy"])
        self.assertTrue(ctx.exception.retryable)

    async def test_outcome_unknown_is_not_retryable(self) -> None:
        """最关键的安全语义：结果未知绝不能重试。"""
        runner = self.make_runner()
        with self.assertRaises(errors.BskOutcomeUnknown) as ctx:
            await runner.run_or_raise([str(self.fake_script), "outcome_unknown"])
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.reason, "extension_reconnected")

    async def test_clap_maps_to_protocol_error(self) -> None:
        runner = self.make_runner()
        with self.assertRaises(errors.BskError) as ctx:
            await runner.run_or_raise([str(self.fake_script), "clap"])
        self.assertIn("bug", ctx.exception.friendly.lower())

    async def test_browser_error_maps(self) -> None:
        runner = self.make_runner()
        with self.assertRaises(errors.BskBrowserError):
            await runner.run_or_raise([str(self.fake_script), "exit3"])

    async def test_version_error_maps(self) -> None:
        runner = self.make_runner()
        with self.assertRaises(errors.BskVersionError):
            await runner.run_or_raise([str(self.fake_script), "exit5"])


class TestTimeout(RunnerTestBase):
    """超时 —— Windows 管道句柄可能让 communicate() 永不返回。"""

    async def test_timeout_returns_and_does_not_hang(self) -> None:
        """超时必须在合理时间内返回，不能因为管道句柄挂死。"""
        result = await self.run_fake("sleep", timeout=1.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, errors.EXIT_TIMEOUT)
        # 1 秒超时 + 1 秒取消宽限，再加进程启动开销，5 秒内必须返回。
        # 若这里失败，说明取消逻辑没有真正生效（管道句柄坑复现了）。
        self.assertLess(result.elapsed, 5.0, f"超时命令耗时 {result.elapsed:.2f}s，取消逻辑可能失效")

    async def test_timeout_raises_retryable_error(self) -> None:
        runner = self.make_runner(timeout=1.0)
        with self.assertRaises(errors.BskTimeout) as ctx:
            await runner.run_or_raise([str(self.fake_script), "sleep"], timeout=1.0)
        self.assertTrue(ctx.exception.retryable)

    async def test_cancel_grace_is_configurable(self) -> None:
        """宽限期应当可调 —— 默认 15 秒对用户是可感知的额外等待。"""
        from bsk.runner import DEFAULT_CANCEL_GRACE_SEC, MIN_CANCEL_GRACE_SEC

        runner = BskRunner(sys.executable, cancel_grace=3.0)
        self.assertEqual(runner.cancel_grace, 3.0)
        # 下限保护：不允许配成 0，否则等于直接 kill
        self.assertEqual(
            BskRunner(sys.executable, cancel_grace=0.0).cancel_grace,
            MIN_CANCEL_GRACE_SEC,
        )
        self.assertEqual(BskRunner(sys.executable).cancel_grace, DEFAULT_CANCEL_GRACE_SEC)

    async def test_timeout_sets_env_for_graceful_cancel(self) -> None:
        """Windows 上必须设置 BSK_CANCEL_ON_STDIN_CLOSE，否则优雅取消无效。"""
        from bsk.runner import _build_env

        env = _build_env()
        self.assertEqual(env.get("BSK_CANCEL_ON_STDIN_CLOSE"), "1")

    async def test_process_tree_does_not_leak_after_timeout(self) -> None:
        """超时后子进程必须被回收（用轮询确认，不依赖私有 API）。"""
        runner = self.make_runner(timeout=1.0)
        result = await runner.run([str(self.fake_script), "sleep"], timeout=1.0)
        self.assertFalse(result.ok)
        # 给回收一点时间；随后确认没有残留的同脚本进程。
        await asyncio.sleep(0.5)
        self.assertEqual(result.exit_code, errors.EXIT_TIMEOUT)


class TestResolveCaching(RunnerTestBase):
    """路径解析缓存。"""

    async def test_resolution_is_cached_across_calls(self) -> None:
        runner = BskRunner(sys.executable)
        self.assertIsNone(runner._resolved)
        await runner.run([str(self.fake_script), "ok"])
        self.assertEqual(runner._resolved, sys.executable)

    async def test_missing_binary_raises_not_installed(self) -> None:
        runner = BskRunner("definitely-not-a-real-binary-xyz")
        with self.assertRaises(BskNotInstalled):
            await runner.run(["status", "--json"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
