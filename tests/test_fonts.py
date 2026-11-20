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
from imgtrans.render.fonts import FontBook, font_dirs_from_env


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


if __name__ == "__main__":
    unittest.main()
