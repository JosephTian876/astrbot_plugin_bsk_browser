"""截图文件管理 —— 唯一路径、真实性校验、清理、给模型看的编码。

``bsk screenshot`` **只返回文件路径，不返回 base64**（实测），所以截图文件的生命周期
必须由插件自己管。这个模块就是唯一管这件事的地方，上层 ``service.py`` 只需要：

1. ``path = make_shot_path(shots_dir, session_id)`` —— 先占一个唯一文件名；
2. 把 ``--out path`` 传给 bsk；
3. ``ok, why = verify_shot(shot)`` —— 不信 bsk 的自述，自己验一遍；
4. ``url = encode_for_llm(path)`` —— 转成 data URL 给模型看。

对应 ARCHITECTURE.md §4.5，每条设计都源自一个实测的坑：

- **``--out`` 会覆盖已有文件**：所以文件名必须唯一（时间戳 + 进程内计数器 + 随机数），
  否则多会话并发截图会互相覆盖，拿到别人的图。
- **省略 ``--out`` 会落到系统 TEMP**（``bsk-screenshot-<毫秒>.png``）：文件名不可预测，
  没法做后续的校验与清理，所以**永远显式传 ``--out``**。
- **``format`` 字段可能在说谎**：某些 Chromium/Edge 构建请求 png 却返回 JPEG，
  后缀名同样不可信。只有**读文件头魔数**才是真的（见 :func:`sniff_format`）。
- **截图实测 1850x1208**（observe 报的视口是 910x604，DPR≈2），整页截图更大，
  150KB 起步。所以清理（:func:`cleanup_shots`）和降采样（:func:`encode_for_llm`）
  都是必需功能，不是优化项。

设计约束：

- 只用标准库，**不引入任何第三方依赖**；``PIL(Pillow)`` 可能没装，
  所以 :func:`encode_for_llm` 必须能在没有 PIL 的机器上优雅降级。
- 不做任何 IO 缓存：截图文件随时可能被清理或覆盖，缓存只会拿到过期结论。
"""

from __future__ import annotations

import base64
import contextlib
import io
import itertools
import os
import re
import struct
import time
from pathlib import Path
from typing import Any

from .models import Screenshot

__all__ = [
    "MAGIC_GIF",
    "MAGIC_JPEG",
    "MAGIC_PNG",
    "MAGIC_WEBP_PREFIX",
    "MAGIC_WEBP_SUFFIX",
    "MAX_SNIFF_BYTES",
    "MIME_TYPES",
    "SAFE_SESSION_ID_FALLBACK",
    "SNIFF_BYTES",
    "cleanup_shots",
    "encode_for_llm",
    "make_shot_path",
    "sanitize_session_id",
    "sniff_format",
    "verify_shot",
]

# --- 文件头魔数 ---------------------------------------------------------------
# 魔数比后缀名可靠：图片内容是浏览器给的，后缀名是我们自己猜的。

MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
"""PNG 的 8 字节签名。"""

MAGIC_JPEG = b"\xff\xd8\xff"
"""JPEG 的开头（SOI + 第一个标记的高字节）。用完这 3 字节就够了。"""

MAGIC_GIF = b"GIF8"
"""GIF87a / GIF89a 的共同前缀。"""

MAGIC_WEBP_PREFIX = b"RIFF"
"""WebP 是 RIFF 容器，这里是它的容器头。"""

MAGIC_WEBP_SUFFIX = b"WEBP"
"""RIFF 容器里偏移 8 处的 4 字节格式标识，必须一起看才能确认是 WebP。"""

SNIFF_BYTES = 16
"""默认读多少字节头部。12 字节就够判断 WebP，留点余量。"""

MAX_SNIFF_BYTES = 4096
"""单次嗅探允许的最大读取量。

:func:`sniff_format` 只读文件头，**绝不**为了判断格式把整个文件读进内存 ——
全页截图可能几十 MB。这里再设一个上限，防止调用方传入离谱的值。
"""

MIME_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
"""data URL 用的 MIME 表。``unknown`` 故意不在表里 —— 格式都没认出来就别往模型发。"""

SAFE_SESSION_ID_FALLBACK = "session"
"""当 session_id 被过滤成空串时用的兜底目录名。"""

_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_]+")
"""白名单取反：不是字母、数字、下划线的**每一段**都换成下划线（连续段合并）。"""

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
"""Windows 的保留设备名：叫 ``con`` 的目录在某些机器上建不出来，兜底改掉。"""

