"""``bsk observe`` 输出的 VOM 文本解析。

``bsk observe --json`` 只返回 4 个字段，其中 ``text`` 是带标记的缩进文本树，
不是 JSON 结构：

.. code-block:: text

    @vom 1
    @view 910x604
    @layers 1 focus=L1
    L1 page
      RootWebArea "Example Domain"
        paragraph "……"
          StaticText "……"
        @e1 link "Learn more" [→ iana.org]

标记含义（实测确认）：

===============  ==================================================
标记              含义
===============  ==================================================
``@vom 1``       VOM 协议版本号
``@view 910x604`` 视口尺寸（CSS px）
``@layers 1 …``  层数与焦点层
``L1 page``      第 1 层，类型 page
``RootWebArea "…"`` 页面标题就在这里（引号内）
``@eN role "…"``  可交互元素：ref + role + 名称 + 可选目标
===============  ==================================================

关键事实（踩过的坑）：

1. ``observe`` 没有独立的 ``title`` / ``url`` 字段。标题只能从
   ``RootWebArea "..."`` 里正则提取。
2. ref 形如 ``@eN``（N 从 1 递增），存进 :class:`~bsk.models.PageRef` 时
   不带 ``@``（即 ``e1``），因为 bsk 的 ``--ref`` 参数两种写法都收，
   而带不带 ``@`` 混用最容易出错，这里统一成不带。
3. ``@view`` 里的尺寸是 CSS px，不是截图的像素尺寸。高分屏
   （DPR≈2）下截图会大整整一倍：实测同一时刻 observe 报 ``910x604``，
   而截图是 ``1850x1208``。所以不要拿 observe 的坐标去点截图里的位置，
   也不要直接比较这两组数字；必须先用 ``截图宽 / 视口宽`` 算出 DPR 再换算。
4. ``paragraph "X"`` 下面紧跟的 ``StaticText "X"`` 内容完全相同，
   摘要里必须去重，否则白白翻倍占用 LLM 上下文。

本模块是纯函数集合：没有 IO、没有全局可变状态、不 import astrbot，
可以脱离 AstrBot 与浏览器独立单测。所有输入都当作外部不可信数据处理，
畸形输入一律优雅降级，绝不抛异常。
"""

from __future__ import annotations

import re

from .models import PageObservation, PageRef

__all__ = ["parse_observation", "parse_vom_text", "summarize"]


# ---------------------------------------------------------------------------
# 解析上限：防止畸形的超长输入把 CPU / 内存吃光
# ---------------------------------------------------------------------------

MAX_TEXT_CHARS = 1_000_000
"""单个 VOM 文本最多解析这么多字符，超出部分直接丢弃。"""

MAX_LINES = 20_000
"""最多扫描这么多行，防止病态输入拖慢解析。"""

MAX_REFS = 500
"""最多收集这么多元素。真实页面通常只有几十个。"""

MAX_TITLE_CHARS = 300
MAX_NAME_CHARS = 200
MAX_TARGET_CHARS = 200
MAX_LINE_CHARS = 400
"""摘要里单行正文的最大长度。"""

MAX_REFS_SHOWN = 40
"""摘要里最多列出多少个元素。"""

TRUNCATION_NOTE = "……（页面内容过长，已截断）"


# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

# 视口：@view 910x604。分隔符宽容一点，兼容大写的 X 与全角乘号。
_RE_VIEW = re.compile(r"@view\s+(\d+)\s*[xX\u00d7]\s*(\d+)")

# 元素行：@e<编号> [role] "名字" [目标]
#   - ^\s*     缩进（空格或 tab 都能吃）
#   - @e\d+    必须是 @ 紧跟 e 再跟数字。这样 @vom / @view / @layers
#              以及正文里出现的 "@example" 都不会被误判成元素
#   - \b       编号后面必须是词边界，避免把 @e1x 这类东西吃进来
#   - role 是可选的，因为有的行没有 role
_RE_ELEMENT = re.compile(
    r"^\s*@(?P<ref>e\d+)\b"
    r"(?:\s+(?P<role>[A-Za-z][A-Za-z0-9_-]*))?"
    r"\s*(?P<tail>.*)$"
)

# tail 的三种形态，按优先级尝试
_RE_NAME_TARGET = re.compile(r'^"(?P<name>.*)"\s*\[(?P<target>[^\[\]]*)\]\s*$')
_RE_NAME_ONLY = re.compile(r'^"(?P<name>.*)"\s*$')
_RE_NAME_ESCAPED = re.compile(r'^"(?P<name>(?:[^"\\]|\\.)*)"')

