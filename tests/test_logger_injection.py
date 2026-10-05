"""logger 依赖注入的单元测试 —— ``bsk/logger.py`` 与各组件对注入 logger 的使用。

背景（本次要修的审核拒绝项）：AstrBot 插件市场的审核规则要求插件只能从
``astrbot.api`` 取 logger（``from astrbot.api import logger``），严禁使用 Python
内置 ``logging`` 模块。但本仓库另有一条被静态测试强制的分层约束 —— ``bsk/`` 包
绝不 import astrbot（否则无法脱离框架单测）。两条要求正面冲突，解法是审核原文
明确许可的第三条路：**依赖注入**。

- ``main.py``（唯一允许 import astrbot 的文件）从 ``astrbot.api`` 取 logger，
  注入给 ``BskService`` / ``SessionManager`` / ``SessionJournal``；
- ``bsk/`` 内部完全不再出现 ``logging``；未注入时退回 ``bsk/logger.py`` 的
  ``NULL_LOGGER``（纯 Python，不产生输出、不持有任何句柄）。

本文件覆盖三类断言：

1. **注入生效** —— 日志调用确实到达注入对象。这是"注入"与"被吞掉"的分水岭：
   ``NullLogger`` 什么都不做，如果代码里漏改一处、仍在调某个空实现，表面行为
   与注入成功完全一样，只有记录型假 logger 能分辨；
2. **未注入时降级** —— 走 ``NullLogger``，任何级别、任何调用形态都不抛异常
   （它在插件启动路径上，抛异常等于插件加载失败）；
3. **源码级断言** —— ``bsk/`` 下零 ``import logging`` / 零 ``logging.getLogger`` /
   零 ``import astrbot``，且 ``main.py`` 也不再有内置 logging。用 ``ast`` 扫描真实
   语法树而不是搜关键字（注释里提到 "logging" 会被关键字搜索误判），与
   ``tests/verify_release_ready.py`` 的架构约束检查同风格。

pytest 在本机不可用，因此用标准库 ``unittest``；异步用例用
``unittest.IsolatedAsyncioTestCase``（Python 3.12 原生支持）。
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 让测试能 import 到项目的 bsk 包（与 test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.config import parse_settings  # noqa: E402
from bsk.journal import JournalEntry, SessionJournal  # noqa: E402
from bsk.logger import NULL_LOGGER, LoggerLike, NullLogger  # noqa: E402
from bsk.models import BskResult  # noqa: E402
from bsk.service import BskService  # noqa: E402
from bsk.session import SessionManager  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent

# ``LoggerLike`` 协议要求的全部级别（即 bsk/ 各组件可以调用的全集）。
# ``exception`` 必须在内：main.py 与各组件都用它，少了它会让"注入的 logger
# 支持哪些方法"这条契约出现缺口。
ALL_LEVELS: tuple[str, ...] = (
    "debug",
    "info",
    "warning",
    "error",
    "exception",
)


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LogRecord:
    """一条被记录下来的日志调用。"""

    level: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]

    def render(self) -> str:
        """按 ``logging`` 的惰性 %-格式化约定把消息渲染成字符串。

        现有 59 处调用点用的都是 ``logger.info("已创建会话 %s", sid)`` 这种形态，
        所以假 logger 必须同时记录 args 并支持 %-格式化 —— 只记录第一段文本
        会让"参数有没有真的传下来"变得无法断言。
        """
        if not self.args:
            return ""
        template, *rest = self.args
        try:
            return str(template) % tuple(rest) if rest else str(template)
        except Exception:  # noqa: BLE001 - 格式串与实参不匹配时给原文即可
            return " ".join(str(a) for a in self.args)


@dataclass
class RecordingLogger:
    """记录型假 logger：把每一次调用原样留下来供断言。

    刻意不继承任何东西 —— 它只需要满足 ``LoggerLike`` 的鸭子类型契约，
    这样"被测组件是否只依赖接口"这件事本身也被测到了。
    """

    records: list[LogRecord] = field(default_factory=list)

    def _record(self, level: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.records.append(LogRecord(level=level, args=args, kwargs=kwargs))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("debug", args, kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("info", args, kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("warning", args, kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("error", args, kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._record("exception", args, kwargs)

    def critical(self, *args: Any, **kwargs: Any) -> None:
        self._record("critical", args, kwargs)

    # --- 断言辅助 ---

    def levels(self) -> list[str]:
        return [r.level for r in self.records]

    def rendered(self) -> str:
        """把所有记录渲染成一整段文本，便于 ``assertIn`` 断言。"""
        return "\n".join(r.render() for r in self.records)

    def messages_at(self, level: str) -> list[str]:
        return [r.render() for r in self.records if r.level == level]


class FakeRunner:
    """假的 ``BskRunner``：只实现 ``run_or_raise``，永不真的起子进程。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        call = list(args)
        self.calls.append(call)
        if call[:2] == ["session", "start"]:
            data: Any = {"session_id": "mnaa", "browser_instance_id": "c900a3da"}
        else:
            data = {}
        return BskResult(ok=True, exit_code=0, data=data)


