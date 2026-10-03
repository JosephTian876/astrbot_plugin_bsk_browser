"""``bsk/shots.py`` 的单元测试。

策略：**用真实的临时文件测，不 mock 文件系统**。这个模块的正事就是跟文件打交道
（读文件头、比大小、删除），mock 掉 OS 之后测的就不是它了。

测试数据全部**用代码现场构造**，不下载网络图片：

- :func:`build_png` —— 用 ``zlib`` + ``struct`` 手工拼一个**真实合法**的最小 PNG
  （IHDR + IDAT + IEND，CRC 自己算）。实测可以被 Pillow 正常打开，所以
  ``encode_for_llm`` 的用例也用它。
- :func:`build_jpeg` —— 手工拼一个结构合法的 JPEG 头（SOI + APP0 + SOF0 + EOI）。
  它的熵编码数据是空的，Pillow 打不开，但**魔数嗅探与 SOF 尺寸解析要的就是这些头**。

覆盖的重点是那些实测踩过的坑：
- bsk 的 ``format`` 字段会撒谎（请求 png 实际给 jpeg）→ 必须靠魔数
- 魔数嗅探**只读文件头**，不能把几十 MB 的全页截图读进内存
- ``session_id`` 来自外部 → 目录穿越
- 清理时文件被占用（Windows 上是 ``PermissionError``）→ 跳过而不是抛异常
- 没装 Pillow → ``encode_for_llm`` 返回 ``None``，不抛异常
"""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import importlib.util
import os
import re
import stat
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from typing import Iterator

# 让测试能 import 到项目的 bsk 包（与 test_runner.py 保持一致）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk import shots  # noqa: E402
from bsk.models import Screenshot  # noqa: E402
from bsk.shots import (  # noqa: E402
    MAGIC_JPEG,
    MAGIC_PNG,
    cleanup_shots,
    encode_for_llm,
    make_shot_path,
    sanitize_session_id,
    sniff_format,
    verify_shot,
)

HAS_PIL = importlib.util.find_spec("PIL") is not None
"""本机是否有 Pillow。没有时相关的用例会被 skip，而不是失败。"""


# --- 测试数据构造 -------------------------------------------------------------


def build_png(width: int = 40, height: int = 20, rgb: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """手工构造一个**真实合法**的 PNG（纯色）。

    PNG 结构：8 字节签名 + 若干 ``长度(4) 类型(4) 数据 CRC(4)`` 的块。
    这里只写必需的三个块：IHDR（尺寸）、IDAT（zlib 压缩的像素）、IEND。

    像素数据每行前面要加一个字节的"过滤器类型"（0 = None），这是 PNG 的规矩。
    """
    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    # IHDR: 宽(4) 高(4) 位深(1) 颜色类型(1=RGB) 压缩(1) 过滤(1) 隔行(1)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return MAGIC_PNG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def build_jpeg(width: int = 40, height: int = 20) -> bytes:
    """手工构造一个**结构合法**的 JPEG 头（不含真实图像数据）。

    SOI + APP0(JFIF) + SOF0(尺寸) + EOI。段格式是 ``FF <marker> <长度(2)> <载荷>``，
    长度字段**包含它自己那 2 字节**（这是 JPEG 最容易写错的地方）。
    """
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01" + b"\x00" + b"\x00\x01\x00\x01" + b"\x00\x00"
    # SOF0 载荷: 精度(1) 高(2) 宽(2) 分量数(1) + 每分量 3 字节
    sof0_payload = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def png_size_from_ihdr(data: bytes) -> tuple[int, int]:
    """用 ``struct`` 直接从 IHDR 解析 PNG 尺寸，作为测试的独立参照。"""
    assert data.startswith(MAGIC_PNG), "不是 PNG"
    assert data[12:16] == b"IHDR", "第一个块不是 IHDR"
    return struct.unpack(">II", data[16:24])


@contextlib.contextmanager
def pil_missing() -> Iterator[None]:
    """模拟"这台机器没装 Pillow"。

    把 ``sys.modules["PIL"]`` 设成 ``None``：CPython 在 import 时看到这个哨兵值会
    直接抛 ``ImportError``（而不是去磁盘上找）。这比 mock ``__import__`` 更贴近真实，
    也不需要真的卸载 Pillow。
    """
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "PIL" or name.startswith("PIL.")
    }
    for name in saved:
        del sys.modules[name]
    sys.modules["PIL"] = None
    try:
        yield
    finally:
        sys.modules.pop("PIL", None)
        sys.modules.update(saved)