# 层声明行，如 "L1 page"（正文按行判断前会先 strip）
_RE_LAYER = re.compile(r"^L\d+\b")

# 标题兜底：万一 RootWebArea 那行前面混进了别的前缀（格式变化），
# 用全文搜索再捞一次。只在逐行严格匹配失败后才用。
_RE_ROOT_FALLBACK = re.compile(r'RootWebArea\s+"((?:[^"\\]|\\.)*)"')

# 元素行开头的 @eN，用于从正文里剔除元素行
_RE_ELEMENT_PREFIX = re.compile(r"^@e\d+\b")

# 取一行里第一个完整引号对的内容（转义感知），用于抽取正文
_RE_FIRST_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')

# target 前缀里的箭头符号，剥掉后才是干净的目标（→ iana.org → iana.org）
_TARGET_ARROW_CHARS = "\u2192\u27f6\u2794\u279c\u27a1\u21d2>-\u2013\u2014 \t"

# JSON 风格转义表
_ESCAPE_MAP = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}


# ---------------------------------------------------------------------------
# 文本清洗小工具
# ---------------------------------------------------------------------------


def _sanitize(value: str) -> str:
    """把无法编码成 UTF-8 的字符换成 U+FFFD。

    bsk 的输出是外部数据，理论上可能残留孤立代理项（lone surrogate，
    例如 ``\\ud800`` 这种"半个 emoji"）。这种字符串打印会抛
    ``UnicodeEncodeError``，进而把整个工具调用打挂，所以必须提前清掉。
    """
    if not value:
        return value
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # 只有代理项区间会导致编码失败，逐字符替换即可。
        return "".join(
            "\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in value
        )
    return value


def _unescape(value: str) -> str:
    """还原 JSON 风格的反斜杠转义。

    只处理已知的转义序列（``\\"`` ``\\\\`` ``\\n`` ``\\uXXXX`` 等），
    认不出来的 ``\\x`` 原样保留 —— 否则 Windows 路径 ``C:\\Users`` 会被吃掉
    一个反斜杠。
    """
    if "\\" not in value:
        return value

    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        # 末尾孤零零一个反斜杠，或普通字符：直接收下
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue

        nxt = value[i + 1]
        if nxt in _ESCAPE_MAP:
            out.append(_ESCAPE_MAP[nxt])
            i += 2
        elif nxt == "u":
            hex4 = value[i + 2 : i + 6]
            try:
                out.append(chr(int(hex4, 16)))
                i += 6
            except ValueError:
                # \u 后面不是 4 位十六进制，当普通反斜杠处理
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _finalize(value: str, limit: int) -> str:
    """解析出来的文本片段的统一出口：反转义 → 清洗 → 限长。"""
    out = _sanitize(_unescape(value))
    if limit > 0 and len(out) > limit:
        out = out[:limit] + "…"
    return out


def _take_quoted(value: str) -> str:
    """从一段以引号开头的文本里取出引号内的内容。

    分三档，越往后越宽松：

    1. 引号有闭合（``"abc"``）→ 取第一对引号之间的内容。贪婪匹配到最后一个
       引号，所以名字里含裸引号也能取全，例如 ``"Say "hi""`` → ``Say "hi"``；
    2. 引号内有转义（``"a\\"b"``）→ 走转义感知匹配；
    3. 引号没闭合（``"abc``）→ 降级取引号后的全部内容。

    不以引号开头时返回空串，由调用方决定怎么兜底。
    """
    if not value.startswith('"'):
        return ""

    m = _RE_NAME_ONLY.match(value)
    if m:
        return m.group("name")

    m = _RE_NAME_ESCAPED.match(value)
    if m:
        return m.group("name")

    return value[1:]


def _first_quoted_or_raw(line: str) -> str:
    """取一行里第一对引号的内容；没有引号就返回整行。

    正文行的形态是 ``paragraph "正文"`` / ``StaticText "正文"``，
    引号里的才是给人看的文字，role 之类的前缀要去掉。
    """
    m = _RE_FIRST_QUOTED.search(line)
    if m:
        return m.group(1)
    return line


def _clean_target(raw: str) -> str:
    """把 ``[→ iana.org]`` 里的 ``→ iana.org`` 洗成 ``iana.org``。"""
    return raw.lstrip(_TARGET_ARROW_CHARS).strip()