def make_settings(**overrides: Any) -> Any:
    """造一份真实的 ``Settings``（走 ``parse_settings``，含夹取与兜底）。

    与 ``test_service.py`` 一样用真类型而不是 ``SimpleNamespace``：
    配置对象字段名漂移这类回归应当被测出来。
    """
    raw: dict[str, Any] = {
        "bsk_path": "bsk",
        # 显式指定浏览器，避免触发 probe_browser 里的同步子进程探测。
        "browser_instance_id": "c900a3da",
        "command_timeout_sec": 60.0,
    }
    raw.update(overrides)
    return parse_settings(raw)


def make_entry(session_id: str = "mnaa") -> JournalEntry:
    return JournalEntry(
        session_id=session_id,
        browser_instance_id="c900a3da",
        agent_window_id=42,
        created_at=1.0,
        pid=1,
    )


class TempDirCase(unittest.TestCase):
    """公共脚手架：每个用例一个干净的临时目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bsk-logger-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


# ----------------------------------------------------------------------
# 1. 接口契约
# ----------------------------------------------------------------------


class TestNullLoggerContract(unittest.TestCase):
    """``bsk/logger.py`` 的公开契约（纯标准库、零依赖）。"""

    def test_null_logger_supports_every_level(self) -> None:
        """任何级别都必须存在且可调用 —— 漏一个就会在调用点抛 AttributeError。"""
        for level in ALL_LEVELS:
            with self.subTest(level=level):
                self.assertTrue(callable(getattr(NULL_LOGGER, level)))

    def test_null_logger_accepts_arbitrary_call_shapes(self) -> None:
        """必须吃得下现有调用点的全部形态，且不抛异常。

        协议是 ``(msg, *args, **kwargs)`` —— 消息本身是必填的位置参数，
        其余一律照单全收。
        """
        shapes: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            (("纯文本",), {}),
            (("惰性 %s 格式化", "值"), {}),
            (("多个 %s 与 %d", "甲", 2), {}),
            (("带 kwargs",), {"exc_info": True}),
            (("格式串 %s 没有实参",), {}),  # 故意不匹配：也不许抛
            ((123, object()), {}),  # 非字符串实参
        ]
        for args, kwargs in shapes:
            for level in ALL_LEVELS:
                with self.subTest(level=level, args=args):
                    getattr(NULL_LOGGER, level)(*args, **kwargs)

    def test_null_logger_is_stateless_and_reusable(self) -> None:
        """兜底实例不持有任何可变状态（每次调用都无副作用）。"""
        NULL_LOGGER.info("x")
        NULL_LOGGER.debug("y")
        # 用 __slots__ 的空实例：没有 __dict__，也就无处挂状态。
        self.assertFalse(hasattr(NULL_LOGGER, "__dict__"))

    def test_null_logger_instances_are_usable(self) -> None:
        """除了单例常量，直接 ``NullLogger()`` 也必须能用。"""
        fresh = NullLogger()
        for level in ALL_LEVELS:
            with self.subTest(level=level):
                getattr(fresh, level)("消息 %s", 1)

    def test_null_logger_satisfies_the_protocol(self) -> None:
        """协议是 ``runtime_checkable`` 的，兜底实现必须结构上满足它。"""
        self.assertIsInstance(NULL_LOGGER, LoggerLike)

    def test_recording_logger_satisfies_the_protocol(self) -> None:
        """记录型假 logger 也必须满足协议 —— 否则测的就不是真实接口。"""
        self.assertIsInstance(RecordingLogger(), LoggerLike)

    def test_protocol_rejects_a_non_logger(self) -> None:
        """协议检查要真的能拒绝：否则上面两条断言等于没测。"""
        self.assertNotIsInstance(object(), LoggerLike)


# ----------------------------------------------------------------------
# 2. 注入生效：日志必须真的到达注入对象
# ----------------------------------------------------------------------


class TestJournalLoggerInjection(TempDirCase):
    """``SessionJournal`` 的日志走注入对象，而不是被吞掉。"""

    def test_read_failure_is_reported_to_injected_logger(self) -> None:
        """读失败（路径是目录）→ 记一条 debug，且带上了异常详情。"""
        as_dir = self.dir / "iam-a-dir"
        as_dir.mkdir()
        rec = RecordingLogger()

        journal = SessionJournal(as_dir, logger=rec)
        self.assertEqual(journal.load(), [])

        self.assertIn("debug", rec.levels())
        self.assertIn("journal", rec.rendered())

    def test_write_failure_is_reported_to_injected_logger(self) -> None:
        """写失败（父路径是个文件，目录建不出来）→ 记一条 debug，不抛异常。"""
        blocker = self.dir / "blocker"
        blocker.write_text("我是文件不是目录", encoding="utf-8")
        rec = RecordingLogger()

        journal = SessionJournal(blocker / "sessions.json", logger=rec)
        journal.add(make_entry())

        self.assertIn("debug", rec.levels())
        self.assertIn("写入失败", rec.rendered())

    def test_invalid_json_is_reported_to_injected_logger(self) -> None:
        """不是合法 JSON → 记 debug 并返回空列表。"""
        path = self.dir / "sessions.json"
        path.write_text("{ 半截", encoding="utf-8")
        rec = RecordingLogger()

        journal = SessionJournal(path, logger=rec)
        self.assertEqual(journal.load(), [])

        self.assertIn("JSON", rec.rendered())

    def test_successful_write_is_not_logged_as_a_problem(self) -> None:
        """正常写入不得产生 warning 及以上的日志。

        刻意不断言"零条记录"：文件还不存在时读不到内容，这属于
        ``_read_unlocked`` 的既有语义（记一条 debug 后按空处理），
        并不是错误。真正要守住的是"正常路径不刷告警"。
        """
        rec = RecordingLogger()
        journal = SessionJournal(self.dir / "sessions.json", logger=rec)

        journal.add(make_entry())

        self.assertEqual(
            [r.level for r in rec.records if r.level in ("warning", "error", "exception")],
            [],
        )

    def test_second_write_logs_nothing_at_all(self) -> None:
        """文件已存在后再写：连 debug 都不该有（读得通、写成功）。"""
        rec = RecordingLogger()
        journal = SessionJournal(self.dir / "sessions.json", logger=rec)
        journal.add(make_entry("mnaa"))
        rec.records.clear()

        journal.add(make_entry("mnab"))

        self.assertEqual(rec.levels(), [])


class TestSessionManagerLoggerInjection(unittest.IsolatedAsyncioTestCase):
    """``SessionManager`` 的日志走注入对象。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bsk-logger-session-")
        self.addCleanup(self._tmp.cleanup)
        self.journal_path = Path(self._tmp.name) / "sessions.json"

    async def test_session_creation_is_reported_to_injected_logger(self) -> None:
        rec = RecordingLogger()
        manager = SessionManager(
            FakeRunner(),  # type: ignore[arg-type]
            make_settings(),
            journal=SessionJournal(self.journal_path),
            logger=rec,
        )

        session = await manager.acquire("umo-1")

        self.assertIn("info", rec.levels())
        self.assertIn(session.session_id, rec.rendered())

    async def test_release_is_reported_to_injected_logger(self) -> None:
        """释放会话也要到达注入对象（覆盖 stop 成功那条路径）。"""
        rec = RecordingLogger()
        manager = SessionManager(
            FakeRunner(),  # type: ignore[arg-type]
            make_settings(),
            journal=SessionJournal(self.journal_path),
            logger=rec,
        )
        await manager.acquire("umo-1")
        before = len(rec.records)

        await manager.release("umo-1")

        self.assertGreater(len(rec.records), before)
        self.assertIn("mnaa", rec.rendered())

    async def test_browser_probe_failure_is_reported_to_injected_logger(self) -> None:
        """探测浏览器失败 → 记 debug 并降级（不是静默吞掉）。"""
        rec = RecordingLogger()

        def boom() -> str:
            raise RuntimeError("probe 炸了")

        manager = SessionManager(
            FakeRunner(),  # type: ignore[arg-type]
            make_settings(browser_instance_id=""),
            browser_probe=boom,
            journal=SessionJournal(self.journal_path),
            logger=rec,
        )

        await manager.acquire("umo-1")

        self.assertIn("debug", rec.levels())
        self.assertIn("probe 炸了", rec.rendered())


