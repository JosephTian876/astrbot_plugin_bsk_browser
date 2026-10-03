"""``bsk/service.py`` 的单元测试 —— 重点是**超时合成规则**。

背景（这次要修的**真实设计缺陷**）：``service.py`` 曾经在调用处直接写
``timeout=TIMEOUT_OBSERVE`` 这样的硬编码值，用户在插件配置里填的
``command_timeout_sec`` 只影响 ``session start`` 那一条命令，其余命令完全不理会它。
后果是"把超时从 60 调到 110 一点用都没有"，而且 README 不得不写一段绕口的话来解释。

现在的语义只有**一条规则**::

    最终超时 = max(该命令的内置下限建议值, settings.command_timeout_sec)

- 内置值是"这条命令**至少**需要多久"（例如 ``navigate`` 是 45 秒，必须大于 bsk
  自身的 ``--timeout`` 30 秒，否则我们会先把它掐掉）；
- 用户值是"我愿意等多久"；
- 取较大值，两者都不会被违背。

本文件因此覆盖三类断言：

1. **规则本身**（``_timeout`` 的纯函数行为），含两条边界保护：
   用户调到最小 5 秒时 ``navigate`` 仍然是 45；用户调到最大 110 秒时
   ``TIMEOUT_FULLPAGE``（180）不被压低。
2. **防御性**：配置对象缺少 ``command_timeout_sec`` 属性时回退到内置值，
   不许抛 ``AttributeError``，也不许把超时算成 0（那会把命令掐死在起点）。
3. **集成式**：用一个假 runner **记录实际传给子进程的 timeout**，验证
   ``service.observe()`` 等用例传下去的是**合成后的值**，而不是硬编码的 15。

假 runner 的写法参考 ``tests/test_session.py`` 的 ``FakeRunner``（那个文件不动），
但这里用**真实的** ``SessionManager``，这样 ``service → session → runner`` 整条
超时透传链路都被覆盖，而不是只测了 service 自己那一层。

pytest 在本机不可用（实测 ``ModuleNotFoundError``），所以用标准库 ``unittest``；
异步用例用 ``unittest.IsolatedAsyncioTestCase``（Python 3.12 原生支持）。

运行：``python -m unittest tests.test_service -v``
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

# 让测试能 import 到项目的 bsk 包（与 tests/test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.config import (  # noqa: E402
    COMMAND_TIMEOUT_MAX_SEC,
    COMMAND_TIMEOUT_MIN_SEC,
    parse_settings,
)
from bsk.models import BskResult  # noqa: E402
from bsk.service import (  # noqa: E402
    TIMEOUT_ACTION,
    TIMEOUT_FULLPAGE,
    TIMEOUT_NAVIGATE,
    TIMEOUT_OBSERVE,
    TIMEOUT_QUICK,
    TIMEOUT_SCREENSHOT,
    BskService,
)

# 被测的全部内置下限建议值。新增一个 TIMEOUT_* 常量时**必须**加进来，
# 否则下面的"单一规则"遍历测试就覆盖不到它。
ALL_BUILTIN_TIMEOUTS: tuple[float, ...] = (
    TIMEOUT_QUICK,
    TIMEOUT_OBSERVE,
    TIMEOUT_ACTION,
    TIMEOUT_NAVIGATE,
    TIMEOUT_SCREENSHOT,
    TIMEOUT_FULLPAGE,
)


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class FakeRunner:
    """假的 ``BskRunner``：记录每次调用的参数与超时，永不真的起子进程。

    Attributes:
        calls: 每次调用的参数列表（原样记录）。
        timeouts: 与 ``calls`` 一一对应的 ``timeout`` 实参。
        resolved: ``resolve()`` 的返回值。
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.resolved = r"C:\fake\bsk.exe"

    # --- 断言辅助 ---

    def resolve(self) -> str:
        """冒充"找到 bsk 可执行文件"这一步。"""
        return self.resolved

    @staticmethod
    def _command_of(call: list[str]) -> str:
        """把 ``["session", "start", ...]`` 归一成 ``"session start"``。"""
        if not call:
            return ""
        if call[0] == "session" and len(call) > 1:
            return f"session {call[1]}"
        return call[0]

    def calls_for(self, command: str) -> list[list[str]]:
        """取出某个命令的所有调用参数。"""
        return [c for c in self.calls if self._command_of(c) == command]

    def timeout_for(self, command: str, nth: int = 0) -> float | None:
        """取某个命令第 nth 次调用时实际传入的超时。

        不能用 ``list.index()``：两次 ``observe`` 的参数可能**逐字节相同**，
        index 永远只会找到第一个。
        """
        matches = [i for i, c in enumerate(self.calls) if self._command_of(c) == command]
        return self.timeouts[matches[nth]]

    # --- 执行 ---

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        """假装执行一条 bsk 命令，返回各命令"最小可用"的成功载荷。"""
        call = list(args)
        self.calls.append(call)
        self.timeouts.append(timeout)
        command = self._command_of(call)

        if command == "session start":
            data: Any = {"session_id": "mnaa", "browser_instance_id": "c900a3da"}
        elif command == "observe":
            data = {
                "text": '@vom 1\nL1 page\n  RootWebArea "Example Domain"',
                "ref_count": 0,
                "tab_id": 1,
                "truncated": False,
            }
        elif command == "navigate":
            data = {"url": "https://example.com", "final_url": "https://example.com/"}
        else:
            # 其余命令给空 dict：from_json 都容忍缺字段。
            data = {}
        return BskResult(ok=True, exit_code=0, data=data, elapsed=0.0)