class TempDirTestCase(unittest.TestCase):
    """带真实临时目录的基类。"""

    def setUp(self) -> None:
        # 测试里可能出现只读文件，Windows 下 rmtree 会失败，所以忽略清理错误。
        self._tmp = tempfile.TemporaryDirectory(
            prefix="bsk-shots-", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name: str, data: bytes) -> Path:
        """在临时目录里写一个真实文件并返回路径。"""
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


# --- sniff_format -------------------------------------------------------------


class TestSniffFormat(TempDirTestCase):
    """魔数嗅探：只信文件头，不信后缀名。"""

    def test_png(self) -> None:
        path = self.write("a.png", build_png())
        self.assertEqual(sniff_format(path), "png")

    def test_jpeg(self) -> None:
        path = self.write("a.jpg", build_jpeg())
        self.assertEqual(sniff_format(path), "jpeg")

    def test_gif(self) -> None:
        path = self.write("a.gif", b"GIF89a" + b"\x00" * 32)
        self.assertEqual(sniff_format(path), "gif")

    def test_webp(self) -> None:
        # RIFF 容器：偏移 8 处必须是 "WEBP"
        path = self.write("a.webp", b"RIFF" + struct.pack("<I", 100) + b"WEBP" + b"VP8 " + b"\x00" * 16)
        self.assertEqual(sniff_format(path), "webp")

    def test_riff_but_not_webp(self) -> None:
        """WAV 也是 RIFF，不能误判成 WebP。"""
        path = self.write("a.wav", b"RIFF" + struct.pack("<I", 100) + b"WAVE" + b"\x00" * 16)
        self.assertEqual(sniff_format(path), "unknown")

    def test_后缀名撒谎时以内容为准(self) -> None:
        """★ 实测坑：文件名是 .png，内容其实是 JPEG。"""
        path = self.write("lying.png", build_jpeg())
        self.assertEqual(sniff_format(path), "jpeg")

    def test_empty_file(self) -> None:
        path = self.write("empty.png", b"")
        self.assertEqual(sniff_format(path), "unknown")

    def test_missing_file(self) -> None:
        self.assertEqual(sniff_format(self.tmp / "不存在.png"), "unknown")

    def test_truncated_png_7_bytes(self) -> None:
        """只有 7 字节签名（缺最后 1 字节）→ 不算 PNG。"""
        path = self.write("short.png", MAGIC_PNG[:7])
        self.assertEqual(sniff_format(path), "unknown")

    def test_three_byte_file(self) -> None:
        """3 字节的残缺文件：正好够 JPEG 的魔数，不够 PNG 的。"""
        self.assertEqual(sniff_format(self.write("t3.jpg", MAGIC_JPEG)), "jpeg")
        self.assertEqual(sniff_format(self.write("t3.png", MAGIC_PNG[:3])), "unknown")

    def test_directory_is_unknown(self) -> None:
        """路径是个目录：不抛异常，返回 unknown。"""
        (self.tmp / "adir").mkdir()
        self.assertEqual(sniff_format(self.tmp / "adir"), "unknown")

    def test_接受_str_和_Path(self) -> None:
        path = self.write("a.png", build_png())
        self.assertEqual(sniff_format(str(path)), "png")
        self.assertEqual(sniff_format(path), "png")

    def test_大文件只读文件头(self) -> None:
        """★ 只读头部：20MB 的截图不能整个读进内存。

        用稀疏文件构造（seek 到 20MB 再写 1 字节），创建瞬间完成，不占磁盘。
        """
        path = self.tmp / "huge.png"
        with open(path, "wb") as fp:
            fp.write(build_png())
            fp.seek(20 * 1024 * 1024)
            fp.write(b"\x00")
        self.assertGreater(path.stat().st_size, 20 * 1024 * 1024)
        self.assertEqual(sniff_format(path), "png")

    def test_返回白名单内的值(self) -> None:
        """返回值只能是约定的 5 种，上层才能放心用。"""
        allowed = {"png", "jpeg", "gif", "webp", "unknown"}
        for name, data in (
            ("a.bin", b"random garbage"),
            ("b.bin", b"\x00\x00\x00\x00"),
            ("c.bin", b"BM" + b"\x00" * 20),  # BMP 不在支持列表里
        ):
            self.assertIn(sniff_format(self.write(name, data)), allowed)