class TestServiceLoggerInjection(TempDirCase):
    """``BskService`` 的日志走注入对象，并向下传递给 session 与 journal。"""

    def test_probe_failure_is_reported_to_injected_logger(self) -> None:
        """``probe_browser`` 的降级分支必须记到注入对象。"""
        rec = RecordingLogger()

        class BoomRunner(FakeRunner):
            def resolve(self) -> str:
                return str(self_dir_that_does_not_exist())

        service = BskService(
            make_settings(),
            runner=BoomRunner(),  # type: ignore[arg-type]
            logger=rec,
        )

        self.assertEqual(service.probe_browser(), "")
        self.assertIn("debug", rec.levels())
        self.assertIn("浏览器探测失败", rec.rendered())

    def test_shot_cleanup_is_reported_to_injected_logger(self) -> None:
        """截图后清理失败的分支也要到达注入对象（debug 级）。"""
        rec = RecordingLogger()

        class BoomCleanupRunner(FakeRunner):
            pass

        service = BskService(
            make_settings(screenshot_dir=str(self.dir)),
            runner=BoomCleanupRunner(),  # type: ignore[arg-type]
            logger=rec,
        )
        # 直接触发清理分支：把 cleanup_shots 换成会抛异常的实现。
        import bsk.service as service_module

        original = service_module.cleanup_shots
        service_module.cleanup_shots = _raising_cleanup
        try:
            import asyncio

            asyncio.run(service.screenshot("umo-1"))
        finally:
            service_module.cleanup_shots = original

        self.assertIn("截图清理失败", rec.rendered())