def make_settings(**overrides: Any) -> Any:
    """造一份**真实的** ``Settings``（走 ``parse_settings``，含夹取）。

    用真类型而不是 ``SimpleNamespace``，是为了让"配置对象字段名漂移"这类
    回归能被测出来（见 ``test_works_with_real_settings_type``）。
    """
    raw: dict[str, Any] = {
        "bsk_path": "bsk",
        # 显式指定浏览器，避免触发 probe_browser 里的同步子进程探测。
        "browser_instance_id": "c900a3da",
        "command_timeout_sec": 60.0,
    }
    raw.update(overrides)
    return parse_settings(raw)


def make_service(settings: Any, runner: FakeRunner | None = None) -> tuple[BskService, FakeRunner]:
    """构造被测服务，注入假 runner（真实 SessionManager）。"""
    fake = runner if runner is not None else FakeRunner()
    return BskService(settings, runner=fake), fake


# ----------------------------------------------------------------------
# 1. 规则本身：max(内置下限, 用户配置)
# ----------------------------------------------------------------------


class TestTimeoutRule(unittest.TestCase):
    """``BskService._timeout`` 的合成规则。"""

    def test_builtin_wins_when_larger(self) -> None:
        """★ 内置值大于用户值时取内置：用户设 10，observe 仍是 15。"""
        service, _ = make_service(make_settings(command_timeout_sec=10.0))

        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), TIMEOUT_OBSERVE)
        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), 15.0)

    def test_user_wins_when_larger(self) -> None:
        """★ 用户值大于内置值时取用户：用户设 100，navigate 变成 100。"""
        service, _ = make_service(make_settings(command_timeout_sec=100.0))

        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 100.0)
        self.assertNotEqual(service._timeout(TIMEOUT_NAVIGATE), TIMEOUT_NAVIGATE)

    def test_min_user_value_still_keeps_navigate_floor(self) -> None:
        """★ 最关键的一条：用户设最小值 5，``navigate`` 的下限保护仍然生效。

        这正是老缺陷的反面：以前用户调到 5 也还是 45（但那是硬编码，与配置无关）；
        现在 45 是**明确的下限语义** —— bsk 自身的 ``--timeout`` 是 30 秒，
        我们绝不能比它先超时，否则会把它正要成功返回的命令掐掉。
        """
        service, _ = make_service(
            make_settings(command_timeout_sec=COMMAND_TIMEOUT_MIN_SEC)
        )

        self.assertEqual(COMMAND_TIMEOUT_MIN_SEC, 5.0)
        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 45.0)

    def test_max_user_value_against_slow_and_fast_commands(self) -> None:
        """★ 用户设最大值 110：全页截图 180 不被压低；observe 被抬到 110。"""
        service, _ = make_service(
            make_settings(command_timeout_sec=COMMAND_TIMEOUT_MAX_SEC)
        )

        self.assertEqual(COMMAND_TIMEOUT_MAX_SEC, 110.0)
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 180.0)
        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), 110.0)

    def test_is_exactly_max_for_every_builtin(self) -> None:
        """★ 只有**一条**规则：对每个内置值与若干用户值都恰好等于 ``max()``。

        这条测试是"别把规则改成快命令取 min、慢命令取 max"的护栏 ——
        那种复杂规则对新手无法解释，正是这次要消灭的东西。
        """
        for user_value in (5.0, 10.0, 30.0, 45.0, 60.0, 110.0):
            service, _ = make_service(
                make_settings(command_timeout_sec=user_value)
            )
            for builtin in ALL_BUILTIN_TIMEOUTS:
                with self.subTest(user=user_value, builtin=builtin):
                    self.assertEqual(
                        service._timeout(builtin), max(builtin, user_value)
                    )

    def test_builtin_is_a_true_floor_for_all_commands(self) -> None:
        """任何用户取值都不能让最终超时**低于**内置下限。"""
        service, _ = make_service(
            make_settings(command_timeout_sec=COMMAND_TIMEOUT_MIN_SEC)
        )
        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertGreaterEqual(service._timeout(builtin), builtin)

    def test_default_user_value_raises_only_the_short_commands(self) -> None:
        """默认 60 秒下的具体表现：短命令被抬到 60，长命令保留下限。"""
        service, _ = make_service(make_settings(command_timeout_sec=60.0))

        self.assertEqual(service._timeout(TIMEOUT_QUICK), 60.0)
        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), 60.0)
        self.assertEqual(service._timeout(TIMEOUT_ACTION), 60.0)
        self.assertEqual(service._timeout(TIMEOUT_SCREENSHOT), 60.0)
        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 60.0)
        # 只有全页截图的下限高过 60。
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 180.0)

    def test_returns_float(self) -> None:
        """返回值必须是 float —— 它会被直接传给子进程超时参数。"""
        service, _ = make_service(make_settings(command_timeout_sec=70.0))
        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertIsInstance(service._timeout(builtin), float)

    def test_integral_user_value_keeps_floor_intact(self) -> None:
        """用户填的是 int（JSON 里 60 和 60.0 长得一样）时也不出错。"""
        service, _ = make_service(make_settings(command_timeout_sec=100))
        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 100.0)


# ----------------------------------------------------------------------
# 2. 防御性：配置对象不正常也不能崩、不能变成 0
# ----------------------------------------------------------------------