# --- verify_shot --------------------------------------------------------------


class TestVerifyShot(TempDirTestCase):
    """三重校验：文件存在、大小一致、魔数匹配。"""

    def test_正常通过(self) -> None:
        data = build_png(40, 20)
        path = self.write("shot.png", data)
        shot = Screenshot(path=str(path), width=40, height=20, format="png", byte_size=len(data))
        ok, why = verify_shot(shot)
        self.assertTrue(ok, why)
        self.assertIn("png", why)
        self.assertIn(str(len(data)), why)

    def test_文件不存在(self) -> None:
        shot = Screenshot(path=str(self.tmp / "没有这个.png"), format="png", byte_size=10)
        ok, why = verify_shot(shot)
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_路径为空(self) -> None:
        ok, why = verify_shot(Screenshot())
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_大小不符_bsk_说谎(self) -> None:
        data = build_png(40, 20)
        path = self.write("shot.png", data)
        # bsk 报的大小比实际大：说明文件被截断或被别的会话覆盖了
        shot = Screenshot(path=str(path), format="png", byte_size=len(data) + 999)
        ok, why = verify_shot(shot)
        self.assertFalse(ok)
        self.assertIn(str(len(data)), why)  # 告诉用户磁盘上到底多大

    def test_大小为0时跳过比较(self) -> None:
        """bsk 没报 byte_size（0）时不误判失败，其余两项仍然校验。"""
        data = build_png(40, 20)
        path = self.write("shot.png", data)
        shot = Screenshot(path=str(path), format="png", byte_size=0)
        ok, why = verify_shot(shot)
        self.assertTrue(ok, why)

    def test_魔数不符_声明png实际jpeg(self) -> None:
        """★ 核心用例：bsk 的 format 字段在撒谎。"""
        path = self.write("lying.png", build_jpeg(40, 20))
        shot = Screenshot(
            path=str(path), width=40, height=20, format="png", byte_size=path.stat().st_size
        )
        ok, why = verify_shot(shot)
        self.assertFalse(ok)
        self.assertIn("jpeg", why)
        self.assertIn("png", why)

    def test_声明jpeg实际jpeg通过(self) -> None:
        """格式别名 jpg / .JPEG 都要能归一化，不能误报。"""
        path = self.write("shot.jpg", build_jpeg(40, 20))
        size = path.stat().st_size
        for declared in ("jpeg", "jpg", "JPEG", ".jpeg"):
            ok, why = verify_shot(Screenshot(path=str(path), format=declared, byte_size=size))
            self.assertTrue(ok, f"声明 {declared} 时不该失败：{why}")

    def test_格式未知时通过_只校验魔数已知(self) -> None:
        """bsk 没给 format 字段时不该报错，只要魔数认得出就行。"""
        data = build_png()
        path = self.write("shot.png", data)
        ok, why = verify_shot(Screenshot(path=str(path), byte_size=len(data)))
        self.assertTrue(ok, why)

    def test_内容不是图片(self) -> None:
        # bytes 字面量只能放 ASCII，中文要先 encode
        data = "这是一个纯文本文件，不是图片。".encode("utf-8")
        path = self.write("fake.png", data)
        shot = Screenshot(path=str(path), format="png", byte_size=len(data))
        ok, why = verify_shot(shot)
        self.assertFalse(ok)
        self.assertIn("不是已知的图片格式", why)

    def test_空文件(self) -> None:
        path = self.write("empty.png", b"")
        ok, why = verify_shot(Screenshot(path=str(path), format="png", byte_size=0))
        self.assertFalse(ok)
        self.assertIn("空", why)

    def test_路径是目录(self) -> None:
        (self.tmp / "adir").mkdir()
        ok, why = verify_shot(Screenshot(path=str(self.tmp / "adir"), byte_size=1))
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_绝不抛异常(self) -> None:
        """任何输入都只能返回 (False, 原因)，不能把异常抛给调用方。"""
        for shot in (
            Screenshot(),
            Screenshot(path="\x00非法路径"),
            Screenshot(path=str(self.tmp)),
            Screenshot(path=str(self.tmp / "nope"), byte_size=-1),
        ):
            ok, why = verify_shot(shot)
            self.assertFalse(ok)
            self.assertIsInstance(why, str)