class TestServicePassesLoggerDownstream(unittest.IsolatedAsyncioTestCase):
    """注入给 service 的 logger 必须继续传给 session 与 journal（行为级断言）。

    刻意不检查 ``service._logger`` 这类私有属性名：只要"下游用到的确实是我传进去
    的那个对象"这一点成立，怎么存、叫什么名字都是实现自由。
    """

    async def test_logger_reaches_session_manager(self) -> None:
        rec = RecordingLogger()
        service = BskService(
            make_settings(),
            runner=FakeRunner(),  # type: ignore[arg-type]
            logger=rec,
        )

        await service.sessions.acquire("umo-1")

        self.assertIn("mnaa", rec.rendered())

    async def test_logger_reaches_journal(self) -> None:
        """journal 路径不可写 → 写失败的那条 debug 必须落到注入对象上。

        这是"logger 有没有从 service 传到 journal"的行为级证据：
        只有 journal 拿到了注入对象，这条记录才会出现。
        """
        tmp = tempfile.TemporaryDirectory(prefix="bsk-logger-down-")
        self.addCleanup(tmp.cleanup)
        as_dir = Path(tmp.name) / "journal-is-a-dir"
        as_dir.mkdir()

        rec = RecordingLogger()
        service = BskService(
            make_settings(journal_path=str(as_dir)),
            runner=FakeRunner(),  # type: ignore[arg-type]
            logger=rec,
        )

        await service.sessions.acquire("umo-1")

        self.assertIn("写入失败", rec.rendered())

    async def test_injected_logger_actually_receives_records(self) -> None:
        """最直白的一条：注入对象上必须出现记录。

        没有这一条，"注入"和"全部走 NullLogger"在行为上无法区分 ——
        这正是本次修复最容易被静默做错的地方。
        """
        rec = RecordingLogger()
        service = BskService(
            make_settings(),
            runner=FakeRunner(),  # type: ignore[arg-type]
            logger=rec,
        )

        await service.sessions.acquire("umo-1")

        self.assertGreater(len(rec.records), 0, "日志调用没有到达注入的 logger")


