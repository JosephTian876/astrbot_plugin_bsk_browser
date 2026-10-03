"""``bsk/session.py`` 的单元测试。

策略：**完全不碰真实 bsk**。用一个可编程的 ``FakeRunner`` 冒充 ``BskRunner``，
它同时负责：

- 记录每一次调用（``calls``）与参数，供断言"id 是位置参数""没用过 --all"；
- 统计 ``session start`` 次数，用来验证"并发只建一次""只重建一次"；
- 按命令名排队注入成功/失败结果（``not_found`` / ``session_busy`` /
  ``outcome_unknown`` 等），验证自动重建、忙重试、不确定态保护。

错误一律用 ``bsk.errors.classify()`` 构造，而不是手搓异常类 —— 这样测试同时
约束了"session.py 必须认识 classify 的产物"这一契约。

pytest 在本机不可用，因此用标准库 ``unittest.IsolatedAsyncioTestCase``
（Python 3.12 原生支持异步用例）。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 让测试能 import 到项目的 bsk 包（与 test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import errors  # noqa: E402
from bsk.errors import (  # noqa: E402
    BskError,
    BskOutcomeUnknown,
    BskSessionBusy,
    BskSessionGone,
)
from bsk.models import BrowserInstance, BskResult  # noqa: E402
from bsk.session import (  # noqa: E402
    BUSY_RETRY_DELAY_SEC,
    SessionManager,
)


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


def make_session_id(n: int) -> str:
    """生成第 n 个假会话 id，形如 ``mnaa`` / ``mnab``（4 个小写字母，与实测一致）。"""
    letters = "abcdefghijklmnopqrstuvwxyz"
    return "mn" + letters[(n // 26) % 26] + letters[n % 26]


@dataclass
class _Ok:
    """一次成功返回。``delay`` 用来模拟慢命令（竞态测试用）。"""

    data: Any = None
    delay: float = 0.0


@dataclass
class _Fail:
    """一次失败返回。字段与 ``errors.classify`` 的入参一一对应。"""

    code: str = ""
    reason: str = ""
    exit_code: int = errors.EXIT_USER_ERROR
    message: str = "fake failure"
    stderr: str = ""


def ok(data: Any = None, *, delay: float = 0.0) -> _Ok:
    """构造一个成功结果。"""
    return _Ok(data=data, delay=delay)


def fail(
    code: str = "",
    *,
    reason: str = "",
    exit_code: int = errors.EXIT_USER_ERROR,
    message: str = "fake failure",
    stderr: str = "",
) -> _Fail:
    """构造一个失败结果。"""
    return _Fail(
        code=code,
        reason=reason,
        exit_code=exit_code,
        message=message,
        stderr=stderr,
    )


class FakeRunner:
    """假的 ``BskRunner``：只实现 ``run_or_raise``，行为完全可编程。

    Args:
        session_ids: 指定 ``session start`` 依次返回的 id；不指定则自动生成。

    Attributes:
        calls: 每次调用的参数列表（原样记录）。
        start_count: ``session start`` 被调用的次数（**含失败的那次**）。
    """

    def __init__(self, session_ids: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.start_count = 0
        self.script: dict[str, list[Any]] = {}
        self._fixed_ids = list(session_ids or [])
        self._auto_n = 0

        # 在"真正在飞"的位置统计并发 —— 这直接对应 bsk 的硬约束：
        # 同一个 session 上同时只允许 1 条 in-flight 命令，否则回 session_busy。
        self.in_flight = 0
        self.peak_in_flight = 0
        self.session_overlap: list[str] = []
        self._active_session_ids: set[str] = set()

        # 竞态测试的同步点：让测试能确定性地等到"某条命令真的在飞"，
        # 而不是靠 sleep 一个魔数去赌调度顺序。
        self.command_started = asyncio.Event()

    # --- 编程接口 ---

    async def wait_until_in_flight(self, timeout: float = 2.0) -> None:
        """等到"已经有一条命令进入执行中"为止（确定性同步点）。"""
        await asyncio.wait_for(self.command_started.wait(), timeout)
        # 再让出一次控制权，确保命令确实进到了 sleep 里（在飞状态已建立）。
        await asyncio.sleep(0)

    def queue(self, command: str, *outcomes: Any) -> None:
        """给某个命令追加一串结果（按顺序消费，用完后回到"默认成功"）。"""
        self.script.setdefault(command, []).extend(outcomes)

    def calls_for(self, command: str) -> list[list[str]]:
        """取出某个命令的所有调用参数。"""
        return [c for c in self.calls if self._command_of(c) == command]

    def timeout_for(self, command: str, nth: int = 0) -> float | None:
        """取某个命令第 nth 次调用时用的超时。

        不能用 ``list.index()``：两次 observe 的参数可能**逐字节相同**，
        index 永远只会找到第一个。
        """
        matches = [
            i
            for i, c in enumerate(self.calls)
            if self._command_of(c) == command
        ]
        return self.timeouts[matches[nth]]

    def browser_instance_id_for(self, call: list[str]) -> str:
        """从参数里取 ``--browser`` 的值。"""
        return self._value_after(call, "--browser")

    def session_id_for(self, call: list[str]) -> str:
        """从参数里取 ``--session`` 的值（stop 除外，它是位置参数）。"""
        return self._value_after(call, "--session")

    # --- 内部 ---

    @staticmethod
    def _command_of(call: list[str]) -> str:
        """把 ``["session", "start", ...]`` 归一成 ``"session start"``。"""
        if not call:
            return ""
        if call[0] == "session" and len(call) > 1:
            return f"session {call[1]}"
        return call[0]

    @classmethod
    def _session_id_of_call(cls, call: list[str]) -> str:
        """从任意一条命令里抠出它操作的是哪个 session。"""
        if not call:
            return ""
        if call[0] == "session":
            # session stop <ID> 是位置参数；start 不带 session。
            if len(call) > 2 and call[1] == "stop" and not call[2].startswith("-"):
                return call[2]
            return ""
        return cls._value_after(call, "--session")

    @staticmethod
    def _value_after(call: list[str], flag: str) -> str:
        """取 ``flag`` 后面紧跟的值。"""
        if flag in call:
            idx = call.index(flag)
            if idx + 1 < len(call):
                return call[idx + 1]
        return ""

    def _next_session_id(self) -> str:
        """给出下一个会话 id。"""
        if self._auto_n < len(self._fixed_ids):
            sid = self._fixed_ids[self._auto_n]
        else:
            sid = make_session_id(self._auto_n)
        self._auto_n += 1
        return sid

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        """假装执行一条 bsk 命令。"""
        call = list(args)
        self.calls.append(call)
        self.timeouts.append(timeout)
        command = self._command_of(call)
        if command == "session start":
            self.start_count += 1

        queued = self.script.get(command)
        outcome: Any = queued.pop(0) if queued else _Ok()

        if isinstance(outcome, _Fail):
            # 走真实分类逻辑，保证抛出的正是 session.py 期待的那个子类。
            raise errors.classify(
                exit_code=outcome.exit_code,
                code=outcome.code,
                message=outcome.message,
                hint="",
                reason=outcome.reason,
                stderr=outcome.stderr,
            )

        assert isinstance(outcome, _Ok)

        # 记录"同一 session 上是否出现重叠调用"—— 这正是 bsk 会回 session_busy 的场景。
        session_id = self._session_id_of_call(call)
        if session_id:
            if session_id in self._active_session_ids:
                self.session_overlap.append(session_id)
            self._active_session_ids.add(session_id)

        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.command_started.set()
        try:
            if outcome.delay:
                await asyncio.sleep(outcome.delay)
        finally:
            self.in_flight -= 1
            if session_id:
                self._active_session_ids.discard(session_id)

        data = outcome.data
        if command == "session start" and data is None:
            data = self._start_payload()
        elif data is None and command == "observe":
            data = {
                "text": '@vom 1\nL1 page\n  RootWebArea "Example Domain"',
                "ref_count": 0,
                "tab_id": 1,
                "truncated": False,
            }
        return BskResult(ok=True, exit_code=0, data=data, elapsed=0.0)

    def _start_payload(self) -> dict[str, Any]:
        """构造 ``session start --json`` 的返回。"""
        return {
            "session_id": self._next_session_id(),
            "browser_instance_id": "c900a3da",
            "agent_window_id": 42,
            "interaction": {
                "borrow_confirmation": "prompt",
                "request_help": "allowed",
            },
        }


class FakeClock:
    """可手动推进的单调时钟，注入 ``SessionManager._clock``。

    用它验证 LRU 与空闲回收，**避免测试里真的 sleep**（慢且不稳定）。
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """把时钟向前拨 ``seconds`` 秒。"""
        self.now += seconds