# --- sanitize_session_id / make_shot_path -------------------------------------


class TestSanitizeSessionId(unittest.TestCase):
    """白名单过滤：只留字母数字下划线。"""

    def test_正常id不变(self) -> None:
        self.assertEqual(sanitize_session_id("mnaa"), "mnaa")
        self.assertEqual(sanitize_session_id("Abc_123"), "Abc_123")

    def test_目录穿越被洗掉(self) -> None:
        for evil in ("../../etc", "..", "../..", "a/b", "a\\b", "C:\\Windows", "..\\..\\x"):
            cleaned = sanitize_session_id(evil)
            self.assertNotIn("/", cleaned, evil)
            self.assertNotIn("\\", cleaned, evil)
            self.assertNotIn("..", cleaned, evil)
            self.assertRegex(cleaned, r"^[A-Za-z0-9_]+$", evil)

    def test_空字节与unicode斜杠(self) -> None:
        for evil in ("a\x00b", "a\uff0fb", "a\uff3cb", "%2e%2e%2f", "...", "a b"):
            cleaned = sanitize_session_id(evil)
            self.assertRegex(cleaned, r"^[A-Za-z0-9_]+$", repr(evil))
            self.assertNotIn("\x00", cleaned)

    def test_空或全非法时兜底(self) -> None:
        for bad in ("", ".", "..", "/", "\\", "\x00", "...", None):
            self.assertEqual(sanitize_session_id(bad), shots.SAFE_SESSION_ID_FALLBACK)

    def test_windows保留设备名加前缀(self) -> None:
        self.assertEqual(sanitize_session_id("con"), "_con")
        self.assertEqual(sanitize_session_id("NUL"), "_NUL")
        self.assertEqual(sanitize_session_id("com1"), "_com1")

    def test_超长id被截断(self) -> None:
        self.assertLessEqual(len(sanitize_session_id("a" * 500)), 64)


class TestMakeShotPath(TempDirTestCase):
    """唯一路径 + 目录创建 + 防穿越。"""

    def test_目录自动创建(self) -> None:
        self.assertFalse((self.tmp / "shots").exists())
        path = make_shot_path(self.tmp, "mnaa")
        self.assertTrue(path.parent.is_dir())
        self.assertEqual(path.parent.name, "mnaa")
        self.assertEqual(path.parent.parent.name, "shots")
        self.assertTrue(path.exists(), "占位文件应已创建，避免并发撞名")

    def test_连续100次唯一(self) -> None:
        """★ 同一毫秒内连续调用也不能撞名。"""
        paths = [make_shot_path(self.tmp, "mnaa") for _ in range(100)]
        self.assertEqual(len(set(paths)), 100)
        self.assertEqual(len(list(paths[0].parent.iterdir())), 100)

    def test_多线程并发唯一(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda i: make_shot_path(self.tmp, f"s{i % 3}"), range(200)))
        self.assertEqual(len(set(paths)), 200)

    def test_多进程式并发_跨进程也不会撞(self) -> None:
        """模拟"另一个进程"：清掉进程内计数器后仍不能撞已有文件。

        直接用 os.open(..., O_EXCL) 手工占一个已存在的名字会失败，
        所以这里反过来验证：已有文件时新路径不会指向它。
        """
        first = make_shot_path(self.tmp, "mnaa")
        others = [make_shot_path(self.tmp, "mnaa") for _ in range(20)]
        self.assertNotIn(first, others)

    def test_恶意session_id不会穿越目录(self) -> None:
        """★ 安全用例：session_id 来自外部。"""
        shots_root = (self.tmp / "shots").resolve()
        for evil in ("../../etc", "a/b", "a\\b", "..", "..\\..", "\x00", "a\x00/../b", "C:\\Windows"):
            path = make_shot_path(self.tmp, evil)
            resolved = path.resolve()
            self.assertTrue(
                resolved.is_relative_to(shots_root),
                f"session_id={evil!r} 逃出了 shots 目录：{resolved}",
            )
            # 必须正好是 shots/<一层安全目录>/<文件>
            self.assertEqual(resolved.parent.parent, shots_root, evil)
            self.assertRegex(resolved.parent.name, r"^_?[A-Za-z0-9_]+$", evil)

    def test_空session_id兜底目录(self) -> None:
        path = make_shot_path(self.tmp, "")
        self.assertEqual(path.parent.name, shots.SAFE_SESSION_ID_FALLBACK)

    def test_ext处理(self) -> None:
        self.assertTrue(make_shot_path(self.tmp, "mnaa").name.endswith(".png"))
        self.assertTrue(make_shot_path(self.tmp, "mnaa", ext=".jpg").name.endswith(".jpg"))
        self.assertTrue(make_shot_path(self.tmp, "mnaa", ext="jpeg").name.endswith(".jpeg"))
        # ext 里塞路径分隔符也要被洗掉，绝不能借此跳目录
        for evil_ext in ("../../evil", "/etc/passwd", "..\\..\\x", ""):
            path = make_shot_path(self.tmp, "mnaa", ext=evil_ext)
            self.assertEqual(path.parent.parent.name, "shots")
            self.assertNotIn(os.sep, path.name)
            self.assertIsNone(re.search(r"[/\\]", path.name), evil_ext)

    def test_文件名按时间排序(self) -> None:
        """时间戳在文件名最前面，清理时一眼能看出新旧。"""
        path = make_shot_path(self.tmp, "mnaa")
        self.assertRegex(path.name, r"^\d{13}-\d+-[0-9a-f]{8}\.png$")

    def test_与目录参数是str或Path都能用(self) -> None:
        self.assertTrue(make_shot_path(str(self.tmp), "mnaa").exists())
        self.assertTrue(make_shot_path(Path(self.tmp), "mnaa").exists())