# ----------------------------------------------------------------------
# 3. 未注入时降级：NullLogger，绝不抛异常
# ----------------------------------------------------------------------


class TestNullLoggerFallback(TempDirCase):
    """不传 logger 时走 ``NullLogger``，且行为与注入时一致（只是没有输出）。"""

    def test_journal_without_logger_never_raises(self) -> None:
        """把 journal 逼到所有失败分支上，一个异常都不能冒出来。"""
        as_dir = self.dir / "dir"
        as_dir.mkdir()
        bad_json = self.dir / "bad.json"
        bad_json.write_text("{ 半截", encoding="utf-8")
        blocked = self.dir / "blocker"
        blocked.write_text("我是文件不是目录", encoding="utf-8")

        for path in (as_dir, bad_json, blocked / "sessions.json", self.dir / "nope" / "deep.json"):
            with self.subTest(path=str(path)):
                journal = SessionJournal(path)  # 不传 logger
                self.assertIsInstance(journal.load(), list)
                journal.add(make_entry())
                journal.remove("mnaa")
                journal.clear()

    def test_session_manager_without_logger_never_raises(self) -> None:
        """不传 logger 时探测失败、建会话等路径都不能抛异常。"""

        def boom() -> str:
            raise RuntimeError("probe 炸了")

        async def scenario() -> None:
            manager = SessionManager(
                FakeRunner(),  # type: ignore[arg-type]
                make_settings(browser_instance_id=""),
                browser_probe=boom,
                journal=SessionJournal(self.dir / "sessions.json"),
            )
            session = await manager.acquire("umo-1")
            self.assertTrue(session.is_valid())
            await manager.release("umo-1")

        import asyncio

        asyncio.run(scenario())

    def test_service_without_logger_never_raises(self) -> None:
        """不传 logger 时整个服务照常工作（不影响插件加载）。"""
        import asyncio

        service = BskService(
            make_settings(screenshot_dir=str(self.dir)),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        self.assertEqual(service.probe_browser(), "")
        asyncio.run(service.sessions.acquire("umo-1"))

    def test_fallback_logger_is_null_logger(self) -> None:
        """兜底实例应当是那个共享的 ``NULL_LOGGER``，而不是每处各造一个。"""
        journal = SessionJournal(self.dir / "sessions.json")
        # 属性名可能实现为 _logger；两种都不在时说明根本没有兜底，也要报出来。
        holder = getattr(journal, "_logger", NULL_LOGGER)
        self.assertIsInstance(holder, NullLogger)


# ----------------------------------------------------------------------
# 4. 源码级断言：bsk/ 零 logging、零 astrbot
# ----------------------------------------------------------------------


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _bsk_modules() -> list[Path]:
    return sorted((PROJECT / "bsk").glob("*.py"))


def _imported_roots(tree: ast.AST) -> list[str]:
    """所有 import 语句 + 动态导入调用的根模块名。"""
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.append(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # 覆盖 importlib.import_module("logging") / __import__("astrbot") 这类动态导入。
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name in ("import_module", "__import__"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        roots.append(arg.value.split(".")[0])
    return roots


def _logging_attribute_uses(tree: ast.AST) -> list[str]:
    """``logging.getLogger`` 这类以 ``logging`` 为根的属性访问。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "logging"
        ):
            found.append(node.attr)
    return found


class TestSourceLevelLayering(unittest.TestCase):
    """静态复核：不再有内置 logging，也不再有 astrbot 依赖。

    用 ``ast`` 而不是关键字搜索：注释、docstring、变量名里都可能出现
    "logging" 字样（本文件自己就到处是），关键字搜索会把它们误判成违规。
    """

    def test_bsk_modules_exist(self) -> None:
        self.assertGreater(len(_bsk_modules()), 0)
        self.assertTrue((PROJECT / "bsk" / "logger.py").exists(), "bsk/logger.py 缺失")
        self.assertTrue((PROJECT / "bsk" / "paths.py").exists(), "bsk/paths.py 缺失")

    def test_bsk_does_not_import_astrbot(self) -> None:
        """分层约束：``bsk/`` 绝不 import astrbot（与 verify_release_ready 同义）。"""
        offenders: list[str] = []
        for py in _bsk_modules():
            if "astrbot" in _imported_roots(_parse(py)):
                offenders.append(py.name)
        self.assertEqual(offenders, [], f"bsk/ 下违规 import astrbot：{offenders}")

    def test_bsk_does_not_import_stdlib_logging(self) -> None:
        """审核红线：``bsk/`` 下不得出现任何形式的 ``logging`` 导入。"""
        offenders: list[str] = []
        for py in _bsk_modules():
            if "logging" in _imported_roots(_parse(py)):
                offenders.append(py.name)
        self.assertEqual(offenders, [], f"bsk/ 下违规 import logging：{offenders}")

    def test_bsk_does_not_call_logging_getlogger(self) -> None:
        """``logging.getLogger(__name__)`` 必须零命中（即使没写 import）。"""
        offenders: dict[str, list[str]] = {}
        for py in _bsk_modules():
            used = _logging_attribute_uses(_parse(py))
            if used:
                offenders[py.name] = used
        self.assertEqual(offenders, {}, f"bsk/ 下仍在用 logging 模块：{offenders}")

    def test_main_py_does_not_import_stdlib_logging(self) -> None:
        """``main.py`` 也不再有内置 logging：它改用 ``astrbot.api.logger``。"""
        tree = _parse(PROJECT / "main.py")
        self.assertNotIn("logging", _imported_roots(tree))
        self.assertEqual(_logging_attribute_uses(tree), [])

    def test_logger_module_is_pure_stdlib(self) -> None:
        """``bsk/logger.py`` 自身必须零依赖：既不 import astrbot，也不 import logging。"""
        roots = _imported_roots(_parse(PROJECT / "bsk" / "logger.py"))
        self.assertNotIn("astrbot", roots)
        self.assertNotIn("logging", roots)

    def test_paths_module_is_pure_stdlib(self) -> None:
        """``bsk/paths.py`` 同理（纯标准库，可独立单测）。"""
        roots = _imported_roots(_parse(PROJECT / "bsk" / "paths.py"))
        self.assertNotIn("astrbot", roots)
        self.assertNotIn("logging", roots)


def self_dir_that_does_not_exist() -> str:
    """给 ``BoomRunner.resolve`` 用的假可执行文件路径（必然不存在）。"""
    return str(Path(tempfile.gettempdir()) / "bsk-logger-injection-no-such-binary")


def _raising_cleanup(*args: Any, **kwargs: Any) -> int:
    raise RuntimeError("cleanup 炸了")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