def _parse_element_line(line: str) -> PageRef | None:
    """解析一行元素声明，失败返回 None（不抛异常）。"""
    m = _RE_ELEMENT.match(line)
    if not m:
        return None

    ref = m.group("ref") or ""
    if not ref:
        return None

    role = m.group("role") or ""
    tail = m.group("tail") or ""

    name = ""
    target = ""
    mt = _RE_NAME_TARGET.match(tail)
    if mt:
        # 形态一：@e1 link "Learn more" [→ iana.org]
        name = mt.group("name")
        target = _clean_target(mt.group("target") or "")
    else:
        name = _take_quoted(tail)
        if not name and not tail.startswith('"'):
            # 形态二：名字没加引号，如 @e1 link Submit
            name = tail.strip()

    return PageRef(
        ref=ref,
        role=_finalize(role, MAX_NAME_CHARS),
        name=_finalize(name, MAX_NAME_CHARS),
        target=_finalize(target, MAX_TARGET_CHARS),
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def parse_vom_text(text: str) -> tuple[str, list[PageRef], tuple[int, int] | None]:
    """解析 VOM 文本树。

    Args:
        text: ``observe --json`` 里的 ``text`` 字段原文。

    Returns:
        三元组 ``(标题, 元素列表, 视口尺寸)``：

        - 标题：``RootWebArea "..."`` 引号里的内容，找不到则为空串；
        - 元素列表：按出现顺序排列的 :class:`~bsk.models.PageRef`，
          ``ref`` 字段不带 ``@``；同一 ref 重复出现只保留第一次；
        - 视口尺寸：``@view`` 里的 ``(宽, 高)``，单位是 CSS px；
          解析不到、或者宽高不是正数时为 ``None``
          （返回 ``None`` 而不是 ``(0, 0)``，是为了避免下游拿它做
          DPR 换算时除零）。

    Note:
        本函数对任何输入都不抛异常。空串、非字符串、只有 ``@vom 1``、
        缺 ``@view``、用 tab 缩进、名字里含引号、超长文本……全部走降级路径。
    """
    if not isinstance(text, str) or not text:
        return "", [], None

    # 超长文本先切掉，后面的 splitlines 就不会建出巨大的列表
    text = text[:MAX_TEXT_CHARS]

    title = ""
    viewport: tuple[int, int] | None = None
    refs: list[PageRef] = []
    seen_refs: set[str] = set()

    for line_no, line in enumerate(text.splitlines()):
        if line_no >= MAX_LINES:
            break
        if not line.strip():
            continue

        # 1) 视口：只在还没拿到时找一次
        if viewport is None:
            mv = _RE_VIEW.search(line)
            if mv:
                width, height = int(mv.group(1)), int(mv.group(2))
                # 0 或负数说明这个 "视口" 没有意义，当作没解析到
                if width > 0 and height > 0:
                    viewport = (width, height)

        # 2) 标题：拿第一个非空的 RootWebArea。
        #    这里用 startswith 严格匹配"行首标记"，而不是 in —— 否则正文里
        #    恰好出现 "RootWebArea" 字样的段落会抢在真标题前面被误当成标题。
        if not title and line.lstrip().startswith("RootWebArea"):
            stripped_line = line.lstrip()
            candidate = _take_quoted(stripped_line[len("RootWebArea") :].lstrip())
            if candidate:
                title = _finalize(candidate, MAX_TITLE_CHARS)

        # 3) 元素：@eN 必须出现在行首（允许前导缩进），
        #    这样正文里出现的 "@e1" 字样不会被误当成元素
        if len(refs) < MAX_REFS:
            ref = _parse_element_line(line)
            if ref is not None and ref.ref not in seen_refs:
                seen_refs.add(ref.ref)
                refs.append(ref)

    # 4) 标题兜底：逐行没找到（格式变了 / 前缀异常）时再全文捞一次
    if not title:
        mf = _RE_ROOT_FALLBACK.search(text)
        if mf:
            title = _finalize(mf.group(1), MAX_TITLE_CHARS)

    return title, refs, viewport


def parse_observation(raw_json: dict) -> PageObservation:
    """把 ``observe --json`` 的原始 dict 解析成 :class:`PageObservation`。

    Args:
        raw_json: ``bsk observe --json`` 反序列化后的 dict。允许它不是 dict
            （畸形数据），也允许缺字段。

    Returns:
        填好 ``title`` / ``refs`` / ``viewport`` 的观察结果。

    Note:
        ``ref_count`` 取"JSON 声明值"与"实际解析出的元素个数"的较大者：
        JSON 缺字段时用实际值兜底，JSON 说页面有 100 个而 text 被截断只解析出
        5 个时，则如实反映页面真实规模。
    """
    observation = PageObservation.from_json(raw_json)

    if not isinstance(observation.text, str):
        observation.text = ""
    title, refs, viewport = parse_vom_text(observation.text)

    observation.title = title
    observation.refs = refs
    observation.viewport = viewport

    declared = observation.ref_count if isinstance(observation.ref_count, int) else 0
    observation.ref_count = max(declared, len(refs))

    return observation


def _extract_text_lines(text: str) -> list[str]:
    """从 VOM 文本里抽出"给人看的正文行"。

    会丢掉：``@vom`` / ``@view`` / ``@layers`` 头部标记、``L1 page`` 层声明、
    ``RootWebArea`` 行（标题已单列）、``@eN`` 元素行（refs 已单列）。

    还会做相邻去重：实测 ``paragraph "X"`` 下面紧跟的
    ``StaticText "X"`` 内容一字不差，不去重的话摘要凭空大一倍。
    """
    if not isinstance(text, str) or not text:
        return []

    out: list[str] = []
    previous = ""
    for line in text[:MAX_TEXT_CHARS].splitlines()[:MAX_LINES]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("@vom", "@view", "@layers")):
            continue
        if _RE_LAYER.match(stripped):
            continue
        if stripped.startswith("RootWebArea"):
            continue
        if _RE_ELEMENT_PREFIX.match(stripped):
            continue

        content = _finalize(_first_quoted_or_raw(stripped), MAX_LINE_CHARS)
        if not content or content == previous:
            continue
        out.append(content)
        previous = content
    return out


