"""字体发现层的跨平台测试。

这一层是"能不能部署到 Linux"的关键：Windows 的字体目录是**扁平**的
（``C:\\Windows\\Fonts\\msyh.ttc``），而 Linux 发行版是**两层嵌套**的
（``/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf``）。

早期实现只扫一层，在 Linux 上索引为 0，``find()`` 返回 ``None``，回填阶段
全部退化成 Pillow 内置位图字体——图能生成，但文字全是 10px 粗糙字形。
这个测试用临时目录模拟 Linux 的真实布局，钉住"必须递归"这条约束。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import imgtrans.render.fonts as fonts
from imgtrans.render.fonts import FontBook, family_for_lang, font_dirs_from_env


class _TempFontTree:
    """临时字体树辅助：按相对路径造空字体文件（``find`` 不读内容）。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="imgtrans_fonts_"))

    def touch(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    def book(self, extra: list[str] | None = None) -> FontBook:
        # 索引是惰性构建的，必须在 patch 生效期间就建好；
        # 否则出了 with 才第一次访问 .index，用的还是真实系统目录。
        with mock.patch.object(fonts, "_SYSTEM_FONT_DIRS", (str(self.root),)):
            book = FontBook(extra or [])
            _ = book.index
            return book

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class RecursiveScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = _TempFontTree()
        self.addCleanup(self.tree.cleanup)

    def test_linux_nested_layout_is_indexed(self) -> None:
        # Debian/Ubuntu 的真实布局：字体在两层子目录里
        self.tree.touch("truetype/dejavu/DejaVuSans.ttf")
        self.tree.touch("truetype/liberation/LiberationSans-Regular.ttf")
        self.tree.touch("opentype/noto/NotoSansCJK-Regular.ttc")
        self.tree.touch("truetype/wqy/wqy-zenhei.ttc")

        book = self.tree.book()
        self.assertEqual(len(book.index), 4)
        self.assertEqual(book.find("en").name, "DejaVuSans.ttf")
        self.assertEqual(book.find("zh").name, "NotoSansCJK-Regular.ttc")

    def test_flat_layout_still_works(self) -> None:
        # Windows/macOS 的扁平布局不能被递归改造破坏
        self.tree.touch("msyh.ttc")
        self.tree.touch("arial.ttf")
        book = self.tree.book()
        self.assertEqual(len(book.index), 2)
        self.assertEqual(book.find("zh").name, "msyh.ttc")

    def test_non_font_files_ignored(self) -> None:
        self.tree.touch("truetype/dejavu/DejaVuSans.ttf")
        self.tree.touch("truetype/dejavu/README.md")
        self.tree.touch("truetype/dejavu/fonts.dir")
        self.tree.touch("truetype/dejavu/DejaVuSans-Bold.ttf.bak")
        book = self.tree.book()
        self.assertEqual(list(book.index), ["dejavusans.ttf"])

    def test_max_depth_limits_recursion(self) -> None:
        # 3 层（在深度上限内）应被收录
        self.tree.touch("a/b/c/Ok.ttf")
        # 5 层（超过 _MAX_SCAN_DEPTH=4）应在第 4 层被剪枝
        self.tree.touch("a/b/c/d/e/TooDeep.ttf")
        book = self.tree.book()
        self.assertIn("ok.ttf", book.index)
        self.assertNotIn("toodeep.ttf", book.index)

    def test_extra_dirs_take_precedence(self) -> None:
        # 显式配置的目录优先于系统目录：生产要用自己放的字体覆盖系统版
        system = self.tree.root / "system"
        custom = self.tree.root / "custom"
        for base in (system, custom):
            (base / "sub").mkdir(parents=True)
            (base / "sub" / "arial.ttf").write_bytes(b"")

        with mock.patch.object(fonts, "_SYSTEM_FONT_DIRS", (str(system),)):
            book = FontBook([str(custom)])
            _ = book.index
        self.assertEqual(book.find("en"), custom / "sub" / "arial.ttf")

    def test_empty_tree_returns_none_and_default_font(self) -> None:
        # 一个字体都没有时不能崩：get_font 回退 Pillow 内置位图字体
        book = self.tree.book()
        self.assertIsNone(book.find("en"))
        self.assertIsNotNone(book.get_font("en", 20))


class EnvFontDirsTest(unittest.TestCase):
    """``IMGTRANS_FONT_DIRS`` 的切分必须与平台无关。"""

    def _parse(self, raw: str) -> tuple[str, ...]:
        with mock.patch.dict(os.environ, {fonts.ENV_FONT_DIRS: raw}):
            return font_dirs_from_env()

    def test_unset_or_blank(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fonts.ENV_FONT_DIRS, None)
            self.assertEqual(font_dirs_from_env(), ())
        self.assertEqual(self._parse("   "), ())

    def test_colon_separated_linux_style(self) -> None:
        self.assertEqual(
            self._parse("/opt/fonts:/data/fonts"), ("/opt/fonts", "/data/fonts")
        )

    def test_semicolon_separated_also_accepted(self) -> None:
        # 在 Linux 上照抄 Windows 写法也要能跑，否则是纯配置陷阱
        self.assertEqual(
            self._parse("/opt/fonts;/data/fonts"), ("/opt/fonts", "/data/fonts")
        )

    def test_windows_drive_letters_not_split(self) -> None:
        self.assertEqual(self._parse(r"C:\fonts"), (r"C:\fonts",))
        self.assertEqual(
            self._parse(r"C:\fonts;D:\fonts2"), (r"C:\fonts", r"D:\fonts2")
        )
        # 混用分隔符时盘符仍不能被切断
        self.assertEqual(self._parse(r"C:\a:D:\b"), (r"C:\a", r"D:\b"))

    def test_blank_entries_dropped(self) -> None:
        self.assertEqual(self._parse("/a::/b"), ("/a", "/b"))
        self.assertEqual(self._parse(" /a ; ; /b "), ("/a", "/b"))


class FallbackLoggingTest(unittest.TestCase):
    """回退告警的分级：豆腐块 vs 字形不理想，不能混为一谈。"""

    def setUp(self) -> None:
        self.tree = _TempFontTree()
        self.addCleanup(self.tree.cleanup)

    def _levels(self, lang: str) -> list[int]:
        book = self.tree.book()
        with self.assertLogs("imgtrans.render.fonts", level="DEBUG") as captured:
            book.find(lang)
        return [r.levelno for r in captured.records]

    def test_ja_falling_back_to_cjk_is_not_an_error(self) -> None:
        # CJK 字体含假名，这是正常回退；报 error 会让真正的缺字体问题失焦
        self.tree.touch("opentype/noto/NotoSansCJK-Regular.ttc")
        self.assertFalse(any(lv >= logging.ERROR for lv in self._levels("ja")))

    def test_zh_falling_back_to_latin_is_an_error(self) -> None:
        # 只有 DejaVu，中文必然画成豆腐块，必须是 error
        self.tree.touch("truetype/dejavu/DejaVuSans.ttf")
        self.assertTrue(any(lv >= logging.ERROR for lv in self._levels("zh")))

    def test_thai_falling_back_to_latin_is_an_error(self) -> None:
        # DejaVu 没有泰文字形
        self.tree.touch("truetype/dejavu/DejaVuSans.ttf")
        self.assertTrue(any(lv >= logging.ERROR for lv in self._levels("th")))

    def test_georgian_fallback_only_warns(self) -> None:
        # DejaVu 带格鲁吉亚字形，只是"不理想"，不该升级成 error
        self.tree.touch("truetype/liberation/LiberationSans-Regular.ttf")
        levels = self._levels("ka")
        self.assertTrue(any(lv >= logging.WARNING for lv in levels))
        self.assertFalse(any(lv >= logging.ERROR for lv in levels))


class SystemFontDirsTest(unittest.TestCase):
    def test_platform_defaults(self) -> None:
        dirs = fonts._SYSTEM_FONT_DIRS
        if sys.platform.startswith("win"):
            self.assertTrue(any("Windows" in d for d in dirs))
        elif sys.platform == "darwin":
            self.assertIn("/System/Library/Fonts", dirs)
        else:
            # 发行版字体根目录 + freedesktop 用户目录，两者缺一都可能在
            # 某些镜像上扫不到字体
            self.assertIn("/usr/share/fonts", dirs)
            self.assertTrue(any(".local/share/fonts" in d for d in dirs))

    def test_no_foreign_platform_paths(self) -> None:
        # Linux 上不该出现 C:\Windows，Windows 上也不该出现 /usr/share/fonts
        dirs = fonts._SYSTEM_FONT_DIRS
        if not sys.platform.startswith("win"):
            self.assertFalse(any(d.startswith("C:") for d in dirs))


def _first_font_covering(sample: str) -> Path | None:
    """在本机真实字体里找一个能画出 ``sample`` 的字体文件（没有则跳过用例）。

    用字体自己的 cmap 判断，而不是写死文件名——CI 上装的是 Noto，开发机是
    Sylfaen，判据必须落在"有没有这个字形"上。
    """
    book = FontBook()
    for path in dict.fromkeys(book.index.values()):
        cps = book.codepoints(path)
        if cps and all(ord(ch) in cps for ch in sample):
            return path
    return None


def _first_font_lacking(sample: str) -> Path | None:
    """在本机找一个**画不出** ``sample`` 的字体（用来构造"缺字形"场景）。"""
    book = FontBook()
    for path in dict.fromkeys(book.index.values()):
        cps = book.codepoints(path)
        if cps and any(ord(ch) not in cps for ch in sample):
            return path
    return None


#: 格鲁吉亚文 "გამარჯობა"：中文/拉丁字体普遍没有这套字形，正好当探针。
GEO = "გამარჯობა"


class CmapCoverageTest(unittest.TestCase):
    """字形覆盖必须真的从字体里读出来，不能靠文件名猜。"""

    def test_reader_rejects_non_font_bytes(self) -> None:
        self.assertIsNone(fonts._cmap_codepoints(b"not a font at all"))
        self.assertIsNone(fonts._cmap_codepoints(b""))

    def test_reader_returns_real_codepoints(self) -> None:
        path = _first_font_covering("abc")
        if path is None:
            self.skipTest("本机没有可解析的字体")
        cps = fonts._cmap_codepoints(path.read_bytes())
        self.assertIsNotNone(cps)
        assert cps is not None
        for ch in "abc":
            self.assertIn(ord(ch), cps, f"{path.name} 缺 {ch!r}")
        # 中文字体两万多个码位，能读出来就不会只有几十个
        self.assertGreater(len(cps), 50)

    def test_ttc_face_is_read(self) -> None:
        """ttc 要按 face 偏移解析，不能把整个容器当裸 sfnt。"""
        book = FontBook()
        ttcs = [p for p in book.index.values() if p.suffix.lower() in (".ttc", ".otc")]
        if not ttcs:
            self.skipTest("本机没有 .ttc 字体")
        cps = book.codepoints(ttcs[0])
        self.assertIsNotNone(cps)
        assert cps is not None
        self.assertGreater(len(cps), 50)


class ResolveFamilyTest(unittest.TestCase):
    """按**实际文字**选族：语言码只是猜测，覆盖才是事实。"""

    def _book_with(self, font: Path, name: str) -> FontBook:
        root = Path(tempfile.mkdtemp(prefix="imgtrans_one_font_"))
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copy(font, root / name)
        with mock.patch.object(fonts, "_SYSTEM_FONT_DIRS", (str(root),)):
            book = FontBook()
            _ = book.index
            return book

    def test_keeps_own_family_when_covered(self) -> None:
        geo = _first_font_covering(GEO)
        if geo is None:
            self.skipTest("本机没有能画格鲁吉亚文的字体")
        book = self._book_with(geo, "sylfaen.ttf")
        self.assertEqual(book.resolve_family("ka", GEO), "georgian")
        self.assertEqual(book.missing_chars("ka", GEO), "")

    def test_switches_to_family_that_can_draw_it(self) -> None:
        """本族缺失、但别的族能画 -> 换族，而不是照旧画方框。

        构造：把能画格鲁吉亚文的字体只命名成 latin 族的候选文件名。
        """
        geo = _first_font_covering(GEO)
        if geo is None:
            self.skipTest("本机没有能画格鲁吉亚文的字体")
        book = self._book_with(geo, "arial.ttf")
        self.assertEqual(book.resolve_family("ka", GEO), "latin")
        self.assertEqual(book.missing_chars("latin", GEO), "")

    def test_reports_missing_when_nothing_covers(self) -> None:
        """谁都画不出来时必须说清楚缺哪些字（这正是用户看到方框的场景）。"""
        cjk = _first_font_lacking(GEO)
        if cjk is None:
            self.skipTest("本机没有缺格鲁吉亚字形的字体")
        book = self._book_with(cjk, cjk.name)
        with self.assertLogs("imgtrans.render.fonts", level="ERROR") as logs:
            book.resolve_family("ka", GEO)
        joined = "\n".join(logs.output)
        self.assertIn("没有任何字体覆盖", joined)
        missing = book.missing_chars("ka", GEO)
        self.assertTrue(missing, "缺字形时 missing_chars 不能返回空")
        self.assertTrue(set(missing) & set(GEO))

    def test_check_langs_flags_undrawable_language(self) -> None:
        cjk = _first_font_lacking(GEO)
        if cjk is None:
            self.skipTest("本机没有缺格鲁吉亚字形的字体")
        book = self._book_with(cjk, cjk.name)
        with self.assertLogs("imgtrans.render.fonts", level="ERROR"):
            problems = book.check_langs(["ka"])
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("ka"))
        self.assertIn("没有字体能画", problems[0])

    def test_check_langs_quiet_when_everything_covered(self) -> None:
        geo = _first_font_covering(GEO + "abc")
        if geo is None:
            self.skipTest("本机没有能画格鲁吉亚文的字体")
        book = self._book_with(geo, "arial.ttf")
        self.assertEqual(book.check_langs(["en", "ka"]), [])


