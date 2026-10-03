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
        """★ 用户设最大值 110：全页截图保留下限；observe 被抬到 110。

        Note:
            全页截图的下限已按用户拍板从 180 改为 120（见 ``TIMEOUT_FULLPAGE``），
            所以这里不再断言"180 不被压低"，而是断言它**仍然是 120**：
            用户把 command_timeout_sec 拉满（110）也压不动它。
        """
        service, _ = make_service(
            make_settings(command_timeout_sec=COMMAND_TIMEOUT_MAX_SEC)
        )

        self.assertEqual(COMMAND_TIMEOUT_MAX_SEC, 110.0)
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 120.0)
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
        # 只有全页截图的下限高过 60（该下限已按用户拍板从 180 改为 120）。
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 120.0)

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

    async def test_screenshot_fullpage_keeps_its_default_budget(self) -> None:
        """★ 用户设最大值 110 时，全页截图仍然是它自己的预算（120，不被压低）。

        这条同时是"全页截图**不**跟着 command_timeout_sec 走"的回归保护：
        它的取值来自独立的 ``fullpage_timeout_sec``（默认 120，见
        ``DEFAULT_FULLPAGE_TIMEOUT_SEC``）。
        """
        service, runner = self.make(command_timeout_sec=110.0)

        await service.screenshot("umo-1", full_page=True)

        self.assertEqual(runner.timeout_for("screenshot"), TIMEOUT_FULLPAGE)
        self.assertEqual(runner.timeout_for("screenshot"), 120.0)

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
        """★ 常量不许被删或改数值 —— 它们是 ARCHITECTURE §5 D6 那张表。

        Note:
            ``TIMEOUT_FULLPAGE`` 已按用户拍板从 **180 改为 120**：它现在的角色是
            "用户没配 ``fullpage_timeout_sec`` 时的默认内置下限"，取值与
            ``DEFAULT_FULLPAGE_TIMEOUT_SEC`` 一致（有专门测试钉住这一点）。
        """
        self.assertEqual(TIMEOUT_QUICK, 5.0)
        self.assertEqual(TIMEOUT_OBSERVE, 15.0)
        self.assertEqual(TIMEOUT_ACTION, 30.0)
        self.assertEqual(TIMEOUT_NAVIGATE, 45.0)
        self.assertEqual(TIMEOUT_SCREENSHOT, 30.0)
        self.assertEqual(TIMEOUT_FULLPAGE, 120.0)

    def test_navigate_floor_exceeds_bsk_own_timeout(self) -> None:
        """navigate 的下限必须**大于** bsk 自身默认的 ``--timeout`` 30 秒。

        否则我们会先把它掐掉，而它其实正要成功返回 —— 这是下限存在的理由本身。
        """
        self.assertGreater(TIMEOUT_NAVIGATE, 30.0)

    def test_fullpage_default_exceeds_plugin_clamp_ceiling(self) -> None:
        """全页截图的默认预算仍然高于 ``command_timeout_sec`` 的夹取上界。

        这条语义**没有变**：即便用户把 command_timeout_sec 拉满（110），
        全页截图也用自己那一项（默认 120）。变的是数值来源 —— 现在它是可配置的
        （``fullpage_timeout_sec``），而不是写死的常量。
        """
        self.assertGreater(TIMEOUT_FULLPAGE, COMMAND_TIMEOUT_MAX_SEC)

    def test_fullpage_constant_matches_settings_default(self) -> None:
        """★ ``TIMEOUT_FULLPAGE`` 必须等于 ``Settings.fullpage_timeout_sec`` 的默认值。

        两者若漂移，会出现"用户什么都没配，但 service 用的值和配置页显示的不一样"
        —— 正是最难排查的那类问题（配置一致性脚本只比对 schema 与 Settings，
        管不到这个常量）。
        """
        from bsk.config import DEFAULT_FULLPAGE_TIMEOUT_SEC

        self.assertEqual(TIMEOUT_FULLPAGE, DEFAULT_FULLPAGE_TIMEOUT_SEC)
        self.assertEqual(parse_settings({}).fullpage_timeout_sec, TIMEOUT_FULLPAGE)

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


# ======================================================================
# 6. evaluate（执行任意 JavaScript）—— 高风险能力
#
# 背景：``bsk evaluate`` 能在用户**已登录**的页面里跑任意 JS，是本插件风险最高
# 的能力（默认关闭 + 强制管理员，见 main.py 的 ``_evaluate_denied``）。
#
# 本层要钉死的是**判读逻辑**，其中最重要的一条来自实测：
#
#     ★ JavaScript 抛异常时，bsk 进程的**退出码仍然是 0**。
#
#     实测原文（bsk 0.3.2）：
#         $ bsk evaluate "throw new Error('boom')" --session ycvt --json
#         { "ok": false, "tab_id": ..., "error": {"text": "Error: boom", ...} }
#         exit=0
#
#     所以只看退出码会把**失败当成功**，然后拿着一个没有值的结构去回答用户。
#     下面 ``TestEvaluateJsErrorWithZeroExit`` 就是这个坑的回归测试。
#
# 全部用例都用假 runner，**绝不真的执行 JS**（那会在用户浏览器里跑代码）。
# ======================================================================

from bsk.errors import (  # noqa: E402
    BskError,
    BskSessionGone,
    BskTimeout,
    classify,
)
from bsk.models import (  # noqa: E402
    EvaluateDialog,
    EvaluateError,
    EvaluateResult,
)
from bsk.service import TIMEOUT_EVALUATE, BskService  # noqa: E402,F811


