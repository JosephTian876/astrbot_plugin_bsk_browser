"""``bsk/pages.py`` 的单元测试。

全部是**纯函数测试**：不需要 AstrBot、不需要浏览器、不需要装 bsk，
甚至不需要联网。直接用实测抓到的真实 VOM 文本当输入。

覆盖的重点（对应踩过的坑）：

- 真实 example.com 的 VOM 能解析出标题 / 元素 / 视口；
- 各种畸形输入（空串、只有头部、缺 ``@view``、tab 缩进、名字含引号、
  没有 target、孤立代理项……）**一律不抛异常**；
- 多语言（中/阿/俄/法/西）文本不丢失、不乱码；
- ``summarize`` 的长度上限是硬保证，截断时必须带提示。

用 ``unittest`` 而不是 ``pytest``：AstrBot 自带的 Python 3.12 环境里没有
装 pytest（实测 ``ModuleNotFoundError``），而框架版本必须能直接跑这些测试。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 让测试能 import 到项目的 bsk 包（与 tests/test_runner.py 保持一致的写法）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bsk.models import PageRef  # noqa: E402
from bsk.pages import (  # noqa: E402
    MAX_REFS_SHOWN,
    TRUNCATION_NOTE,
    parse_observation,
    parse_vom_text,
    summarize,
)

# ---------------------------------------------------------------------------
# 真实样例：直接来自 D:\...\astrbot-browserskill\research\_e2e_summary.json
# 的 points.observe.json.text（2129 字符，逐字节抄录，未做任何删改）
# ---------------------------------------------------------------------------

REAL_VOM_TEXT = (
    "@vom 1\n"
    "@view 910x604\n"
    "@layers 1 focus=L1\n"
    "L1 page\n"
    '  RootWebArea "Example Domain"\n'
    '    paragraph "该域名仅用于文档示例，无需获得许可。这并非一项服务，请勿将其用于测试和监控目的。"\n'
    '      StaticText "该域名仅用于文档示例，无需获得许可。这并非一项服务，请勿将其用于测试和监控目的。"\n'
    '    paragraph "This domain is for use in documentation examples without needing permission. '
    'This is not a service; avoid relying on it for testing and monitoring purposes."\n'
    '      StaticText "This domain is for use in documentation examples without needing permission. '
    'This is not a service; avoid relying on it for testing and monitoring purposes."\n'
    '    paragraph "هذا النطاق مُخصص للاستخدام في أمثلة التوثيق دون الحاجة إلى إذن. '
    'هذه ليست خدمة، يُرجى تجنب الاعتماد عليها لأغراض الاختبار والمراقبة."\n'
    '      StaticText "هذا النطاق مُخصص للاستخدام في أمثلة التوثيق دون الحاجة إلى إذن. '
    'هذه ليست خدمة، يُرجى تجنب الاعتماد عليها لأغراض الاختبار والمراقبة."\n'
    '    paragraph "L’usage de ce domaine est réservé à des exemples de documentation, '
    'sans autorisation préalable. Il ne s’agit pas d’un service ; son utilisation à des fins '
    'de test ou de surveillance est à éviter."\n'
    '      StaticText "L’usage de ce domaine est réservé à des exemples de documentation, '
    'sans autorisation préalable. Il ne s’agit pas d’un service ; son utilisation à des fins '
    'de test ou de surveillance est à éviter."\n'
    '    paragraph "Данный домен предназначен для использования в примерах документации '
    'без необходимости получения предварительного разрешения. Это не сервис; не рекомендуется '
    'его использование для тестирования и мониторинга."\n'
    '      StaticText "Данный домен предназначен для использования в примерах документации '
    'без необходимости получения предварительного разрешения. Это не сервис; не рекомендуется '
    'его использование для тестирования и мониторинга."\n'
    '    paragraph "Este dominio está destinado al uso en ejemplos de documentación sin '
    'necesidad de permiso. Esto no es un servicio; evitar utilizarlo para realizar pruebas '
    'o monitoreos."\n'
    '      StaticText "Este dominio está destinado al uso en ejemplos de documentación sin '
    'necesidad de permiso. Esto no es un servicio; evitar utilizarlo para realizar pruebas '
    'o monitoreos."\n'
    '    @e1 link "Learn more" [→ iana.org]'
)

REAL_OBSERVE_JSON = {
    "text": REAL_VOM_TEXT,
    "ref_count": 1,
    "tab_id": 1398284761,
    "truncated": False,
}


def vom(*lines: str) -> str:
    """把多行拼成 VOM 文本，省得每行都写 ``\\n``。"""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1) 真实样例
# ---------------------------------------------------------------------------


class TestRealExampleCom(unittest.TestCase):
    """真实抓取的 example.com VOM 必须一字不差地解析出来。"""

    def test_parses_title_refs_viewport(self) -> None:
        title, refs, viewport = parse_vom_text(REAL_VOM_TEXT)

        self.assertEqual(title, "Example Domain")
        self.assertEqual(viewport, (910, 604))

        self.assertEqual(len(refs), 1, "这个页面只有 1 个可交互元素")
        ref = refs[0]
        # ref 存进 PageRef 时必须**不带 @**
        self.assertEqual(ref.ref, "e1")
        self.assertEqual(ref.role, "link")
        self.assertEqual(ref.name, "Learn more")
        self.assertEqual(ref.target, "iana.org")

    def test_parse_observation_full(self) -> None:
        obs = parse_observation(REAL_OBSERVE_JSON)

        self.assertEqual(obs.title, "Example Domain")
        self.assertEqual(obs.viewport, (910, 604))
        self.assertEqual(obs.ref_count, 1)
        self.assertEqual(obs.tab_id, 1398284761)
        self.assertFalse(obs.truncated)
        self.assertEqual([r.ref for r in obs.refs], ["e1"])
        self.assertIs(obs.text, REAL_VOM_TEXT)

    def test_target_arrow_is_stripped(self) -> None:
        """`[→ iana.org]` 里的箭头必须剥掉，只留域名。"""
        _, refs, _ = parse_vom_text('@vom 1\n  @e1 link "X" [→ iana.org]')
        self.assertEqual(refs[0].target, "iana.org")

    def test_viewport_is_css_px_not_screenshot_px(self) -> None:
        """视口是 CSS px。截图同刻是 1850x1208，两者差 DPR 倍，不能混用。"""
        _, _, viewport = parse_vom_text(REAL_VOM_TEXT)
        self.assertEqual(viewport, (910, 604))
        self.assertNotEqual(viewport, (1850, 1208))


# ---------------------------------------------------------------------------
# 2) 畸形输入：一个都不许抛异常
# ---------------------------------------------------------------------------


class TestMalformedInputNeverRaises(unittest.TestCase):
    """输入是外部数据，可能畸形。所有降级路径都要走通。"""

    def test_empty_string(self) -> None:
        self.assertEqual(parse_vom_text(""), ("", [], None))

    def test_only_header_vom(self) -> None:
        title, refs, viewport = parse_vom_text("@vom 1")
        self.assertEqual(title, "")
        self.assertEqual(refs, [])
        self.assertIsNone(viewport)

    def test_headers_without_content(self) -> None:
        text = vom("@vom 1", "@view 800x600", "@layers 1 focus=L1", "L1 page")
        title, refs, viewport = parse_vom_text(text)
        self.assertEqual(title, "")
        self.assertEqual(refs, [])
        # @view 有，所以视口应该能拿到
        self.assertEqual(viewport, (800, 600))

    def test_missing_view(self) -> None:
        text = vom("@vom 1", "@layers 1 focus=L1", "L1 page", '  RootWebArea "无视口"')
        title, refs, viewport = parse_vom_text(text)
        self.assertEqual(title, "无视口")
        self.assertEqual(refs, [])
        self.assertIsNone(viewport)

    def test_view_with_zero_size_degrades_to_none(self) -> None:
        """0 尺寸的视口没有意义，必须给 None，否则下游算 DPR 会除零。"""
        _, _, viewport = parse_vom_text("@view 0x0")
        self.assertIsNone(viewport)

    def test_view_with_garbage_numbers(self) -> None:
        _, _, viewport = parse_vom_text("@view axb")
        self.assertIsNone(viewport)

    def test_non_string_input(self) -> None:
        """类型传错也不能炸。"""
        for bad in (None, 123, [], {}, b"@vom 1"):
            with self.subTest(bad=bad):
                self.assertEqual(parse_vom_text(bad), ("", [], None))  # type: ignore[arg-type]

    def test_parse_observation_non_dict(self) -> None:
        for bad in (None, [], "x", 42):
            with self.subTest(bad=bad):
                obs = parse_observation(bad)  # type: ignore[arg-type]
                self.assertEqual(obs.text, "")
                self.assertEqual(obs.refs, [])
                self.assertIsNone(obs.viewport)
                self.assertEqual(obs.ref_count, 0)

    def test_element_name_containing_quotes(self) -> None:
        """名字里含裸引号：取最外层引号之间的全部内容。"""
        text = '@vom 1\n  @e7 button "Say "hi" now"'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].ref, "e7")
        self.assertEqual(refs[0].role, "button")
        self.assertEqual(refs[0].name, 'Say "hi" now')

    def test_element_name_with_escaped_quotes(self) -> None:
        """名字里含转义引号：按 JSON 规则反转义。"""
        text = '@vom 1\n  @e2 link "a \\"b\\" c" [→ example.org]'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(refs[0].name, 'a "b" c')
        self.assertEqual(refs[0].target, "example.org")

    def test_element_without_target(self) -> None:
        text = '@vom 1\n  @e3 textbox "搜索"'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].name, "搜索")
        self.assertEqual(refs[0].target, "")

    def test_element_without_role(self) -> None:
        """role 可能缺失（非 a11y 节点），仍然要能拿到 ref 和名字。"""
        text = '@vom 1\n  @e4 "只有名字"'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].ref, "e4")
        self.assertEqual(refs[0].role, "")
        self.assertEqual(refs[0].name, "只有名字")

    def test_unclosed_quote_does_not_raise(self) -> None:
        text = '@vom 1\n  @e5 link "没闭合的名字'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].ref, "e5")
        self.assertIn("没闭合", refs[0].name)

    def test_tab_indentation(self) -> None:
        """缩进可能是 tab 而不是空格，解析必须一致。"""
        text = vom(
            "@vom 1",
            "@view 1024x768",
            "@layers 1 focus=L1",
            "L1 page",
            '\tRootWebArea "Tab 缩进页面"',
            '\t\t@e1 link "A" [→ a.example]',
            '\t\t@e2 button "B"',
        )
        title, refs, viewport = parse_vom_text(text)
        self.assertEqual(title, "Tab 缩进页面")
        self.assertEqual(viewport, (1024, 768))
        self.assertEqual([r.ref for r in refs], ["e1", "e2"])

    def test_mixed_tabs_and_spaces(self) -> None:
        text = vom(
            "@vom 1",
            ' \t  RootWebArea "混排"',
            "\t  @e1 link \"X\" [→ x.example]",
        )
        title, refs, _ = parse_vom_text(text)
        self.assertEqual(title, "混排")
        self.assertEqual(refs[0].target, "x.example")

    def test_crlf_line_endings(self) -> None:
        """Windows 上可能出现 CRLF，splitlines 必须吃掉 \\r。"""
        text = REAL_VOM_TEXT.replace("\n", "\r\n")
        title, refs, viewport = parse_vom_text(text)
        self.assertEqual(title, "Example Domain")
        self.assertEqual([r.ref for r in refs], ["e1"])
        self.assertEqual(viewport, (910, 604))

    def test_surrogate_residue_is_replaced_not_crashing(self) -> None:
        """非法 UTF-8 残留可能变成孤立代理项，打印时会炸 —— 必须替换掉。"""
        text = '@vom 1\n  RootWebArea "坏\ud800字符"\n  @e1 link "名\udfff字"'
        title, refs, _ = parse_vom_text(text)
        self.assertIn("坏", title)
        self.assertIn("字符", title)
        # 替换后必须能被正常编码输出
        title.encode("utf-8")
        refs[0].name.encode("utf-8")
        self.assertNotIn("\ud800", title)

    def test_root_webarea_without_quotes(self) -> None:
        """引号丢失的退化形态：不该炸，也不该把整行当标题。"""
        title, refs, _ = parse_vom_text("@vom 1\n  RootWebArea")
        self.assertEqual(title, "")
        self.assertEqual(refs, [])

    def test_multiple_root_webarea_takes_first(self) -> None:
        text = vom('  RootWebArea "第一个"', '  RootWebArea "第二个"')
        title, _, _ = parse_vom_text(text)
        self.assertEqual(title, "第一个")

    def test_root_webarea_word_inside_body_is_not_title(self) -> None:
        """正文里恰好出现 RootWebArea 字样时，不能抢在真标题前面被当成标题。

        所以标题只认**行首**的 RootWebArea，不做全文子串搜索。
        """
        text = vom(
            "@vom 1",
            '  paragraph "这是讲解 RootWebArea 用法的正文"',
            '  RootWebArea "真标题"',
        )
        title, _, _ = parse_vom_text(text)
        self.assertEqual(title, "真标题")

    def test_mention_of_ref_inside_body_is_not_an_element(self) -> None:
        """正文里提到 @e1 不能被误判成元素行（元素必须出现在行首）。"""
        text = vom(
            "@vom 1",
            '  paragraph "请点击 @e1 这个链接"',
            '  @e1 link "真链接"',
        )
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].name, "真链接")

    def test_duplicate_ref_keeps_first(self) -> None:
        text = vom('@e1 link "第一次"', '@e1 link "第二次"')
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].name, "第一次")

    def test_headers_are_not_parsed_as_elements(self) -> None:
        """@vom / @view / @layers 都不能被当成 @eN 元素。"""
        text = vom("@vom 1", "@view 800x600", "@layers 1 focus=L1")
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(refs, [])

    def test_very_long_single_line_does_not_explode(self) -> None:
        """超长文本必须能解析完，且不把内存吃光。"""
        huge = "@vom 1\n  @e1 link \"" + ("长" * 200_000) + '"'
        title, refs, _ = parse_vom_text(huge)
        self.assertEqual(title, "")
        self.assertEqual(len(refs), 1)
        # 名字被限长，不会把 20 万字符原样带出去
        self.assertLess(len(refs[0].name), 1000)


# ---------------------------------------------------------------------------
# 3) 多元素场景
# ---------------------------------------------------------------------------


class TestManyElements(unittest.TestCase):
    """5+ 个元素必须全部解析出来，且顺序与原文一致。"""

    MULTI = vom(
        "@vom 1",
        "@view 1440x900",
        "@layers 1 focus=L1",
        "L1 page",
        '  RootWebArea "搜索页面"',
        '    @e1 textbox "搜索关键词"',
        '    @e2 button "搜索"',
        '    @e3 link "首页" [→ home.example.com]',
        '    @e4 link "登录" [→ login.example.com]',
        '    @e5 checkbox "记住我"',
        '    @e6 combobox "语言"',
        '    @e7 button "提交"',
    )

    def test_all_seven_refs_in_order(self) -> None:
        title, refs, viewport = parse_vom_text(self.MULTI)

        self.assertEqual(title, "搜索页面")
        self.assertEqual(viewport, (1440, 900))
        self.assertEqual([r.ref for r in refs], ["e1", "e2", "e3", "e4", "e5", "e6", "e7"])
        self.assertEqual(
            [r.role for r in refs],
            ["textbox", "button", "link", "link", "checkbox", "combobox", "button"],
        )
        self.assertEqual([r.name for r in refs][:3], ["搜索关键词", "搜索", "首页"])
        self.assertEqual(refs[2].target, "home.example.com")
        self.assertEqual(refs[0].target, "", "没有 target 的元素应该是空串")

    def test_parse_observation_counts_refs(self) -> None:
        obs = parse_observation({"text": self.MULTI, "ref_count": 7})
        self.assertEqual(obs.ref_count, 7)
        self.assertEqual(len(obs.refs), 7)

    def test_ref_count_falls_back_to_parsed_count(self) -> None:
        """JSON 缺 ref_count 时用实际解析出来的个数兜底。"""
        obs = parse_observation({"text": self.MULTI})
        self.assertEqual(obs.ref_count, 7)

    def test_ref_count_keeps_larger_declared_value(self) -> None:
        """JSON 说有 100 个但 text 被截断只解析出 7 个 → 保留 100。"""
        obs = parse_observation({"text": self.MULTI, "ref_count": 100})
        self.assertEqual(obs.ref_count, 100)
        self.assertEqual(len(obs.refs), 7)


# ---------------------------------------------------------------------------
# 4) 多语言
# ---------------------------------------------------------------------------


class TestMultilingual(unittest.TestCase):
    """中/阿/俄/法/西文本必须原样保留，不丢失不乱码。"""

    def test_title_and_name_keep_all_scripts(self) -> None:
        text = vom(
            '@vom 1',
            '  RootWebArea "中文标题 العربية Русский"',
            '  @e1 link "تحقق من الصحة" [→ example.org]',
            '  @e2 button "Подтвердить"',
            '  @e3 link "Vérifier les données"',
        )
        title, refs, _ = parse_vom_text(text)

        self.assertEqual(title, "中文标题 العربية Русский")
        self.assertEqual(refs[0].name, "تحقق من الصحة")
        self.assertEqual(refs[1].name, "Подтвердить")
        self.assertEqual(refs[2].name, "Vérifier les données")

    def test_arabic_rtl_not_reordered(self) -> None:
        """阿拉伯语是从右往左写的，Python 字符串层面不该被重排。"""
        arabic = "هذا النطاق مُخصص للاستخدام في أمثلة التوثيق"
        text = vom("@vom 1", f'  paragraph "{arabic}"')
        summary = summarize(text, "", [], max_chars=2000)
        self.assertIn(arabic, summary)

    def test_real_sample_multilingual_body_survives(self) -> None:
        """真实例子里 6 种语言都在，摘要里不能乱码。"""
        summary = summarize(REAL_VOM_TEXT, "Example Domain", [PageRef("e1", "link", "Learn more", "iana.org")])

        self.assertIn("该域名仅用于文档示例", summary)
        self.assertIn("This domain is for use in documentation examples", summary)
        self.assertIn("هذا النطاق", summary)
        self.assertIn("Данный домен", summary)
        self.assertIn("L’usage de ce domaine", summary)
        # 不能出现替换字符（说明编码被搞坏了）
        self.assertNotIn("\ufffd", summary)

    def test_emoji_and_astral_plane(self) -> None:
        text = '@vom 1\n  @e1 button "提交 🚀🎉"'
        _, refs, _ = parse_vom_text(text)
        self.assertEqual(refs[0].name, "提交 🚀🎉")


# ---------------------------------------------------------------------------
# 5) summarize 的长度控制
# ---------------------------------------------------------------------------


class TestSummarize(unittest.TestCase):

    REFS = [
        PageRef("e1", "link", "首页", "home.example.com"),
        PageRef("e2", "button", "搜索"),
        PageRef("e3", "textbox", "关键词"),
    ]

    def test_never_exceeds_max_chars(self) -> None:
        """硬保证：无论输入多大，输出都不能超过 max_chars。"""
        huge = vom("@vom 1", "@view 800x600") + "\n" + "\n".join(
            f'  paragraph "第{i}段内容，需要足够长才能把摘要顶爆。"' for i in range(5000)
        )
        for limit in (200, 500, 1000, 3000, 10000):
            with self.subTest(limit=limit):
                out = summarize(huge, "超长页面", self.REFS, max_chars=limit)
                self.assertLessEqual(len(out), limit, f"max_chars={limit} 时超长了")

    def test_truncation_adds_notice(self) -> None:
        huge = vom("@vom 1") + "\n" + "\n".join(
            f'  paragraph "第{i}段的正文内容在这里占位置。"' for i in range(2000)
        )
        out = summarize(huge, "长页面", [], max_chars=500)
        self.assertIn(TRUNCATION_NOTE, out)
        self.assertIn("已截断", out)

    def test_short_content_is_not_marked_truncated(self) -> None:
        text = vom("@vom 1", '  RootWebArea "短"', '  paragraph "很短的一段话。"')
        out = summarize(text, "短", [], max_chars=3000)
        self.assertNotIn(TRUNCATION_NOTE, out)

    def test_empty_input_is_safe(self) -> None:
        out = summarize("", "", [], max_chars=3000)
        self.assertIsInstance(out, str)
        self.assertNotIn("\ufffd", out)

    def test_max_chars_zero_or_negative_returns_empty(self) -> None:
        self.assertEqual(summarize(REAL_VOM_TEXT, "T", [], max_chars=0), "")
        self.assertEqual(summarize(REAL_VOM_TEXT, "T", [], max_chars=-5), "")

    def test_max_chars_tiny_still_bounded(self) -> None:
        """极端小的上限也不能超长（此时放不下截断提示，只做硬截断）。"""
        out = summarize(REAL_VOM_TEXT, "Example Domain", self.REFS, max_chars=10)
        self.assertLessEqual(len(out), 10)

    def test_invalid_max_chars_falls_back_to_default(self) -> None:
        out = summarize("", "标题", [], max_chars="不是数字")  # type: ignore[arg-type]
        self.assertIsInstance(out, str)

    def test_duplicate_paragraph_statictext_collapsed(self) -> None:
        """paragraph 与紧跟的 StaticText 内容相同，摘要里只留一份。"""
        text = vom(
            "@vom 1",
            '  paragraph "重复的正文"',
            '    StaticText "重复的正文"',
        )
        out = summarize(text, "", [], max_chars=3000)
        self.assertEqual(out.count("重复的正文"), 1)

    def test_summary_lists_refs_with_at_prefix(self) -> None:
        out = summarize(REAL_VOM_TEXT, "Example Domain", self.REFS, max_chars=3000)
        self.assertIn("@e1", out)
        self.assertIn("首页", out)
        self.assertIn("home.example.com", out)

    def test_refs_list_is_capped(self) -> None:
        """元素太多时摘要要收敛，不能无限列。"""
        many = [PageRef(f"e{i}", "link", f"第{i}个") for i in range(1, 121)]
        out = summarize(REAL_VOM_TEXT, "很多元素", many, max_chars=8000)
        self.assertIn(f"还有 {120 - MAX_REFS_SHOWN} 个元素未列出", out)

    def test_no_refs_message(self) -> None:
        out = summarize(REAL_VOM_TEXT, "Example Domain", [], max_chars=3000)
        self.assertIn("没有发现可交互元素", out)

    def test_header_marks_viewport_as_css_px(self) -> None:
        """摘要里必须写明 CSS px，免得模型拿它当截图像素用。"""
        out = summarize(REAL_VOM_TEXT, "Example Domain", [], max_chars=3000)
        self.assertIn("910x604", out)
        self.assertIn("CSS px", out)

    def test_does_not_mutate_refs(self) -> None:
        refs = list(self.REFS)
        summarize(REAL_VOM_TEXT, "T", refs, max_chars=100)
        self.assertEqual(refs, self.REFS)

    def test_bad_inputs_do_not_raise(self) -> None:
        for bad_refs in (None, "x", 42, [None, "y", 1]):
            with self.subTest(refs=bad_refs):
                out = summarize(REAL_VOM_TEXT, None, bad_refs, max_chars=500)  # type: ignore[arg-type]
                self.assertIsInstance(out, str)

    def test_non_string_text_does_not_raise(self) -> None:
        out = summarize(None, "标题", [], max_chars=500)  # type: ignore[arg-type]
        self.assertIn("标题", out)

    def test_output_has_no_lone_surrogates(self) -> None:
        """核心不变量：输出里不能有孤立代理项。

        这类字符（``\\ud800``）是非法 UTF-8 解码的残留，一旦留在字符串里，
        ``print()`` / ``.encode()`` 就会抛 ``UnicodeEncodeError``，把整个
        工具调用打挂（见 ARCHITECTURE.md 的 C9）。

        Note:
            这里**不能**断言"能被 gbk 编码" —— 真实页面正文里就有阿拉伯文、
            俄文，它们本来就编不进 gbk。要保证的是"没有代理项"，
            也就是 ``encode("utf-8")`` 一定成功。
        """
        text = '@vom 1\n  paragraph "坏\ud800字"'
        out = summarize(text, "坏\udfff标题", [], max_chars=1000)

        # 不能抛异常
        out.encode("utf-8")
        self.assertNotIn("\ud800", out)
        self.assertNotIn("\udfff", out)
        self.assertIn("标题", out)


# ---------------------------------------------------------------------------
# 6) 正文抽取（通过 summarize 间接验证）
# ---------------------------------------------------------------------------


class TestBodyExtraction(unittest.TestCase):

    def test_markers_are_removed_from_body(self) -> None:
        text = vom(
            "@vom 1",
            "@view 800x600",
            "@layers 2 focus=L2",
            "L1 page",
            '  RootWebArea "标题"',
            '    paragraph "正文一"',
            '    @e1 link "按钮文字"',
        )
        out = summarize(text, "标题", [PageRef("e1", "link", "按钮文字")], max_chars=3000)
        self.assertNotIn("@vom", out)
        self.assertNotIn("@layers", out)
        self.assertNotIn("L1 page", out)
        # 标题行只在头部出现一次
        self.assertEqual(out.count("RootWebArea"), 0)
        self.assertIn("正文一", out)

    def test_multi_layer_lines_are_dropped(self) -> None:
        text = vom("L1 page", "L2 dialog", '  paragraph "正文"')
        out = summarize(text, "", [], max_chars=3000)
        self.assertNotIn("L1 page", out)
        self.assertNotIn("L2 dialog", out)
        self.assertIn("正文", out)

    def test_unquoted_body_lines_are_kept(self) -> None:
        text = vom("@vom 1", "  generic 裸文本内容")
        out = summarize(text, "", [], max_chars=3000)
        self.assertIn("裸文本内容", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