# --- cleanup_shots ------------------------------------------------------------


class TestCleanupShots(TempDirTestCase):
    """清理：keep 与 max_age 两条规则，且必须容错。"""

    def _make(self, session: str, count: int, mtime: float | None = None) -> list[Path]:
        """造 count 个截图文件；mtime 不为空时改写修改时间。"""
        made = []
        for _ in range(count):
            path = make_shot_path(self.tmp, session)
            path.write_bytes(build_png())
            if mtime is not None:
                os.utime(path, (mtime, mtime))
            made.append(path)
        return made

    def test_keep生效(self) -> None:
        made = self._make("mnaa", 10)
        deleted = cleanup_shots(self.tmp, keep=3, max_age_sec=0)
        self.assertEqual(deleted, 7)
        left = sorted(p.name for p in made if p.exists())
        self.assertEqual(len(left), 3)

    def test_keep保留的是最新的(self) -> None:
        made = self._make("mnaa", 5)
        # 手工把 mtime 拉开：越靠后越新
        base = 1_700_000_000.0
        for index, path in enumerate(made):
            os.utime(path, (base + index * 10, base + index * 10))
        cleanup_shots(self.tmp, keep=2, max_age_sec=0)
        left = {p.name for p in made if p.exists()}
        self.assertEqual(left, {made[3].name, made[4].name})

    def test_max_age生效(self) -> None:
        old = self._make("mnaa", 3, mtime=1_600_000_000.0)
        fresh = self._make("mnaa", 3)
        deleted = cleanup_shots(self.tmp, keep=0, max_age_sec=86400)
        self.assertEqual(deleted, 3)
        self.assertTrue(all(not p.exists() for p in old))
        self.assertTrue(all(p.exists() for p in fresh))

    def test_两条规则同时生效(self) -> None:
        """过期的一律删，没过期的只留最新 keep 张。"""
        self._make("old", 2, mtime=1_600_000_000.0)
        fresh = self._make("new", 5)
        deleted = cleanup_shots(self.tmp, keep=2, max_age_sec=86400)
        self.assertEqual(deleted, 5)  # 2 张过期的 + 3 张超出 keep 的
        self.assertEqual(len([p for p in fresh if p.exists()]), 2)

    def test_跨会话子目录都清理(self) -> None:
        """keep 是**全局**的：跨 session 目录一起按新旧排序，保留最新的 N 张。"""
        self._make("mnaa", 3)
        self._make("xyzw", 3)
        deleted = cleanup_shots(self.tmp, keep=1, max_age_sec=0)
        self.assertEqual(deleted, 5)

    def test_保留的文件全局最新(self) -> None:
        old = self._make("aaa", 2, mtime=1_700_000_000.0)
        new = self._make("bbb", 2, mtime=1_700_000_100.0)
        cleanup_shots(self.tmp, keep=2, max_age_sec=0)
        self.assertEqual({p.name for p in old if p.exists()}, set())
        self.assertEqual({p.name for p in new if p.exists()}, {p.name for p in new})

    def test_空目录(self) -> None:
        root = self.tmp / "shots"
        root.mkdir()
        self.assertEqual(cleanup_shots(self.tmp, keep=1, max_age_sec=1), 0)

    def test_目录不存在不抛异常(self) -> None:
        """第一次跑插件时目录本来就不存在，绝不能因此报错。"""
        self.assertEqual(cleanup_shots(self.tmp / "压根没有这个目录"), 0)
        self.assertEqual(cleanup_shots(self.tmp / "shots"), 0)

    def test_目录参数不存在也不抛(self) -> None:
        self.assertEqual(cleanup_shots("\x00非法路径"), 0)

    def test_文件被占用时跳过而不是抛异常(self) -> None:
        """★ Windows 实测：只读/被占用的文件 os.remove 会抛 PermissionError。"""
        made = self._make("mnaa", 3)
        locked = made[0]
        # 把三张都改成"很久以前"，让 max_age 规则把它们全列为待删对象，
        # 这样无论排序如何，锁住的那个都一定是受害者之一。
        old = time.time() - 10_000
        for path in made:
            os.utime(path, (old, old))
        os.chmod(locked, stat.S_IREAD)  # 置为只读
        # 兜底恢复权限，否则临时目录删不掉
        self.addCleanup(lambda: os.chmod(locked, stat.S_IWRITE) if locked.exists() else None)

        deleted = cleanup_shots(self.tmp, keep=0, max_age_sec=1)
        self.assertEqual(deleted, 2, "删不掉的那个不应计入，其余两个必须删掉")
        self.assertTrue(locked.exists(), "被占用的文件应该跳过")

    def test_keep为0且不设时间时不删(self) -> None:
        """keep=0 + max_age=0 = 两条规则都关掉，属于"只统计不清理"的保守行为。"""
        self._make("mnaa", 4)
        self.assertEqual(cleanup_shots(self.tmp, keep=0, max_age_sec=0), 0)

    def test_返回删除数量准确(self) -> None:
        self._make("mnaa", 6)
        self.assertEqual(cleanup_shots(self.tmp, keep=4, max_age_sec=0), 2)
        self.assertEqual(cleanup_shots(self.tmp, keep=4, max_age_sec=0), 0)

    def test_直接传shots目录也能清理(self) -> None:
        """兼容把 ``<data>/shots`` 本身当参数传进来的调用方式。"""
        self._make("mnaa", 3)
        deleted = cleanup_shots(self.tmp / "shots", keep=1, max_age_sec=0)
        self.assertEqual(deleted, 2)

    def test_忽略子目录外的散落文件(self) -> None:
        """根目录下直接放的文件不该被当成截图删掉。"""
        stray = self.write("notes.txt", b"keep me")
        self._make("mnaa", 2)
        cleanup_shots(self.tmp, keep=0, max_age_sec=0)
        self.assertTrue(stray.exists())


