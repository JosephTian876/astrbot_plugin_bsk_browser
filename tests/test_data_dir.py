"""插件数据目录与降级容错的单元测试 —— ``bsk/paths.py`` 的落盘位置语义。

背景（本次要修的审核拒绝项）：AstrBot 插件市场的审核规则要求持久化数据存到
``data/plugin_data/astrbot_plugin_bsk_browser``。此前：

- 会话所有权 journal 写在 ``<系统临时目录>/astrbot_bsk_browser/sessions.json``；
- 截图写在 ``<系统临时目录>/astrbot_bsk_shots/``。

两者都迁到插件数据目录，但**必须保留"data_dir 不可用时退回临时目录"的容错** ——
这条同样是审核的明确要求，而且 ``StarTools.get_data_dir()`` 在失败时会抛异常，
插件加载路径上绝不能因此崩掉。降级后的统一落点是
``<系统临时目录>/astrbot_bsk_browser``（journal 在其下，截图在其 ``shots`` 子目录）。

本文件覆盖：

1. **落盘位置** —— 注入 ``data_dir`` 时，journal 与截图的默认路径都落在它之下；
2. **降级容错** —— ``data_dir`` 为空 / 非法 / 不可写时退回临时目录，且不抛异常；
3. **配置优先级** —— 用户显式填的 ``journal_path`` / ``screenshot_dir`` 仍然最高，
   注入的 ``data_dir`` 不得覆盖它；
4. **贯通** —— 以上三点在 ``BskService`` 里真的生效（用假 runner 记录的 ``--out``
   实参来验证，而不是只看 ``Settings`` 的字段值）。

pytest 在本机不可用，因此用标准库 ``unittest``；异步用例用
``unittest.IsolatedAsyncioTestCase``（Python 3.12 原生支持）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

# 让测试能 import 到项目的 bsk 包（与 test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.config import parse_settings  # noqa: E402
from bsk.models import BskResult  # noqa: E402
from bsk.paths import (  # noqa: E402
    default_journal_path,
    default_shot_dir,
    resolve_data_dir,
)
from bsk.service import BskService  # noqa: E402

# 降级目标：系统临时目录。
#
# 断言"降级发生了"不能只看 ``under(x, TEMP_ROOT)`` —— 本测试自己就把 data_dir 造在
# 临时目录下，那样写对"原样返回被拒绝的 data_dir"也成立，等于没断言。所以每个用例
# 都补一条"被拒绝的那个 data_dir 不得出现在结果里"。
#
# journal 的位置可以钉得更死：旧的降级行为就是 ``<临时目录>/astrbot_bsk_browser``，
# 这条已被 ``test_journal.py`` 与实现共同固定。截图的降级目录名不做硬断言 ——
# 审核只要求"退回临时目录"，钉死具体子目录名会把实现自由度当成缺陷来报。
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
LEGACY_TEMP_JOURNAL_ROOT = (TEMP_ROOT / "astrbot_bsk_browser").resolve()


def under(child: Path, parent: Path) -> bool:
    """``child`` 是否落在 ``parent`` 之下（含"就是 parent 本身"）。"""
    try:
        resolved_child = Path(child).resolve()
        resolved_parent = Path(parent).resolve()
    except Exception:  # noqa: BLE001 - 非法路径在断言里算"不满足"而不是把用例炸掉
        return False
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


class FakeRunner:
    """假的 ``BskRunner``：记录每次调用的参数，永不真的起子进程。"""

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

    def out_path_for(self, command: str, nth: int = 0) -> str:
        """取某条命令第 nth 次调用时 ``--out`` 的实参（截图落盘位置）。"""
        matches = [c for c in self.calls if c and c[0] == command]
        argv = matches[nth]
        return argv[argv.index("--out") + 1]


class DataDirCase:
    """公共脚手架：每个用例一个干净的可写 data_dir。

    刻意做成不继承 ``TestCase`` 的 mixin：下面既有同步用例也有
    ``IsolatedAsyncioTestCase`` 的异步用例，两者都要这套脚手架。
    """

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="bsk-datadir-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "astrbot_plugin_bsk_browser"

    @staticmethod
    def build_settings(data_dir: Any = "", **overrides: Any) -> Any:
        """按给定 ``data_dir`` 解析配置（其余用最小可用配置）。"""
        raw: dict[str, Any] = {
            "bsk_path": "bsk",
            # 显式指定浏览器，避免触发 probe_browser 里的同步子进程探测。
            "browser_instance_id": "c900a3da",
            "command_timeout_sec": 60.0,
        }
        raw.update(overrides)
        return parse_settings(raw, data_dir=data_dir)

    def make_settings(self, **overrides: Any) -> Any:
        """默认把 ``data_dir`` 指向本用例的数据目录。"""
        return self.build_settings(data_dir=str(self.data_dir), **overrides)


# ----------------------------------------------------------------------
# 1. 落盘位置：注入 data_dir 时路径落在它之下
# ----------------------------------------------------------------------


class TestPathsUnderDataDir(DataDirCase, unittest.TestCase):
    """``bsk/paths.py`` 三个函数的正常路径语义。"""

    def test_resolve_data_dir_returns_the_injected_dir(self) -> None:
        """可用时用注入的目录（不落到临时目录、不改名）。"""
        resolved = Path(resolve_data_dir(str(self.data_dir)))

        self.assertTrue(resolved.is_absolute())
        self.assertTrue(under(resolved, self.data_dir))
        # 本用例的 data_dir 就在系统临时目录下，所以"没用注入目录"要直接比目录名。
        self.assertEqual(resolved.name, self.data_dir.name)

    def test_default_journal_path_is_under_data_dir(self) -> None:
        path = default_journal_path(str(self.data_dir))

        self.assertEqual(path.name, "sessions.json")
        self.assertTrue(path.is_absolute())
        self.assertTrue(under(path, self.data_dir), f"{path} 不在 {self.data_dir} 之下")

    def test_default_shot_dir_is_under_data_dir(self) -> None:
        path = default_shot_dir(str(self.data_dir))

        self.assertTrue(under(path, self.data_dir), f"{path} 不在 {self.data_dir} 之下")

    def test_shot_paths_are_unique_and_under_data_dir(self) -> None:
        """两个默认路径不能互相覆盖，也不能跑到数据目录外面去。"""
        journal = default_journal_path(str(self.data_dir))
        shots = default_shot_dir(str(self.data_dir))

        self.assertNotEqual(journal, shots)
        self.assertTrue(under(journal, self.data_dir))
        self.assertTrue(under(shots, self.data_dir))

    def test_resolve_data_dir_accepts_a_path_object(self) -> None:
        """调用方给 ``Path`` 也要能工作（``Settings.data_dir`` 是 str，但不是唯一入口）。"""
        self.assertTrue(under(resolve_data_dir(self.data_dir), self.data_dir))


# ----------------------------------------------------------------------
# 2. 降级容错：不可用时退回临时目录，且不抛异常
# ----------------------------------------------------------------------


class TestFallbackToTemp(unittest.TestCase):
    """data_dir 不可用时必须降级 —— 它在插件加载路径上，抛异常等于插件起不来。"""

    def assert_falls_back_to_temp(self, data_dir: Any, *, must_not_contain: str = "") -> None:
        """给定一个不可用的 data_dir，断言三者都降级到临时目录且不抛异常。

        每个断言都成对：① 落在临时目录里；② 被拒绝的那个 data_dir 不出现在结果里。
        只写 ① 会漏掉"原样返回了一个不可用的目录"这种最该抓的错法。
        """
        resolved = Path(resolve_data_dir(data_dir))
        journal = default_journal_path(data_dir)
        shots = default_shot_dir(data_dir)

        self.assertTrue(under(resolved, TEMP_ROOT), f"resolve_data_dir 未降级：{resolved}")
        self.assertTrue(under(journal, TEMP_ROOT), f"journal 未降级到临时目录：{journal}")
        self.assertTrue(under(shots, TEMP_ROOT), f"截图目录未降级到临时目录：{shots}")
        self.assertEqual(journal.name, "sessions.json")
        # journal 的降级位置有既有契约，可以钉得更死。
        self.assertTrue(
            under(journal, LEGACY_TEMP_JOURNAL_ROOT), f"journal 降级位置不对：{journal}"
        )
        if must_not_contain:
            self.assertNotIn(must_not_contain, str(resolved))
            self.assertNotIn(must_not_contain, str(journal))
            self.assertNotIn(must_not_contain, str(shots))

    def test_empty_data_dir_falls_back(self) -> None:
        """空串 = 未注入（main.py 拿不到数据目录时就是传空串）。"""
        self.assert_falls_back_to_temp("")

    def test_none_data_dir_falls_back(self) -> None:
        """``None`` 也要当"未注入"处理，而不是抛 TypeError。"""
        self.assert_falls_back_to_temp(None)

    def test_whitespace_data_dir_falls_back(self) -> None:
        """只有空白字符的路径不是有效目录，应当按"未注入"处理。"""
        self.assert_falls_back_to_temp("   ")

    def test_data_dir_with_embedded_null_falls_back(self) -> None:
        """含空字节的路径是非法路径 —— 建目录会抛 ValueError，必须被吞掉。"""
        bad = str(Path(tempfile.gettempdir()) / "nul") + "\x00zz"
        self.assert_falls_back_to_temp(bad, must_not_contain="nul")

    def test_data_dir_whose_parent_is_a_file_falls_back(self) -> None:
        """父路径是个普通文件 → 目录永远建不出来，必须降级而不是抛异常。"""
        with tempfile.TemporaryDirectory(prefix="bsk-datadir-block-") as tmp:
            blocker = Path(tmp) / "iam-a-file"
            blocker.write_text("我不是目录", encoding="utf-8")
            bad = str(blocker / "child")

            self.assert_falls_back_to_temp(bad, must_not_contain="child")

    def test_fallback_paths_are_still_absolute(self) -> None:
        """降级后给的仍必须是绝对路径：相对路径会随工作目录漂移。"""
        for data_dir in ("", None, "   "):
            with self.subTest(data_dir=repr(data_dir)):
                self.assertTrue(default_journal_path(data_dir).is_absolute())
                self.assertTrue(resolve_data_dir(data_dir).is_absolute())

    def test_hostile_data_dir_values_never_raise(self) -> None:
        """配置可能被填成任何东西，一个都不能把插件加载搞崩。

        ⚠️ 必须在临时工作目录里跑：像 ``"con"`` 这样的**相对路径**会被实现当成
        可用的数据目录并就地 ``mkdir``，跑在仓库根目录下就会留下一个 ``con/``
        垃圾目录（Windows 保留设备名，删起来还格外别扭）。
        所以这里先把 CWD 切到临时目录，顺带断言"没有污染当前目录"。
        """
        hostile: list[Any] = [
            42,
            3.14,
            True,
            b"bytes",
            ["list"],
            {"dict": 1},
            object(),
            "\x00",
            "\n\t",
            "con",  # Windows 保留设备名
        ]
        with tempfile.TemporaryDirectory(prefix="bsk-datadir-hostile-") as tmp:
            original_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                for value in hostile:
                    with self.subTest(value=repr(value)):
                        # 不抛异常即为通过；返回什么由实现决定。
                        resolve_data_dir(value)
                        default_journal_path(value)
                        default_shot_dir(value)

                created = {p.name for p in Path(tmp).iterdir()}
            finally:
                os.chdir(original_cwd)

        # 不管实现怎么选，都不该在"当前目录"里留下一堆目录。
        # "con" 与 "42" 是仅有的两个相对路径输入，最多只能有它们。
        self.assertTrue(
            created <= {"con", "42"}, f"敌意输入在 CWD 造出了额外目录：{created}"
        )


# ----------------------------------------------------------------------
# 3. 配置优先级：用户显式配置最高，data_dir 不得覆盖
# ----------------------------------------------------------------------


class TestExplicitConfigWins(DataDirCase, unittest.TestCase):
    """用户填了 ``journal_path`` / ``screenshot_dir`` 时必须听用户的。"""

    def test_explicit_journal_path_survives_data_dir(self) -> None:
        explicit = str(self._tmp.name) + "/自己指定的/s.json"

        settings = self.make_settings(journal_path=explicit)

        self.assertEqual(settings.journal_path, explicit)
        self.assertNotIn(str(self.data_dir), settings.journal_path)

    def test_explicit_screenshot_dir_survives_data_dir(self) -> None:
        explicit = str(self._tmp.name) + "/自己指定的截图"

        settings = self.make_settings(screenshot_dir=explicit)

        self.assertEqual(settings.screenshot_dir, explicit)
        self.assertNotIn(str(self.data_dir), settings.screenshot_dir)

    def test_settings_records_injected_data_dir(self) -> None:
        """注入项本身要被记下来，供 ``bsk/paths.py`` 与诊断使用。"""
        settings = self.make_settings()

        self.assertEqual(str(getattr(settings, "data_dir", "")), str(self.data_dir))

    def test_no_data_dir_keeps_explicit_config_working(self) -> None:
        """没注入 data_dir（旧调用方式）时，显式配置照旧生效，且不抛异常。"""
        explicit = str(self._tmp.name) + "/仍然生效/s.json"

        settings = parse_settings({"bsk_path": "bsk", "journal_path": explicit})

        self.assertEqual(settings.journal_path, explicit)


# ----------------------------------------------------------------------
# 4. 贯通：BskService 实际用到的路径
# ----------------------------------------------------------------------


class TestServiceUsesDataDir(DataDirCase, unittest.IsolatedAsyncioTestCase):
    """上面的语义必须真的落到 ``BskService`` 的实际行为上。"""

    def make_service(self, runner: FakeRunner | None = None, **overrides: Any) -> tuple[BskService, FakeRunner]:
        fake = runner if runner is not None else FakeRunner()
        settings = self.make_settings(**overrides)
        return BskService(settings, runner=fake), fake  # type: ignore[arg-type]

    def test_journal_default_path_lands_in_data_dir(self) -> None:
        """未配置 journal_path 时，journal 实际指向 data_dir 之下。"""
        service, _ = self.make_service()

        self.assertTrue(
            under(service.journal.path, self.data_dir),
            f"journal 落在 {service.journal.path}，不在 {self.data_dir} 之下",
        )

    async def test_screenshot_out_path_lands_in_data_dir(self) -> None:
        """未配置 screenshot_dir 时，``--out`` 实参必须在 data_dir 之下。"""
        service, runner = self.make_service()

        await service.screenshot("umo-1")

        out = Path(runner.out_path_for("screenshot"))
        self.assertTrue(out.is_absolute())
        self.assertTrue(under(out, self.data_dir), f"截图落在 {out}，不在 {self.data_dir} 之下")

    async def test_journal_file_really_created_in_data_dir(self) -> None:
        """建会话后 journal 文件必须真实出现在 data_dir 之下（落盘，而非只在内存）。"""
        service, _ = self.make_service()

        await service.sessions.acquire("umo-1")

        self.assertTrue(service.journal.path.exists(), f"{service.journal.path} 不存在")
        self.assertTrue(under(service.journal.path, self.data_dir))

    async def test_explicit_journal_path_beats_data_dir_in_service(self) -> None:
        """显式 ``journal_path`` 时，service 绝不能用 data_dir 覆盖它。"""
        explicit = Path(self._tmp.name) / "explicit" / "s.json"
        service, _ = self.make_service(journal_path=str(explicit))

        await service.sessions.acquire("umo-1")

        self.assertEqual(service.journal.path, explicit)
        self.assertFalse(under(service.journal.path, self.data_dir))

    async def test_explicit_screenshot_dir_beats_data_dir_in_service(self) -> None:
        """显式 ``screenshot_dir`` 时，``--out`` 必须落在用户指定的目录下。"""
        explicit = Path(self._tmp.name) / "explicit-shots"
        service, runner = self.make_service(screenshot_dir=str(explicit))

        await service.screenshot("umo-1")

        out = Path(runner.out_path_for("screenshot"))
        self.assertTrue(under(out, explicit), f"截图落在 {out}，不在 {explicit} 之下")
        self.assertFalse(under(out, self.data_dir))

    def test_unusable_data_dir_falls_back_in_service(self) -> None:
        """data_dir 不可用时 service 照常构造（构造期不做 IO，也不能抛异常）。"""
        with tempfile.TemporaryDirectory(prefix="bsk-datadir-bad-") as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x", encoding="utf-8")

            settings = self.build_settings(data_dir=str(blocker / "child"))
            service = BskService(settings, runner=FakeRunner())  # type: ignore[arg-type]

            self.assertTrue(under(service.journal.path, LEGACY_TEMP_JOURNAL_ROOT))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