def make_settings(**overrides: Any) -> types.SimpleNamespace:
    """造一个假 Settings（config.py 尚未落地，见模块文档的类型导入说明）。"""
    values: dict[str, Any] = {
        "max_sessions": 8,
        "idle_release_sec": 240.0,
        "browser_instance_id": "c900a3da",
        "command_timeout_sec": 60.0,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def observe_builder(session_id: str) -> list[str]:
    """标准的只读命令参数构造器。"""
    return ["observe", "--session", session_id, "--json"]


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    """公共脚手架：每个用例一个干净的 manager + fake runner + 假时钟。"""

    def setUp(self) -> None:
        self.runner = FakeRunner()
        self.clock = FakeClock()
        self.settings = make_settings()

    def make_manager(
        self, settings: types.SimpleNamespace | None = None, **kwargs: Any
    ) -> SessionManager:
        """构造被测对象，并把时钟换成假时钟。"""
        manager = SessionManager(
            self.runner, settings if settings is not None else self.settings, **kwargs
        )
        manager._clock = self.clock
        return manager


# ----------------------------------------------------------------------
# acquire / 会话复用
# ----------------------------------------------------------------------


class TestAcquire(SessionTestCase):
    """``acquire`` 的取或建语义。"""

    async def test_creates_session_on_first_call(self) -> None:
        manager = self.make_manager()
        session = await manager.acquire("umo-1")

        self.assertTrue(session.is_valid())
        self.assertEqual(session.session_id, "mnaa")
        self.assertEqual(session.agent_window_id, 42)
        self.assertEqual(self.runner.start_count, 1)

        start_calls = self.runner.calls_for("session start")
        self.assertEqual(len(start_calls), 1)
        # 必须是 --no-focus：不能让 bsk 抢用户焦点。
        self.assertIn("--no-focus", start_calls[0])
        self.assertIn("--json", start_calls[0])

    async def test_same_key_returns_same_session_without_second_start(self) -> None:
        manager = self.make_manager()
        first = await manager.acquire("umo-1")
        second = await manager.acquire("umo-1")

        self.assertIs(first, second)  # 同一个对象，不是副本
        self.assertEqual(self.runner.start_count, 1)

    async def test_different_keys_get_different_sessions(self) -> None:
        manager = self.make_manager()
        a = await manager.acquire("umo-a")
        b = await manager.acquire("umo-b")

        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(self.runner.start_count, 2)
        self.assertEqual(manager.stats()["sessions"], 2)

    async def test_concurrent_acquire_starts_exactly_once(self) -> None:
        """10 个协程同时 acquire 同一 key → 只能有 1 次 start。"""
        manager = self.make_manager()
        sessions = await asyncio.gather(*(manager.acquire("umo-1") for _ in range(10)))

        self.assertEqual(self.runner.start_count, 1)
        self.assertEqual(len(self.runner.calls_for("session start")), 1)
        for session in sessions:
            self.assertIs(session, sessions[0])
        self.assertEqual(sessions[0].session_id, "mnaa")

    async def test_concurrent_acquire_different_keys_start_each(self) -> None:
        """不同 key 并发时各建各的（锁是按 key 分的，不是全局一把）。"""
        manager = self.make_manager()
        keys = [f"umo-{i}" for i in range(5)]
        sessions = await asyncio.gather(*(manager.acquire(k) for k in keys))

        self.assertEqual(self.runner.start_count, 5)
        self.assertEqual(len({s.session_id for s in sessions}), 5)

    async def test_browser_probe_used_when_config_empty(self) -> None:
        probe_calls = 0

        async def probe() -> BrowserInstance:
            nonlocal probe_calls
            probe_calls += 1
            return BrowserInstance(instance_id="probed-01", browser_name="Chrome")

        manager = self.make_manager(
            make_settings(browser_instance_id=""), browser_probe=probe
        )
        await manager.acquire("umo-1")

        self.assertEqual(probe_calls, 1)
        start_call = self.runner.calls_for("session start")[0]
        self.assertEqual(self.runner.browser_instance_id_for(start_call), "probed-01")

    async def test_browser_probe_failure_falls_back_to_default(self) -> None:
        """探测炸了不能连累建会话 —— 退化成不传 --browser。"""

        async def boom() -> BrowserInstance:
            raise RuntimeError("bsk browsers 失败了")

        manager = self.make_manager(
            make_settings(browser_instance_id=""), browser_probe=boom
        )
        session = await manager.acquire("umo-1")

        self.assertTrue(session.is_valid())
        start_call = self.runner.calls_for("session start")[0]
        self.assertNotIn("--browser", start_call)

    async def test_missing_settings_attributes_fall_back_to_defaults(self) -> None:
        """config.py 还没写完时，空 settings 也必须能跑（全部走 getattr 兜底）。"""
        manager = self.make_manager(types.SimpleNamespace())
        session = await manager.acquire("umo-1")

        self.assertTrue(session.is_valid())
        self.assertEqual(manager.stats()["max_sessions"], 8)
        self.assertEqual(manager.stats()["idle_release_sec"], 240.0)


# ----------------------------------------------------------------------
# execute：自动重建 / 忙重试 / 不确定态
# ----------------------------------------------------------------------


class TestExecute(SessionTestCase):
    """``execute`` 的重试与保护逻辑。"""

    async def test_happy_path_uses_session_id(self) -> None:
        manager = self.make_manager()
        result = await manager.execute("umo-1", observe_builder)

        self.assertTrue(result.ok)
        observe_calls = self.runner.calls_for("observe")
        self.assertEqual(len(observe_calls), 1)
        self.assertEqual(self.runner.session_id_for(observe_calls[0]), "mnaa")
        self.assertEqual(self.runner.start_count, 1)

    async def test_timeout_default_comes_from_settings(self) -> None:
        manager = self.make_manager(make_settings(command_timeout_sec=7.5))
        await manager.execute("umo-1", observe_builder)
        self.assertEqual(self.runner.timeout_for("observe", 0), 7.5)

        await manager.execute("umo-1", observe_builder, timeout=3.0)
        self.assertEqual(self.runner.timeout_for("observe", 1), 3.0)

    async def test_async_args_builder_is_awaited(self) -> None:
        manager = self.make_manager()
        seen: list[str] = []

        async def builder(session_id: str) -> list[str]:
            seen.append(session_id)
            return ["observe", "--session", session_id, "--json"]

        await manager.execute("umo-1", builder)
        self.assertEqual(seen, ["mnaa"])

    async def test_args_builder_returning_non_list_raises_protocol_error(
        self,
    ) -> None:
        manager = self.make_manager()
        with self.assertRaises(BskError) as ctx:
            await manager.execute("umo-1", lambda sid: "observe")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "bad_args_builder")

    async def test_rebuild_on_not_found_and_retry_with_new_id(self) -> None:
        """★ 用失败触发重建：not_found → 重建 → 用**新 id** 重试一次。"""
        manager = self.make_manager()
        self.runner.queue("observe", fail("not_found"))

        result = await manager.execute("umo-1", observe_builder)

        self.assertTrue(result.ok)
        self.assertEqual(self.runner.start_count, 2)  # 首次 + 重建
        observe_calls = self.runner.calls_for("observe")
        self.assertEqual(len(observe_calls), 2)
        self.assertEqual(self.runner.session_id_for(observe_calls[0]), "mnaa")
        # ★ 重试用的是重建后的新 id，而不是把死 id 再发一次。
        self.assertEqual(self.runner.session_id_for(observe_calls[1]), "mnab")
        self.assertEqual(manager.stats()["counters"]["not_found_rebuilds"], 1)

    async def test_not_found_rebuild_does_not_stop_dead_session(self) -> None:
        """bsk 已确认会话不存在，就不该再 stop 一次（省一次往返，也避免误杀同 id 会话）。"""
        manager = self.make_manager()
        self.runner.queue("observe", fail("not_found"))
        await manager.execute("umo-1", observe_builder)

        self.assertEqual(self.runner.calls_for("session stop"), [])

    async def test_not_found_retries_only_once(self) -> None:
        """持续 not_found 时必须放弃，start 只多调用 1 次（绝不无限重试）。"""
        manager = self.make_manager()
        self.runner.queue("observe", *(fail("not_found") for _ in range(6)))

        with self.assertRaises(BskSessionGone):
            await manager.execute("umo-1", observe_builder)

        self.assertEqual(len(self.runner.calls_for("observe")), 2)
        self.assertEqual(self.runner.start_count, 2)  # 1 次初始 + 1 次重建

    async def test_session_busy_retries_once_then_succeeds(self) -> None:
        manager = self.make_manager()
        self.runner.queue("observe", fail("session_busy"))

        result = await manager.execute("umo-1", observe_builder)

        self.assertTrue(result.ok)
        observe_calls = self.runner.calls_for("observe")
        self.assertEqual(len(observe_calls), 2)
        # 忙重试是**同一条会话**上的重试，不该重建。
        self.assertEqual(self.runner.start_count, 1)
        self.assertEqual(
            self.runner.session_id_for(observe_calls[0]),
            self.runner.session_id_for(observe_calls[1]),
        )
        self.assertEqual(manager.stats()["counters"]["busy_retries"], 1)

    async def test_session_busy_retry_actually_waits(self) -> None:
        """★ 忙重试必须真的等一小会儿再发第二次，不能立刻连发。

        这里断言"两次 observe 之间至少隔了一个 BUSY_RETRY_DELAY_SEC 的**睡眠**"，
        用事件循环的时钟来量。**不能**直接量"execute 总耗时 >= 0.1"：Windows 上
        asyncio 的 ``_run_once`` 用 ``end_time = now + _clock_resolution`` 判定到期，
        定时器可能提前约 1ms 触发（实测偶发），那样断言的是事件循环的粒度而不是
        我们的代码。这里留一点余量，只验证"确实 sleep 了，而不是忙等重发"。

        Note:
            ``execute`` 内部还会**懒创建会话**（先跑一次 ``session start``），
            所以这里必须按命令名过滤，只取 ``observe`` 的两次调用；
            否则会把 start 也算进来，得到 3 个时间点。
        """
        manager = self.make_manager()
        started = asyncio.get_running_loop().time()
        observe_marks: list[float] = []
        original = manager._runner.run_or_raise

        async def timed(args, **kwargs):  # type: ignore[no-untyped-def]
            # 只记录 observe，跳过懒创建会话时的 session start。
            if args and args[0] == "observe":
                observe_marks.append(asyncio.get_running_loop().time() - started)
            return await original(args, **kwargs)

        manager._runner = types.SimpleNamespace(run_or_raise=timed)
        self.runner.queue("observe", fail("session_busy"))
        await manager.execute("umo-1", observe_builder)

        self.assertEqual(len(observe_marks), 2, "应当恰好 observe 两次（首次 + 忙重试一次）")
        gap = observe_marks[1] - observe_marks[0]
        # 容忍 Windows 定时器提前触发的粒度误差（约 1ms）。
        self.assertGreaterEqual(gap, BUSY_RETRY_DELAY_SEC - 0.005)

    async def test_session_busy_retries_only_once(self) -> None:
        manager = self.make_manager()
        self.runner.queue("observe", *(fail("session_busy") for _ in range(4)))

        with self.assertRaises(BskSessionBusy):
            await manager.execute("umo-1", observe_builder)

        self.assertEqual(len(self.runner.calls_for("observe")), 2)

    async def test_unknown_action_failure_is_not_retried(self) -> None:
        """普通失败（例如参数错误）不重试。"""
        manager = self.make_manager()
        self.runner.queue("observe", fail("", exit_code=errors.EXIT_BROWSER))

        with self.assertRaises(BskError):
            await manager.execute("umo-1", observe_builder)
        self.assertEqual(len(self.runner.calls_for("observe")), 1)

    async def test_outcome_unknown_marks_session_and_blocks_next_action(
        self,
    ) -> None:
        """★ 动作结果未知绝不重试；此后拒绝新的操作动作。"""
        manager = self.make_manager()
        session = await manager.acquire("umo-1")
        self.runner.queue(
            "observe",
            fail(reason="extension_reconnected", exit_code=errors.EXIT_BROWSER),
            ok(),
        )

        with self.assertRaises(BskOutcomeUnknown):
            await manager.execute("umo-1", observe_builder)

        self.assertTrue(session.uncertain)
        self.assertEqual(len(self.runner.calls_for("observe")), 1)  # 没有重试

        # 后续操作动作被拒（连命令都没发出去）。
        with self.assertRaises(BskOutcomeUnknown) as ctx:
            await manager.execute("umo-1", observe_builder)
        self.assertEqual(ctx.exception.code, "session_uncertain")
        self.assertEqual(len(self.runner.calls_for("observe")), 1)
        self.assertEqual(manager.stats()["uncertain_count"], 1)
        self.assertEqual(manager.stats()["counters"]["uncertain_blocks"], 1)

    async def test_allow_uncertain_permits_readonly_command(self) -> None:
        """只读动作显式声明 allow_uncertain=True 时可以继续。"""
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.runner.queue(
            "observe",
            fail(reason="extension_reconnected", exit_code=errors.EXIT_BROWSER),
        )

        with self.assertRaises(BskOutcomeUnknown):
            await manager.execute("umo-1", observe_builder)

        result = await manager.execute(
            "umo-1", observe_builder, allow_uncertain=True
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(self.runner.calls_for("observe")), 2)

    async def test_outcome_unknown_with_custom_code_is_still_blocked(self) -> None:
        """带 outcome_unknown reason 但 code 是别的，也必须按不可重试处理。"""
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.runner.queue(
            "observe", fail("weird_code", reason="input_outcome_unknown")
        )

        with self.assertRaises(BskOutcomeUnknown):
            await manager.execute("umo-1", observe_builder)
        self.assertEqual(len(self.runner.calls_for("observe")), 1)

    async def test_release_clears_uncertain_state(self) -> None:
        """重建会话后不确定态应清除（新会话是干净状态）。"""
        manager = self.make_manager()
        session = await manager.acquire("umo-1")
        self.runner.queue(
            "observe",
            fail(reason="extension_reconnected", exit_code=errors.EXIT_BROWSER),
        )
        with self.assertRaises(BskOutcomeUnknown):
            await manager.execute("umo-1", observe_builder)
        self.assertTrue(session.uncertain)

        self.assertTrue(await manager.release("umo-1"))
        again = await manager.acquire("umo-1")
        self.assertFalse(again.uncertain)
        self.assertNotEqual(again.session_id, "mnaa")

    async def test_same_key_execute_is_serialized(self) -> None:
        """★ 同 key 的 execute 严格串行 —— bsk 侧才不会 session_busy。

        并发度由 FakeRunner 在"真正在飞"的位置统计（同一 session 上重叠 = bsk
        会回 session_busy 的场景），而不是在进入 execute 前后计数。
        """
        manager = self.make_manager()
        self.runner.queue("observe", *(ok(delay=0.03) for _ in range(5)))

        await asyncio.gather(
            *(manager.execute("umo-1", observe_builder) for _ in range(5))
        )

        self.assertEqual(self.runner.peak_in_flight, 1)
        self.assertEqual(self.runner.session_overlap, [])  # 无任何重叠
        self.assertEqual(self.runner.start_count, 1)
        self.assertEqual(len(self.runner.calls_for("observe")), 5)

    async def test_different_keys_execute_concurrently(self) -> None:
        """不同 key 不被同一把全局锁串行化（否则多用户会互相拖慢）。"""
        manager = self.make_manager()
        keys = ("umo-a", "umo-b", "umo-c")
        for _ in keys:
            self.runner.queue("observe", ok(delay=0.05))

        await asyncio.gather(*(manager.execute(k, observe_builder) for k in keys))

        self.assertGreater(self.runner.peak_in_flight, 1)
        self.assertEqual(self.runner.session_overlap, [])  # 重叠的是不同 session