# --- encode_for_llm -----------------------------------------------------------


class TestEncodeForLlm(TempDirTestCase):
    """给模型的 data URL：必须降采样，且没有 Pillow 时优雅降级。"""

    def test_没有PIL时返回None不抛异常(self) -> None:
        """★ 关键降级路径：PIL 可能没装。"""
        path = self.write("shot.png", build_png())
        with pil_missing():
            self.assertIsNone(encode_for_llm(path))

    def test_没有PIL时文件不存在也返回None(self) -> None:
        with pil_missing():
            self.assertIsNone(encode_for_llm(self.tmp / "nope.png"))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_返回jpeg_data_url(self) -> None:
        path = self.write("shot.png", build_png(40, 20))
        url = encode_for_llm(path)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(url.split(",", 1)[1])
        self.assertEqual(raw[:3], MAGIC_JPEG, "输出的必须是真 JPEG")

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_小图不放大(self) -> None:
        path = self.write("shot.png", build_png(40, 20))
        url = encode_for_llm(path, max_width=1280)
        assert url is not None
        size = self._decode_size(url)
        self.assertEqual(size, (40, 20), "宽度没超上限时不应改变尺寸")

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_大图降采样到max_width(self) -> None:
        """★ 1850x1208 的原图很贵，必须能降到 max_width。"""
        path = self.write("shot.png", build_png(2000, 1000))
        url = encode_for_llm(path, max_width=1280)
        assert url is not None
        width, height = self._decode_size(url)
        self.assertEqual(width, 1280)
        self.assertEqual(height, 640, "必须等比缩放")
        # 降采样后的体积应当远小于原图（这里是纯色图，用像素数做代理指标）
        self.assertLess(width * height, 2000 * 1000)

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_max_width非法时返回None(self) -> None:
        path = self.write("shot.png", build_png())
        self.assertIsNone(encode_for_llm(path, max_width=0))
        self.assertIsNone(encode_for_llm(path, max_width=-100))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_文件不存在返回None(self) -> None:
        self.assertIsNone(encode_for_llm(self.tmp / "nope.png"))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_不是图片返回None(self) -> None:
        """Pillow 对损坏图片会抛各种异常，必须全被吞掉。"""
        path = self.write("fake.png", "这不是图片".encode("utf-8") * 100)
        self.assertIsNone(encode_for_llm(path))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_残缺JPEG头返回None(self) -> None:
        """只有 JPEG 头、没有真实图像数据：Pillow 打不开，必须降级成 None。"""
        path = self.write("broken.jpg", build_jpeg())
        self.assertIsNone(encode_for_llm(path))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_空文件返回None(self) -> None:
        path = self.write("empty.png", b"")
        self.assertIsNone(encode_for_llm(path))

    @unittest.skipUnless(HAS_PIL, "本机没有 Pillow，跳过需要 PIL 的用例")
    def test_接受str路径(self) -> None:
        path = self.write("shot.png", build_png())
        self.assertIsNotNone(encode_for_llm(str(path)))

    @staticmethod
    def _decode_size(data_url: str) -> tuple[int, int]:
        """把 data URL 解回来读尺寸，验证的是"模型真能拿到什么"。"""
        from PIL import Image
        import io

        raw = base64.b64decode(data_url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            return image.size


# --- 端到端小串场 -------------------------------------------------------------


class Test端到端(TempDirTestCase):
    """把几个函数串起来走一遍真实的截图生命周期。"""

    def test_取路径_校验_编码_清理(self) -> None:
        # 1) 假装 bsk 往我们给的 --out 路径写了一张图
        path = make_shot_path(self.tmp, "mnaa")
        data = build_png(1850, 1208)
        path.write_bytes(data)

        # 2) 模拟 bsk 返回的 JSON（实测样本，注意 format 字段可能是假的）
        shot = Screenshot.from_json(
            {
                "byte_size": len(data),
                "format": "png",
                "height": 1208,
                "width": 1850,
                "path": str(path),
                "tab_id": 1398284761,
            }
        )
        ok, why = verify_shot(shot)
        self.assertTrue(ok, why)

        # 3) 给模型看之前降采样
        url = encode_for_llm(path)
        if HAS_PIL:
            self.assertIsNotNone(url)
            assert url is not None
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        else:
            self.assertIsNone(url, "没有 PIL 时应返回 None，调用方回退到原文件路径")

        # 4) 清理：先用"既不限数量也不过期"确认不动文件，再用 1 秒过期把它删掉
        self.assertEqual(cleanup_shots(self.tmp, keep=0, max_age_sec=0), 0)
        self.assertEqual(cleanup_shots(self.tmp, keep=0, max_age_sec=-1), 0)
        old = time.time() - 10
        os.utime(path, (old, old))
        self.assertEqual(cleanup_shots(self.tmp, keep=0, max_age_sec=1), 1)
        self.assertFalse(path.exists())

    def test_魔数校验能识别出撒谎的format(self) -> None:
        """端到端复现实测坑：请求 png，浏览器给了 JPEG。"""
        path = make_shot_path(self.tmp, "mnaa", ext=".png")
        data = build_jpeg(1850, 1208)
        path.write_bytes(data)
        shot = Screenshot.from_json(
            {
                "byte_size": len(data),
                "format": "png",  # ← 在撒谎
                "height": 1208,
                "width": 1850,
                "path": str(path),
            }
        )
        ok, why = verify_shot(shot)
        self.assertFalse(ok)
        self.assertIn("jpeg", why)
        self.assertTrue(path.exists(), "文件本身没坏，不该被删掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