class EvaluateFakeRunner:
    """可编排返回值的假 runner：专门给 evaluate 用例用。

    与上面的 ``FakeRunner`` 分开写（而不是改它）是为了**不动既有测试**：
    那个类的返回值是按命令名硬编码的，而这里需要"按用例指定任意载荷"。
    """

    def __init__(self, payload: Any = None, *, raises: BaseException | None = None) -> None:
        self.payload = payload
        self.raises = raises
        self.calls: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.resolved = r"C:\fake\bsk.exe"

    def resolve(self) -> str:
        return self.resolved

    async def run_or_raise(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> BskResult:
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        # ★ 建会话永远返回真实形状的 start 载荷，与 payload 无关 ——
        #   否则 evaluate 的载荷会被当成 session start 的返回，会话建不起来。
        if args[:2] == ["session", "start"]:
            data: Any = {"session_id": "mnaa", "browser_instance_id": "c900a3da"}
        else:
            data = self.payload
        return BskResult(ok=True, exit_code=0, data=data, elapsed=0.0)

    def evaluate_calls(self) -> list[list[str]]:
        """取出所有 ``evaluate`` 调用。"""
        return [c for c in self.calls if c and c[0] == "evaluate"]


class EvaluateServiceCase(unittest.IsolatedAsyncioTestCase):
    """带真实 ``SessionManager`` + 可编排假 runner 的脚手架。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="bsk_eval_test_")
        self.addCleanup(self.tmp.cleanup)

    def make(
        self, payload: Any = None, *, raises: BaseException | None = None, **overrides: Any
    ) -> tuple[BskService, EvaluateFakeRunner]:
        overrides.setdefault("screenshot_dir", self.tmp.name)
        runner = EvaluateFakeRunner(payload, raises=raises)
        service = BskService(make_settings(**overrides), runner=runner)
        return service, runner


# --- 6.1 成功路径 ------------------------------------------------------


class TestEvaluateSuccess(EvaluateServiceCase):
    """``ok: true`` 的成功载荷 → 返回结果对象，不抛异常。"""

    async def test_returns_value_from_ok_payload(self) -> None:
        """★ 实测成功原文：``{"ok": true, "tab_id": ..., "value": 2}``。"""
        service, _ = self.make({"ok": True, "tab_id": 1398286752, "value": 2})

        result = await service.evaluate("umo-1", "1+1")

        self.assertIsInstance(result, EvaluateResult)
        self.assertTrue(result.ok)
        self.assertTrue(result.has_value)
        self.assertEqual(result.value, 2)
        self.assertEqual(result.tab_id, 1398286752)

    async def test_expression_is_positional_argument(self) -> None:
        """★ ``EXPRESSION`` 必须是**位置参数**（实测帮助文本如此）。"""
        service, runner = self.make({"ok": True, "value": "Example Domain"})

        await service.evaluate("umo-1", "document.title")

        call = runner.evaluate_calls()[0]
        self.assertEqual(call[0], "evaluate")
        self.assertEqual(call[1], "document.title")
        self.assertIn("--session", call)
        self.assertIn("--json", call)
        # 绝不能写成 --expression（bsk 会报 clap 参数错误）。
        self.assertNotIn("--expression", call)

    async def test_undefined_value_is_reported_as_no_value(self) -> None:
        """★ 实测：求值成 undefined 时 bsk **整个省掉** ``value`` 字段。

        这时 ``has_value`` 必须是 False —— 否则渲染出来的会是 "返回值：None"，
        模型会以为脚本真的返回了一个 null。
        """
        service, _ = self.make({"ok": True, "tab_id": 1398286752})

        result = await service.evaluate("umo-1", "undefined")

        self.assertTrue(result.ok)
        self.assertFalse(result.has_value)
        self.assertIsNone(result.value)

    async def test_null_value_is_distinct_from_missing(self) -> None:
        """``null`` 与"没有 value 字段"语义不同，都必须能表达。"""
        service, _ = self.make({"ok": True, "value": None})

        result = await service.evaluate("umo-1", "null")

        self.assertTrue(result.ok)
        self.assertTrue(result.has_value)
        self.assertIsNone(result.value)

    async def test_string_result_is_rendered_verbatim(self) -> None:
        """字符串原样给模型（不加 JSON 引号），读起来最自然。"""
        service, _ = self.make({"ok": True, "value": "Example Domain"})

        result = await service.evaluate("umo-1", "document.title")

        rendered = service.render_evaluate(result, "document.title")
        self.assertIn("Example Domain", rendered)
        self.assertNotIn('"Example Domain"', rendered)


# --- 6.2 ★★ 最关键：JS 抛异常但退出码为 0 -------------------------------


class TestEvaluateJsErrorWithZeroExit(EvaluateServiceCase):
    """★★ 本功能**最容易漏掉**的用例：退出码 0 不等于成功。

    bsk 的 ``evaluate`` 在 JS 抛异常时返回 ``ok: false``，但**进程退出码是 0**。
    如果实现只看退出码（``SessionManager.execute`` 就是这么判的，对别的命令都对），
    这次失败会被当成成功，模型会拿着一个空值编答案。

    实测三种失败原文（全部 exit=0）：

    - ``throw new Error('boom')`` → ``Error: boom\\n    at <anonymous>:1:7``
    - ``notDefinedVar.foo``       → ``ReferenceError: notDefinedVar is not defined``
    - ``1+``                      → ``SyntaxError: Unexpected end of input``
    """

    async def test_throw_is_failure_despite_exit_code_zero(self) -> None:
        """★ 实测原文：exit=0，但 ``ok: false`` 且带 error 结构。"""
        payload = {
            "ok": False,
            "tab_id": 1398286752,
            "error": {
                "text": "Error: boom\n    at <anonymous>:1:7",
                "line": 1,
                "column": 0,
            },
        }
        service, _ = self.make(payload)

        with self.assertRaises(BskError) as ctx:
            await service.evaluate("umo-1", "throw new Error('boom')")

        exc = ctx.exception
        # 关键断言：它被**判为失败**，而不是返回一个 ok=True 的结果。
        self.assertEqual(exc.code, "evaluate_js_error")
        # 退出码就是 0 —— 这正是"不能只看退出码"的证据。
        self.assertEqual(exc.exit_code, 0)
        # 中文提示里要带上 JS 报错原文，便于定位。
        self.assertIn("Error: boom", exc.friendly)
        self.assertIn("第 1 行", exc.friendly)

    async def test_reference_error_is_failure(self) -> None:
        """ReferenceError（拼错变量名）是模型最常犯的错，必须报清楚。"""
        service, _ = self.make(
            {
                "ok": False,
                "error": {
                    "text": "ReferenceError: notDefinedVar is not defined\n"
                    "    at <anonymous>:1:1",
                    "line": 1,
                    "column": 0,
                },
            }
        )

        with self.assertRaises(BskError) as ctx:
            await service.evaluate("umo-1", "notDefinedVar.foo")

        self.assertIn("ReferenceError", ctx.exception.friendly)

    async def test_syntax_error_is_failure(self) -> None:
        """``1+`` 这种语法错误同样只体现在 ok 字段里。"""
        service, _ = self.make(
            {
                "ok": False,
                "error": {
                    "text": "SyntaxError: Unexpected end of input",
                    "line": 1,
                    "column": 2,
                },
            }
        )

        with self.assertRaises(BskError) as ctx:
            await service.evaluate("umo-1", "1+")

        self.assertIn("SyntaxError", ctx.exception.friendly)

    async def test_js_error_does_not_retry_or_rebuild_session(self) -> None:
        """★ JS 报错**不是**会话故障：不许触发会话重建/重试。

        会话层的重试只应该由 ``not_found`` / ``session_busy`` 触发。JS 自己
        写错了是确定性的，重试一百次结果一样，只会白花一次往返。
        """
        service, runner = self.make(
            {"ok": False, "error": {"text": "Error: boom", "line": 1, "column": 0}}
        )

        with self.assertRaises(BskError):
            await service.evaluate("umo-1", "throw new Error('boom')")

        self.assertEqual(len(runner.evaluate_calls()), 1, "JS 报错不该重试")
        self.assertEqual(
            len([c for c in runner.calls if c[:2] == ["session", "start"]]), 1
        )

    async def test_failure_without_error_detail_still_reports_something(self) -> None:
        """``ok: false`` 但没有 error 结构时，提示不能是空的。"""
        service, _ = self.make({"ok": False})

        with self.assertRaises(BskError) as ctx:
            await service.evaluate("umo-1", "whatever")

        self.assertEqual(ctx.exception.code, "evaluate_js_error")
        self.assertTrue(ctx.exception.friendly.strip())
        self.assertIn("重试", ctx.exception.friendly)

    async def test_unknown_payload_shape_is_failure_not_success(self) -> None:
        """★ 拿不到 ``ok`` 字段时按**失败**处理（宁可误报失败，不可误报成功）。"""
        for junk in ({}, {"unexpected": 1}, [], "not json", None):
            service, _ = self.make(junk)
            with self.subTest(payload=junk):
                with self.assertRaises(BskError):
                    await service.evaluate("umo-1", "document.title")

    async def test_missing_ok_field_is_failure(self) -> None:
        """有 ``value`` 但**没有** ``ok``：结构不完整，不能当成成功。"""
        service, _ = self.make({"value": 2, "tab_id": 1})

        with self.assertRaises(BskError):
            await service.evaluate("umo-1", "1+1")


# --- 6.3 超大返回被截断 ------------------------------------------------


class TestEvaluateTruncation(EvaluateServiceCase):
    """★ JS 可以返回巨大对象，不截断会撑爆模型上下文。"""

    async def test_huge_array_is_truncated(self) -> None:
        """实测 ``Array.from({length:2000},...)`` 的命令输出有 32947 字符。

        ``max_page_chars`` 是同类先例，这里复用同一个上限（不新增配置项）。
        """
        huge = [f"item-{i}" for i in range(2000)]
        service, _ = self.make({"ok": True, "value": huge}, max_page_chars=200)

        result = await service.evaluate("umo-1", "Array.from({length:2000},...)")
        rendered = service.render_evaluate(result, "huge")

        self.assertIn("已截断", rendered)
        # 必须显著小于完整内容，而不是"截了一点"。
        full = service._format_evaluate_value(huge)
        self.assertGreater(len(full), 20000)
        self.assertLess(len(rendered), len(full) / 10)

    async def test_truncation_mentions_length_and_suggests_narrowing(self) -> None:
        """截断提示要告诉模型"怎么办"，而不是只说"太长了"。"""
        service, _ = self.make({"ok": True, "value": "x" * 5000}, max_page_chars=200)

        result = await service.evaluate("umo-1", "'x'.repeat(5000)")
        rendered = service.render_evaluate(result, "'x'.repeat(5000)")

        self.assertIn("5000", rendered, "要给出完整长度")
        self.assertIn("已截断", rendered)

    async def test_small_value_is_not_truncated(self) -> None:
        """正常小值不该被截断 —— 截断提示只在真的超长时出现。"""
        service, _ = self.make({"ok": True, "value": "Example Domain"})

        result = await service.evaluate("umo-1", "document.title")
        rendered = service.render_evaluate(result, "document.title")

        self.assertNotIn("已截断", rendered)
        self.assertIn("Example Domain", rendered)

    async def test_truncation_limit_follows_config(self) -> None:
        """上限确实读的是配置（而不是写死的数字）。"""
        payload = {"ok": True, "value": "y" * 9000}

        small, _ = self.make(payload, max_page_chars=200)
        big, _ = self.make(payload, max_page_chars=20000)

        result_small = await small.evaluate("umo-1", "expr")
        result_big = await big.evaluate("umo-1", "expr")

        self.assertLess(
            len(small.render_evaluate(result_small, "expr")),
            len(big.render_evaluate(result_big, "expr")),
        )
        # 上限 20000 时 9000 字符放得下，不该出现截断提示。
        self.assertNotIn("已截断", big.render_evaluate(result_big, "expr"))

    async def test_junk_max_page_chars_falls_back(self) -> None:
        """配置被填成乱七八糟时回退到默认上限，不许崩、也不许变成 0。"""
        for junk in (None, "3000", True, 0, -5):
            service = BskService(
                types.SimpleNamespace(max_page_chars=junk),
                runner=EvaluateFakeRunner({"ok": True, "value": "z" * 100}),
            )
            with self.subTest(junk=repr(junk)):
                self.assertGreater(service._evaluate_char_limit(), 0)


# --- 6.4 超时与错误分类 ------------------------------------------------


class TestEvaluateErrors(EvaluateServiceCase):
    """超时/会话失效等错误必须保持既有分类语义（由 session 层抛出）。"""

    async def test_timeout_is_classified_as_bsk_timeout(self) -> None:
        """★ 超时必须归类成 ``BskTimeout``，``friendly`` 是给模型看的中文。

        实测 bsk 自己超时报 ``exit=4``、``code: "timeout"``：
            { "code": "timeout", "message": "tool RPC timed out after 2s",
              "hint": "retry the command; ...", "exit_code": 4 }
        """
        service, _ = self.make(
            raises=classify(
                exit_code=4,
                code="timeout",
                message="tool RPC timed out after 2s",
                hint="retry the command",
            )
        )

        with self.assertRaises(BskTimeout) as ctx:
            await service.evaluate("umo-1", "new Promise(()=>{})")

        self.assertEqual(ctx.exception.exit_code, 4)
        self.assertIn("超时", ctx.exception.friendly)

    async def test_session_gone_is_retried_once_then_succeeds(self) -> None:
        """会话失效（``not_found``）走既有的"重建 + 重试一次"路径。

        这条验证 evaluate 复用了会话层的能力，而不是自己造一套重试。
        """

        class GoneThenOkRunner(EvaluateFakeRunner):
            def __init__(self) -> None:
                super().__init__({"ok": True, "value": "recovered"})
                self.evaluate_attempts = 0

            async def run_or_raise(self, args, *, timeout=None, expect_json=True):
                if args and args[0] == "evaluate":
                    self.evaluate_attempts += 1
                    self.calls.append(list(args))
                    self.timeouts.append(timeout)
                    if self.evaluate_attempts == 1:
                        raise BskSessionGone(
                            "session not registered",
                            friendly="会话已失效",
                            code="not_found",
                            exit_code=1,
                        )
                    return BskResult(ok=True, exit_code=0, data=self.payload)
                return await super().run_or_raise(
                    args, timeout=timeout, expect_json=expect_json
                )

        runner = GoneThenOkRunner()
        service = BskService(make_settings(screenshot_dir=self.tmp.name), runner=runner)

        result = await service.evaluate("umo-1", "document.title")

        self.assertEqual(runner.evaluate_attempts, 2, "应当重建后重试一次")
        self.assertEqual(result.value, "recovered")

    async def test_uses_synthesised_timeout_with_floor(self) -> None:
        """★ evaluate 遵守唯一的超时规则：max(TIMEOUT_EVALUATE, 用户配置)。"""
        service, runner = self.make({"ok": True, "value": 1}, command_timeout_sec=5.0)

        await service.evaluate("umo-1", "1+1")

        self.assertEqual(runner.timeouts[-1], TIMEOUT_EVALUATE)
        self.assertEqual(TIMEOUT_EVALUATE, 45.0)

    async def test_evaluate_floor_exceeds_bsk_own_timeout(self) -> None:
        """★ 下限必须**大于** bsk 自身默认的 ``--timeout 30s``。

        否则我们会先把它掐掉，而它正要成功返回 —— 与 ``navigate`` 同一条原则。
        """
        self.assertGreater(TIMEOUT_EVALUATE, 30.0)

    async def test_user_timeout_wins_when_larger(self) -> None:
        """用户配置更大时取用户值（单一规则，不要为 evaluate 搞特例）。"""
        service, runner = self.make({"ok": True, "value": 1}, command_timeout_sec=100.0)

        await service.evaluate("umo-1", "1+1")

        self.assertEqual(runner.timeouts[-1], 100.0)

    async def test_follows_single_rule_for_all_builtins(self) -> None:
        """★ 与既有测试同一条护栏：evaluate 也只是 ``max(内置, 用户)`` 的一个实例。"""
        for user_value in (5.0, 30.0, 45.0, 60.0, 110.0):
            service, _ = self.make({"ok": True, "value": 1}, command_timeout_sec=user_value)
            with self.subTest(user=user_value):
                self.assertEqual(
                    service._timeout(TIMEOUT_EVALUATE), max(TIMEOUT_EVALUATE, user_value)
                )

    async def test_does_not_pass_bsk_timeout_flag(self) -> None:
        """★ 不给 bsk 传 ``--timeout``：让它保持自己的 30s 默认值。

        这样"bsk 内部超时"与"我们的外层超时"有明确先后关系（见 TIMEOUT_EVALUATE）。
        """
        service, runner = self.make({"ok": True, "value": 1})

        await service.evaluate("umo-1", "1+1")

        self.assertNotIn("--timeout", runner.evaluate_calls()[0])


# --- 6.5 对话框（实测风险：confirm 被自动确认）-------------------------


class TestEvaluateDialogs(EvaluateServiceCase):
    """★ 实测：``evaluate`` 会自动**确认**页面的 confirm 弹窗。

    实测原文：
        $ bsk evaluate "confirm('bsk-evaluate-probe')" --session ycvt --json
        { "ok": true, "value": true,
          "dialogs": [{"type": "confirm", "message": "bsk-evaluate-probe",
                       "handled": "accepted", ...}] }

    ``handled: "accepted"`` 意味着页面本来能让人亲自拦一下的确认框**消失了**。
    这必须显式告诉模型，否则它会以为"用户点了确定"。
    """

    async def test_dialogs_are_parsed(self) -> None:
        service, _ = self.make(
            {
                "ok": True,
                "tab_id": 1398286752,
                "value": True,
                "dialogs": [
                    {
                        "tab_id": 1398286752,
                        "type": "confirm",
                        "message": "bsk-evaluate-probe",
                        "url": "about:blank",
                        "default_prompt": "",
                        "has_browser_handler": True,
                        "handled": "accepted",
                        "sequence": 1,
                    }
                ],
            }
        )

        result = await service.evaluate("umo-1", "confirm('bsk-evaluate-probe')")

        self.assertEqual(len(result.dialogs), 1)
        dialog = result.dialogs[0]
        self.assertIsInstance(dialog, EvaluateDialog)
        self.assertEqual(dialog.type, "confirm")
        self.assertEqual(dialog.handled, "accepted")

    async def test_rendered_text_warns_dialog_was_auto_handled(self) -> None:
        """★ 渲染必须点明"自动处理、不是用户点的" —— 这是安全信息。"""
        service, _ = self.make(
            {
                "ok": True,
                "value": True,
                "dialogs": [
                    {"type": "confirm", "message": "确定要提交吗", "handled": "accepted"}
                ],
            }
        )

        result = await service.evaluate("umo-1", "confirm('确定要提交吗')")
        rendered = service.render_evaluate(result, "confirm('确定要提交吗')")

        self.assertIn("自动", rendered)
        self.assertIn("确定要提交吗", rendered)
        self.assertIn("不是用户点的", rendered)

    async def test_no_dialogs_field_is_fine(self) -> None:
        """★ 实测：没有弹窗时 ``dialogs`` 字段**整个不存在**。"""
        service, _ = self.make({"ok": True, "value": 2})

        result = await service.evaluate("umo-1", "1+1")

        self.assertEqual(result.dialogs, [])
        self.assertNotIn("对话框", service.render_evaluate(result, "1+1"))


# --- 6.6 模型可读性 ----------------------------------------------------


class TestEvaluateRenderShape(EvaluateServiceCase):
    """渲染结果要能被模型直接读懂，且不泄露异常堆栈。"""

    async def test_render_echoes_expression(self) -> None:
        """回显表达式，模型才能把结果与它发起的调用对上。"""
        service, _ = self.make({"ok": True, "value": "Example Domain"})

        result = await service.evaluate("umo-1", "document.title")
        rendered = service.render_evaluate(result, "document.title")

        self.assertIn("document.title", rendered)

    async def test_object_is_pretty_printed_json(self) -> None:
        """对象被渲染成可读 JSON（实测 ``({a:1,b:'x'})`` 返回嵌套对象）。"""
        service, _ = self.make({"ok": True, "value": {"a": 1, "b": "x", "c": [1, 2]}})

        result = await service.evaluate("umo-1", "({a:1,b:'x',c:[1,2]})")
        rendered = service.render_evaluate(result, "({a:1,b:'x',c:[1,2]})")

        self.assertIn('"a"', rendered)
        self.assertIn("1", rendered)

    async def test_undefined_is_explained_not_printed_as_none(self) -> None:
        """★ 不能给模型看 "返回值：None"（Python 的字面量）——那会误导它。"""
        service, _ = self.make({"ok": True})

        result = await service.evaluate("umo-1", "undefined")
        rendered = service.render_evaluate(result, "undefined")

        self.assertIn("undefined", rendered)
        self.assertNotIn("None", rendered)

    async def test_non_serialisable_value_does_not_crash(self) -> None:
        """★ 实测 ``document.body`` 返回 ``{}``（DOM 节点无法按值序列化），
        但载荷仍可能是任何东西。渲染绝不能抛异常 —— 脚本已经执行过了。
        """
        class Weird:
            def __repr__(self) -> str:
                return "<weird>"

        service, _ = self.make({"ok": True, "value": Weird()})

        result = await service.evaluate("umo-1", "document.body")
        rendered = service.render_evaluate(result, "document.body")

        self.assertIn("weird", rendered)


# --- 6.7 模型层解析（纯函数）------------------------------------------


class TestEvaluateModels(unittest.TestCase):
    """``EvaluateResult`` 的宽松解析 —— bsk 的输出是外部输入。"""

    def test_from_json_tolerates_garbage(self) -> None:
        for junk in (None, [], "x", 3, object()):
            with self.subTest(junk=repr(junk)):
                result = EvaluateResult.from_json(junk)
                self.assertFalse(result.ok)
                self.assertFalse(result.has_value)

    def test_has_value_distinguishes_null_from_missing(self) -> None:
        self.assertTrue(EvaluateResult.from_json({"ok": True, "value": None}).has_value)
        self.assertFalse(EvaluateResult.from_json({"ok": True}).has_value)

    def test_non_dict_dialogs_are_ignored(self) -> None:
        """``dialogs`` 结构不对时退化成空列表，而不是让整个结果解析失败。"""
        result = EvaluateResult.from_json(
            {"ok": True, "value": 1, "dialogs": ["nonsense", None, 42]}
        )
        self.assertEqual(len(result.dialogs), 3)
        self.assertEqual(result.dialogs[0].type, "")

    def test_non_dict_error_is_ignored(self) -> None:
        result = EvaluateResult.from_json({"ok": False, "error": "oops"})
        self.assertFalse(result.ok)
        self.assertIsNone(result.error)

    def test_error_location_omits_meaningless_zero_column(self) -> None:
        """★ 实测 ``column`` 经常是 0 —— 别渲染成"第 1 行第 0 列"。"""
        error = EvaluateError.from_json({"text": "Error: boom", "line": 1, "column": 0})

        self.assertNotIn("第 0 列", error.location())
        self.assertIn("第 1 行", error.location())

    def test_error_location_empty_when_line_unknown(self) -> None:
        self.assertEqual(EvaluateError.from_json({}).location(), "")

    def test_error_location_includes_column_when_present(self) -> None:
        error = EvaluateError.from_json(
            {"text": "SyntaxError", "line": 1, "column": 2}
        )
        self.assertIn("第 2 列", error.location())

    def test_ok_accepts_string_truthy(self) -> None:
        """bsk 输出里 ``ok`` 理论上可能是字符串（容忍外部输入）。"""
        self.assertTrue(EvaluateResult.from_json({"ok": "true", "value": 1}).ok)
        self.assertFalse(EvaluateResult.from_json({"ok": "false"}).ok)


# ======================================================================
# 7. 框架上限钳制：别被 AstrBot 从外面掐断
#
# 背景（这次要修的第二个**真实**问题，与文档错误是两件事）：
#
#   ● 文档错误（已被实测推翻）：README/ARCHITECTURE 曾写"全页截图必须同时调大
#     插件的 command_timeout_sec 与 AstrBot 的 tool_call_timeout"。实测长页面
#     全页截图只要 11.72 / 11.11 / 10.91 秒（1820x11741，4.5MB），短页面 2.91 秒，
#     而框架默认上限是 120 秒 —— 只用了约 1/10，留了 108 秒余量。所以那条警告
#     在实践中是多余的，已从文档里删掉。
#
#   ● 真实问题（本段要钉住的）：插件给全页截图准备的最终超时是 **180 秒**，
#     而框架默认只等 120 秒。于是插件允许自己等 180 秒，框架却在 120 秒时把它
#     掐断 —— 用户看到的是框架抛的英文
#     `tool <name> execution timeout after 120 seconds.`，
#     **而不是插件精心写的中文提示**（"网页响应太慢……该改哪个配置"）。
#     这既难懂，也让"会话可能留下未完成状态"这件事被掩盖。
#
# 修法（插件侧，不要求用户改任何配置）：
#
#     最终超时 = min( max(内置下限, 用户配置), 框架上限 - 安全余量 )
#
# 三条不变量（下面逐个钉死）：
#   1. 框架上限**已知**时，最终超时严格低于它（不会被框架抢先掐断）；
#   2. 框架上限**未知**（None / 读不到 / 结构畸形）时，结果与改动前逐字节相同
#      —— 拿不到事实就不该凭猜测缩短用户愿意等待的时间；
#   3. 钳制**不会低于**一个合理下限（否则退化成"秒失败"，比超时更糟）。
# ======================================================================

from bsk.config import (  # noqa: E402
    FRAMEWORK_TIMEOUT_FLOOR_SEC,
    FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC,
)


def make_clamped_service(
    framework_limit: Any, **settings_overrides: Any
) -> tuple[BskService, FakeRunner]:
    """构造注入了框架上限的服务 + 假 runner。

    与上面的 ``make_service`` 分开写（而不是改它）是为了**不动既有测试**：
    那个函数没有"框架上限"这个形参，而既有用例必须继续按老方式构造
    （等价于"框架上限未知"，行为不变）。

    Args:
        framework_limit: 框架上限，原样传给 ``BskService``（含各种畸形值）。
        **settings_overrides: 透传给 ``make_settings``。

    Returns:
        ``(服务, 假 runner)``。
    """
    fake = FakeRunner()
    return (
        BskService(
            make_settings(**settings_overrides),
            runner=fake,
            framework_tool_timeout=framework_limit,
        ),
        fake,
    )


class TestFrameworkTimeoutClamp(unittest.TestCase):
    """框架上限已知时，最终超时被钳到上限以下。"""

    def test_fullpage_is_clamped_below_framework_limit(self) -> None:
        """★ 核心用例：框架 120 秒时，全页截图的 180 秒被钳到 115 秒。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        timeout = service._timeout(TIMEOUT_FULLPAGE)

        self.assertEqual(timeout, 115.0)
        self.assertLess(timeout, 120.0, "插件必须在框架动手之前自己超时")
        self.assertLess(timeout, TIMEOUT_FULLPAGE, "钳制没有生效")

    def test_clamped_value_leaves_the_safety_margin(self) -> None:
        """钳制后与框架上限之间恰好留出安全余量（不是贴着上限）。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        self.assertEqual(
            service._timeout(TIMEOUT_FULLPAGE),
            120.0 - FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC,
        )
        self.assertGreaterEqual(FRAMEWORK_TIMEOUT_SAFETY_MARGIN_SEC, 1.0)

    def test_short_commands_are_not_touched_when_below_the_ceiling(self) -> None:
        """★ 钳制只压"本来就超限"的命令，不动其他的（observe 仍是 110）。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), 110.0)
        self.assertEqual(service._timeout(TIMEOUT_NAVIGATE), 110.0)
        self.assertEqual(service._timeout(TIMEOUT_QUICK), 110.0)
        # 只有全页截图的 180 高过 115。
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 115.0)

    def test_larger_framework_limit_still_clamps(self) -> None:
        """用户把框架上限放宽到 300 秒时，180 的下限保住，钳制不再收紧。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=300.0,
        )

        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), TIMEOUT_FULLPAGE)
        self.assertEqual(service._timeout(TIMEOUT_OBSERVE), 110.0)

    def test_lower_framework_limit_clamps_everything(self) -> None:
        """框架上限只有 60 秒时，所有命令都被压到 55 秒。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=60.0,
        )

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(service._timeout(builtin), 55.0)
                self.assertLess(service._timeout(builtin), 60.0)

    def test_framework_limit_as_string_is_accepted(self) -> None:
        """配置里写成字符串 ``"120"`` 时也要能钳制（JSON 手改很常见）。"""
        service = BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout="120",  # type: ignore[arg-type]
        )

        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 115.0)


