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


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