# ----------------------------------------------------------------------
# release / release_all / close
# ----------------------------------------------------------------------


class TestRelease(SessionTestCase):
    """显式停止 —— 这是唯一能保证标签页被归还的手段。"""

    async def test_release_uses_positional_session_id(self) -> None:
        """★ `session stop <ID>` 的 id 是位置参数，绝不能用 --session。"""
        manager = self.make_manager()
        await manager.acquire("umo-1")

        self.assertTrue(await manager.release("umo-1"))

        stop_calls = self.runner.calls_for("session stop")
        self.assertEqual(stop_calls, [["session", "stop", "mnaa"]])
        self.assertNotIn("--session", stop_calls[0])
        self.assertNotIn("--json", stop_calls[0])
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_never_uses_stop_all(self) -> None:
        """★ 任何路径都不许出现 `session stop --all`（会停掉别的程序的会话）。"""
        manager = self.make_manager()
        for i in range(3):
            await manager.acquire(f"umo-{i}")
        await manager.release("umo-0")
        await manager.reap_idle()
        await manager.release_all()

        for call in self.runner.calls:
            self.assertNotIn("--all", call, f"出现了 --all：{call}")

    async def test_release_does_not_corrupt_held_handle(self) -> None:
        """release 之后，调用方手里的 session 对象仍应保留它的 id。

        回归测试：曾经 stop 成功后会顺手把 ``session.session_id`` 清空，
        结果把 ``acquire()`` 交给调用方的句柄一起毁掉了（/bskstatus 和日志
        都要读它）。
        """
        manager = self.make_manager()
        session = await manager.acquire("umo-1")
        await manager.release("umo-1")

        self.assertEqual(session.session_id, "mnaa")
        self.assertEqual(
            self.runner.calls_for("session stop"), [["session", "stop", "mnaa"]]
        )

    async def test_failed_start_leaves_no_stale_id(self) -> None:
        """start 失败后槽位不能留着假 id，否则下次会拿死 id 去撞 not_found。"""
        manager = self.make_manager()
        self.runner.queue(
            "session start",
            fail("", exit_code=errors.EXIT_BROWSER),
            ok(),  # 第二次 start 成功
        )
        with self.assertRaises(BskError):
            await manager.acquire("umo-1")

        session = await manager.acquire("umo-1")  # 应该真的重建
        self.assertTrue(session.is_valid())
        self.assertEqual(self.runner.start_count, 2)
        result = await manager.execute("umo-1", observe_builder)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.runner.calls_for("observe")), 1)  # 没多撞一次

    async def test_release_unknown_key_returns_false(self) -> None:
        manager = self.make_manager()
        self.assertFalse(await manager.release("nope"))
        self.assertEqual(self.runner.calls_for("session stop"), [])

    async def test_release_placeholder_without_real_session(self) -> None:
        """占位槽位（start 失败过）没有真实会话，release 不该发 stop。"""
        manager = self.make_manager()
        self.runner.queue("session start", fail("", exit_code=errors.EXIT_BROWSER))
        with self.assertRaises(BskError):
            await manager.acquire("umo-1")

        self.assertTrue(await manager.release("umo-1"))
        self.assertEqual(self.runner.calls_for("session stop"), [])
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_release_survives_stop_failure(self) -> None:
        """stop 失败也必须把本地记录删掉，否则内存里永远留着死会话。"""
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.runner.queue("session stop", fail("", exit_code=errors.EXIT_BROWSER))

        self.assertFalse(await manager.release("umo-1"))  # 没关成功
        self.assertEqual(manager.stats()["sessions"], 0)  # 但记录已清干净
        self.assertEqual(manager.stats()["counters"]["stop_failed"], 1)
        self.assertTrue(manager.stats()["recent_stop_errors"])

    async def test_release_treats_not_found_as_already_closed(self) -> None:
        """bsk 说会话早就没了 —— 目的已达成，算关成功。"""
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.runner.queue("session stop", fail("not_found"))

        self.assertTrue(await manager.release("umo-1"))

    async def test_stop_exception_never_escapes(self) -> None:
        """stop 抛任意异常（不只是 BskError）也不能逃出 release。"""

        class ExplodingRunner(FakeRunner):
            async def run_or_raise(self, args, **kwargs):  # type: ignore[no-untyped-def]
                if list(args)[:2] == ["session", "stop"]:
                    self.calls.append(list(args))
                    raise RuntimeError("管道炸了")
                return await super().run_or_raise(args, **kwargs)

        self.runner = ExplodingRunner()
        manager = self.make_manager()
        await manager.acquire("umo-1")

        self.assertFalse(await manager.release("umo-1"))
        self.assertEqual(manager.stats()["sessions"], 0)


