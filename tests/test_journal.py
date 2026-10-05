"""``bsk/journal.py`` 的单元测试。

策略：用真实的临时文件测，不 mock 文件系统。这个模块的正事就是跟文件打交道
（原子写、容错读、并发），mock 掉 OS 之后测的就不是它了。

模块的硬性契约（每一条都有对应用例）：

1. 绝不抛异常 —— 空文件、半截 JSON、非法 JSON、二进制垃圾、目录不存在、
   路径是目录、没有权限，一律降级成"读不到"并返回空列表。它在插件启动路径上跑，
   抛异常就等于插件加载失败。
2. 原子写 —— 先写 ``<path>.tmp`` 再 ``os.replace``。写一半崩溃绝不能污染
   已经存在的好文件（AstrBot 是常驻服务，随时可能被杀）。
3. 并发安全 —— 多线程同时 add 不能丢记录、不能写坏文件。
4. 显式 UTF-8 —— Windows 中文环境下默认编码是 gbk，含中文的路径与内容会炸。

pytest 在本机不可用，因此用标准库 ``unittest``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# 让测试能 import 到项目的 bsk 包（与 test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.journal import (  # noqa: E402
    JOURNAL_VERSION,
    JournalEntry,
    SessionJournal,
    default_journal_path,
)


def make_entry(session_id: str, window_id: int = 42, **overrides) -> JournalEntry:
    """造一条 journal 记录。"""
    values = {
        "session_id": session_id,
        "browser_instance_id": "c900a3da",
        "agent_window_id": window_id,
        "created_at": time.time(),
        "pid": os.getpid(),
    }
    values.update(overrides)
    return JournalEntry(**values)  # type: ignore[arg-type]


class JournalTestCase(unittest.TestCase):
    """公共脚手架：每个用例一个干净的临时目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bsk-journal-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sessions.json"
        self.journal = SessionJournal(self.path)

    def read_raw(self) -> dict:
        """把 journal 文件当 JSON 读出来（断言落盘格式用）。"""
        return json.loads(self.path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 基本读写
# ----------------------------------------------------------------------


class TestBasicReadWrite(JournalTestCase):
    """add → load → remove → load 这条主线。"""

    def test_missing_file_loads_empty(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(self.journal.load(), [])

    def test_add_then_load_roundtrip(self) -> None:
        entry = make_entry("mnaa", 123)
        self.journal.add(entry)

        loaded = self.journal.load()
        self.assertEqual(len(loaded), 1)
        # frozen dataclass，可以直接比相等 —— 每个字段都必须原样往返。
        self.assertEqual(loaded[0], entry)

    def test_entries_is_alias_of_load(self) -> None:
        self.journal.add(make_entry("mnaa"))
        self.assertEqual(self.journal.entries(), self.journal.load())

    def test_add_multiple_preserves_all(self) -> None:
        self.journal.add(make_entry("mnaa", 1))
        self.journal.add(make_entry("mnab", 2))
        self.journal.add(make_entry("mnac", 3))

        ids = [e.session_id for e in self.journal.load()]
        self.assertEqual(sorted(ids), ["mnaa", "mnab", "mnac"])

    def test_remove_then_load(self) -> None:
        self.journal.add(make_entry("mnaa", 1))
        self.journal.add(make_entry("mnab", 2))

        self.journal.remove("mnaa")

        remaining = self.journal.load()
        self.assertEqual([e.session_id for e in remaining], ["mnab"])
        # 剩下的那条内容也必须完好，不能被"顺手重写"弄坏。
        self.assertEqual(remaining[0].agent_window_id, 2)

    def test_remove_missing_session_is_noop(self) -> None:
        self.journal.add(make_entry("mnaa"))

        self.journal.remove("zzzz")  # 不存在，不该抛也不该改动文件
        self.assertEqual([e.session_id for e in self.journal.load()], ["mnaa"])

    def test_remove_blank_id_is_noop(self) -> None:
        self.journal.add(make_entry("mnaa"))
        self.journal.remove("")
        self.assertEqual(len(self.journal.load()), 1)

    def test_remove_does_not_rewrite_when_nothing_changed(self) -> None:
        """没删掉任何东西时不该写文件 —— 少一次写就少一个损坏窗口。"""
        self.journal.add(make_entry("mnaa"))
        before = self.path.stat().st_mtime_ns

        self.journal.remove("zzzz")
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_add_same_session_id_replaces_old_record(self) -> None:
        """同一个 session_id 只可能对应一条记录，重复 add 必须去重。"""
        self.journal.add(make_entry("mnaa", 111))
        self.journal.add(make_entry("mnaa", 222))

        loaded = self.journal.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].agent_window_id, 222)

    def test_file_format_has_version_and_entries(self) -> None:
        """格式固定为 ``{"version": ..., "entries": [...]}``，便于人工排查与演进。"""
        self.journal.add(make_entry("mnaa", 7))
        raw = self.read_raw()

        self.assertEqual(raw["version"], JOURNAL_VERSION)
        self.assertIsInstance(raw["entries"], list)
        self.assertEqual(raw["entries"][0]["session_id"], "mnaa")
        self.assertEqual(raw["entries"][0]["agent_window_id"], 7)
        self.assertEqual(raw["entries"][0]["browser_instance_id"], "c900a3da")
        # 跨进程的时间戳必须是墙钟，且是合理的当前时间附近。
        self.assertAlmostEqual(raw["entries"][0]["created_at"], time.time(), delta=60)
        self.assertEqual(raw["entries"][0]["pid"], os.getpid())

    def test_file_written_as_utf8_readable_text(self) -> None:
        """落盘必须是可读的 UTF-8 文本（人能直接用记事本打开）。"""
        self.journal.add(make_entry("mnaa"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("session_id", text)
        self.assertNotIn("\\u", text)  # ensure_ascii=False，中文不会被转义

    def test_load_is_repeatable(self) -> None:
        """load 不该消耗或改动任何东西。"""
        self.journal.add(make_entry("mnaa"))
        self.assertEqual(self.journal.load(), self.journal.load())

    def test_tmp_file_is_not_left_behind(self) -> None:
        """正常写完不该残留 .tmp。"""
        self.journal.add(make_entry("mnaa"))
        self.assertFalse(self.journal.tmp_path.exists())


# ----------------------------------------------------------------------
# 原子性
# ----------------------------------------------------------------------


class TestAtomicWrite(JournalTestCase):
    """写一半崩溃不能污染已存在的好文件。"""

    def test_partial_tmp_does_not_corrupt_existing_file(self) -> None:
        """模拟"写盘中途被杀"：.tmp 是半截 JSON，但主文件仍是完整旧内容。"""
        self.journal.add(make_entry("mnaa", 111))
        before = self.path.read_text(encoding="utf-8")

        # 模拟下一次 add 写到一半就被强杀：临时文件只写了一半。
        self.journal.tmp_path.write_text('{"version": 1, "entr', encoding="utf-8")

        loaded = self.journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnaa"])
        self.assertEqual(loaded[0].agent_window_id, 111)
        # 主文件一个字节都没被动过。
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_tmp_garbage_does_not_affect_load(self) -> None:
        """.tmp 里是二进制垃圾也不该影响读取主文件。"""
        self.journal.add(make_entry("mnab", 222))
        self.journal.tmp_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02garbage")

        self.assertEqual([e.session_id for e in self.journal.load()], ["mnab"])

    def test_write_uses_replace_not_truncate(self) -> None:
        """断言实现细节：整个写入过程结束后主文件始终是完整 JSON。

        做法：反复 add，每次 add 后立刻读 —— 若实现是"直接覆盖写"，
        Windows 上很容易读到空文件或半截内容（读与写在同线程里虽然串行，
        但这个用例至少把"每次写完必须立刻可解析"这条钉死）。
        """
        for i in range(20):
            self.journal.add(make_entry(f"m{i:03d}", i))
            raw = self.read_raw()  # 任何一次不是完整 JSON 都会在这里炸
            self.assertEqual(raw["version"], JOURNAL_VERSION)
            self.assertEqual(len(raw["entries"]), i + 1)

    def test_add_survives_when_parent_dir_missing(self) -> None:
        """父目录不存在时自动创建（首次运行时就是这个状态）。"""
        nested = self.dir / "a" / "b" / "sessions.json"
        journal = SessionJournal(nested)
        journal.add(make_entry("mnaa"))

        self.assertTrue(nested.exists())
        self.assertEqual(len(journal.load()), 1)

    def test_add_survives_when_parent_is_a_file(self) -> None:
        """父路径是个文件（而不是目录）→ 写不进去，但绝不能抛异常。"""
        blocker = self.dir / "blocker"
        blocker.write_text("我不是目录", encoding="utf-8")
        journal = SessionJournal(blocker / "sessions.json")

        journal.add(make_entry("mnaa"))  # 不抛
        self.assertEqual(journal.load(), [])


# ----------------------------------------------------------------------
# 损坏容错 —— 全部返回空列表，绝不抛异常
# ----------------------------------------------------------------------


class TestCorruptionTolerance(JournalTestCase):
    """这一组是模块最重要的契约：插件启动路径上不许崩。"""

    def test_empty_file(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(self.journal.load(), [])

    def test_whitespace_only_file(self) -> None:
        self.path.write_text("   \n\t\n  ", encoding="utf-8")
        self.assertEqual(self.journal.load(), [])

    def test_truncated_json(self) -> None:
        """半截 JSON —— 写盘时被杀就会留下这种文件。"""
        self.path.write_text('{"version": 1, "entries": [{"session_id": "mn', encoding="utf-8")
        self.assertEqual(self.journal.load(), [])

    def test_truncated_but_valid_json_object_missing_entries(self) -> None:
        self.path.write_text('{"version": 1}', encoding="utf-8")
        self.assertEqual(self.journal.load(), [])

    def test_invalid_json(self) -> None:
        for junk in ["not json at all", "{", "}", "[}", "null,", "{{{{"]:
            with self.subTest(junk=junk):
                self.path.write_text(junk, encoding="utf-8")
                self.assertEqual(self.journal.load(), [])

    def test_json_scalar_instead_of_object(self) -> None:
        for junk in ["42", '"a string"', "true", "null", "3.14"]:
            with self.subTest(junk=junk):
                self.path.write_text(junk, encoding="utf-8")
                self.assertEqual(self.journal.load(), [])

    def test_binary_garbage(self) -> None:
        self.path.write_bytes(bytes(range(256)) * 4)
        self.assertEqual(self.journal.load(), [])

    def test_utf8_bom_is_tolerated(self) -> None:
        """有些编辑器（记事本）会给 UTF-8 文件加 BOM —— 不该因此读不到。"""
        payload = json.dumps(
            {
                "version": JOURNAL_VERSION,
                "entries": [
                    {
                        "session_id": "mnaa",
                        "browser_instance_id": "x",
                        "agent_window_id": 5,
                        "created_at": 1.0,
                        "pid": 1,
                    }
                ],
            }
        )
        self.path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

        loaded = self.journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnaa"])

    def test_version_mismatch_still_loads_entries(self) -> None:
        """未知版本不报错：能读出多少算多少（宁可多清一次，也不要插件起不来）。"""
        self.path.write_text(
            json.dumps(
                {
                    "version": 999,
                    "entries": [
                        {
                            "session_id": "mnaa",
                            "browser_instance_id": "x",
                            "agent_window_id": 5,
                            "created_at": 1.0,
                            "pid": 1,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual([e.session_id for e in self.journal.load()], ["mnaa"])

    def test_bad_entry_is_skipped_good_ones_kept(self) -> None:
        """坏一条丢一条，而不是坏一条丢一整份。"""
        self.path.write_text(
            json.dumps(
                {
                    "version": JOURNAL_VERSION,
                    "entries": [
                        {"session_id": "mnaa", "agent_window_id": 1},
                        "我不是字典",
                        {"no_session_id": True},
                        {"session_id": "    "},
                        {"session_id": "mnab", "agent_window_id": 2},
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded = self.journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnaa", "mnab"])
        self.assertEqual([e.agent_window_id for e in loaded], [1, 2])

    def test_non_list_entries(self) -> None:
        for junk in ['{"version": 1, "entries": "abc"}', '{"version": 1, "entries": {}}']:
            with self.subTest(junk=junk):
                self.path.write_text(junk, encoding="utf-8")
                self.assertEqual(self.journal.load(), [])

    def test_bare_list_format_is_accepted(self) -> None:
        """兼容裸数组写法（手工编辑时很自然会写成这样）。"""
        self.path.write_text(
            json.dumps([{"session_id": "mnaa", "agent_window_id": 9}]),
            encoding="utf-8",
        )
        loaded = self.journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnaa"])
        self.assertEqual(loaded[0].agent_window_id, 9)

    def test_path_is_a_directory(self) -> None:
        """journal 路径指向一个目录 —— 读不出内容，但不抛异常。"""
        as_dir = self.dir / "iam-a-dir"
        as_dir.mkdir()
        journal = SessionJournal(as_dir)

        self.assertEqual(journal.load(), [])
        journal.add(make_entry("mnaa"))  # 写失败也不抛
        self.assertEqual(journal.load(), [])

    def test_nonexistent_directory_loads_empty(self) -> None:
        journal = SessionJournal(self.dir / "nope" / "deep" / "sessions.json")
        self.assertEqual(journal.load(), [])

    def test_clear_on_nonexistent_file_is_noop(self) -> None:
        self.journal.clear()  # 不抛
        self.assertEqual(self.journal.load(), [])

    def test_clear_removes_leftover_tmp_too(self) -> None:
        self.journal.tmp_path.write_text("半截", encoding="utf-8")
        self.journal.clear()
        self.assertFalse(self.journal.tmp_path.exists())


# ----------------------------------------------------------------------
# 并发
# ----------------------------------------------------------------------


class TestConcurrency(JournalTestCase):
    """add/remove 可能从不同线程（不同协程）被调到，必须串行化。"""

    def test_concurrent_add_100_entries(self) -> None:
        """8 个线程各 add，最后必须能完整读到所有记录（不丢、不坏）。"""
        total = 100
        threads_count = 8
        entries = [make_entry(f"s{i:03d}", i) for i in range(total)]

        def worker(chunk: list[JournalEntry]) -> None:
            for entry in chunk:
                self.journal.add(entry)

        chunks = [entries[i::threads_count] for i in range(threads_count)]
        threads = [threading.Thread(target=worker, args=(c,)) for c in chunks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        loaded = self.journal.load()
        self.assertEqual(len(loaded), total, "并发 add 丢记录了")
        self.assertEqual(
            sorted(e.session_id for e in loaded),
            sorted(e.session_id for e in entries),
        )
        # 文件必须仍是完整合法的 JSON（没有交错写坏）。
        self.assertEqual(self.read_raw()["version"], JOURNAL_VERSION)
        # 窗口号也要对得上，不能出现"记录在但字段错位"。
        for entry in loaded:
            self.assertEqual(entry.agent_window_id, int(entry.session_id[1:]))

    def test_concurrent_add_and_remove(self) -> None:
        """边加边删也不能写坏文件，且不能抛异常。"""
        for i in range(50):
            self.journal.add(make_entry(f"keep{i:02d}", i))

        errors: list[BaseException] = []

        def adder() -> None:
            try:
                for i in range(50):
                    self.journal.add(make_entry(f"add{i:02d}", i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def remover() -> None:
            try:
                for i in range(50):
                    self.journal.remove(f"keep{i:02d}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=adder), threading.Thread(target=remover)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        loaded = self.journal.load()
        ids = {e.session_id for e in loaded}
        # 被删的 50 条必须都不在了；新增的 50 条必须都在。
        self.assertFalse({f"keep{i:02d}" for i in range(50)} & ids)
        self.assertEqual({f"add{i:02d}" for i in range(50)}, ids)

    def test_concurrent_load_during_add(self) -> None:
        """一边写一边读：读到的要么是旧内容要么是新内容，绝不能崩。"""
        self.journal.add(make_entry("mnaa", 1))
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                while not stop.is_set():
                    self.journal.load()  # 每次都必须能解析
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        try:
            for i in range(30):
                self.journal.add(make_entry(f"m{i:03d}", i))
        finally:
            stop.set()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])


# ----------------------------------------------------------------------
# 编码
# ----------------------------------------------------------------------


class TestEncoding(JournalTestCase):
    """Windows 中文环境下默认编码是 gbk，不显式 UTF-8 就会炸。"""

    def test_chinese_path_and_content(self) -> None:
        chinese_dir = self.dir / "中文目录" / "会话记录"
        journal = SessionJournal(chinese_dir / "sessions.json")

        entry = make_entry("mnaa", 123, browser_instance_id="浏览器实例")
        journal.add(entry)

        loaded = journal.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].browser_instance_id, "浏览器实例")
        self.assertEqual(loaded[0], entry)

    def test_chinese_survives_remove(self) -> None:
        journal = SessionJournal(self.dir / "中文" / "sessions.json")
        journal.add(make_entry("mnaa", 1, browser_instance_id="甲"))
        journal.add(make_entry("mnab", 2, browser_instance_id="乙"))

        journal.remove("mnaa")

        loaded = journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnab"])
        self.assertEqual(loaded[0].browser_instance_id, "乙")

    def test_non_ascii_is_written_as_real_utf8(self) -> None:
        """文件里必须是真正的 UTF-8 字符，不是 \\uXXXX 转义。"""
        journal = SessionJournal(self.path)
        journal.add(make_entry("mnaa", browser_instance_id="日本語テスト"))

        raw_bytes = self.path.read_bytes()
        self.assertIn("日本語テスト".encode("utf-8"), raw_bytes)
        self.assertIn("日本語テスト", self.path.read_text(encoding="utf-8"))

    def test_very_long_path_and_content(self) -> None:
        """长路径不应被截断；长内容也不该丢。"""
        deep = self.dir / ("长" * 40) / ("深" * 40)
        journal = SessionJournal(deep / "sessions.json")
        long_id = "m" + "n" * 200

        journal.add(make_entry(long_id, 9))
        self.assertEqual(journal.load()[0].session_id, long_id)


# ----------------------------------------------------------------------
# clear()
# ----------------------------------------------------------------------


class TestClear(JournalTestCase):
    """clear 之后文件必须不存在（或为空），且 load 返回空。"""

    def test_clear_removes_file(self) -> None:
        self.journal.add(make_entry("mnaa"))
        self.assertTrue(self.path.exists())

        self.journal.clear()

        self.assertFalse(self.path.exists())
        self.assertEqual(self.journal.load(), [])

    def test_clear_then_add_starts_fresh(self) -> None:
        self.journal.add(make_entry("mnaa", 1))
        self.journal.clear()
        self.journal.add(make_entry("mnab", 2))

        loaded = self.journal.load()
        self.assertEqual([e.session_id for e in loaded], ["mnab"])

    def test_clear_is_idempotent(self) -> None:
        self.journal.add(make_entry("mnaa"))
        self.journal.clear()
        self.journal.clear()  # 第二次不抛

        self.assertFalse(self.path.exists())
        self.assertEqual(self.journal.load(), [])


# ----------------------------------------------------------------------
# 辅助：默认路径与单条记录解析
# ----------------------------------------------------------------------


class TestDefaultPath(unittest.TestCase):
    """默认路径的构成。

    默认位置有两个分支，两个都要测：注入 ``data_dir`` 时落在插件数据目录下
    （``data/plugin_data/astrbot_plugin_bsk_browser``），未注入时退回旧的
    系统临时目录行为。降级容错是审核明确要求保留的，所以它同样有守护测试。
    """

    def test_default_path_shape_under_data_dir(self) -> None:
        """传入 ``data_dir`` 时：文件名固定，且落在该目录之下。"""
        with tempfile.TemporaryDirectory(prefix="bsk-journal-datadir-") as tmp:
            data_dir = Path(tmp) / "astrbot_plugin_bsk_browser"
            path = default_journal_path(str(data_dir))

            self.assertEqual(path.name, "sessions.json")
            self.assertTrue(path.is_absolute())
            self.assertTrue(
                path.resolve().is_relative_to(data_dir.resolve()),
                f"{path} 不在 {data_dir} 之下",
            )

    def test_default_path_is_under_system_temp_when_data_dir_missing(self) -> None:
        """未注入 ``data_dir``（空串）→ 退回系统临时目录下的 ``astrbot_bsk_browser``。

        这条路径必须保留：``StarTools.get_data_dir()`` 失败时插件会传空串进来，
        那时 journal 仍要有个可写的位置，且不能因为拿不到数据目录就抛异常。
        """
        path = default_journal_path("")

        self.assertEqual(path.name, "sessions.json")
        self.assertEqual(path.parent.name, "astrbot_bsk_browser")
        temp = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(path.parent.parent.resolve(), temp)

    def test_default_path_never_raises_for_any_data_dir(self) -> None:
        """非法 ``data_dir`` 一律降级，绝不在插件启动路径上抛异常。"""
        for data_dir in ("", "   ", "\x00bad", 42, None):
            with self.subTest(data_dir=repr(data_dir)):
                path = default_journal_path(data_dir)  # type: ignore[arg-type]
                self.assertEqual(path.name, "sessions.json")
                self.assertTrue(path.is_absolute())


class TestJournalEntry(unittest.TestCase):
    """单条记录的解析容错。"""

    def test_from_json_rejects_non_dict(self) -> None:
        for junk in [None, "abc", 42, [1, 2], True]:
            with self.subTest(junk=repr(junk)):
                self.assertIsNone(JournalEntry.from_json(junk))

    def test_from_json_requires_session_id(self) -> None:
        self.assertIsNone(JournalEntry.from_json({}))
        self.assertIsNone(JournalEntry.from_json({"session_id": ""}))
        self.assertIsNone(JournalEntry.from_json({"session_id": "   "}))
        self.assertIsNone(JournalEntry.from_json({"session_id": 123}))

    def test_from_json_defaults_missing_fields(self) -> None:
        entry = JournalEntry.from_json({"session_id": "mnaa"})
        assert entry is not None
        self.assertEqual(entry.browser_instance_id, "")
        self.assertEqual(entry.agent_window_id, 0)
        self.assertEqual(entry.created_at, 0.0)
        self.assertEqual(entry.pid, 0)

    def test_from_json_coerces_numeric_strings(self) -> None:
        entry = JournalEntry.from_json(
            {"session_id": "mnaa", "agent_window_id": "123", "created_at": "1.5"}
        )
        assert entry is not None
        self.assertEqual(entry.agent_window_id, 123)
        self.assertEqual(entry.created_at, 1.5)

    def test_from_json_survives_wrong_types(self) -> None:
        entry = JournalEntry.from_json(
            {
                "session_id": "mnaa",
                "browser_instance_id": {"nested": 1},
                "agent_window_id": {"nope": 1},
                "created_at": [1, 2],
                "pid": None,
            }
        )
        assert entry is not None
        self.assertEqual(entry.session_id, "mnaa")
        self.assertEqual(entry.browser_instance_id, "")
        self.assertEqual(entry.agent_window_id, 0)
        self.assertEqual(entry.created_at, 0.0)
        self.assertEqual(entry.pid, 0)

    def test_from_json_strips_whitespace(self) -> None:
        entry = JournalEntry.from_json(
            {"session_id": "  mnaa  ", "browser_instance_id": " c900a3da "}
        )
        assert entry is not None
        self.assertEqual(entry.session_id, "mnaa")
        self.assertEqual(entry.browser_instance_id, "c900a3da")

    def test_entry_is_frozen(self) -> None:
        entry = make_entry("mnaa")
        with self.assertRaises(Exception):
            entry.session_id = "mnab"  # type: ignore[misc]

    def test_to_json_roundtrip(self) -> None:
        entry = make_entry("mnaa", 321)
        self.assertEqual(JournalEntry.from_json(entry.to_json()), entry)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