_counter = itertools.count()
"""进程内自增序号，用于同一毫秒内的多次调用也不撞名。

``itertools.count`` 的 ``next()`` 是原子操作，多线程并发调用不会拿到同一个值。
"""


# --- 内部小工具 ---------------------------------------------------------------


def _format_magic(head: bytes) -> str:
    """把文件头转成 ``89 50 4E 47`` 这样的十六进制串，写进错误信息方便排查。"""
    return " ".join(f"{b:02X}" for b in head[:8])


def _to_bytes(value: str | bytes | bytearray, encoding: str = "ascii") -> bytes:
    """把 str / bytes 统一成 bytes，用于前缀比较。

    ``encoding="ascii"`` 时非 ASCII 字符会编码失败，此时返回 ``b""``：
    这样的值**永远不可能**匹配魔数，正好是我们要的结果（函数式魔数是 ASCII）。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return value.encode(encoding)
    except (UnicodeEncodeError, AttributeError):
        return b""


def _read_head(path: str | Path, size: int = SNIFF_BYTES) -> bytes:
    """读取文件头若干字节；读不到（不存在、没权限、是目录）就返回空 bytes。"""
    try:
        limit = max(4, min(int(size), MAX_SNIFF_BYTES))
    except (TypeError, ValueError):
        limit = SNIFF_BYTES
    try:
        with open(path, "rb") as fp:
            # 只读 limit 字节：文件可能几十 MB，绝不能 read() 全量。
            return fp.read(limit)
    except OSError:
        return b""


def _png_size(head: bytes) -> tuple[int, int] | None:
    """从 PNG 头部解析出 ``(宽, 高)``。

    PNG 的 IHDR 结构固定：偏移 0 是 8 字节签名，接着 4 字节长度 + 4 字节类型 ``IHDR``，
    因此宽高分别在偏移 16 和 20 处（大端 4 字节无符号）。只要 24 字节就能拿到。
    """
    if len(head) < 24 or not head.startswith(MAGIC_PNG) or head[12:16] != b"IHDR":
        return None

    try:
        width, height = struct.unpack(">II", head[16:24])
    except struct.error:  # pragma: no cover - len 检查已经挡住了
        return None
    return width, height


def _jpeg_size(head: bytes) -> tuple[int, int] | None:
    """从 JPEG 的 SOF 段解析出 ``(宽, 高)``。

    需要一路跳过可变长的段（每段是 ``FF <marker> <2字节长度> <载荷>``），所以光靠
    文件头那十几个字节是不够的，必须往后多读一些。这里会按需扩读，最多 256KB，
    拿不到就返回 ``None`` —— 尺寸只用于给用户展示，拿不到不算错误。
    """
    if len(head) < 4 or not head.startswith(MAGIC_JPEG):
        return None

    index = 2  # 跳过 SOI(FFD8)
    # 逐段前进：段头是 FF + marker，其中 FF 可以重复出现（填充）。
    while index + 9 <= len(head):
        if head[index] != 0xFF:
            index += 1
            continue
        marker = head[index + 1]
        if marker == 0xFF:  # 填充字节
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            # 无载荷的独立标记（TEM/RSTn），只占 2 字节。
            index += 2
            continue
        if marker == 0xD9:  # EOI，图像结束了还没见到 SOF
            return None
        if index + 4 > len(head):  # 长度字段都读不全
            return None
        segment_length = int.from_bytes(head[index + 2 : index + 4], "big")
        if segment_length < 2:  # 长度非法，防死循环
            return None
        # SOF0..SOF15 里的 0xC4(DHT)/0xC8(JPG)/0xCC(DAC) 不是 SOF。
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > len(head):
                return None
            height = int.from_bytes(head[index + 5 : index + 7], "big")
            width = int.from_bytes(head[index + 7 : index + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        index += 2 + segment_length
    return None


def _image_size(path: str | Path, sniffed: str) -> tuple[int, int] | None:
    """尽力解析图片尺寸，失败返回 ``None``（调用方要能接受拿不到）。"""
    if sniffed == "png":
        return _png_size(_read_head(path, 32))
    if sniffed == "jpeg":
        # 最多 256KB：足够覆盖元数据段很多的 JPEG，又不至于把大图整个读进来。
        return _jpeg_size(_read_head(path, 262144))
    return None


# --- 公开 API -----------------------------------------------------------------


def sniff_format(path: str | Path) -> str:
    """读文件头判断图片的**真实**格式。

    为什么不能信后缀名或 bsk 返回的 ``format`` 字段：实测某些 Chromium/Edge 构建
    即使请求 PNG 也返回 JPEG，而插件是按 ``--out`` 自己起的文件名（通常是 ``.png``），
    于是"后缀说是 png、内容其实是 jpeg"。只有魔数不会骗人。

    Args:
        path: 图片文件路径。

    Returns:
        ``"png"`` / ``"jpeg"`` / ``"gif"`` / ``"webp"`` / ``"unknown"``。
        文件不存在、是个目录、没权限时一律返回 ``"unknown"``（**不抛异常**：
        调用方只是想知道格式，不该因为这个把整条链路炸掉）。

    Note:
        只读文件头十几个字节，读全文的代价在大截图上不可接受。
    """
    head = _read_head(path, SNIFF_BYTES)
    if not head:
        return "unknown"
    if head.startswith(MAGIC_PNG):
        return "png"
    if head.startswith(MAGIC_JPEG):
        return "jpeg"
    if head.startswith(MAGIC_GIF):
        return "gif"
    # WebP 必须同时看容器头和格式标识，否则会把所有 RIFF 文件（wav/avi）都算成 WebP。
    if head.startswith(MAGIC_WEBP_PREFIX) and head[8:12] == MAGIC_WEBP_SUFFIX:
        return "webp"
    return "unknown"


def verify_shot(shot: Screenshot) -> tuple[bool, str]:
    """校验一张截图是不是"真的存在且和 bsk 说的一致"。

    校验三件事（缺一不可），按"最便宜的检查放前面"排序：

    1. **文件存在**（且是普通文件，不是目录）；
    2. **磁盘大小 == ``shot.byte_size``** —— 大小不符说明文件被截断、被覆盖，
       或者（并发场景）我们拿到的路径根本不是这次截图写出来的；
    3. **魔数与 ``shot.format`` 一致** —— 用真实文件头戳穿"format 字段说谎"。
       注意这里比的是**是否受支持**：bsk 说 ``png`` 而实际是 ``jpeg`` 属于典型的
       "字段在说谎"；如果两边说的是同一种格式那就没问题。

    Args:
        shot: bsk ``screenshot --json`` 解析出来的对象。

    Returns:
        ``(是否通过, 原因)``。通过时原因是简短说明（含真实格式与大小），
        失败时原因是**给模型/用户看的中文**，说明下一步能怎么办。

    Note:
        任何 IO 异常都被吞成"不通过"，**绝不向外抛异常**：这个函数的语义就是
        "这张图能不能用"，抛异常会让调用方没法统一处理。
    """
    if not shot.path:
        return False, "bsk 没有返回截图路径，这次截图失败了，请重试一次。"

    path = Path(shot.path)
    try:
        stat_result = path.stat()
    except OSError as exc:
        return False, (
            f"截图文件不存在或无法访问（{path}）：{exc}。"
            "可能是文件被清理掉了，请重新截图。"
        )

    if not path.is_file():
        return False, f"截图路径不是文件（{path}），请重新截图。"

    # 检查 2：大小。byte_size <= 0 说明 bsk 没报大小，这时跳过比较而不是误判失败。
    actual_size = stat_result.st_size
    if shot.byte_size > 0 and actual_size != shot.byte_size:
        return False, (
            f"截图大小对不上：bsk 说有 {shot.byte_size} 字节，磁盘上是 {actual_size} 字节。"
            "文件可能被截断或被别的会话覆盖了，请重新截图。"
        )
    if actual_size == 0:
        return False, "截图文件是空的（0 字节），请重新截图。"

    # 检查 3：魔数 vs bsk 自述的格式。
    actual_format = sniff_format(path)
    if actual_format == "unknown":
        head = _read_head(path, 8)
        return False, (
            f"截图内容不是已知的图片格式（文件头：{_format_magic(head) or '读不出来'}）。"
            "文件可能损坏了，请重新截图。"
        )

    declared = (shot.format or "").strip().lower().lstrip(".")
    if declared in ("jpg", "jpe"):
        declared = "jpeg"
    if declared and declared in MIME_TYPES and declared != actual_format:
        return False, (
            f"截图格式和 bsk 声明的不一致：bsk 说 {declared}，实际文件头是 {actual_format}。"
            "这是浏览器返回了另一种格式（已知某些 Chromium/Edge 构建会这样），"
            "文件本身可用，但请按实际格式处理。"
        )

    size_hint = f"{shot.width}x{shot.height}" if shot.width and shot.height else "尺寸未知"
    return True, f"截图正常（{actual_format}，{size_hint}，{actual_size} 字节）。"


def sanitize_session_id(session_id: str) -> str:
    """把 ``session_id`` 洗成可以安全当目录名的字符串。

    ``session_id`` 来自 bsk（外部输入），可能含 ``..``、``/``、``\\``、``:``、空字节
    等，直接拼进路径会造成**目录穿越**（``../../`` 能一路写到工作区外面）。

    策略是**白名单**：只保留 ``[A-Za-z0-9_]``，其它字符的连续段统统替换成一个 ``_``。
    黑名单（"过滤掉 ``..``"）挡不住 URL 编码、Unicode 同形字之类的变体，白名单才可靠。

    Args:
        session_id: bsk 的会话 id（正常是 4 个小写字母，如 ``mnaa``）。

    Returns:
        只含安全字符的目录名；全是不安全字符时返回 ``SAFE_SESSION_ID_FALLBACK``。
    """
    cleaned = _SAFE_SESSION_ID_RE.sub("_", str(session_id or "")).strip("_")
    if not cleaned:
        return SAFE_SESSION_ID_FALLBACK
    # Windows 保留设备名（con/nul/com1…）当目录名会失败，加个前缀绕开。
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        return f"_{cleaned}"
    # 留出余量，避免超长 session_id 撞上路径长度上限。
    return cleaned[:64]


def make_shot_path(directory: str | Path, session_id: str, *, ext: str = ".png") -> Path:
    """生成一个**唯一**的截图路径，并把父目录建好。

    路径格式：``<directory>/shots/<session_id 洗净后>/<时间戳毫秒>-<序号>-<随机>.png``。

    并发安全靠三步（单靠时间戳不够：同一毫秒内并发调用会撞名，实测 Windows 上
    毫秒级甚至更粗的时间源都会撞）：

    1. 毫秒时间戳 —— 让文件名**天然按时间排序**，清理时好读；
    2. 进程内自增序号 —— 同一进程同一毫秒的调用不会撞；
    3. 随机后缀 + ``O_CREAT|O_EXCL`` 抢占 —— 跨进程也不会撞，真撞了就重试。

    **注意**：创建的只是一个 0 字节占位文件。这是故意的 —— ``--out`` 会覆盖已有文件，
    所以"先占名字"比"先算名字"更安全；而且这样返回的路径在 bsk 写之前就保证可写。

    Args:
        directory: 截图根目录（通常是插件数据目录下的 ``shots`` 的父目录）。
        session_id: bsk 的会话 id，会被 :func:`sanitize_session_id` 白名单过滤。
        ext: 文件后缀，默认 ``.png``。

    Returns:
        一个当前不存在（已被本函数占用）的绝对路径。

    Raises:
        OSError: 目录创建失败，或重试多次仍抢不到唯一名字。**这个必须让调用方知道**：
            拿不到确定路径就没法安全地截图。
    """
    base = Path(directory) / "shots" / sanitize_session_id(session_id)

    suffix = str(ext or ".png").strip()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    # 后缀也过一遍白名单，防止 ext 里再塞路径分隔符。
    suffix = f".{re.sub(r'[^A-Za-z0-9]', '', suffix[1:])[:8] or 'png'}"

    base.mkdir(parents=True, exist_ok=True)

    for _ in range(8):
        name = f"{int(time.time() * 1000)}-{next(_counter)}-{os.urandom(4).hex()}{suffix}"
        candidate = base / name
        try:
            # O_EXCL：文件已存在就失败。抢到就说明这个名字归我们了。
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue  # 撞名（极小概率），换一个再来
        except OSError:
            # 别的 IO 错误（权限、路径过长）没有重试价值，直接冒泡给调用方。
            raise
        with contextlib.suppress(OSError):
            os.close(fd)
        return candidate

    raise OSError(f"连续 8 次都没能抢到唯一的截图文件名，目录：{base}")


def cleanup_shots(
    directory: str | Path, *, keep: int = 50, max_age_sec: float = 86400
) -> int:
    """清理旧截图，返回**实际删除**的文件数。

    两条规则同时生效（满足任意一条就删）：

    - **保留最新 ``keep`` 张**：先按修改时间倒序，第 ``keep`` 张之后的全部删掉；
    - **删除超过 ``max_age_sec`` 秒的**：按文件的修改时间算，默认 86400 秒（1 天）。

    为什么要两条：只留数量挡不住"一天只截几张但每张都是几十 MB 的全页图"；
    只留时间挡不住"一分钟内截 500 张"把磁盘打满。

    容错（每一条都必须做到，否则清理会把业务搞崩）：

    - 目录不存在 → 返回 0，**不抛异常**（第一次跑插件时目录本来就不存在）；
    - 单个文件删不掉（被占用、只读）→ **跳过并继续**，不中断整轮清理
      （Windows 上文件被占用是 ``PermissionError``，实测会直接抛）；
    - 统计、排序过程中的任何 IO 异常 → 当作"这个文件不存在"，忽略它。

    Args:
        directory: 截图根目录（内部会递归一层 ``shots/**``）。
        keep: 最多保留多少张最新的截图；传 0 表示不看数量，只按时间清理。
        max_age_sec: 多少秒之前的算过期；传 0 或负数表示不看时间。

    Returns:
        成功删掉的文件数（删失败的不计入）。
    """
    root = Path(directory)
    if not root.is_dir():
        return 0

    entries: list[os.DirEntry[str]] = []
    try:
        with os.scandir(root) as it:  # 用 scandir：一次遍历同时拿到名字和 stat
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sub = entry.path
                        try:
                            with os.scandir(sub) as sub_it:
                                entries.extend(sub_it)
                        except OSError:
                            continue  # 子目录读不了就跳过它，别影响其它目录
                    elif entry.is_file(follow_symlinks=False):
                        entries.append(entry)
                except OSError:
                    continue
    except OSError:
        return 0

    now = time.time()
    stats: dict[str, tuple[float, float]] = {}  # path -> (mtime, size)
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # 文件在遍历过程中被删掉了，正常现象
        stats[entry.path] = (info.st_mtime, info.st_size)

    # 排序键用 (mtime, 路径)：路径只用来在 mtime 相同时保证顺序稳定，不参与业务判断。
    ordered = sorted(stats.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)

    keep_count = max(0, int(keep))
    victims: list[str] = []
    for index, (path, (mtime, _size)) in enumerate(ordered):
        too_many = keep_count > 0 and index >= keep_count
        too_old = max_age_sec > 0 and (now - mtime) > max_age_sec
        if too_many or too_old:
            victims.append(path)

    deleted = 0
    for path in victims:
        try:
            os.remove(path)
            deleted += 1
        except OSError:
            # 被占用 / 只读 / 已经没了：跳过。清理是尽力而为，
            # 绝不能因为一个文件删不掉就让整个截图功能报错。
            continue
    return deleted


def encode_for_llm(path: str | Path, *, max_width: int = 1280) -> str | None:
    """把截图压成 data URL，供模型直接看。

    原图实测 1850x1208、150KB 起步，全页截图能到几十 MB —— **绝不能原样塞给模型**。
    这里用 Pillow 缩到 ``max_width`` 宽以内并重新编码，体积通常能降到十分之一。

    Args:
        path: 截图文件路径。
        max_width: 允许的最大宽度（像素）。宽度已经不超过它时**不放大**，只重编码。

    Returns:
        形如 ``data:image/jpeg;base64,...`` 的字符串；以下情况返回 ``None``：

        - **没装 Pillow**（``PIL`` 可能不存在）—— 调用方应回退成直接用原文件路径；
        - 文件读不了 / 不是图片 / 编码过程出错。

    Note:
        输出统一是 **JPEG**：体积最小，而且截图没有透明通道，PNG 换 JPEG 不丢信息
        （透明像素会被合成到白底）。模型对 JPEG 与 PNG 的识别没有区别。
    """
    if max_width <= 0 or not Path(path).is_file():
        return None

    try:
        from PIL import Image
    except ImportError:
        # 没装 Pillow：这是**预期内**的情况，不是错误，直接让调用方走原图路径。
        return None

    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                return None

            # 先等比缩小，再转 RGB 存 JPEG。放大没有意义，所以只在超宽时才 resize。
            if width > max_width:
                ratio = max_width / float(width)
                target = (max_width, max(1, round(height * ratio)))
                resampling = getattr(
                    getattr(Image, "Resampling", Image), "LANCZOS", None
                )
                if resampling is not None:
                    image = image.resize(target, resampling)
                else:  # pragma: no cover - 只有很老的 Pillow 才会走到
                    image = image.resize(target)

            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=85)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        # Pillow 对损坏图片会抛一堆互不相关的异常类型，这里统一吞掉：
        # 编码只是"锦上添花"，失败时返回 None 让调用方回退到原图路径。
        return None

    return f"data:{MIME_TYPES['jpeg']};base64,{encoded}"