class TestReleaseAll(SessionTestCase):
    """``release_all`` 是 terminate() 的落地路径，必须绝对稳。"""

    async def test_release_all_stops_every_session(self) -> None:
        manager = self.make_manager()
        for i in range(3):
            await manager.acquire(f"umo-{i}")

        self.assertEqual(await manager.release_all(), 3)
        self.assertEqual(len(self.runner.calls_for("session stop")), 3)
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_release_all_tolerates_partial_failure(self) -> None:
        """★ 部分 stop 失败时：不抛异常、全部本地记录清空、计数正确。"""
        manager = self.make_manager()
        for i in range(3):
            await manager.acquire(f"umo-{i}")
        self.runner.queue(
            "session stop",
            fail("", exit_code=errors.EXIT_BROWSER),
            fail("", exit_code=errors.EXIT_PROTOCOL),
        )

        released = await manager.release_all()  # 不抛异常

        self.assertEqual(released, 1)  # 3 个里只有 1 个确认关掉
        self.assertEqual(manager.stats()["sessions"], 0)  # 但记录全清了
        self.assertEqual(len(self.runner.calls_for("session stop")), 3)
        self.assertEqual(manager.stats()["counters"]["stop_failed"], 2)

    async def test_release_all_is_idempotent(self) -> None:
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.assertEqual(await manager.release_all(), 1)
        self.assertEqual(await manager.release_all(), 0)
        self.assertEqual(len(self.runner.calls_for("session stop")), 1)

    async def test_close_releases_everything_and_refuses_new_work(self) -> None:
        manager = self.make_manager()
        await manager.acquire("umo-1")
        await manager.close()

        self.assertEqual(len(self.runner.calls_for("session stop")), 1)
        self.assertTrue(manager.stats()["closed"])
        with self.assertRaises(BskError) as ctx:
            await manager.acquire("umo-2")
        self.assertEqual(ctx.exception.code, "manager_closed")
        with self.assertRaises(BskError) as ctx:
            await manager.execute("umo-2", observe_builder)
        self.assertEqual(ctx.exception.code, "manager_closed")

    async def test_close_survives_stop_failure_and_is_idempotent(self) -> None:
        manager = self.make_manager()
        await manager.acquire("umo-1")
        self.runner.queue("session stop", fail("", exit_code=errors.EXIT_BROWSER))

        await manager.close()  # 不抛
        await manager.close()  # 幂等，也不抛
        self.assertEqual(manager.stats()["sessions"], 0)