class TestFrameworkTimeoutUnknown(unittest.TestCase):
    """★ 框架上限未知时，行为与改动前**逐字节相同**（不许凭猜测缩短超时）。"""

    JUNK_LIMITS: tuple[Any, ...] = (
        None,
        "",
        "abc",
        True,
        False,
        0,
        -5.0,
        float("nan"),
        float("inf"),
        [],
        {},
        object(),
    )

    def _service(self, limit: Any = None) -> BskService:
        return BskService(
            make_settings(command_timeout_sec=110.0),
            runner=FakeRunner(),
            framework_tool_timeout=limit,
        )

    def test_none_keeps_old_behaviour_exactly(self) -> None:
        """★ 不传框架上限：结果与 ``max(内置下限, 用户配置)`` 完全一致。"""
        service = self._service(None)

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(service._timeout(builtin), max(builtin, 110.0))
        # 全页截图仍然是它自己的预算（120）—— 读不到框架上限就**不**缩短它。
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), TIMEOUT_FULLPAGE)
        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), 120.0)

    def test_old_constructor_call_is_unchanged(self) -> None:
        """不传这个新参数时与传 None 等价（既有调用点零改动）。"""
        old = BskService(make_settings(command_timeout_sec=110.0), runner=FakeRunner())
        explicit = self._service(None)

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(old._timeout(builtin), explicit._timeout(builtin))

    def test_junk_limits_are_treated_as_unknown(self) -> None:
        """任何非法取值都等于"未知"，绝不因此缩短超时，也绝不抛异常。"""
        for junk in self.JUNK_LIMITS:
            service = self._service(junk)
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(service.framework_tool_timeout)
                # 全页截图拿到的仍是它自己的预算（120），没有被"未知"改动。
                self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), TIMEOUT_FULLPAGE)
                self.assertEqual(service._timeout(TIMEOUT_QUICK), 110.0)

    def test_missing_settings_attribute_still_works(self) -> None:
        """配置对象畸形 + 框架上限已知时，两个兜底同时生效且不抛异常。

        Note:
            期望值是 ``min(builtin, 115)`` 而不是 ``min(builtin, 115)`` 再抬到下限：
            钳制的下限（10 秒）加在**上限**上，不会把本来就短的命令抬高 ——
            5 秒的只读命令仍然拿 5 秒。这是刻意的：下限是为了防止钳制把命令
            压成"秒失败"，不是为了给短命令加时间。
        """
        service = BskService(
            types.SimpleNamespace(),  # 完全空的对象
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(service._timeout(builtin), min(builtin, 115.0))
        # 5 秒的只读命令**不该**被抬高到 10 秒。
        self.assertEqual(service._timeout(TIMEOUT_QUICK), 5.0)

    def test_none_limit_equals_no_parameter(self) -> None:
        """显式传 ``None`` 与完全不传这个新参数，结果必须一模一样。

        后者是既有的所有调用点（含其他测试文件与验证脚本）—— 它们的行为
        绝不能因为这次改动而变化。
        """
        explicit, _ = make_clamped_service(None, command_timeout_sec=110.0)
        implicit = BskService(
            make_settings(command_timeout_sec=110.0), runner=FakeRunner()
        )

        for builtin in ALL_BUILTIN_TIMEOUTS:
            with self.subTest(builtin=builtin):
                self.assertEqual(
                    explicit._timeout(builtin), implicit._timeout(builtin)
                )


class TestFrameworkTimeoutFloor(unittest.TestCase):
    """★ 钳制不得把超时压到"秒失败"级别。"""

    def test_tiny_framework_limit_does_not_go_below_floor(self) -> None:
        """框架上限 12 秒时，公式会算出 7，但最终值是下限 10。"""
        service = BskService(
            make_settings(command_timeout_sec=5.0),
            runner=FakeRunner(),
            framework_tool_timeout=12.0,
        )

        self.assertEqual(service._timeout(TIMEOUT_FULLPAGE), FRAMEWORK_TIMEOUT_FLOOR_SEC)
        self.assertEqual(FRAMEWORK_TIMEOUT_FLOOR_SEC, 10.0)

    def test_absurd_framework_limit_still_yields_usable_timeout(self) -> None:
        """框架上限被填成 1 秒这种荒唐值时，最终超时仍是可用的正数。"""
        for limit in (1.0, 0.5, 3.0):
            service = BskService(
                make_settings(command_timeout_sec=60.0),
                runner=FakeRunner(),
                framework_tool_timeout=limit,
            )
            for builtin in ALL_BUILTIN_TIMEOUTS:
                with self.subTest(limit=limit, builtin=builtin):
                    timeout = service._timeout(builtin)
                    self.assertGreaterEqual(timeout, FRAMEWORK_TIMEOUT_FLOOR_SEC)
                    self.assertGreater(timeout, 0)

    def test_clamp_never_raises_and_always_returns_float(self) -> None:
        """钳制路径上无论塞什么，返回值都是正 float。"""
        for limit in (None, 120.0, "120", 0, -1, float("nan"), object()):
            service = BskService(
                make_settings(command_timeout_sec=110.0),
                runner=FakeRunner(),
                framework_tool_timeout=limit,
            )
            for builtin in ALL_BUILTIN_TIMEOUTS:
                with self.subTest(limit=repr(limit), builtin=builtin):
                    self.assertIsInstance(service._timeout(builtin), float)
                    self.assertGreater(service._timeout(builtin), 0)


class TestFrameworkTimeoutCeiling(unittest.TestCase):
    """``framework_timeout_ceiling()`` 的语义（供其他代码判断"还有多少预算"）。"""

    def test_unknown_returns_none(self) -> None:
        service = BskService(
            make_settings(command_timeout_sec=60.0), runner=FakeRunner()
        )
        self.assertIsNone(service.framework_timeout_ceiling())

    def test_known_subtracts_the_margin(self) -> None:
        service = BskService(
            make_settings(command_timeout_sec=60.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )
        self.assertEqual(service.framework_timeout_ceiling(), 115.0)

    def test_ceiling_respects_the_floor(self) -> None:
        service = BskService(
            make_settings(command_timeout_sec=60.0),
            runner=FakeRunner(),
            framework_tool_timeout=6.0,
        )
        self.assertEqual(service.framework_timeout_ceiling(), FRAMEWORK_TIMEOUT_FLOOR_SEC)


class TestFrameworkTimeoutStartupWarning(unittest.TestCase):
    """启动告警：**只在真有风险时**才提示（不吓唬用户，也不隐瞒极端情况）。"""

    def _service(self, limit: Any) -> BskService:
        return BskService(
            make_settings(command_timeout_sec=60.0),
            runner=FakeRunner(),
            framework_tool_timeout=limit,
        )

    def test_default_120_seconds_warns(self) -> None:
        """★ 默认配置必然告警：框架 120 = 全页截图预算 120 → 会被钳到 115。

        这正是它该做的事（用户明确确认过）：如实说明"预算顶到了框架上限"，
        同时说明**默认值已足够**（实测最长 11.7 秒），不要让人以为出了故障。
        """
        warnings = self._service(120.0).startup_warnings()

        self.assertEqual(len(warnings), 1)
        text = warnings[0]
        # 必须给出"该改哪个配置项"，否则用户不知道下一步做什么。
        self.assertIn("agent_runner.config.misc.tool_call_timeout", text)
        self.assertIn("120", text)
        # 必须同时说明实测数据与"不用改"，避免把理论风险说成故障。
        self.assertIn("11 秒", text)
        self.assertIn("不影响正常使用", text)

    def test_warning_does_not_read_like_an_error(self) -> None:
        """★ 告警措辞必须读起来像解释，不像报错（用户明确要求）。

        因为它默认必然出现 —— 如果写成"失败/异常"，所有用户一装就以为坏了。
        注意"错误"这个词**只允许**出现在"这不是错误"这种否定句里，所以这里
        先断言否定句存在，再检查其余告警词一个都不出现。
        """
        text = self._service(120.0).startup_warnings()[0]

        # 必须把话说死：这不是错误。
        self.assertIn("这不是错误", text)
        for alarming in ("失败", "异常", "损坏", "不可用", "出错"):
            with self.subTest(word=alarming):
                self.assertNotIn(alarming, text)

    def test_limit_above_budget_is_silent(self) -> None:
        """框架上限大于全页截图预算时没有风险，一条都不打印。"""
        for limit in (121.0, 200.0, 300.0):
            with self.subTest(limit=limit):
                self.assertEqual(self._service(limit).startup_warnings(), [])

    def test_silent_when_configured_budget_is_below_limit(self) -> None:
        """用户把整页截图预算调小到框架上限之下 → 不告警（已经没有风险）。"""
        service = BskService(
            make_settings(command_timeout_sec=60.0, fullpage_timeout_sec=60.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        self.assertEqual(service.startup_warnings(), [])

    def test_unknown_limit_is_silent(self) -> None:
        """读不到框架上限时不告警 —— 没有事实依据就不制造担心。"""
        for junk in (None, "", "abc", 0, -1, object()):
            with self.subTest(junk=repr(junk)):
                self.assertEqual(self._service(junk).startup_warnings(), [])

    def test_exact_boundary_warns(self) -> None:
        """正好等于预算时仍然算有风险（此时撞不上安全余量）。"""
        self.assertEqual(len(self._service(TIMEOUT_FULLPAGE).startup_warnings()), 1)

    def test_warning_is_chinese_and_actionable(self) -> None:
        text = self._service(120.0).startup_warnings()[0]

        self.assertIn("中文提示", text)
        self.assertIn("重启", text)
        # 必须说明我们**已经**做了什么（钳制到 115），否则用户以为插件没处理。
        self.assertIn("115", text)
        # 必须点名插件里的那一项，用户才知道第二处该动哪里。
        self.assertIn("fullpage_timeout_sec", text)


# ----------------------------------------------------------------------
# 7.1 集成式：钳制后的值必须真的传给子进程
# ----------------------------------------------------------------------


class TestFrameworkClampReachesSubprocess(ServiceIntegrationCase):
    """★ 钳制不能只停在 ``_timeout()`` 里 —— 实际传给子进程的必须是钳制后的值。"""

    def make_clamped(
        self, framework_limit: Any, **overrides: Any
    ) -> tuple[BskService, FakeRunner]:
        """``self.make`` 的带框架上限版本（截图目录等脚手架保持一致）。"""
        overrides.setdefault("screenshot_dir", self.tmp.name)
        return make_clamped_service(framework_limit, **overrides)

    async def test_fullpage_screenshot_uses_clamped_timeout(self) -> None:
        service, runner = self.make_clamped(
            command_timeout_sec=110.0, framework_limit=120.0
        )

        await service.screenshot("umo-1", full_page=True)

        self.assertEqual(runner.timeout_for("screenshot"), 115.0)
        self.assertLess(runner.timeout_for("screenshot"), 120.0)

    async def test_observe_keeps_its_user_value(self) -> None:
        """钳制不影响本来就合规的命令（observe 仍是 110，不是 115）。"""
        service, runner = self.make_clamped(
            command_timeout_sec=110.0, framework_limit=120.0
        )

        await service.observe("umo-1")

        self.assertEqual(runner.timeout_for("observe"), 110.0)

    async def test_unknown_framework_limit_keeps_the_budget(self) -> None:
        """框架上限未知时，全页截图实际拿到的仍然是它自己的预算（默认 120）。"""
        service, runner = self.make(command_timeout_sec=110.0)

        await service.screenshot("umo-1", full_page=True)

        self.assertEqual(runner.timeout_for("screenshot"), TIMEOUT_FULLPAGE)

    async def test_low_framework_limit_records_55_for_every_command(self) -> None:
        """框架上限 60 秒时，所有实际超时都不超过 55 秒。

        Note:
            ``session start`` 那条命令由 ``SessionManager`` 管（它有自己的一套
            预算推导，见 ``session.START_TIMEOUT_FLOOR_SEC``），**不经过**
            ``service._timeout()``，所以不在本断言的范围内 —— 这里只钉
            "由 service 合成超时的那些命令"。
        """
        service, runner = self.make_clamped(
            command_timeout_sec=110.0, framework_limit=60.0
        )

        await service.open_page("umo-1", "https://example.com")
        await service.act("umo-1", "click", target="@e3")
        await service.screenshot("umo-1", full_page=True)
        await service.read_console("umo-1")
        await service.list_browsers()
        await service.status("umo-1")

        self.assertTrue(runner.timeouts, "假 runner 一次都没被调用，测试本身失效了")
        for call, timeout in zip(runner.calls, runner.timeouts):
            if call[:2] == ["session", "start"]:
                continue
            with self.subTest(call=call):
                self.assertIsNotNone(timeout)
                self.assertLessEqual(timeout, 55.0)


# ======================================================================
# 8. `fullpage_timeout_sec`：整页截图的超时**可配置**
#
# 背景（用户拍板的新需求）：
#
#   ● 整页截图的内置下限从硬编码的 180 秒改为**可配置的** 120 秒默认值
#     （`settings.fullpage_timeout_sec`，范围 30–600）；
#   ● 改小是因为 180 秒对实测的 11.72 秒过于宽松（15 倍余量），120 秒既贴着
#     "约 10 倍余量"这个合理值，又与框架默认上限对齐（用户想表达的是"最多 2 分钟"）；
#   ● 上限给到 600 秒，让放宽了框架 `tool_call_timeout` 的用户能真的用上更大的值。
#
# 本段要钉死三条**容易写错**的点：
#   1. 全页截图读的是**配置项**，不是硬编码常量（否则用户改了没用）；
#   2. **视口截图完全不受这个配置影响**（回归保护：这是最容易连带改坏的地方，
#      视口截图实测只要 0.12 秒，跟着变成 120 秒纯属误伤）；
#   3. 配置项自己也要被夹取（30–600），越界值不能变成"永不超时"或"秒失败"。
#
# 另外还要钉住用户明确提出的**语义区分**：
#   这一项管的是"调用浏览器这段最多等多久"，**与模型出 token 的快慢无关**
#   —— 框架的 tool_call_timeout 同样不包含模型生成时间。文档里不能说
#   "调大它能让慢模型不出错"（那是错的），本段用文案断言把这条守住。
# ======================================================================

from bsk.config import (  # noqa: E402
    DEFAULT_FULLPAGE_TIMEOUT_SEC,
    FULLPAGE_TIMEOUT_MAX_SEC,
    FULLPAGE_TIMEOUT_MIN_SEC,
)


class TestFullpageTimeoutIsConfigurable(unittest.TestCase):
    """全页截图必须走 ``settings.fullpage_timeout_sec``，而不是硬编码常量。"""

    def test_default_matches_the_constant_and_schema_default(self) -> None:
        """默认值三处一致：常量、Settings、schema。"""
        self.assertEqual(DEFAULT_FULLPAGE_TIMEOUT_SEC, 120.0)
        self.assertEqual(TIMEOUT_FULLPAGE, DEFAULT_FULLPAGE_TIMEOUT_SEC)
        self.assertEqual(parse_settings({}).fullpage_timeout_sec, 120.0)

    def test_user_value_is_used(self) -> None:
        """★ 用户配 300（并放宽了框架上限）时，全页截图真的拿到 300。"""
        service = BskService(
            make_settings(fullpage_timeout_sec=300.0),
            runner=FakeRunner(),
            framework_tool_timeout=600.0,
        )

        self.assertEqual(service.fullpage_budget(), 300.0)
        self.assertEqual(service._timeout(service.fullpage_budget()), 300.0)

    def test_user_value_below_default_is_respected(self) -> None:
        """用户把它调小（90 秒）时也生效 —— 不是"只能调大"。"""
        service = BskService(
            make_settings(fullpage_timeout_sec=90.0),
            runner=FakeRunner(),
            framework_tool_timeout=600.0,
        )

        self.assertEqual(service._timeout(service.fullpage_budget()), 90.0)

    def test_config_value_is_clamped_to_the_allowed_range(self) -> None:
        """★ 越界值被夹到 ``[30, 600]``（不是原样使用）。"""
        cases = (
            (1.0, FULLPAGE_TIMEOUT_MIN_SEC),
            (0.0, FULLPAGE_TIMEOUT_MIN_SEC),
            (-10.0, FULLPAGE_TIMEOUT_MIN_SEC),
            (29.9, FULLPAGE_TIMEOUT_MIN_SEC),
            (99999.0, FULLPAGE_TIMEOUT_MAX_SEC),
            (601.0, FULLPAGE_TIMEOUT_MAX_SEC),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                settings = make_settings(fullpage_timeout_sec=raw)
                self.assertEqual(settings.fullpage_timeout_sec, expected)
                self.assertGreaterEqual(settings.fullpage_timeout_sec, 30.0)
                self.assertLessEqual(settings.fullpage_timeout_sec, 600.0)

    def test_junk_config_falls_back_to_default(self) -> None:
        """配置被填成乱七八糟时回退默认 120，不抛异常、也不变成 0。"""
        for junk in (None, "abc", True, False, [], {}, float("nan"), object()):
            with self.subTest(junk=repr(junk)):
                settings = make_settings(fullpage_timeout_sec=junk)
                self.assertEqual(settings.fullpage_timeout_sec, 120.0)

    def test_numeric_string_is_accepted(self) -> None:
        """JSON 手改成 ``"300"`` 时按 300 处理。"""
        self.assertEqual(
            make_settings(fullpage_timeout_sec="300").fullpage_timeout_sec, 300.0
        )

    def test_budget_reads_config_not_a_hardcoded_constant(self) -> None:
        """★ 字段名漂移会把用户配置静默吞掉 —— 这条按**名字**钉住它。"""
        from dataclasses import fields as _fields

        from bsk.config import Settings as _Settings

        settings = make_settings(fullpage_timeout_sec=240.0)
        self.assertIn(
            "fullpage_timeout_sec",
            {f.name for f in _fields(_Settings)},
        )
        service = BskService(settings, runner=FakeRunner())
        self.assertEqual(service.fullpage_budget(), 240.0)

    def test_budget_falls_back_when_settings_lacks_the_field(self) -> None:
        """旧配置对象没有这个字段时回退到默认值，不抛 ``AttributeError``。"""
        service = BskService(types.SimpleNamespace(), runner=FakeRunner())

        self.assertEqual(service.fullpage_budget(), TIMEOUT_FULLPAGE)
        self.assertEqual(service.fullpage_budget(), 120.0)

    def test_budget_junk_values_fall_back(self) -> None:
        """字段存在但取值荒唐时回退默认值（且绝不为 0）。"""
        for junk in (None, "300", True, False, 0, -1.0, float("nan"), float("inf")):
            with self.subTest(junk=repr(junk)):
                service = BskService(
                    types.SimpleNamespace(fullpage_timeout_sec=junk),
                    runner=FakeRunner(),
                )
                self.assertEqual(service.fullpage_budget(), TIMEOUT_FULLPAGE)
                self.assertGreater(service.fullpage_budget(), 0)


class TestViewportScreenshotUnaffected(unittest.TestCase):
    """★ 回归保护：``fullpage_timeout_sec`` **绝不能**影响视口截图。"""

    def test_viewport_ignores_the_fullpage_setting(self) -> None:
        """★ 改 ``fullpage_timeout_sec`` **不影响**视口截图的超时。

        做法：同一个 ``command_timeout_sec`` 下，只改 fullpage 配置，
        断言视口截图那条路径算出的值**不变**。
        （视口截图的值本身是 ``max(TIMEOUT_SCREENSHOT, command_timeout_sec)``，
        这里固定 command_timeout_sec=5，把它隔离出来，只看 fullpage 配置的影响。）
        """
        baseline = BskService(
            make_settings(command_timeout_sec=5.0, fullpage_timeout_sec=120.0),
            runner=FakeRunner(),
        )._timeout(TIMEOUT_SCREENSHOT)
        self.assertEqual(baseline, TIMEOUT_SCREENSHOT)
        self.assertEqual(baseline, 30.0)

        for configured in (30.0, 60.0, 120.0, 300.0, 600.0):
            with self.subTest(fullpage_timeout_sec=configured):
                settings = make_settings(
                    command_timeout_sec=5.0, fullpage_timeout_sec=configured
                )
                service = BskService(settings, runner=FakeRunner())
                # 视口截图不受 fullpage 配置影响 —— 始终等于基线值。
                self.assertEqual(service._timeout(TIMEOUT_SCREENSHOT), baseline)
                # 而全页那一项确实跟着配置走。
                self.assertEqual(service.fullpage_budget(), configured)

    def test_both_paths_use_different_builtins(self) -> None:
        """两条分支的内置值必须不同 —— 合并它们就是这次要防的误伤。"""
        self.assertNotEqual(TIMEOUT_SCREENSHOT, TIMEOUT_FULLPAGE)
        self.assertLess(TIMEOUT_SCREENSHOT, TIMEOUT_FULLPAGE)


class TestFullpageTimeoutStillClampedByFramework(unittest.TestCase):
    """★ 新配置项同样受框架上限钳制（这是它与旧常量的关键区别）。"""

    def test_large_configured_value_is_clamped_by_the_framework(self) -> None:
        """用户填 600，但框架仍是默认 120 → 实际拿到 115（不是 600）。"""
        service = BskService(
            make_settings(fullpage_timeout_sec=600.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        self.assertEqual(service.fullpage_budget(), 600.0)
        self.assertEqual(service._timeout(service.fullpage_budget()), 115.0)

    def test_large_configured_value_is_usable_once_framework_is_raised(self) -> None:
        """★ 只有**同时**放宽框架上限，600 才能真的用上（两处一起调才有效）。"""
        service = BskService(
            make_settings(fullpage_timeout_sec=600.0),
            runner=FakeRunner(),
            framework_tool_timeout=900.0,
        )

        self.assertEqual(service._timeout(service.fullpage_budget()), 600.0)

    def test_default_pair_yields_115(self) -> None:
        """★ 默认组合（两边都是 120）算出 115 —— 用户要知道这个连带关系。"""
        service = BskService(
            make_settings(fullpage_timeout_sec=120.0),
            runner=FakeRunner(),
            framework_tool_timeout=120.0,
        )

        self.assertEqual(service._timeout(service.fullpage_budget()), 115.0)
        # 并且启动告警必须解释这件事（默认必然触发）。
        self.assertEqual(len(service.startup_warnings()), 1)


class TestFullpageTimeoutDocumentationWording(unittest.TestCase):
    """审查文案：不许把"浏览器操作超时"说成能解决"模型慢"。"""

    def _texts(self) -> list[str]:
        project = Path(__file__).resolve().parent.parent
        return [
            (project / "README.md").read_text(encoding="utf-8"),
            (project / "_conf_schema.json").read_text(encoding="utf-8"),
            (project / "bsk" / "config.py").read_text(encoding="utf-8"),
        ]

    def test_docs_explain_the_scope_of_this_setting(self) -> None:
        """★ 必须写明它只管"浏览器操作"耗时，与模型出字快慢无关。

        用户提出的理由是"有的模型很慢"，但技术上 ``tool_call_timeout`` 限制的是
        工具执行耗时，**不包含**模型生成 token 的时间 —— 所以文档必须划清界限，
        不能暗示"调大它能让慢模型不出错"。
        """
        joined = "\n".join(self._texts())

        self.assertIn("浏览器", joined)
        # 明确写出与模型快慢无关。
        self.assertIn("模型", joined)
        self.assertTrue(
            any(
                phrase in joined
                for phrase in (
                    "与模型生成回复的快慢无关",
                    "与模型出 token",
                    "不包含**模型出 token",
                    "与模型生成 token",
                )
            ),
            "文档没有说清『这一项与模型快慢无关』",
        )

    def test_docs_explain_the_dual_clamp_relation(self) -> None:
        """★ 必须说明"在这里填超过框架上限的值不会等更久"这个连带关系。"""
        joined = "\n".join(self._texts())

        self.assertIn("tool_call_timeout", joined)
        self.assertTrue(
            any(
                phrase in joined
                for phrase in ("不会让实际等得更久", "实际并不会更久", "钳到")
            ),
            "文档没有说清钳制带来的连带关系",
        )

    def test_no_stale_180_claim_remains(self) -> None:
        """过时的"整页截图需要 180 秒 / 必须两处一起调大"说法必须清除。"""
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("需要两处一起调大才可能成功", readme)
        self.assertNotIn("只改一处仍然会在 120 秒处被打断", readme)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
