"""排版与回填的测试。

重点验证两件事：

1. **字号自适应**：译文变长时必须缩字号或加行，绝不能横向冲出原框；
2. **超采样真的生效**：回填后文字边缘要有抗锯齿过渡，不能是硬像素块。
"""

from __future__ import annotations

import unittest

import numpy as np

try:
    import cv2  # noqa: F401
    from PIL import Image, ImageDraw
    from PIL import ImageFont

    HAS_DEPS = True
except ImportError:  # pragma: no cover
    HAS_DEPS = False

from imgtrans.types import BlockStyle, Polygon, TextBlock


def _font(size: int):
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _block(text: str, x: int, y: int, w: int, h: int) -> TextBlock:
    poly: Polygon = (
        (float(x), float(y)),
        (float(x + w), float(y)),
        (float(x + w), float(y + h)),
        (float(x), float(y + h)),
    )
    return TextBlock(index=0, polygon=poly, text=text, confidence=0.99)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class ShortLabelTest(unittest.TestCase):
    """短标签必须单行：两个字符棱着摞像排版事故。"""

    def setUp(self) -> None:
        from imgtrans.render.fonts import FontBook

        self.font_book = FontBook()

    def _fit(self, text: str, w: int, h: int):  # noqa: ANN202
        from imgtrans.render.text import fit_block

        return fit_block(text, w, h, self.font_book, "zh", initial_size=h)

    def test_two_char_cjk_label_stays_on_one_line(self) -> None:
        # 25px 宽的框放 "写真"：折行会变成上下两字，缩到 12px 单行才是正解
        layout = self._fit("写真", 25, 24)
        self.assertEqual(len(layout.lines), 1)

    def test_short_latin_word_stays_on_one_line(self) -> None:
        layout = self._fit("Room", 40, 14)
        self.assertEqual(len(layout.lines), 1)

    def test_long_latin_word_is_not_broken(self) -> None:
        """窄栏里的长拉丁词整词成行，允许溢出，绝不拦腰截断。

        "Originator" 被切成 "Originato" + "r" 是肉眼可见的排版事故。
        """
        from PIL import Image, ImageDraw

        from imgtrans.render.text import wrap_text

        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        lines = wrap_text(draw, "Originator of phase change materials", _font(16), 50)
        self.assertIn("Originator", lines)
        for line in lines:
            self.assertFalse(
                any(line.endswith(p) and len(line) > 3 for p in ("Originato",)),
                f"单词被截断: {line!r}",
            )

    def test_long_sentence_still_wraps(self) -> None:
        # 长句不受短标签规则影响，该折行还是要折
        layout = self._fit("当相变导热材料达到相变温度时会发生什么", 120, 40)
        self.assertGreater(len(layout.lines), 1)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class WrapTextTest(unittest.TestCase):
    def setUp(self) -> None:
        from imgtrans.render.fonts import FontBook

        self.book = FontBook()

    def test_cjk_wraps_by_character(self) -> None:
        """中文没有空格，必须能按字符折行。"""
        from imgtrans.render.text import wrap_text

        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        font = self.book.get_font("zh-CN", 20)
        text = "这是一段很长很长的中文文本需要折行显示"

        width, _ = (
            lambda: (
                draw.textbbox((0, 0), text, font=font)[2],
                draw.textbbox((0, 0), text, font=font)[3],
            )
        )()
        lines = wrap_text(draw, text, font, width / 3)

        self.assertGreater(len(lines), 1, "长中文没有折行")
        # 每行都不超宽
        for line in lines:
            w = draw.textbbox((0, 0), line, font=font)[2]
            self.assertLessEqual(w, width / 3 + 1)
        # 内容不丢失（去空白后应等于原文）
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))

    def test_latin_wraps_at_word_boundary(self) -> None:
        """拉丁文本应在空格处断行，不能把单词劈成两半。"""
        from imgtrans.render.text import wrap_text

        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        font = self.book.get_font("en", 16)
        text = "The quick brown fox jumps over the lazy dog"
        full_w = draw.textbbox((0, 0), text, font=font)[2]

        lines = wrap_text(draw, text, font, full_w / 3)
        self.assertGreater(len(lines), 1)

        for line in lines:
            for word in line.split():
                # 每个片段都应是原文里存在的完整单词
                self.assertIn(word, text.split(), f"单词被切断: {word}")

    def test_empty_text_returns_empty(self) -> None:
        from imgtrans.render.text import wrap_text

        draw = ImageDraw.Draw(Image.new("L", (1, 1)))
        font = self.book.get_font("en", 16)
        self.assertEqual(wrap_text(draw, "   ", font, 100), [])


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class FitBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        from imgtrans.render.fonts import FontBook

        self.book = FontBook()

    def test_short_text_uses_large_font(self) -> None:
        from imgtrans.render.text import fit_block

        layout = fit_block("Hi", 200, 40, self.book, "en")
        self.assertGreater(layout.font_size, 20)
        self.assertEqual(len(layout.lines), 1)

    def test_long_text_shrinks_font(self) -> None:
        """同样框里放更长的译文，字号必须变小。"""
        from imgtrans.render.text import fit_block

        short = fit_block("Hi", 200, 36, self.book, "en")
        long = fit_block(
            "This is a much longer sentence that needs more room", 200, 36, self.book, "en"
        )
        self.assertLess(long.font_size, short.font_size)
        self.assertGreaterEqual(len(long.lines), 1)

    def test_text_never_exceeds_box_width(self) -> None:
        """横向永不溢出——溢出会压到画面其他元素上。"""
        from imgtrans.render.text import fit_block

        box_w = 150
        cases = [
            "Hello",
            "这是一句比较长的中文翻译文本",
            "Ein sehr langer deutscher Satz mit vielen Wörtern",
            "Очень длинное предложение на русском языке",
        ]
        for text in cases:
            with self.subTest(text=text[:16]):
                layout = fit_block(text, box_w, 40, self.book, "en")
                self.assertLessEqual(
                    layout.text_width,
                    box_w + 1.5,
                    f"{text[:20]!r} 排出来 {layout.text_width:.0f}px 超过框宽 {box_w}px",
                )

    def test_overflow_flagged_when_impossible(self) -> None:
        """极小框里塞长文本，应标记 overflow 而不是崩溃或静默截断。"""
        from imgtrans.render.text import fit_block

        layout = fit_block(
            "This text cannot possibly fit in a tiny box, really", 40, 12, self.book, "en"
        )
        self.assertEqual(layout.fit_mode, "overflow")
        self.assertGreater(len(layout.lines), 1)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class RenderBlocksTest(unittest.TestCase):
    def _canvas(self, w: int = 300, h: int = 100) -> np.ndarray:
        # 浅灰底，便于观察回填像素
        return np.full((h, w, 3), 240, dtype=np.uint8)

    def _style(self) -> BlockStyle:
        return BlockStyle(fg_color=(0, 0, 0), bg_color=(240, 240, 240))

    def test_paints_pixels_in_box(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas()
        block = _block("Login", 20, 30, 120, 40)
        out, reports = render_blocks(
            canvas,
            [block],
            ["登录"],
            [self._style()],
            FontBook(),
            "zh-CN",
        )

        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].painted)
        # 框内必须有明显变暗的像素（画上了深色文字）
        region = out[30:70, 20:140]
        self.assertLess(int(region.min()), 100, "框内没有画出深色文字")

    def test_no_white_plate_drawn(self) -> None:
        """关键：不能画底板。文字之外的空间必须保持原背景。"""
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas()
        block = _block("A", 20, 30, 200, 40)  # 框很宽，文字很窄
        out, _ = render_blocks(
            canvas, [block], ["B"], [self._style()], FontBook(), "en"
        )

        # 框的右半部分不该被涂成白色/纯色底板——应仍是 240
        right = out[30:70, 160:220]
        self.assertTrue(
            np.all(right > 230),
            f"右侧出现了底板，最小值 {right.min()}",
        )

    def test_identical_translation_is_skipped(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas()
        block = _block("ABC", 20, 30, 100, 40)
        out, reports = render_blocks(
            canvas, [block], ["ABC"], [self._style()], FontBook(), "en"
        )
        self.assertFalse(reports[0].painted)
        np.testing.assert_array_equal(out, canvas)

    def test_unrenderable_block_is_skipped(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas()
        block = _block("ABC", 20, 30, 100, 40)
        style = BlockStyle(fg_color=(0, 0, 0), bg_color=(240, 240, 240), renderable=False)
        out, reports = render_blocks(canvas, [block], ["XYZ"], [style], FontBook(), "en")
        self.assertFalse(reports[0].painted)
        np.testing.assert_array_equal(out, canvas)

    def test_supersampling_produces_antialiasing(self) -> None:
        """超采样生效的证据：文字边缘存在中间灰度，而不是二值跳变。"""
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas(200, 80)
        # 原文与译文必须不同，否则会被"译文未变化则跳过"的逻辑拦掉
        block = _block("A", 20, 20, 60, 50)
        out, reports = render_blocks(
            canvas, [block], ["H"], [self._style()], FontBook(), "en"
        )
        self.assertTrue(reports[0].painted)

        region = out[20:70, 20:80]
        vals = region.reshape(-1)
        # 统计中间灰度（非纯背景、非纯深色）的像素
        mid = np.sum((vals > 60) & (vals < 220))
        self.assertGreater(mid, 20, "没有抗锯齿过渡，超采样可能未生效")


class ExpandWidthTest(unittest.TestCase):
    """回归：译文比原文长时，要**先争取横向空间**，再缩字号。

    OCR 框紧贴原文，而 "限时优惠"(4 字) → "Limited time offer"(18 字符)
    是常态。只缩字号会把它压到原字号一半以下——实测真实海报上
    20px 的正文被压到 9px，基本不可读。
    """

    def _canvas(self, w: int = 600, h: int = 120) -> np.ndarray:
        return np.full((h, w, 3), 240, dtype=np.uint8)

    def _style(self) -> BlockStyle:
        return BlockStyle(fg_color=(0, 0, 0), bg_color=(240, 240, 240))

    def test_expands_into_blank_background(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas(600, 120)
        # 框只有 100px 宽，右侧大片空白
        block = _block("限时优惠", 20, 30, 100, 50)
        out, reports = render_blocks(
            canvas, [block], ["Limited time offer"], [self._style()], FontBook(), "en"
        )

        self.assertTrue(reports[0].painted)
        # 扩宽后字号应明显大于"只能塞进 100px"时的字号（约 12px）
        self.assertGreater(
            reports[0].font_size, 20, f"未利用右侧空白，字号仅 {reports[0].font_size}px"
        )
        # 文字应该真的画到了原框右侧之外
        right_zone = out[30:80, 130:400]
        self.assertLess(
            float(right_zone.mean()), 235.0, "文字没有延伸到原框右侧，扩框未生效"
        )

    def test_does_not_overlap_neighbour_block(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = self._canvas(600, 120)
        left = _block("限时优惠", 20, 30, 100, 50)
        right = _block("右侧元素", 200, 30, 100, 50)
        right.index = 1

        out, reports = render_blocks(
            canvas,
            [left, right],
            ["Limited time offer", "Right side element"],
            [self._style(), self._style()],
            FontBook(),
            "en",
        )
        # 左块必须止步于右块的左边界，不能压过去
        overlap = out[30:80, 195:305]
        # 允许右边块自己画的内容，所以只检查"左边块是否越界"很难。
        # 换个判据：左边块的排版宽度不应超过 200-20=180px。
        from imgtrans.render.text import _available_width, _edge_map

        edges = _edge_map(canvas)
        avail = _available_width(left, [left, right], edges, 600)
        self.assertLessEqual(avail, 180, f"扩宽越过了相邻块，avail={avail}")

    def test_stops_at_visual_content(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import _available_width, _edge_map

        canvas = self._canvas(600, 120)
        # 在 x=150 处画一条竖线，模拟 OCR 识别不出的图形元素
        canvas[0:120, 150:154] = 30

        block = _block("限时优惠", 20, 30, 100, 50)
        edges = _edge_map(canvas)
        avail = _available_width(block, [block], edges, 600)

        self.assertLess(avail, 140, f"没有在竖线处停下，avail={avail}")

    def test_no_expand_beyond_image_edge(self) -> None:
        from imgtrans.render.text import _available_width, _edge_map

        canvas = self._canvas(300, 120)
        block = _block("abc", 200, 30, 90, 50)  # x1=290，距右边只剩 10px
        avail = _available_width(block, [block], _edge_map(canvas), 300)
        self.assertLessEqual(avail, 100, f"越过图像右边界，avail={avail}")

    def test_keeps_gutter_to_neighbour(self) -> None:
        """扩框必须止步于邻块**左侧若干像素**，不能顶到邻居边上。

        实测："决明子磁石护颈枕" 译成 ``Cassia Seed Magnet Neck Pillow`` 后
        正好铺满到右列"产品等级"的起点，两段英文粘成
        ``PillowProduct grade``。扩框是为了别把字号缩太狠，
        不是为了把栏间空白吃干净。
        """
        from imgtrans.render.text import _available_width, _edge_map

        canvas = self._canvas(600, 120)
        left = _block("限时优惠", 20, 30, 100, 40)
        left.line_height = 20.0
        right = _block("右侧元素", 250, 30, 100, 40)
        right.index = 1

        avail = _available_width(left, [left, right], _edge_map(canvas), 600)
        # 最多画到 250-1，且必须比"顶到邻居"再少一截
        self.assertLess(avail, 230, f"紧贴邻块，没有留栏间距：avail={avail}")
        self.assertGreater(avail, 100, f"栏间距过大，扩框形同失效：avail={avail}")


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class KeepSingleLineTest(unittest.TestCase):
    """回归：单行原文应尽量保持单行。

    折行会让整行变高，参数表/图标注释这类"按单行设计"的版面会错位——
    实测 ``安全类别`` → ``security category`` 折成两行后把右侧的
    ``Category B`` 挤得对不齐。
    """

    def setUp(self) -> None:
        from imgtrans.render.fonts import FontBook

        self.font_book = FontBook()

    def test_short_label_shrinks_instead_of_wrapping(self) -> None:
        from imgtrans.render.text import fit_block

        # 120px 宽、高度够放两行（实测块是 77x25 的框，向下扩到 43）：
        # 折成两行完全放得下，但会把整行撑高、破坏行结构
        layout = fit_block(
            "security category",
            120,
            43,
            self.font_book,
            "en",
            initial_size=18,
            prefer_single_line=True,
        )
        self.assertEqual(len(layout.lines), 1, f"仍然折行了: {layout.lines}")
        self.assertGreaterEqual(layout.font_size, 11, "为保持单行把字号压得太小")

    def test_without_preference_it_wraps(self) -> None:
        """对照：不要求单行时会折行（说明上面的用例确实起作用）。"""
        from imgtrans.render.text import fit_block

        layout = fit_block(
            "security category", 120, 43, self.font_book, "en", initial_size=18
        )
        self.assertEqual(len(layout.lines), 2)

    def test_long_sentence_still_wraps(self) -> None:
        """长句不该被压成一行蚂蚁字。"""
        from imgtrans.render.text import fit_block

        text = "Due to color deviations in lighting/angle/monitor when shooting, " * 2
        layout = fit_block(
            text, 440, 40, self.font_book, "en", initial_size=18, prefer_single_line=True
        )
        self.assertGreaterEqual(len(layout.lines), 2)
        self.assertGreaterEqual(layout.font_size, 12, "长句被压得过小")


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class NoClipTest(unittest.TestCase):
    """回归：行尾不能被 patch 宽度裁掉。

    折行是在 1x 下测量的，绘制却在 ``SUPERSAMPLE`` 倍下进行，而 Pillow 的
    字形宽度随字号非线性变化——同一行在 4x 下宽 2~3%。按 1x 的
    ``text_width`` 定 patch 宽度，行尾必然被裁掉一两个字符：
    实测 "and personal differences" 被裁成 "and personal difference"。
    """

    def test_last_glyph_not_clipped(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import WRAP_SAFETY, draw_block, fit_block

        font_book = FontBook()
        text = "Due to color deviations in lighting/angle/monitor when shooting, and personal differences"

        # 先用 1x 测量得到"刚好放得下"的宽度，再用它当可用宽度
        from PIL import Image as _Image, ImageDraw as _ImageDraw

        from imgtrans.render.text import _measure

        f1 = font_book.get_font("en", 15)
        d1 = _ImageDraw.Draw(_Image.new("L", (1, 1)))
        tight = int(_measure(d1, text, f1)[0])

        # 折行本身留了 WRAP_SAFETY 余量（防超采样非线性导致的溢出），
        # 所以"正好等于文字宽度"的框会折成两行——这是预期行为，不是裁字。
        self.assertGreaterEqual(len(fit_block(text, tight, 40, font_book, "en").lines), 1)

        # 给足 1/WRAP_SAFETY 的余量，在**单行**前提下测"绘制不裁字"
        box_w = int(tight / WRAP_SAFETY) + 2
        layout = fit_block(text, box_w, 40, font_book, "en", initial_size=15)
        self.assertEqual(len(layout.lines), 1)

        block = _block(text, 20, 30, 200, 24)
        style = BlockStyle(fg_color=(0, 0, 0), bg_color=(240, 240, 240))

        def render(avail: int) -> np.ndarray:
            canvas = np.full((100, 1200, 3), 240, dtype=np.uint8)
            draw_block(canvas, block, text, style, layout, font_book, "en", avail)
            return canvas

        clipped = render(box_w)
        roomy = render(box_w + 200)

        # 直接判据：墨迹必须超出 1x 测量出的宽度。没超出就说明行尾被 patch 裁了。
        ink_cols = np.where((clipped[:, :, 0] < 200).any(axis=0))[0]
        self.assertTrue(len(ink_cols) > 0, "没有画出任何文字")
        self.assertGreater(
            int(ink_cols.max()) - 20,
            tight,
            f"行尾墨迹止于 {int(ink_cols.max()) - 20}px（1x 宽度 {tight}px），疑似被裁",
        )

        # 两者在"紧凑宽度 + 少量余量"范围内必须逐像素一致：
        # 若紧凑版发生了裁切，右侧字符就画不出来，像素必然不同。
        left, right = 20, min(20 + tight + 40, clipped.shape[1])
        self.assertTrue(
            np.array_equal(clipped[:, left:right], roomy[:, left:right]),
            "可用宽度不足时行尾被裁掉了（紧凑版与宽松版像素不一致）",
        )


def _rotated_poly(cx: float, cy: float, w: float, h: float, deg: float) -> Polygon:
    """绕中心旋转的四点（顺时针为正，与屏幕坐标 y 向下一致）。"""
    import math

    rad = math.radians(deg)
    cos, sin = math.cos(rad), math.sin(rad)
    corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return tuple(
        (cx + dx * cos - dy * sin, cy + dx * sin + dy * cos) for dx, dy in corners
    )


def _ink_angle(canvas: np.ndarray) -> tuple[float, int]:
    """墨迹最小外接矩形的行方向角度（度）与墨迹像素数。

    规整到 ``[-90, 90)`` 且以长边为行方向，否则水平文字会被报成 180°。
    """
    import cv2 as _cv2

    mask = (canvas[:, :, 0].astype(int) < 170).astype(np.uint8)
    pts = np.column_stack(np.where(mask > 0)[::-1]).astype(np.float32)
    if len(pts) < 10:
        return 0.0, len(pts)
    _, (w, h), ang = _cv2.minAreaRect(pts)
    if w < h:
        w, h = h, w
        ang += 90.0
    while ang >= 90.0:
        ang -= 180.0
    while ang < -90.0:
        ang += 180.0
    return float(ang), int(mask.sum())


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class TiltedRenderTest(unittest.TestCase):
    """倾斜文字必须按原角度绘制。

    之前只认轴对齐外接框，8° 的贴纸标题会被画成水平，并且因为外接框把
    ``sin(倾角) × 行长`` 算进了"字高"，字号被估大 39%，整段往右下漂。
    """

    def test_keeps_tilt_angle(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import draw_block, fit_block

        font_book = FontBook()
        text = "Today's deal"
        block = TextBlock(
            index=0,
            polygon=_rotated_poly(200, 150, 160, 34, 10.0),
            text="今日特价",
            confidence=0.99,
            line_height=40.0,
        )
        self.assertAlmostEqual(block.slope * 180 / 3.14159265, 10.0, delta=0.5)

        canvas = np.full((300, 400, 3), 240, dtype=np.uint8)
        style = BlockStyle(fg_color=(20, 20, 24), bg_color=(240, 240, 240))
        layout = fit_block(text, 160, 34, font_book, "en", initial_size=34)
        draw_block(canvas, block, text, style, layout, font_book, "en")

        ang, ink = _ink_angle(canvas)
        self.assertGreater(ink, 50, "倾斜文字没有画出来")
        self.assertAlmostEqual(
            ang, 10.0, delta=2.0, msg=f"倾角没有保持，实测 {ang:.1f}°（期望 10°）"
        )

    def test_horizontal_is_not_rotated(self) -> None:
        """对照：水平块不能被误旋转（±0.5° 抖动不该触发插值）。"""
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import draw_block, fit_block

        font_book = FontBook()
        text = "Today's deal"
        block = TextBlock(
            index=0,
            polygon=_rotated_poly(200, 150, 160, 34, 0.4),
            text="今日特价",
            confidence=0.99,
            line_height=40.0,
        )
        canvas = np.full((300, 400, 3), 240, dtype=np.uint8)
        style = BlockStyle(fg_color=(20, 20, 24), bg_color=(240, 240, 240))
        layout = fit_block(text, 160, 34, font_book, "en", initial_size=34)
        draw_block(canvas, block, text, style, layout, font_book, "en")

        ang, _ = _ink_angle(canvas)
        self.assertAlmostEqual(ang, 0.0, delta=1.0)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class CanvasCenterTest(unittest.TestCase):
    """整幅图居中的文字（大标题、图注、页脚）必须**还在居中位置**。

    OCR 给出的框紧贴字形，框内没有任何左右留白，``_estimate_align``
    只能按左对齐处理——于是译文从原文左端一路向右铺开，中心整体右移。
    实测一张 790 宽参数图的标题：中心从 396 移到 519，右端距画布只剩 29px。

    这里的判据是"译文墨迹中心是否仍等于画布中心"，因为偏移正是这个问题
    唯一可见的症状——字号、行数都是对的，只有位置错。
    """

    W = 790

    def _style(self) -> BlockStyle:
        return BlockStyle(
            fg_color=(30, 30, 34),
            bg_color=(235, 235, 235),
            text_height=48.0,
        )

    def _render(self, blocks: list[TextBlock], texts: list[str]) -> np.ndarray:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        canvas = np.full((200, self.W, 3), 235, dtype=np.uint8)
        styles = [self._style() for _ in blocks]
        out, _ = render_blocks(canvas, blocks, texts, styles, FontBook(), "en")
        return out

    def _ink_center(self, canvas: np.ndarray) -> tuple[float, int, int]:
        gray = canvas[:, :, 0]
        ys, xs = np.nonzero(gray < 150)
        self.assertGreater(len(xs), 50, "译文没有画出来")
        return (float(xs.min() + xs.max()) / 2.0, int(xs.min()), int(xs.max()))

    @staticmethod
    def _tight(x0: int, x1: int) -> TextBlock:
        """模拟 OCR 紧贴框：四点正好包住文字，框内没有留白。"""
        return TextBlock(
            index=0,
            polygon=((float(x0), 64.0), (float(x1), 64.0), (float(x1), 121.0), (float(x0), 121.0)),
            text="商品参数",
            confidence=0.99,
            line_height=50.0,
        )

    def test_tight_centered_title_stays_centered(self) -> None:
        """原文居中的紧贴框，译文中心仍落在画布中心。"""
        out = self._render([self._tight(279, 514)], ["Product parameters"])
        center, left, right = self._ink_center(out)
        self.assertAlmostEqual(
            center, self.W / 2.0, delta=4.0,
            msg=f"译文中心 {center:.0f} 偏离画布中心 {self.W / 2:.0f}（墨迹 {left}..{right}）",
        )

    def test_off_center_block_is_not_recentred(self) -> None:
        """对照：左偏的块仍左对齐到原框左缘，不能被推到中央。

        注意不能拿"中心是否靠左"当判据：这段译文本身就有原框的 6 倍长，
        即使老老实实左对齐，墨迹中心也会跑到画面中部。要看的是**左端**——
        左对齐的含义就是左端锚定不动。
        """
        block = self._tight(92, 330)
        out = self._render([block], ["Brand: Tea Ceremony Family"])
        _, left, _ = self._ink_center(out)
        self.assertLessEqual(
            abs(left - 92), 3,
            f"左偏块的左端跑了（实测 {left}，应锚定在 92），说明被误判成整幅居中",
        )

    def test_wide_row_not_centred_by_center_only(self) -> None:
        """宽块"中心恰好在画布中间"不等于居中，要看左右留白。

        实测反例：一张图顶部有一行左对齐正文，宽 656（图宽 790），中心 404，
        离画布中心只差 9px。按"中心偏差"判据会被误判成整幅居中，译文短的时候
        整行文字被推到画面中间；按左右留白（76 / 58）一眼就能排除。
        """
        from imgtrans.render.text import _is_canvas_centered

        # 中心 404（≈画布中心 395），但左留白 76、右留白 58
        block = self._tight(76, 732)
        style = self._style()
        self.assertFalse(
            _is_canvas_centered(block, [block], style, self.W),
            "左右留白不等（76 / 58）的宽块被误判成整幅居中",
        )

    def test_row_with_neighbour_is_not_centred(self) -> None:
        """多栏版面中间那栏的中心天然落在画布中心，不能当作整幅居中。

        误判的后果不是"位置稍微偏一点"，而是向**两侧**借空间——中间栏的
        译文会越过自己的左缘插进左侧栏。所以判据是左端有没有守住原框左缘。
        """
        mid = self._tight(300, 490)  # 中心 395 = 790/2，单看中心会被误判
        left_col = TextBlock(
            index=1,
            polygon=((80.0, 60.0), (250.0, 60.0), (250.0, 120.0), (80.0, 120.0)),
            text="品牌：茶礼世家",
            confidence=0.99,
            line_height=50.0,
        )
        out = self._render([mid, left_col], ["Middle column", "Tea"])
        # 左栏译文特意取短（墨迹止于 200 以内），才能干净地只看中间栏
        gray = out[:, :, 0]
        rows, cols = np.nonzero(gray < 150)
        mid_cols = cols[cols > 240]
        self.assertGreater(len(mid_cols), 50, "中间栏没有画出来")
        self.assertGreaterEqual(
            int(mid_cols.min()), 298,
            f"中间栏左端跑到 {int(mid_cols.min())}，越过原框左缘 300——被当成整幅居中了",
        )


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class EdgeMarginTest(unittest.TestCase):
    """画布边缘留白要有绝对下限。

    ``EDGE_MARGIN`` 会随 ``_visual_unit`` 按字高等比缩放，小字图上只剩 2~3px，
    译文末字几乎贴边（实测右余 3px，降部差点被切）。留白是为"看起来没被裁"
    服务的，不该跟着字号一起缩。
    """

    def test_small_unit_keeps_min_margin(self) -> None:
        from imgtrans.render import text as T

        image_w = 200
        block = TextBlock(
            index=0,
            polygon=((150.0, 10.0), (190.0, 10.0), (190.0, 30.0), (150.0, 30.0)),
            text="abc",
            confidence=0.99,
        )
        edges = np.zeros((200, image_w), dtype=np.uint8)
        edges[10:30, :] = 0  # 整行干净，扩展一路到头
        avail = T._available_width(
            block, [block], edges, image_w, unit=0.5  # 小字图
        )
        right = 150 + avail
        # 阈值写死 8 而不是引用 T.EDGE_MARGIN_MIN：引用被测常量的话，
        # 把常量改小断言也会跟着放宽，用例就永远抓不住回归。
        self.assertLessEqual(
            right, image_w - 8,
            f"右端 {right} 距画布 {image_w} 太近（下限应留 8px）",
        )


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class StrokeRenderTest(unittest.TestCase):
    """描边字：既要认出来，也要画得下（不能被 patch 边缘裁掉）。

    描边画在字形轮廓**之外**，patch 若只按字面尺寸开，描边会被自己的边界裁掉，
    回填结果比原图"细一圈"。这里用"描边在字面左右两侧的延伸量是否对称"
    来验证留白够不够：一旦漏掉那次外扩，靠画布边缘的那一侧会明显短一截。
    """

    FG = (255, 230, 60)
    STROKE = (200, 20, 20)

    def _style(self) -> BlockStyle:
        return BlockStyle(
            fg_color=self.FG,
            bg_color=(40, 90, 170),
            stroke_color=self.STROKE,
            stroke_width=5,
        )

    @staticmethod
    def _extents(canvas: np.ndarray, face, stroke) -> tuple[int, int, int, int]:
        """返回描边相对字面在左右两侧的延伸量。"""
        f = np.abs(canvas.astype(np.int16) - np.array(face, np.int16)).max(axis=2) < 60
        s = np.abs(canvas.astype(np.int16) - np.array(stroke, np.int16)).max(axis=2) < 60
        if not f.any() or not s.any():
            return 0, 0, 0, 0
        fy, fx = np.nonzero(f)
        sy, sx = np.nonzero(s)
        return (
            int(fx.min() - sx.min()),
            int(sx.max() - fx.max()),
            int(fy.min() - sy.min()),
            int(sy.max() - fy.max()),
        )

    def test_stroke_not_clipped_horizontal(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import draw_block, fit_block

        font_book = FontBook()
        canvas = np.full((140, 320, 3), 40, dtype=np.uint8)
        canvas[:, :, 0], canvas[:, :, 1], canvas[:, :, 2] = 170, 90, 40  # 蓝底(BGR)
        block = _block("MEGA", 40, 40, 200, 60)
        layout = fit_block("MEGA", 200, 60, font_book, "en", initial_size=44)
        draw_block(
            canvas, block, "MEGA", self._style(), layout, font_book, "en"
        )

        left, right, top, bottom = self._extents(canvas, self.FG[::-1], self.STROKE[::-1])
        self.assertGreaterEqual(left, 3, f"左侧描边被裁掉了（延伸 {left}px）")
        self.assertGreaterEqual(right, 3, f"右侧描边被裁掉了（延伸 {right}px）")
        self.assertLessEqual(
            abs(left - right), 3, f"左右描边不对称: 左 {left} 右 {right}"
        )
        self.assertGreaterEqual(min(top, bottom), 2, f"上下描边被裁: 上 {top} 下 {bottom}")

    def test_stroke_not_clipped_vertical(self) -> None:
        """竖排从 patch 右缘量列位置，漏掉描边留白会让整列右移、描边被裁。"""
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import _layout_vertical, draw_block

        font_book = FontBook()
        canvas = np.full((360, 160, 3), 40, dtype=np.uint8)
        canvas[:, :, 0], canvas[:, :, 1], canvas[:, :, 2] = 170, 90, 40
        block = TextBlock(
            index=0,
            polygon=((56.0, 30.0), (96.0, 30.0), (96.0, 330.0), (56.0, 330.0)),
            text="禁止",
            confidence=0.99,
            line_height=40.0,
        )
        layout = _layout_vertical("禁止", 40, 300, font_book, "zh", initial_size=34)
        self.assertIsNotNone(layout)
        draw_block(canvas, block, "禁止", self._style(), layout, font_book, "zh")

        left, right, _, _ = self._extents(canvas, self.FG[::-1], self.STROKE[::-1])
        self.assertGreaterEqual(right, 3, f"右侧描边被裁掉了（延伸 {right}px）")
        self.assertGreaterEqual(left, 3, f"左侧描边被裁掉了（延伸 {left}px）")


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class StrokeDetectTest(unittest.TestCase):
    """描边检测：真描边要认出来，照片背景要放弃。

    旧实现取 mask 的"外边缘带"，而那圈像素按构造落在字形外侧的**背景**上
    （mask 做过 2px 膨胀），于是"有没有描边"永远判成没有。
    """

    def _outlined(self):
        from PIL import Image, ImageDraw

        im = Image.new("RGB", (360, 150), (40, 90, 170))
        draw = ImageDraw.Draw(im)
        font = _font(52)
        draw.text(
            (20, 40), "超级折扣", font=font, fill=(255, 230, 60),
            stroke_width=4, stroke_fill=(120, 20, 20),
        )
        bbox = draw.textbbox((20, 40), "超级折扣", font=font, stroke_width=4)
        block = TextBlock(
            index=0,
            polygon=(
                (float(bbox[0]), float(bbox[1])),
                (float(bbox[2]), float(bbox[1])),
                (float(bbox[2]), float(bbox[3])),
                (float(bbox[0]), float(bbox[3])),
            ),
            text="超级折扣",
            confidence=0.99,
        )
        return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR), block

    def test_outlined_text_detected(self) -> None:
        from imgtrans.inpaint.mask import analyze_box
        from imgtrans.render.style import analyze_style

        img, block = self._outlined()
        style = analyze_style(img, block, analyze_box(img, block))
        self.assertIsNotNone(style.stroke_color, "描边没有被认出来")
        assert style.stroke_color is not None
        self.assertLess(
            np.linalg.norm(np.array(style.stroke_color, float) - np.array((120, 20, 20), float)),
            45,
            f"描边色不对: {style.stroke_color}",
        )
        self.assertGreaterEqual(style.stroke_width, 2, f"描边宽 {style.stroke_width} 偏细")
        self.assertLessEqual(style.stroke_width, 8)

    def test_plain_text_has_no_stroke(self) -> None:
        from PIL import Image, ImageDraw

        from imgtrans.inpaint.mask import analyze_box
        from imgtrans.render.style import analyze_style

        im = Image.new("RGB", (300, 120), (229, 228, 235))
        draw = ImageDraw.Draw(im)
        font = _font(36)
        draw.text((30, 40), "普通文字", font=font, fill=(28, 28, 32))
        bbox = draw.textbbox((30, 40), "普通文字", font=font)
        block = TextBlock(
            index=0,
            polygon=(
                (float(bbox[0]), float(bbox[1])),
                (float(bbox[2]), float(bbox[1])),
                (float(bbox[2]), float(bbox[3])),
                (float(bbox[0]), float(bbox[3])),
            ),
            text="普通文字",
            confidence=0.99,
        )
        img = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        style = analyze_style(img, block, analyze_box(img, block))
        self.assertIsNone(style.stroke_color, f"无描边却认出了 {style.stroke_color}")

    def test_textured_background_gives_up_stroke(self) -> None:
        """照片背景上"离字色和底色都远"必然被满足，必须直接放弃描边。"""
        from PIL import Image

        from imgtrans.inpaint.mask import analyze_box
        from imgtrans.render.style import analyze_style

        rng = np.random.default_rng(11)
        arr = np.full((160, 420, 3), 110, np.uint8)
        arr[:, :130] = 34
        arr[:, 300:] = 176
        arr = np.clip(
            arr.astype(np.int16) + rng.integers(-30, 31, arr.shape), 0, 255
        ).astype(np.uint8)
        im = Image.fromarray(arr[:, :, ::-1])
        block = _make_text_block(im, (140, 62), "白色小字", 34, (250, 248, 240))
        img = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        analysis = analyze_box(img, block)
        style = analyze_style(img, block, analysis)
        self.assertGreater(analysis.bg_std, 12.0, "用例前提：背景该被判成纹理")
        self.assertIsNone(
            style.stroke_color, f"照片背景上不该认描边，却得到 {style.stroke_color}"
        )


def _make_text_block(img, xy, text, size, fill) -> TextBlock:
    """在图上画字并返回贴墨迹的块（同 test_mask._text_block，避免跨文件引用）。"""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    font = _font(size)
    draw.text(xy, text, font=font, fill=fill)
    x0, y0, x1, y1 = draw.textbbox(xy, text, font=font)
    return TextBlock(
        index=0,
        polygon=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        text=text,
        confidence=0.99,
    )


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class VerticalLayoutTest(unittest.TestCase):
    """竖排：逐字成列，列宽受框宽约束。"""

    def test_single_column(self) -> None:
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import VERTICAL_COL_RATIO, _layout_vertical

        layout = _layout_vertical("禁止转载", 50, 220, FontBook(), "zh", initial_size=40)
        self.assertIsNotNone(layout, "竖排布局返回了 None")
        assert layout is not None
        self.assertTrue(layout.vertical)
        self.assertEqual(len(layout.lines), 1, f"4 个字应排成一列: {layout.lines}")
        self.assertEqual(layout.lines[0], "禁止转载")
        self.assertLessEqual(
            layout.font_size * VERTICAL_COL_RATIO,
            50.5,
            "字号超出列宽，会横向挤出框",
        )

    def test_too_long_falls_back(self) -> None:
        """框太矮装不下时返回 None，让调用方改走横排。"""
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import _layout_vertical

        # 24 个字塞进 50x60 的框：无论怎么缩都放不下
        long_text = "这是一段非常长的竖排文字总共二十四个字符"
        layout = _layout_vertical(long_text, 50, 60, FontBook(), "zh", initial_size=40)
        self.assertIsNone(layout, "放不下时应返回 None 以便退回横排")


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class LinePitchTest(unittest.TestCase):
    """多行段落必须继承原图行距，固定 1.2 会把整段压扁三分之一。"""

    def test_recovers_pitch_from_multiline_block(self) -> None:
        from imgtrans.render.text import LINE_HEIGHT_RATIO, _line_pitch_ratio

        # 段落框高 134、4 行、单行框高 26 -> 行距 (134-26)/3 = 36，字号 21
        block = TextBlock(
            index=0,
            polygon=((20.0, 14.0), (440.0, 14.0), (440.0, 148.0), (20.0, 148.0)),
            text="a" * 40,
            confidence=0.99,
            line_height=26.0,
            line_count=4,
        )
        ratio = _line_pitch_ratio(block, 21.0)
        self.assertAlmostEqual(ratio, 36.0 / 21.0, delta=0.05)

    def test_single_line_uses_default(self) -> None:
        from imgtrans.render.text import LINE_HEIGHT_RATIO, _line_pitch_ratio

        block = _block("abc", 10, 10, 100, 26)
        self.assertAlmostEqual(_line_pitch_ratio(block, 21.0), LINE_HEIGHT_RATIO)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class ScaleAdaptivityTest(unittest.TestCase):
    """视觉间距必须随字号缩放：6px 留白在 13px 小图和 90px 大图上都不合适。"""

    def test_visual_unit(self) -> None:
        from imgtrans.render.text import UNIT_MAX, UNIT_MIN, _visual_unit

        self.assertAlmostEqual(_visual_unit(40), 1.0)
        self.assertAlmostEqual(_visual_unit(80), 2.0)
        self.assertAlmostEqual(_visual_unit(10), UNIT_MIN)
        self.assertAlmostEqual(_visual_unit(400), UNIT_MAX)
        self.assertAlmostEqual(_visual_unit(None), 1.0)

    def test_large_text_leaves_more_margin(self) -> None:
        from imgtrans.render.text import _available_width, _edge_map

        canvas = np.full((400, 900, 3), 240, dtype=np.uint8)
        big = _block("abc", 100, 100, 400, 90)
        small = _block("abc", 100, 300, 400, 20)
        edges = _edge_map(canvas)
        # 同一个块位，视觉尺大时留白更多 -> 可用宽度更小
        w_big = _available_width(big, [big], edges, 900, unit=2.0)
        w_small = _available_width(small, [small], edges, 900, unit=0.5)
        self.assertLess(w_big, w_small)

    def test_supersample_by_size(self) -> None:
        from imgtrans.render.text import SUPERSAMPLE, _supersample

        self.assertEqual(_supersample(12), SUPERSAMPLE)
        self.assertEqual(_supersample(40), 3)
        self.assertEqual(_supersample(90), 2)


@unittest.skipUnless(HAS_DEPS, "需要 opencv + pillow")
class WrapSafetyTest(unittest.TestCase):
    """折行要留余量，否则超采样下同一行宽 3%，patch 又被画布卡住就裁字。"""

    def test_wraps_before_hitting_exact_width(self) -> None:
        from PIL import Image as _Image, ImageDraw as _ImageDraw

        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import _measure, fit_block

        font_book = FontBook()
        text = "Due to color deviations in lighting and personal differences here"
        d1 = _ImageDraw.Draw(_Image.new("L", (1, 1)))
        exact = int(_measure(d1, text, font_book.get_font("en", 15))[0])

        layout = fit_block(text, exact, 60, font_book, "en", initial_size=15)
        self.assertGreaterEqual(
            len(layout.lines), 2, "正好等于文字宽度时应提前折行（留出安全余量）"
        )
        # 每行都必须真的没超
        for ln in layout.lines:
            self.assertLessEqual(
                _measure(d1, ln, font_book.get_font("en", 15))[0], exact
            )


class FontFallbackRenderTest(unittest.TestCase):
    """混排文本要真的画出字，而不是一排方框。

    线上 bn 图里孟加拉文正常、`PTM7950 (PCM) 150°C` 全是方框：孟加拉字体
    有数字没字母。修复的关键不是换字体（没有字体两样都有），而是在绘制时
    按字符切段、各用各的字体——这里钉住"确实画出来了"且"不再误报缺字形"。
    """

    def test_mixed_script_is_rendered_without_missing_warning(self) -> None:
        from imgtrans.render import fonts as fonts_mod
        from imgtrans.render.fonts import FontBook
        from imgtrans.render.text import render_blocks

        fonts_mod._WARNED_NO_RAQM.clear()
        self.addCleanup(fonts_mod._WARNED_NO_RAQM.clear)

        book = FontBook()
        if book._family_font("bengali") is None:
            self.skipTest("本机没有孟加拉字体")

        canvas = np.full((120, 520, 3), 245, dtype=np.uint8)
        # 原文必须与译文不同，否则 render_blocks 会判定"无需回填"直接跳过
        blocks = [_block("原文占位", 20, 20, 460, 50)]
        out, reports = render_blocks(
            canvas,
            blocks,
            ["构建 বাংলা 完成"],
            [BlockStyle(fg_color=(0, 0, 0), bg_color=(240, 240, 240))],
            book,
            "zh",
        )

        ink = (out < 128).any(axis=2)
        self.assertGreater(int(ink.sum()), 100, "一个字都没画出来")
        painted = [r for r in reports if r.painted]
        self.assertTrue(painted, "应当有块被回填")
        for report in painted:
            self.assertNotIn("方框", report.note, f"混排不该报缺字形：{report.note}")


if __name__ == "__main__":
    unittest.main()