# ----------------------------------------------------------------------
# LRU 淘汰与空闲回收
# ----------------------------------------------------------------------


class TestEvictionAndReap(SessionTestCase):
    """容量与空闲两条回收路径。"""

    async def test_lru_eviction_stops_oldest(self) -> None:
        """★ max_sessions=2 时建 3 个 → 最久未使用的那个被 stop 掉。"""
        manager = self.make_manager(make_settings(max_sessions=2))

        first = await manager.acquire("umo-a")
        self.clock.advance(1)
        second = await manager.acquire("umo-b")
        self.clock.advance(1)
        third = await manager.acquire("umo-c")

        self.assertEqual(self.runner.calls_for("session stop"), [
            ["session", "stop", first.session_id]
        ])
        details = manager.stats()["details"]
        self.assertEqual(len(details), 2)
        keys = {d["key"] for d in details}
        self.assertEqual(keys, {"umo-b", "umo-c"})
        self.assertEqual(manager.stats()["counters"]["evicted"], 1)
        # 后两个会话仍然可用。
        self.assertNotEqual(second.session_id, third.session_id)

    async def test_lru_uses_recency_not_creation_order(self) -> None:
        """刚被用过的老会话不能被淘汰 —— 淘汰依据是 last_used。"""
        manager = self.make_manager(make_settings(max_sessions=2))
        a = await manager.acquire("umo-a")
        self.clock.advance(1)
        await manager.acquire("umo-b")

        self.clock.advance(1)
        await manager.acquire("umo-a")  # 刷新 a 的活跃时间
        self.clock.advance(1)
        await manager.acquire("umo-c")  # 该淘汰 b 了

        stopped_ids = [c[2] for c in self.runner.calls_for("session stop")]
        self.assertEqual(stopped_ids, ["mnab"])  # mnab 是 umo-b
        self.assertIn("umo-a", {d["key"] for d in manager.stats()["details"]})
        self.assertEqual(a.session_id, "mnaa")

    async def test_reap_idle_only_reaps_stale_sessions(self) -> None:
        """★ 只回收空闲超时的，新鲜的不动。"""
        manager = self.make_manager(make_settings(idle_release_sec=60.0))
        stale = await manager.acquire("umo-stale")
        self.clock.advance(90)  # stale 空闲 90s > 60s
        fresh = await manager.acquire("umo-fresh")

        reaped = await manager.reap_idle()

        self.assertEqual(reaped, 1)
        self.assertEqual(manager.stats()["counters"]["reaped"], 1)
        self.assertEqual(
            self.runner.calls_for("session stop"), [["session", "stop", stale.session_id]]
        )
        self.assertEqual(
            {d["key"] for d in manager.stats()["details"]}, {"umo-fresh"}
        )
        self.assertEqual(fresh.session_id, "mnab")

    async def test_reap_idle_keeps_fresh_sessions(self) -> None:
        manager = self.make_manager(make_settings(idle_release_sec=60.0))
        await manager.acquire("umo-1")
        self.clock.advance(30)  # 还没到期

        self.assertEqual(await manager.reap_idle(), 0)
        self.assertEqual(self.runner.calls_for("session stop"), [])

    async def test_reap_idle_uses_last_used_after_execute(self) -> None:
        """execute 会刷新活跃时间，长时间不被操作的会话才会被回收。"""
        manager = self.make_manager(make_settings(idle_release_sec=60.0))
        await manager.acquire("umo-1")
        self.clock.advance(50)
        await manager.execute("umo-1", observe_builder)  # 刷新

        self.assertEqual(await manager.reap_idle(), 0)
        self.clock.advance(61)  # 距最后一次使用 61s > 60s
        self.assertEqual(await manager.reap_idle(), 1)

    async def test_reap_idle_skips_busy_session(self) -> None:
        """正在跑命令的会话不算空闲，不能被打断（否则长截图会被砍掉）。"""
        manager = self.make_manager(make_settings(idle_release_sec=0.05))
        self.runner.queue("observe", ok(delay=0.2))

        task = asyncio.create_task(manager.execute("umo-1", observe_builder))
        await self.runner.wait_until_in_flight()  # 确定命令已在飞
        self.clock.advance(100)

        self.assertEqual(await manager.reap_idle(), 0)
        self.assertTrue((await task).ok)
        self.assertEqual(manager.stats()["sessions"], 1)

    async def test_reap_idle_disabled_when_non_positive(self) -> None:
        manager = self.make_manager(make_settings(idle_release_sec=0))
        await manager.acquire("umo-1")
        self.clock.advance(10_000)

        self.assertEqual(await manager.reap_idle(), 0)
        self.assertEqual(manager.stats()["sessions"], 1)

    async def test_reap_idle_then_reacquire_builds_new_session(self) -> None:
        """被回收后再 acquire 应该建一个新会话，而不是复用死 id。"""
        manager = self.make_manager(make_settings(idle_release_sec=60.0))
        first = await manager.acquire("umo-1")
        self.clock.advance(90)
        await manager.reap_idle()

        second = await manager.acquire("umo-1")
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(self.runner.start_count, 2)