class TestTimeoutDefensive(unittest.TestCase):
    """``_timeout`` 必须对任何"长得像配置"的对象都安全。"""

    def _service_with_raw_settings(self, settings: Any) -> BskService:
        """注入假 runner，这样 ``__init__`` 不会去碰 ``settings.bsk_path``。"""
        return BskService(settings, runner=FakeRunner())

    def test_missing_attribute_falls_back_to_builtin(self) -> None:
        """★ 缺 ``command_timeout_sec`` 属性时回退到内置值，不抛异常。"""
        service = self._service_with_raw_settings(types.SimpleNamespace())

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(service._timeout(builtin), builtin)

    def test_settings_is_none_falls_back_to_builtin(self) -> None:
        """整个 settings 是 ``None`` 也一样（``__init__`` 里不读配置字段）。"""
        service = BskService(None, runner=FakeRunner())  # type: ignore[arg-type]
        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), TIMEOUT_OBSERVE)

    def test_junk_values_fall_back_to_builtin(self) -> None:
        """★ 属性存在但取值荒唐（None/字符串/布尔/nan/inf/0/负数）时回退到内置值。

        重点是**不能变成 0**：超时 0 会让每条命令一启动就被掐死，
        那比"用内置下限"糟糕得多。
        """
        junk_values: tuple[Any, ...] = (
            None,
            "60",  # 正常入口不会出现，但手写配置/旧对象可能有
            True,  # bool 是 int 的子类，很容易被 isinstance 放过
            False,
            float("nan"),  # nan 参与 max() 的结果不可靠
            float("inf"),  # inf 等于"永不超时"
            0,
            -5.0,
            object(),
        )
        for junk in junk_values:
            service = self._service_with_raw_settings(
                types.SimpleNamespace(command_timeout_sec=junk)
            )
            for builtin in ALL_BUILTIN_TIMEOUTS:
                with self.subTest(junk=repr(junk), builtin=builtin):
                    result = service._timeout(builtin)
                    self.assertEqual(result, builtin)
                    self.assertGreater(result, 0)

    def test_never_raises(self) -> None:
        """兜底：无论塞什么进去，``_timeout`` 都不许抛异常。

        它跑在每个工具的调用路径上，崩了就等于所有浏览器工具全废。
        """
        for junk in (None, "", [], {}, object(), True, float("nan")):
            service = self._service_with_raw_settings(
                types.SimpleNamespace(command_timeout_sec=junk)
            )
            with self.subTest(junk=repr(junk)):
                self.assertIsInstance(service._timeout(TIMEOUT_QUICK), float)


# ----------------------------------------------------------------------
# 3. 集成式：实际传给子进程的 timeout 必须是合成值
# ----------------------------------------------------------------------