def summarize(
    text: str,
    title: str,
    refs: list[PageRef],
    *,
    max_chars: int = 3000,
) -> str:
    """生成给 LLM 看的紧凑页面摘要。

    Args:
        text: VOM 文本原文，用于抽取可见正文。
        title: 页面标题，来自 :func:`parse_vom_text`。
        refs: 可交互元素列表，来自 :func:`parse_vom_text`。
        max_chars: 返回字符串的最大字符数（不是字节数）。默认 3000 字符，
            对这个规模的中文文本约合 2000 左右 token，塞进上下文是安全的。

    Returns:
        摘要文本。长度保证不超过 ``max_chars`` —— 这一点是硬保证，
        因为整棵 VOM 树可能有几万字符，直接塞给 LLM 会撑爆上下文。
        一旦发生截断，末尾一定带 :data:`TRUNCATION_NOTE` 提示
        （``max_chars`` 小到连提示都放不下时例外，此时只做硬截断）。

    Note:
        本函数是纯函数，不改动传入的 ``refs``，也不依赖任何全局状态。
    """
    # --- 参数归一化：外部传进来的东西可能是任何类型 ---
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 3000
    if limit <= 0:
        return ""

    if not isinstance(text, str):
        text = ""
    if title is None:
        title = ""
    elif not isinstance(title, str):
        title = str(title)
    title = _sanitize(title).strip()

    if not isinstance(refs, (list, tuple)):
        refs = []
    safe_refs = [r for r in refs if isinstance(r, PageRef)]

    # --- 头部：标题 / 视口 / 元素清单 ---
    header_lines: list[str] = []
    if title:
        header_lines.append(f"标题：{title}")

    mv = _RE_VIEW.search(text) if text else None
    if mv:
        header_lines.append(f"视口：{mv.group(1)}x{mv.group(2)}（CSS px）")

    if safe_refs:
        header_lines.append(f"可交互元素（共 {len(safe_refs)} 个，用 @ref 引用）：")
        for ref in safe_refs[:MAX_REFS_SHOWN]:
            role = ref.role or "未知角色"
            target = f"  → {ref.target}" if ref.target else ""
            header_lines.append(f'  @{ref.ref} {role} "{ref.name}"{target}')
        hidden = len(safe_refs) - MAX_REFS_SHOWN
        if hidden > 0:
            header_lines.append(f"  ……还有 {hidden} 个元素未列出")
    else:
        header_lines.append("没有发现可交互元素（页面上没有可点击或可输入的东西）。")

    header = "\n".join(header_lines)

    # --- 正文：按行填充，塞不下就停 ---
    # 预算里先给截断提示留出位置，否则提示本身会把总长顶出上限。
    budget = limit - len(header) - len(TRUNCATION_NOTE) - 2
    body: list[str] = []
    used = 0
    truncated = False
    for line in _extract_text_lines(text):
        cost = len(line) + 1  # +1 是行尾换行
        if used + cost > budget:
            truncated = True
            break
        body.append(line)
        used += cost

    result = header
    if body:
        result += "\n\n" + "\n".join(body)
    if truncated:
        result += "\n" + TRUNCATION_NOTE

    # 最后一道保险：无论如何都不许超过 max_chars
    if len(result) > limit:
        result = result[:limit]
    return result