class FamilyKeyPassthroughTest(unittest.TestCase):
    def test_family_name_is_not_remapped(self) -> None:
        """``resolve_family`` 选出的族名要能直接喂回 ``get_font``。

        否则 ``georgian`` 会被当成未知语言码再映射回 ``cjk``，等于白选。
        """
        for family in fonts.FONT_CANDIDATES:
            self.assertEqual(family_for_lang(family), family)
        # 语言码仍然按原表映射
        self.assertEqual(family_for_lang("ka"), "georgian")
        self.assertEqual(family_for_lang("zh"), "cjk")
        self.assertEqual(family_for_lang(None), "cjk")


class ShapingTest(unittest.TestCase):
    """复杂文字：字形全都在，但排列是错的——比方块更难发现。

    孟加拉文的 ি 要画到辅音**左侧**、ক্ষ 要合成一个连字、阿拉伯文要从右往左。
    这些 FreeType 一概不做，只有 Raqm（HarfBuzz + FriBiDi）会做。缺 Raqm 时
    Pillow 收到 ``layout_engine=RAQM`` **不报错**，只发一条 UserWarning 就退回
    basic layout，所以"检测到了"和"接线了"必须分别钉住。
    """

    def setUp(self) -> None:
        self._orig = fonts._RAQM_AVAILABLE
        self.addCleanup(self._restore_raqm)

    def _restore_raqm(self) -> None:
        fonts._RAQM_AVAILABLE = self._orig

    def test_shaping_languages_are_flagged(self) -> None:
        for code in ("bn", "hi", "th", "ar", "he"):
            self.assertTrue(fonts.needs_shaping(code), f"{code} 需要整形")
        for code in ("en", "zh", "ja", "ko", "ka", "ru", "vi"):
            self.assertFalse(fonts.needs_shaping(code), f"{code} 不需要整形")

    def _spy_truetype(self, lang: str) -> dict:
        """取 ``get_font`` 传给 ``ImageFont.truetype`` 的关键字参数。"""
        book = FontBook()
        if book.find(lang) is None:
            self.skipTest(f"本机没有能画 {lang} 的字体")
        captured: dict = {}
        real = fonts.ImageFont.truetype

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(fonts.ImageFont, "truetype", side_effect=spy):
            book.get_font(lang, 20)
        return captured

    def test_raqm_engine_is_requested_when_available(self) -> None:
        """只提供 has_raqm() 而不接线，等于什么都没修。"""
        import warnings

        from PIL import features as pil_features

        fonts._RAQM_AVAILABLE = True
        # 本机若真的没有 raqm，Pillow 只会发 UserWarning 然后退回 basic——
        # 这条警告本身就是"为什么必须自己 check"的证据，顺手断言掉，
        # 免得它混进测试输出被当成噪音忽略。
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            kwargs = self._spy_truetype("bn")
        self.assertEqual(kwargs.get("layout_engine"), fonts.ImageFont.Layout.RAQM)
        if not pil_features.check("raqm"):
            self.assertTrue(
                any("Raqm layout was requested" in str(w.message) for w in caught),
                [str(w.message) for w in caught],
            )

    def test_latin_keeps_basic_layout(self) -> None:
        """拉丁/中日韩不启用 RAQM：kerning 会让已调好的回填宽度发生偏移。"""
        fonts._RAQM_AVAILABLE = True
        kwargs = self._spy_truetype("en")
        self.assertNotIn("layout_engine", kwargs)

    def test_missing_raqm_warns_for_shaping_language(self) -> None:
        """没有 Raqm 时必须留痕，不能安静地画出一段排错的文字。"""
        fonts._RAQM_AVAILABLE = False
        book = FontBook()
        if book.find("bn") is None:
            self.skipTest("本机没有能画 bn 的字体")
        with self.assertLogs("imgtrans.render.fonts", level="WARNING") as logs:
            book.get_font("bn", 20)
        self.assertTrue(
            any("整形" in line or "Raqm" in line for line in logs.output),
            logs.output,
        )