class ServiceIntegrationCase(unittest.IsolatedAsyncioTestCase):
    """带真实 ``SessionManager`` 的脚手架。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bsk_service_test_")
        self.addCleanup(self.tmp.cleanup)

    def make(self, **overrides: Any) -> tuple[BskService, FakeRunner]:
        overrides.setdefault("screenshot_dir", self.tmp.name)
        return make_service(make_settings(**overrides))


class TestObserveTimeout(ServiceIntegrationCase):
    """``observe`` 是这次缺陷最典型的受害者（内置 15 秒）。"""

    async def test_observe_uses_synthesised_timeout_not_hardcoded(self) -> None:
        """★ 用户设 100 → observe 实际传下去的是 100，不是硬编码的 15。"""
        service, runner = self.make(command_timeout_sec=100.0)

        await service.observe("umo-1")

        self.assertEqual(runner.timeout_for("observe"), 100.0)
        self.assertNotEqual(runner.timeout_for("observe"), TIMEOUT_OBSERVE)
        self.assertNotEqual(runner.timeout_for("observe"), 15.0)

    async def test_observe_keeps_floor_when_user_asks_for_less(self) -> None:
        """用户设 5 → observe 仍是 15（下限保护）。"""
        service, runner = self.make(command_timeout_sec=5.0)

        await service.observe("umo-1")

        self.assertEqual(runner.timeout_for("observe"), TIMEOUT_OBSERVE)

    async def test_observe_follows_config_change(self) -> None:
        """同一个用例在多组配置下的表现 —— 配置确实是唯一变量。"""
        for user_value, expected in ((5.0, 15.0), (20.0, 20.0), (110.0, 110.0)):
            with self.subTest(user=user_value):
                service, runner = self.make(command_timeout_sec=user_value)
                await service.observe(f"umo-{user_value}")
                self.assertEqual(runner.timeout_for("observe"), expected)


class TestEveryCommandTimeout(ServiceIntegrationCase):
    """逐个命令验证超时透传 —— 就是"不要漏改某一处"的自动化版本。"""

    async def test_navigate_and_observe_in_open_page(self) -> None:
        """``open_page`` 内部两条命令各自用自己的下限合成。"""
        service, runner = self.make(command_timeout_sec=100.0)

        await service.open_page("umo-1", "https://example.com")

        self.assertEqual(runner.timeout_for("navigate"), 100.0)
        self.assertEqual(runner.timeout_for("observe"), 100.0)

    async def test_navigate_floor_with_min_config(self) -> None:
        """★ 用户设 5 时 navigate 仍是 45 —— 这条命令的下限保护最关键。"""
        service, runner = self.make(command_timeout_sec=5.0)

        await service.open_page("umo-1", "https://example.com")

        self.assertEqual(runner.timeout_for("navigate"), TIMEOUT_NAVIGATE)
        self.assertEqual(runner.timeout_for("navigate"), 45.0)

    async def test_act_uses_synthesised_timeout(self) -> None:
        service, runner = self.make(command_timeout_sec=77.0)

        await service.act("umo-1", "click", target="@e3")

        self.assertEqual(runner.timeout_for("click"), 77.0)

    async def test_act_keeps_action_floor(self) -> None:
        service, runner = self.make(command_timeout_sec=5.0)

        await service.act("umo-1", "click", target="@e3")

        self.assertEqual(runner.timeout_for("click"), TIMEOUT_ACTION)

    async def test_screenshot_viewport_uses_synthesised_timeout(self) -> None:
        service, runner = self.make(command_timeout_sec=90.0)

        await service.screenshot("umo-1")

        self.assertEqual(runner.timeout_for("screenshot"), 90.0)

    async def test_screenshot_fullpage_keeps_180_floor(self) -> None:
        """★ 用户设最大值 110 时，全页截图仍然是 180（内置值更大，取内置）。"""
        service, runner = self.make(command_timeout_sec=110.0)

        await service.screenshot("umo-1", full_page=True)

        self.assertEqual(runner.timeout_for("screenshot"), TIMEOUT_FULLPAGE)
        self.assertEqual(runner.timeout_for("screenshot"), 180.0)

    async def test_screenshot_viewport_floor_below_user_value(self) -> None:
        """视口截图的下限是 30，用户设 5 时保留下限。"""
        service, runner = self.make(command_timeout_sec=5.0)

        await service.screenshot("umo-1")

        self.assertEqual(runner.timeout_for("screenshot"), TIMEOUT_SCREENSHOT)

    async def test_console_and_network_use_quick_floor(self) -> None:
        """只读日志类命令的内置下限是 5 秒，用户调大后跟随用户值。"""
        service, runner = self.make(command_timeout_sec=88.0)

        await service.read_console("umo-1")
        await service.read_network("umo-1")

        self.assertEqual(runner.timeout_for("console"), 88.0)
        self.assertEqual(runner.timeout_for("network"), 88.0)

    async def test_console_floor_when_user_asks_for_less(self) -> None:
        service, runner = self.make(command_timeout_sec=5.0)

        await service.read_console("umo-1")

        self.assertEqual(runner.timeout_for("console"), TIMEOUT_QUICK)

    async def test_list_browsers_uses_quick_floor(self) -> None:
        service, runner = self.make(command_timeout_sec=5.0)

        await service.list_browsers()

        self.assertEqual(runner.timeout_for("browsers"), TIMEOUT_QUICK)

    async def test_status_uses_quick_floor(self) -> None:
        service, runner = self.make(command_timeout_sec=99.0)

        await service.status("umo-1")

        self.assertEqual(runner.timeout_for("status"), 99.0)

    async def test_console_network_and_status_at_min_config(self) -> None:
        """最低配置下这三条也都还拿得到 5 秒下限（不是 0）。"""
        service, runner = self.make(command_timeout_sec=5.0)

        await service.list_browsers()
        await service.read_console("umo-1")
        await service.read_network("umo-1")
        await service.status("umo-1")

        for command in ("browsers", "console", "network", "status"):
            with self.subTest(command=command):
                self.assertEqual(runner.timeout_for(command), TIMEOUT_QUICK)

    async def test_session_start_floor_is_not_lowered_by_service(self) -> None:
        """``session start`` 的超时由 ``SessionManager`` 管（它有自己的下限），
        但**至少**不能低于用户配置 —— 而它的默认下限就是用户配置。
        """
        service, runner = self.make(command_timeout_sec=100.0)

        await service.observe("umo-1")

        self.assertEqual(runner.timeout_for("session start"), 100.0)


class TestTimeoutNeverZeroOrNone(ServiceIntegrationCase):
    """★ 传给子进程的超时永远是一个正数 —— 这是"每个调用点都改到了"的硬证据。

    漏改的调用点会传 ``None``（runner 会退化成它自己的 default_timeout）
    或者硬编码值；``None`` 在这里一律判失败。
    """

    async def test_all_recorded_timeouts_are_positive_numbers(self) -> None:
        service, runner = self.make(command_timeout_sec=5.0)

        await service.open_page("umo-1", "https://example.com")
        await service.act("umo-1", "click", target="@e3")
        await service.screenshot("umo-1")
        await service.read_console("umo-1")
        await service.read_network("umo-1")
        await service.list_browsers()
        await service.status("umo-1")

        self.assertTrue(runner.timeouts, "假 runner 一次都没被调用，测试本身失效了")
        for call, timeout in zip(runner.calls, runner.timeouts):
            with self.subTest(call=call):
                self.assertIsNotNone(timeout, f"{call} 没有传超时（漏改了调用点？）")
                self.assertGreater(timeout, 0, f"{call} 的超时不是正数：{timeout}")

    async def test_all_recorded_timeouts_respect_rule(self) -> None:
        """把实际传下去的超时与 `max(内置, 用户)` 逐条对上。"""
        service, runner = self.make(command_timeout_sec=110.0)

        await service.open_page("umo-1", "https://example.com")
        await service.screenshot("umo-1", full_page=True)

        expected = {
            "navigate": max(TIMEOUT_NAVIGATE, 110.0),
            "observe": max(TIMEOUT_OBSERVE, 110.0),
            "screenshot": max(TIMEOUT_FULLPAGE, 110.0),
        }
        for command, want in expected.items():
            with self.subTest(command=command):
                self.assertEqual(runner.timeout_for(command), want)


# ----------------------------------------------------------------------
# 4. 跨模块一致性（内置值语义变了，别的地方别跟着漂）
# ----------------------------------------------------------------------


class TestBuiltinConstants(unittest.TestCase):
    """内置常量必须**保留**（它们是"下限建议值"，语义比裸数字清晰）。"""

    def test_constants_still_exist_with_documented_values(self) -> None:
        """★ 常量不许被删或改数值 —— 它们是 ARCHITECTURE §5 D6 那张表。"""
        self.assertEqual(TIMEOUT_QUICK, 5.0)
        self.assertEqual(TIMEOUT_OBSERVE, 15.0)
        self.assertEqual(TIMEOUT_ACTION, 30.0)
        self.assertEqual(TIMEOUT_NAVIGATE, 45.0)
        self.assertEqual(TIMEOUT_SCREENSHOT, 30.0)
        self.assertEqual(TIMEOUT_FULLPAGE, 180.0)

    def test_navigate_floor_exceeds_bsk_own_timeout(self) -> None:
        """navigate 的下限必须**大于** bsk 自身默认的 ``--timeout`` 30 秒。

        否则我们会先把它掐掉，而它其实正要成功返回 —— 这是下限存在的理由本身。
        """
        self.assertGreater(TIMEOUT_NAVIGATE, 30.0)

    def test_fullpage_floor_exceeds_plugin_clamp_ceiling(self) -> None:
        """全页截图的下限高于 ``command_timeout_sec`` 的夹取上界。

        这正是"必须两处一起调大"的根源：光把插件这一项拉满也不够 180 秒，
        还要放宽 AstrBot 的 ``tool_call_timeout``（默认 120）。
        """
        self.assertGreater(TIMEOUT_FULLPAGE, COMMAND_TIMEOUT_MAX_SEC)

    def test_works_with_real_settings_type(self) -> None:
        """★ 用真实 ``Settings`` 跑一遍，钉死"字段名漂移"这种静默故障。

        ``_timeout`` 是按**名字**读配置的：名字一旦和 ``Settings`` 对不上，
        就会静默退回内置值（不报错，但用户的配置全部失效）—— 那正是这次
        要修的病。这条测试把它钉住。
        """
        settings = make_settings(command_timeout_sec=33.0)
        service, _ = make_service(settings)

        self.assertEqual(settings.command_timeout_sec, 33.0)
        self.assertEqual(service._timeout(TIMEOUT_QUICK), 33.0)
        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 45.0)

    def test_settings_dataclass_has_command_timeout_field(self) -> None:
        """``Settings`` 必须有这个字段 —— ``_timeout`` 的兜底不该被日常路径用到。"""
        from dataclasses import fields

        from bsk.config import Settings

        self.assertIn("command_timeout_sec", {f.name for f in fields(Settings)})


# ======================================================================
# 5. 多浏览器歧义：**明确报错**，而不是静默随机选一个
#
# 背景（这次要修的**真实体验缺陷**）：``probe_browser()`` 以前只在"恰好 1 个"
# 时返回 instance_id，其余情况一律返回空串。于是用户同时连着 Edge + Chrome
#（或同一浏览器的两个 profile）却没配 ``browser_instance_id`` 时，插件不传
# ``--browser``，bsk 就自己随便挑一个 —— 用户看到的现象是"有时候对这个、
# 有时候对那个"，既不知道是哪个，也不知道为什么，无从排查。
# 而 README 早就**声称**"连了好几个时会报错要求你指定"，代码却没实现。
#
# 现在的规则（三条分支必须分清，下面逐个钉住）：
#
#   1. 探测**失败**（bsk 没装/命令报错/输出不是 JSON）→ 返回空串，静默降级；
#   2. **恰好 1 个** → 自动返回它的 instance_id（**免配置**，最常见的场景）；
#   3. **≥2 个且用户没配** → 抛 ``BskBrowserAmbiguous``，列出所有实例；
#   4. 用户**显式配了** ``browser_instance_id`` → 直接用配置的，
#      **不做歧义检查**（用户已经明确表态了）。
#
# 假 subprocess：``probe_browser`` 走的是**同步** ``subprocess.run``，
# 所以这里替换掉 ``subprocess.run`` 本身，而不是真的去执行 bsk
#（本机可能真的有浏览器连着，测试绝不能依赖这一点，也绝不能碰到它）。
# ======================================================================

import json  # noqa: E402 - 追加段落自带 import，不改动文件上方的任何一行
from unittest import mock  # noqa: E402

from bsk.errors import (  # noqa: E402
    CODE_BROWSER_AMBIGUOUS,
    BskBrowserAmbiguous,
)
from bsk.models import BrowserInstance  # noqa: E402


def browsers_payload(*instances: tuple[str, str]) -> list[dict[str, Any]]:
    """造 ``bsk browsers --json`` 的载荷。

    Args:
        *instances: 若干 ``(instance_id, browser_name)`` 二元组。
            ``label`` 一律填**空字符串** —— 实测它经常是空的，
            而"展示时必须能兜底"正是要测的东西之一。
    """
    return [
        {
            "instance_id": instance_id,
            "browser_name": name,
            "browser_version": "154.0.0.0",
            "extension_version": "0.3.2",
            "label": "",
            "session_count": 0,
            "unresponsive": False,
            "version_skew": False,
        }
        for instance_id, name in instances
    ]


def fake_subprocess_run(
    payload: Any,
    *,
    returncode: int = 0,
) -> Any:
    """造一个 ``subprocess.run`` 替身，假装 ``bsk browsers --json`` 的返回。

    Args:
        payload: 要假装成 bsk 输出的对象（会被 JSON 序列化）。
        returncode: 假的退出码。非 0 表示"探测本身失败"。
    """

    def _run(args: list[str], **kwargs: Any) -> Any:
        return types.SimpleNamespace(
            returncode=returncode,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
            args=args,
        )

    return _run


def browser_id_of(start_call: list[str]) -> str:
    """从 ``session start`` 的参数列表里取出 ``--browser`` 的值。

    返回空串表示**没传** ``--browser``（也就是"交给 bsk 自己选"）。
    """
    if "--browser" not in start_call:
        return ""
    index = start_call.index("--browser")
    return start_call[index + 1] if index + 1 < len(start_call) else ""


class TestPickBrowserFromProbe(unittest.TestCase):
    """``BskService._pick_browser_from_probe``：**探测成功**之后的选浏览器规则。

    单独抽出来测是因为它是个纯函数：输入 bsk 的 JSON 载荷，输出
    instance_id 或抛歧义异常，完全不碰子进程与浏览器。
    """

    def test_zero_browsers_returns_empty_and_never_raises(self) -> None:
        """★ 0 个浏览器 → 空串（不抛歧义），维持"交给 bsk 自己报错"的现有行为。

        不能在这里报歧义：歧义的含义是"有好几个、不知道选哪个"，
        一个都没有时报"请从下面几个里选"会让用户完全摸不着头脑。
        """
        self.assertEqual(BskService._pick_browser_from_probe([]), "")

    def test_single_browser_is_selected_automatically(self) -> None:
        """★★ 回归保护（本文件最重要的一条）：恰好 1 个 → 自动选中，免配置。

        这是最常见的场景（实测本机就只有一个 edge），一旦这里退化成报错，
        所有"本来不用配置就能用"的用户全部被打断 —— 那是比原缺陷更糟的倒退。
        """
        payload = browsers_payload(("c900a3da", "edge"))

        self.assertEqual(BskService._pick_browser_from_probe(payload), "c900a3da")

    def test_two_browsers_raise_ambiguous_with_both_ids(self) -> None:
        """★ 2 个且未配置 → 抛歧义异常，且文案里**两个 instance_id 都在**。

        文案里必须有两个 id：只说"有多个浏览器"用户没法照着做，
        他需要把其中一个**原样复制**到配置里。
        """
        payload = browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome"))

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)

        friendly = ctx.exception.friendly
        self.assertIn("c900a3da", friendly)
        self.assertIn("ab12cd34", friendly)
        self.assertEqual(ctx.exception.code, CODE_BROWSER_AMBIGUOUS)
        # 这是用户配置问题，重试多少次都一样 —— 不许被当成可重试的瞬时故障。
        self.assertFalse(ctx.exception.retryable)

    def test_two_browsers_message_shows_browser_name_not_only_label(self) -> None:
        """★ label 为空时展示不能崩，且要用 ``browser_name`` 兜底。

        实测 ``label`` 经常是空字符串。只依赖 label 的文案会变成
        ``-  (c900a3da)``，用户看不出哪个是 Edge、哪个是 Chrome。
        """
        payload = browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome"))

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)

        friendly = ctx.exception.friendly
        self.assertIn("edge", friendly)
        self.assertIn("chrome", friendly)
        # 不能出现空名字那种"两个空格接着括号"的痕迹。
        self.assertNotIn("-  (", friendly)

    def test_label_is_preferred_when_present_but_id_still_shown(self) -> None:
        """label 非空时用 label 做展示名，但 **instance_id 必须仍然可见**。

        用户要复制的是 instance_id；只显示 "工作用的 Chrome" 等于没说。
        """
        line = BskService._describe_instance(
            BrowserInstance.from_json(
                {
                    "instance_id": "c900a3da",
                    "browser_name": "chrome",
                    "label": "工作用的 Chrome",
                }
            )
        )

        self.assertIn("工作用的 Chrome", line)
        self.assertIn("c900a3da", line)

    def test_message_is_actionable(self) -> None:
        """★ 文案必须可操作：说清"去哪个配置项、填什么"。

        这段文字最终会经 ``main.py`` 的 ``except BskError`` 变成给模型的
        字符串，模型要据此告诉用户去改什么配置 —— 只说"有多个浏览器"
        模型也只能干瞪眼。
        """
        payload = browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome"))

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)

        friendly = ctx.exception.friendly
        self.assertIn("配置", friendly)
        self.assertIn("instance_id", friendly)
        # 配置项的真名也要出现，否则用户不知道在 WebUI 里找哪一项。
        self.assertIn("browser_instance_id", friendly)

    def test_three_browsers_all_listed(self) -> None:
        """3 个以上时一个都不能漏 —— 漏掉的那个恰好是用户想选的就麻烦了。"""
        payload = browsers_payload(
            ("c900a3da", "edge"), ("ab12cd34", "chrome"), ("77889900", "brave")
        )

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)

        for instance_id in ("c900a3da", "ab12cd34", "77889900"):
            with self.subTest(instance_id=instance_id):
                self.assertIn(instance_id, ctx.exception.friendly)

    def test_unresponsive_instance_is_marked_but_still_listed(self) -> None:
        """无响应的实例**照样列出**，但明确标注"不建议选它"。

        刻意不静默过滤掉它：那也是一种"替用户做决定"。它确实连着，
        用户有权知道自己有两个实例，以及该避开哪一个。
        """
        payload = browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome"))
        payload[1]["unresponsive"] = True

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)

        friendly = ctx.exception.friendly
        self.assertIn("ab12cd34", friendly)
        self.assertIn("无响应", friendly)

    def test_non_list_or_junk_payload_returns_empty(self) -> None:
        """载荷结构不认识时返回空串（交给 bsk），不许把插件搞崩。

        这是外部输入：daemon 版本不同、输出被截断都可能让它不是列表。
        """
        for junk in (None, {}, "oops", 42, [None, "x", 3]):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(BskService._pick_browser_from_probe(junk), "")

    def test_entries_without_instance_id_are_ignored(self) -> None:
        """★ 没有 instance_id 的条目不算"可用的浏览器"。

        instance_id 正是用户要填进配置的值：它空的既选不中也填不了，
        拿它去凑"多个"只会报一个列不出第二个实例的歧义错误。
        """
        payload = browsers_payload(("c900a3da", "edge"))
        payload.append({"browser_name": "ghost", "label": ""})

        self.assertEqual(BskService._pick_browser_from_probe(payload), "c900a3da")

    def test_duplicate_instance_ids_count_once(self) -> None:
        """同一个 id 出现两次不算歧义（bsk 理论上不会这样，但别自己吓自己）。"""
        payload = browsers_payload(("c900a3da", "edge"), ("c900a3da", "edge"))

        self.assertEqual(BskService._pick_browser_from_probe(payload), "c900a3da")


class TestBrowserProbeIsWiredIntoSessionCreation(ServiceIntegrationCase):
    """``probe_browser`` 经**真实** ``SessionManager`` 走的端到端行为。

    这一组才是真正的回归保护：``session.py`` 的 ``_resolve_browser_instance``
    出于容错会吞掉探测异常，所以"抛异常"本身**不足以保证**用户能看到 ——
    必须证明它确实穿过了那一层，而不是被吞成静默降级。
    """

    def setUp(self) -> None:
        super().setUp()
        # 在**没有**任何补丁的情况下，绝不允许真的去执行 bsk。
        # 任何一次真实调用都会撞上这个断言（下面的用例各自按需覆盖它）。
        self._patchers: list[Any] = []
        self._patch_run(mock.Mock(side_effect=AssertionError("不该真的执行 bsk")))

    def _patch_run(self, replacement: Any) -> None:
        """把 ``subprocess.run`` 换成 ``replacement``（同一用例内可反复替换）。

        自己管 patcher 列表而不是用 ``mock.patch.stopall()``：后者会把
        ``unittest`` 框架自己的补丁也一起停掉，属于误伤。
        """
        patcher = mock.patch("subprocess.run", replacement)
        patcher.start()
        self._patchers.append(patcher)
        self.addCleanup(patcher.stop)

    def patch_browsers(self, payload: Any, *, returncode: int = 0) -> None:
        """让 ``bsk browsers --json`` 返回给定载荷。"""
        self._patch_run(fake_subprocess_run(payload, returncode=returncode))

    # --- 分支 2：恰好 1 个 → 免配置（★ 回归保护）---

    async def test_single_browser_is_used_without_any_config(self) -> None:
        """★ 未配置 + 只有 1 个浏览器 → 会话照常建立，并自动带上 --browser。

        这就是"免配置体验"本身：它**绝不能**因为这次改动变成报错。
        """
        self.patch_browsers(browsers_payload(("c900a3da", "edge")))
        service, runner = self.make(browser_instance_id="")

        observation = await service.observe("umo-1")

        self.assertTrue(observation is not None)
        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "c900a3da")

    # --- 分支 1：0 个 → 静默降级 ---

    async def test_zero_browsers_creates_session_without_browser_flag(self) -> None:
        """★ 0 个浏览器 → 不报歧义，照常建会话且**不传** --browser。

        设计选择：这一档维持"交给 bsk 自己报错"的现有行为。bsk 那句
        "没有已连接的浏览器"本身就是准确的诊断，插件在这里另造一句
        只会增加不一致；而没有 ``--browser`` 正是让 bsk 说那句话的前提。
        """
        self.patch_browsers([])
        service, runner = self.make(browser_instance_id="")

        await service.observe("umo-1")

        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "")
        self.assertNotIn("--browser", start_call)

    # --- 分支 3：≥2 个 → 歧义错误（★ 本次改动的主角）---

    async def test_two_browsers_raise_ambiguous_through_real_manager(self) -> None:
        """★★ 2 个且未配置 → 异常必须穿过 ``SessionManager`` 冒到调用方。

        ``_resolve_browser_instance`` 里那条 ``except Exception`` 是为了
        "探测失败不影响建会话"。歧义**不是**探测失败，如果被它一起吞掉，
        就会退回"不传 --browser、bsk 随便选一个"的老毛病 —— 而且更隐蔽，
        因为异常看起来"处理过了"。这条测试专门钉死这一点。
        """
        self.patch_browsers(browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome")))
        service, runner = self.make(browser_instance_id="")

        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            await service.observe("umo-1")

        friendly = ctx.exception.friendly
        self.assertIn("c900a3da", friendly)
        self.assertIn("ab12cd34", friendly)
        # 【反证】绝不能在报错的同时还建出了会话 —— 那说明我们其实选了某个浏览器。
        self.assertEqual(runner.calls_for("session start"), [])

    async def test_two_browsers_also_blocks_open_page(self) -> None:
        """用户最常走的入口（打开网页）同样被挡住，而不是悄悄开一个。

        ``open_page`` 内部会 navigate + observe；歧义在建会话时就该失败，
        所以 navigate 一次都不该发出去。
        """
        self.patch_browsers(browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome")))
        service, runner = self.make(browser_instance_id="")

        with self.assertRaises(BskBrowserAmbiguous):
            await service.open_page("umo-1", "https://example.com")

        self.assertEqual(runner.calls_for("navigate"), [])

    # --- 分支 4：配了就必须尊重配置，不做歧义检查 ---

    async def test_configured_browser_wins_even_with_two_connected(self) -> None:
        """★★ 2 个浏览器但用户配了 id → 用配置的，**不报歧义**。

        用户已经明确表态了。这时再去"检测歧义"就是多管闲事，
        而且会让他刚填好的配置失效 —— 最让人恼火的那种 bug。
        """
        self.patch_browsers(browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome")))
        service, runner = self.make(browser_instance_id="ab12cd34")

        await service.observe("umo-1")

        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "ab12cd34")

    async def test_configured_browser_never_even_probes(self) -> None:
        """★ 配了 id 时**连探测都不该发生**（上面 setUp 的断言会抓住真实调用）。

        这既是"尊重配置"，也顺带保证了这种场景下不多花一次子进程开销。
        """
        service, runner = self.make(browser_instance_id="deadbeef")

        await service.observe("umo-1")

        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "deadbeef")

    # --- 分支 1 的变体：探测本身失败 → 保持原有容错语义 ---

    async def test_probe_command_failure_still_creates_session(self) -> None:
        """★ ``bsk browsers`` 报错（退出码非 0）→ 建会话**照常成功**。

        这是刻意保留的容错语义：探测只是"帮用户省一步配置"的优化，
        它失败不该让整个插件不可用。
        """
        self.patch_browsers(["irrelevant"], returncode=1)
        service, runner = self.make(browser_instance_id="")

        await service.observe("umo-1")

        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "")
        self.assertEqual(len(runner.calls_for("session start")), 1)

    async def test_probe_crash_still_creates_session(self) -> None:
        """探测抛异常（bsk 没装、超时等）→ 同样不许连累建会话。"""
        self._patch_run(mock.Mock(side_effect=OSError("bsk 不存在")))
        service, runner = self.make(browser_instance_id="")

        await service.observe("umo-1")

        start_call = runner.calls_for("session start")[0]
        self.assertEqual(browser_id_of(start_call), "")

    async def test_probe_returns_broken_json_still_creates_session(self) -> None:
        """探测输出不是 JSON（截断/clap 报错）→ 同样静默降级。"""
        self._patch_run(
            lambda *a, **k: types.SimpleNamespace(
                returncode=0, stdout=b"not json at all", stderr=b""
            )
        )
        service, runner = self.make(browser_instance_id="")

        await service.observe("umo-1")

        self.assertEqual(browser_id_of(runner.calls_for("session start")[0]), "")

    # --- 调用频率：不许在热路径上多探测 ---

    async def test_probe_happens_once_per_session_not_per_command(self) -> None:
        """★ 探测次数 = 建会话次数，**不**随后续命令增长。

        ``probe_browser`` 走的是同步 ``subprocess.run``，每次都有真实开销。
        检查必须搭在"建会话的那一次探测"上，绝不能变成每条命令都探一次。
        """
        calls: list[list[str]] = []
        real = fake_subprocess_run(browsers_payload(("c900a3da", "edge")))

        def counting_run(args: list[str], **kwargs: Any) -> Any:
            calls.append(list(args))
            return real(args, **kwargs)

        self._patch_run(counting_run)
        service, _ = self.make(browser_instance_id="")

        await service.observe("umo-1")
        first = len(calls)
        self.assertEqual(first, 1, f"建会话时应该恰好探测一次，实际 {first} 次")

        # 同一个 key 上再跑三条命令：会话已存在，一次都不该再探。
        await service.observe("umo-1")
        await service.read_console("umo-1")
        await service.read_network("umo-1")

        self.assertEqual(len(calls), first, "后续命令不应该再触发探测（热路径开销）")
        for call in calls:
            with self.subTest(call=call):
                self.assertEqual(call[1:], ["browsers", "--json"])


class TestAmbiguousBrowserErrorShape(unittest.TestCase):
    """歧义异常自身的形状 —— 它要能安全地经 ``main.py`` 变成给模型的字符串。"""

    def _make(self) -> BskBrowserAmbiguous:
        payload = browsers_payload(("c900a3da", "edge"), ("ab12cd34", "chrome"))
        with self.assertRaises(BskBrowserAmbiguous) as ctx:
            BskService._pick_browser_from_probe(payload)
        return ctx.exception

    def test_is_a_bsk_error_so_main_py_catches_it(self) -> None:
        """★ 必须是 ``BskError`` 子类。

        ``main.py`` 只捕获 ``BskError`` 来生成给用户的中文提示；
        不是它子类的话会掉进 ``except Exception`` 那条"未预期的错误"分支，
        用户看到的是一句带异常类型名的内部报错。
        """
        from bsk.errors import BskError

        self.assertIsInstance(self._make(), BskError)

    def test_carries_error_code_and_friendly_text(self) -> None:
        """``code`` 供诊断，``friendly`` 是给用户/模型看的那段中文。"""
        exc = self._make()

        self.assertEqual(exc.code, CODE_BROWSER_AMBIGUOUS)
        self.assertTrue(exc.friendly.strip())
        self.assertIn("\n", exc.friendly, "多实例清单需要换行才读得懂")

    def test_friendly_text_explains_next_step(self) -> None:
        """★ ``friendly`` 必须自解释：症状 + 下一步动作，模型要照着转述。"""
        friendly = self._make().friendly

        self.assertIn("检测到 2 个已连接的浏览器", friendly)
        self.assertIn("请", friendly)
        # 用户最终要做的那件事（去配置里填一个 instance_id）必须写清楚。
        self.assertIn("填成", friendly)

    def test_repr_and_str_do_not_crash(self) -> None:
        """日志里会 ``%r`` 它；文案含换行与中文，别在这里出岔子。"""
        exc = self._make()

        self.assertTrue(str(exc))
        self.assertTrue(repr(exc))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