# ----------------------------------------------------------------------
# 竞态
# ----------------------------------------------------------------------


class TestRace(SessionTestCase):
    """并发路径的健壮性：宁可命令失败，也不能崩、不能泄漏。"""

    async def test_release_all_during_execute_does_not_crash(self) -> None:
        """★ 一个协程在 execute 时另一个调 release_all：不崩。"""
        manager = self.make_manager()
        self.runner.queue("observe", ok(delay=0.15))

        task = asyncio.create_task(manager.execute("umo-1", observe_builder))
        await self.runner.wait_until_in_flight()  # 确定性等到命令真的在飞

        released = await manager.release_all()
        result = await task  # 不抛异常

        self.assertTrue(result.ok)
        self.assertEqual(released, 1)
        # 在飞命令结束后，会话被显式停掉，本地不留残余。
        self.assertEqual(manager.stats()["sessions"], 0)
        self.assertEqual(len(self.runner.calls_for("session stop")), 1)

    async def test_release_during_execute_does_not_crash(self) -> None:
        manager = self.make_manager()
        self.runner.queue("observe", ok(delay=0.12))

        task = asyncio.create_task(manager.execute("umo-1", observe_builder))
        await self.runner.wait_until_in_flight()
        released = await manager.release("umo-1")
        result = await task

        self.assertTrue(result.ok)
        self.assertTrue(released)
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_start_landing_after_release_is_stopped_not_leaked(self) -> None:
        """★ start 慢于 release 的窄窗口：新会话必须被停掉，不能变成孤儿。

        模拟：一条 execute 正在建会话（start 要 0.2s），此时另一个协程把 key
        release 掉。该 start 的结果已经无人接管，必须立刻 stop —— 否则它会一直
        占用用户的浏览器（bsk 5 分钟后才回收，且回收不保证归还借用的标签页）。
        """
        manager = self.make_manager()
        self.runner.queue("session start", ok(delay=0.2))

        task = asyncio.create_task(manager.execute("umo-1", observe_builder))
        await self.runner.wait_until_in_flight()  # 此刻 start 正在飞
        # release 会最多等 RELEASE_WAIT_SEC（5s），足够覆盖这 0.2s。
        released = await manager.release("umo-1")

        with self.assertRaises(BskError) as ctx:
            await task
        self.assertEqual(ctx.exception.code, "session_released")

        self.assertTrue(released)
        # 那个已经建出来的会话必须被显式停掉。
        stop_calls = self.runner.calls_for("session stop")
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0][0:2], ["session", "stop"])
        self.assertTrue(stop_calls[0][2])
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_execute_after_release_all_gets_fresh_session(self) -> None:
        """release_all 不是 close：之后还能正常开工（不崩、不复活死 id）。"""
        manager = self.make_manager()
        first = await manager.acquire("umo-1")
        await manager.release_all()

        result = await manager.execute("umo-1", observe_builder)

        self.assertTrue(result.ok)
        observe_calls = self.runner.calls_for("observe")
        self.assertEqual(self.runner.session_id_for(observe_calls[0]), "mnab")
        self.assertNotEqual(
            self.runner.session_id_for(observe_calls[0]), first.session_id
        )

    async def test_many_concurrent_executes_release_and_reap(self) -> None:
        """压力：多 key 并发 execute + 同时 release/reap 混跑，全部不能崩。

        这里只看**不变量**，不断言具体成功数（混跑结果本来就是竞态相关的）：
        不许抛未处理的异常、不许有会话重叠、结束后不许残留"已死但在册"的会话。
        """
        manager = self.make_manager(make_settings(max_sessions=3))
        keys = [f"umo-{i}" for i in range(6)]

        async def worker(key: str) -> Any:
            try:
                return await manager.execute(
                    key, observe_builder, allow_uncertain=True
                )
            except BskError as exc:  # 会话被回收/释放导致的失败是可接受的
                return exc

        async def churn() -> None:
            for i in range(12):
                await asyncio.sleep(0.005)
                self.clock.advance(30)
                await manager.reap_idle()
                if i % 4 == 0:
                    await manager.release(keys[i % len(keys)])

        results = await asyncio.gather(
            *(worker(k) for k in keys), churn(), return_exceptions=False
        )

        self.assertEqual(len(results), 7)
        # 没有未处理的异常逃出来（gather 会直接抛，这里到不了说明没炸）。
        self.assertEqual(self.runner.session_overlap, [])  # 同 session 无并发
        # 最终状态：在册的会话都必须有真实 id。
        for detail in manager.stats()["details"]:
            self.assertTrue(detail["session_id"])
        # 结束前必须能干净收尾，且每个在册会话恰好被 stop 一次。
        remaining = manager.stats()["sessions"]
        stopped_before = len(self.runner.calls_for("session stop"))
        await manager.release_all()
        self.assertEqual(len(self.runner.calls_for("session stop")) - stopped_before, remaining)
        self.assertEqual(manager.stats()["sessions"], 0)

    async def test_concurrent_release_all_calls_are_safe(self) -> None:
        manager = self.make_manager()
        for i in range(3):
            await manager.acquire(f"umo-{i}")

        counts = await asyncio.gather(
            manager.release_all(), manager.release_all(), manager.release_all()
        )
        self.assertEqual(sum(counts), 3)  # 每个会话只被 stop 一次
        self.assertEqual(len(self.runner.calls_for("session stop")), 3)

    async def test_stop_recorded_once_per_session(self) -> None:
        """并发 acquire/execute 之后 release_all，每个 session 恰好 stop 一次。"""
        manager = self.make_manager()

        async def worker(i: int) -> None:
            await manager.acquire(f"umo-{i}")
            await manager.execute(f"umo-{i}", observe_builder)

        await asyncio.gather(*(worker(i) for i in range(5)))
        await manager.release_all()

        stopped = [c[2] for c in self.runner.calls_for("session stop")]
        self.assertEqual(len(stopped), 5)
        self.assertEqual(len(set(stopped)), 5)  # 无重复 stop


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