class ShapedForPaintTest(unittest.TestCase):
    """RTL 整形只能走一条路，走两条会翻转两次。"""

    def setUp(self) -> None:
        self._orig = fonts._RAQM_AVAILABLE
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        fonts._RAQM_AVAILABLE = self._orig

    def test_raqm_gets_raw_logical_order(self) -> None:
        from imgtrans.render import text as T

        fonts._RAQM_AVAILABLE = True
        arabic = "مرحبا"
        with mock.patch.object(T, "shape_rtl_text", side_effect=AssertionError("不该手工翻转")):
            self.assertEqual(T.shaped_for_paint(arabic, True), arabic)

    def test_without_raqm_falls_back_to_manual_reorder(self) -> None:
        from imgtrans.render import text as T

        fonts._RAQM_AVAILABLE = False
        arabic = "مرحبا"
        with mock.patch.object(T, "shape_rtl_text", return_value="REORDERED") as spy:
            self.assertEqual(T.shaped_for_paint(arabic, True), "REORDERED")
        spy.assert_called_once_with(arabic)

    def test_ltr_text_is_untouched(self) -> None:
        from imgtrans.render import text as T

        fonts._RAQM_AVAILABLE = False
        self.assertEqual(T.shaped_for_paint("hello", False), "hello")


if __name__ == "__main__":
    unittest.main()
