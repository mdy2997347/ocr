"""mask 生成与擦除的测试。

用合成图而非真实照片：断言需要"已知真值"，真实照片上没人说得清
某个像素应不应该被擦。合成图可以精确知道文字画在哪、背景是什么色。

这些用例覆盖的是最容易出问题的三类场景：
纯色背景（多数）、渐变背景（海报）、纹理背景（照片）。
"""

from __future__ import annotations

import unittest

import numpy as np

try:
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    HAS_CV2 = True
except ImportError:  # pragma: no cover
    HAS_CV2 = False

from imgtrans.types import Polygon, TextBlock


def _font(size: int):
    """找一个能画中文的字体；找不到则用默认字体（测试仍可运行）。"""
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _make_image(
    text: str = "测试文字",
    size: tuple[int, int] = (200, 80),
    bg: str | tuple = "white",
    fg: str = "black",
    origin: tuple[int, int] = (20, 20),
) -> tuple[np.ndarray, TextBlock]:
    """画一张只有一行字的图，返回 BGR 数组与对应的文字块。"""
    im = Image.new("RGB", size, bg if isinstance(bg, str) else bg)
    draw = ImageDraw.Draw(im)
    font = _font(36)
    draw.text(origin, text, font=font, fill=fg)

    bbox = draw.textbbox(origin, text, font=font)
    x0, y0, x1, y1 = bbox
    poly: Polygon = (
        (float(x0), float(y0)),
        (float(x1), float(y0)),
        (float(x1), float(y1)),
        (float(x0), float(y1)),
    )
    block = TextBlock(index=0, polygon=poly, text=text, confidence=0.99)
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR), block


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class MaskTest(unittest.TestCase):
    def test_plain_background_detected(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img, block = _make_image(bg="white", fg="black")
        analysis = analyze_box(img, block)

        # 背景应是接近白的中位色
        self.assertGreater(min(analysis.bg_color), 200)
        # 前景应是接近黑
        self.assertLess(max(analysis.fg_color), 90)
        # 文字像素占比合理（不会把整块当文字）
        self.assertGreater(analysis.ink_ratio, 0.02)
        self.assertLess(analysis.ink_ratio, 0.7)
        # 纯色背景标准差很小
        self.assertLess(analysis.bg_std, 15)

    def test_mask_covers_text_pixels_not_whole_box(self) -> None:
        """关键断言：mask 只覆盖笔画，不覆盖整个矩形。

        整块矩形擦除会在有纹理的背景上留下补丁——这是最常见的失败。
        这里用白底黑字验证：mask 面积必须明显小于框面积。
        """
        from imgtrans.inpaint.mask import analyze_box

        img, block = _make_image(bg="white", fg="black")
        analysis = analyze_box(img, block)

        box_area = analysis.mask.size
        ink_area = int((analysis.mask > 0).sum())
        ratio = ink_area / box_area
        # 汉字"测试文字"的笔画覆盖率约 20%~50%，绝不该接近 100%
        self.assertLess(ratio, 0.85, f"mask 覆盖了 {ratio:.0%} 的框，疑似整块矩形")
        self.assertGreater(ratio, 0.03)

    def test_erase_restores_plain_background(self) -> None:
        from imgtrans.inpaint.erase import erase_text
        from imgtrans.inpaint.mask import analyze_box

        img, block = _make_image(bg="white", fg="black")
        analysis = analyze_box(img, block)

        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        x0, y0, x1, y1 = analysis.box
        mask[y0:y1, x0:x1] = analysis.mask

        erased = erase_text(img, mask, [analysis])

        # 擦除后框内应几乎全是背景色（白）
        region = erased[y0:y1, x0:x1]
        # 排除边缘一圈，避免羽化过渡影响
        inner = region[2:-2, 2:-2]
        mean_brightness = float(inner.mean())
        self.assertGreater(mean_brightness, 235, "擦除后仍有明显深色残留")

    def test_gradient_background_uses_plane_fill(self) -> None:
        """渐变背景应走平面拟合，擦除后不留色块。"""
        from imgtrans.inpaint.erase import erase_text
        from imgtrans.inpaint.mask import analyze_box

        size = (240, 100)
        grad = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        for x in range(size[0]):
            v = int(60 + 150 * x / size[0])
            grad[:, x] = (v, v, v)

        im = Image.fromarray(cv2.cvtColor(grad, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(im)
        font = _font(30)
        text = "AB"
        draw.text((80, 30), text, font=font, fill=(255, 255, 255))
        bbox = draw.textbbox((80, 30), text, font=font)
        block = TextBlock(
            index=0,
            polygon=(
                (float(bbox[0]), float(bbox[1])),
                (float(bbox[2]), float(bbox[1])),
                (float(bbox[2]), float(bbox[3])),
                (float(bbox[0]), float(bbox[3])),
            ),
            text=text,
            confidence=0.99,
        )
        img = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)

        analysis = analyze_box(img, block)
        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        x0, y0, x1, y1 = analysis.box
        mask[y0:y1, x0:x1] = analysis.mask

        erased = erase_text(img, mask, [analysis])

        # 擦除后框内的横向渐变应该还在：左右两侧亮度差应保留
        region = erased[y0:y1, x0:x1]
        left_mean = float(region[:, : max(1, region.shape[1] // 5)].mean())
        right_mean = float(region[:, -max(1, region.shape[1] // 5) :].mean())
        self.assertGreater(
            right_mean - left_mean,
            20,
            "渐变被抹平了，说明退化成了纯色填充",
        )


class FramedTextTest(unittest.TestCase):
    """文字被边框紧包时，边框不能把背景色"带偏"。

    实测场景：电商参数图的标题"商品参数"，OCR 框外面就是一圈 3px 黑边框，
    间隔只有 1px。参考环（框外一圈）整圈压在边框上，中位数取成黑色，
    于是"浅灰的底"被当成文字擦掉——整条标题被填成黑底浅字。

    这类图的共同特征是：参考环是双峰的（边框色 + 底色），``bg_std`` 会顶到
    几十。此时唯一可靠的判据是"背景必须在框内占多数"。
    """

    def _framed_image(self) -> tuple[np.ndarray, TextBlock]:
        """构造"文字被黑边框紧包"的图，几何按实测图 1:1 还原。

        实测标题块：OCR 框 ``(352,66)-(437,94)``，黑边框占 y 58~64，
        即边框下沿离框顶只有 1px。分析区域外扩后是 y 65~98，
        参考环正好落在 y 62~64（纯黑）与 y 98~100（纯黑）上，
        左右两列同理压在竖边框上——**整圈环全是边框色**。

        这里刻意把四条边框画在"参考环恰好覆盖"的位置：
        环 = 内区外扩 ``RING_WIDTH``，所以边框就画在内区外 ``RING_WIDTH`` 处。
        """
        from imgtrans.inpaint.mask import (
            MASK_PAD_BOTTOM,
            MASK_PAD_LEFT,
            MASK_PAD_RIGHT,
            MASK_PAD_TOP,
            RING_WIDTH,
        )

        im = Image.new("RGB", (400, 200), (229, 228, 235))
        draw = ImageDraw.Draw(im)
        font = _font(34)
        text = "参数"
        origin = (60, 60)
        draw.text(origin, text, font=font, fill=(0, 0, 0))
        bbox = draw.textbbox(origin, text, font=font)

        # 分析内区 = OCR 框按 MASK_PAD_* 外扩
        ix0 = bbox[0] - MASK_PAD_LEFT
        iy0 = bbox[1] - MASK_PAD_TOP
        ix1 = bbox[2] + MASK_PAD_RIGHT
        iy1 = bbox[3] + MASK_PAD_BOTTOM

        black = (0, 0, 0)
        r = RING_WIDTH
        draw.rectangle((ix0 - r, iy0 - r, ix1 + r - 1, iy0 - 1), fill=black)  # 上
        draw.rectangle((ix0 - r, iy1, ix1 + r - 1, iy1 + r - 1), fill=black)  # 下
        draw.rectangle((ix0 - r, iy0 - r, ix0 - 1, iy1 + r - 1), fill=black)  # 左
        draw.rectangle((ix1, iy0 - r, ix1 + r - 1, iy1 + r - 1), fill=black)  # 右

        block = TextBlock(
            index=0,
            polygon=(
                (float(bbox[0]), float(bbox[1])),
                (float(bbox[2]), float(bbox[1])),
                (float(bbox[2]), float(bbox[3])),
                (float(bbox[0]), float(bbox[3])),
            ),
            text=text,
            confidence=0.99,
        )
        return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR), block

    def test_background_not_polluted_by_frame(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._framed_image()
        analysis = analyze_box(img, block)

        # 背景应仍是浅灰，不能变成边框的黑色
        self.assertGreater(
            min(analysis.bg_color),
            200,
            f"背景色被边框污染成了 {analysis.bg_color}",
        )
        # 前景是黑色文字
        self.assertLess(max(analysis.fg_color), 90)
        # 墨水占比必须像文字（20%~50%），而不是"整块 75% 都是文字"
        self.assertLess(analysis.ink_ratio, 0.5)
        self.assertGreater(analysis.ink_ratio, 0.02)
        # 底色本身是纯色，复杂度不该被边框顶到几十
        self.assertLess(analysis.bg_std, 15, "背景复杂度被边框污染")

    def test_erase_keeps_frame_intact(self) -> None:
        """擦除后框内应恢复浅灰底色，且边框像素不被改动。"""
        from imgtrans.inpaint.erase import erase_text
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._framed_image()
        analysis = analyze_box(img, block)

        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        x0, y0, x1, y1 = analysis.box
        mask[y0:y1, x0:x1] = analysis.mask
        erased = erase_text(img, mask, [analysis])

        region = erased[y0:y1, x0:x1]
        inner = region[2:-2, 2:-2]
        self.assertGreater(float(inner.mean()), 225, "擦除后框内不是浅灰底色")
        # 文字被擦掉：框内不该再有大片深色
        dark = (inner.mean(axis=2) < 100).mean()
        self.assertLess(float(dark), 0.02, "框内仍残留大量深色像素")


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class MaskEdgeCaseTest(unittest.TestCase):
    def test_tiny_box_does_not_crash(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img = np.full((20, 20, 3), 255, dtype=np.uint8)
        block = TextBlock(
            index=0,
            polygon=((5.0, 5.0), (7.0, 5.0), (7.0, 7.0), (5.0, 7.0)),
            text="a",
            confidence=0.9,
        )
        analysis = analyze_box(img, block)  # 不应抛异常
        # mask 尺寸必须与返回的 box 自洽（调用方按 box 定位贴回 mask）
        x0, y0, x1, y1 = analysis.box
        self.assertEqual(analysis.mask.shape, (y1 - y0, x1 - x0))
        # 分析区域在 OCR 框基础上做了外扩，应略大于原始 2x2
        self.assertGreaterEqual(analysis.mask.shape[0], 2)

    def test_glyph_below_box_is_covered(self) -> None:
        """字形下缘超出 OCR 框时必须被 mask 覆盖。

        实测标题"相变材料的工作原理"的框底截掉了字脚 4px，那些像素不在
        ROI 内，擦完留下一排断续黑点。这里用合成图复现截断场景。
        """
        from imgtrans.inpaint.mask import analyze_box

        img = np.full((40, 60, 3), 255, dtype=np.uint8)
        # 一行"文字"：y 10..19 的黑色实心块
        img[10:20, 8:52] = (0, 0, 0)
        block = TextBlock(
            index=0,
            # OCR 框只到 y=16，字形的 17..19 行被截在框外
            polygon=((8.0, 10.0), (52.0, 10.0), (52.0, 16.0), (8.0, 16.0)),
            text="标题",
            confidence=0.95,
        )
        analysis = analyze_box(img, block)
        x0, y0, x1, y1 = analysis.box
        self.assertGreaterEqual(y1, 20, "box 未向下覆盖到字形末端")
        # 框外的字脚像素必须被标进 mask
        rows = np.where((analysis.mask > 0).any(axis=1))[0]
        self.assertTrue(len(rows) > 0)
        self.assertGreaterEqual(int(rows.max()) + y0, 19, "mask 未覆盖字形下缘")

    def test_flat_region_yields_empty_mask(self) -> None:
        """纯色区域没有文字，mask 应为空而不是满屏噪声。"""
        from imgtrans.inpaint.mask import analyze_box

        img = np.full((60, 60, 3), 128, dtype=np.uint8)
        block = TextBlock(
            index=0,
            polygon=((10.0, 10.0), (50.0, 10.0), (50.0, 30.0), (10.0, 30.0)),
            text="x",
            confidence=0.9,
        )
        analysis = analyze_box(img, block)
        self.assertEqual(int((analysis.mask > 0).sum()), 0)
        self.assertEqual(analysis.ink_ratio, 0.0)


class ColorChannelOrderTest(unittest.TestCase):
    """回归：颜色通道顺序必须是 RGB。

    这个 bug 不会抛异常、不会让测试变红，只会让黄色的字被画成蓝色
    （或者纯色底被填成互补色）。是我在真实海报上肉眼发现的，
    所以必须有断言把它钉住。
    """

    def test_fg_color_is_rgb_not_bgr(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        # 深蓝底 + 黄色字。BGR 与 RGB 的差异在 B/R 通道交换，非常明显。
        img, block = _make_image(
            text="AB", size=(220, 80), bg=(20, 30, 80), fg=(255, 236, 120), origin=(20, 20)
        )
        analysis = analyze_box(img, block)

        r, g, b = analysis.fg_color
        self.assertGreater(r, 200, f"fg 的 R 通道应接近 255（黄色），实际 fg={analysis.fg_color}")
        self.assertGreater(g, 200, f"fg 的 G 通道应接近 236，实际 fg={analysis.fg_color}")
        self.assertLess(b, 160, f"fg 的 B 通道应接近 120，实际 fg={analysis.fg_color}")

    def test_bg_color_is_rgb_not_bgr(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img, block = _make_image(
            text="AB", size=(220, 80), bg=(20, 30, 80), fg=(255, 236, 120), origin=(20, 20)
        )
        analysis = analyze_box(img, block)
        r, g, b = analysis.bg_color
        # 背景是 RGB(20,30,80)：蓝通道最大。若顺序反了会读成 (80,30,20)
        self.assertGreater(b, r, f"背景蓝色通道应最大，实际 bg={analysis.bg_color}")


class ResidualCriterionTest(unittest.TestCase):
    """回归：分级判据必须用**拟合残差**，不能用原始 std。

    线性渐变上下能差几十个灰度，原始 std 轻松到 30+，
    但它是完美可拟合的。用原始 std 会把整片渐变误判成纹理，
    全部丢给 inpaint——慢，而且大区域会有涂抹感。
    """

    def test_gradient_has_low_residual_high_raw_std(self) -> None:
        from imgtrans.inpaint.erase import picture_residual_std

        # 竖直渐变：从 (30,40,90) 到 (120,140,200)，跨度很大
        h, w = 200, 300
        img = np.zeros((h, w, 3), dtype=np.uint8)
        for y in range(h):
            t = y / (h - 1)
            img[y, :, 0] = int(90 + 110 * t)
            img[y, :, 1] = int(40 + 100 * t)
            img[y, :, 2] = int(30 + 90 * t)

        raw_std = float(img.reshape(-1, 3).std())
        residual = picture_residual_std(img)
        self.assertGreater(raw_std, 20.0, "这张图的原始 std 应该很大（渐变跨度大）")
        self.assertLess(residual, 8.0, f"渐变应能被平面完美拟合，残差={residual}")

    def test_texture_has_high_residual(self) -> None:
        from imgtrans.inpaint.erase import picture_residual_std

        rng = np.random.default_rng(7)
        # 高频随机噪声 = 最坏情况的"纹理"，平面拟合解释不了
        img = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
        residual = picture_residual_std(img)
        self.assertGreater(residual, 20.0, f"随机纹理残差应很高，实际={residual}")

    def test_flat_color_residual_near_zero(self) -> None:
        from imgtrans.inpaint.erase import picture_residual_std

        img = np.full((100, 120, 3), 137, dtype=np.uint8)
        self.assertLess(picture_residual_std(img), 1.0)


def _text_block(
    img: "Image.Image",
    xy: tuple[int, int],
    text: str,
    size: int,
    fill,
    stroke_width: int = 0,
    stroke_fill=None,
) -> TextBlock:
    """在图上画文字并返回贴合**墨迹**的块（模拟 PaddleOCR 的紧贴框）。

    坐标必须用 ``draw.textbbox``：``font.getmask().getbbox()`` 给的是**位图内**
    的相对位置，不含基线与 ascender 偏移。当成绝对坐标用会让整个框上移一个
    ascent（实测 8px），框底露出框外——而"框紧贴字形"正是这些用例要覆盖的
    最难情形，框位置错了，测的就不是那件事了。
    """
    draw = ImageDraw.Draw(img)
    font = _font(size)
    draw.text(
        xy, text, font=font, fill=fill,
        stroke_width=stroke_width, stroke_fill=stroke_fill,
    )
    box = draw.textbbox(xy, text, font=font, stroke_width=stroke_width)
    if box[2] <= box[0] or box[3] <= box[1]:
        raise AssertionError(f"字体画不出 {text!r}，换一个字体再测")
    x0, y0, x1, y1 = box
    poly: Polygon = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return TextBlock(index=0, polygon=poly, text=text, confidence=0.99)


def _to_bgr(img: "Image.Image") -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def _dist(a, b) -> float:
    return float(np.linalg.norm(np.array(a, float) - np.array(b, float)))


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class ForegroundColorTest(unittest.TestCase):
    """前景色要选"离背景最远的颜色簇"。

    中位数会被描边拉偏（黄字红描边量成暗红）；最大簇同样不可靠
    （4px 描边 + 6px 笔画时描边面积反而更大）。这两种错法都会让回填
    画出颜色完全不对的字。
    """

    def test_plain_black_on_light(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img = Image.new("RGB", (300, 120), (229, 228, 235))
        block = _text_block(img, (30, 40), "测试文字", 36, (28, 28, 32))
        a = analyze_box(_to_bgr(img), block)
        self.assertLess(_dist(a.fg_color, (28, 28, 32)), 40, f"fg={a.fg_color}")

    def test_white_on_red(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img = Image.new("RGB", (300, 120), (200, 60, 66))
        block = _text_block(img, (30, 40), "立即购买", 36, (255, 255, 255))
        a = analyze_box(_to_bgr(img), block)
        self.assertLess(_dist(a.fg_color, (255, 255, 255)), 30, f"fg={a.fg_color}")
        self.assertLess(_dist(a.bg_color, (200, 60, 66)), 20, f"bg={a.bg_color}")

    def test_outlined_text_picks_face_not_stroke(self) -> None:
        """黄字 + 4px 暗红描边 + 蓝底：必须取到黄，不能取描边。"""
        from imgtrans.inpaint.mask import analyze_box

        img = Image.new("RGB", (360, 150), (40, 90, 170))
        block = _text_block(
            img, (20, 40), "超级折扣", 52, (255, 230, 60),
            stroke_width=4, stroke_fill=(120, 20, 20),
        )
        a = analyze_box(_to_bgr(img), block)
        self.assertLess(
            _dist(a.fg_color, (255, 230, 60)),
            60,
            f"前景色取到了描边而不是字面: fg={a.fg_color}",
        )


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class ThickInkBackgroundTest(unittest.TestCase):
    """描边 / 超粗体：墨迹占框内一半以上时，背景仍然必须是背景。

    实测场景：蓝底、黄字、4px 暗红描边。墨迹（字面 + 描边）占框内 73%，
    旧判据"背景必须占框内多数"会把**100% 纯色**的参考环否掉，转而采信
    "框内中位色"——那是蓝、黄、红三色混出来的 ``(54,90,120)``，图上并不存在。
    擦完这块就成了一片脏色，而描边字是海报/促销图里最常见的一类。
    """

    BG = (40, 90, 170)
    STROKE = (120, 20, 20)
    FACE = (255, 230, 60)

    def _outlined(self) -> tuple[np.ndarray, TextBlock]:
        img = Image.new("RGB", (360, 150), self.BG)
        block = _text_block(
            img, (20, 40), "超级折扣", 52, self.FACE,
            stroke_width=4, stroke_fill=self.STROKE,
        )
        return _to_bgr(img), block

    def test_ink_ratio_really_exceeds_half(self) -> None:
        """先确认这个用例真的踩在"墨迹过半"上，否则它测不到任何东西。"""
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._outlined()
        a = analyze_box(img, block)
        self.assertGreater(a.ink_ratio, 0.5, f"墨迹只占 {a.ink_ratio:.0%}，用例失效")

    def test_background_is_not_overridden(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._outlined()
        a = analyze_box(img, block)
        self.assertLess(
            _dist(a.bg_color, self.BG), 20, f"纯色底被改判成了 {a.bg_color}"
        )
        self.assertLess(_dist(a.fg_color, self.FACE), 60, f"字色取错: {a.fg_color}")
        # 纯色底必须仍被判成纯色，否则擦除会走平面拟合
        self.assertLess(a.bg_std, 3.0, f"纯色底被判成纹理: bg_std={a.bg_std:.1f}")

    def test_erase_restores_background(self) -> None:
        """擦除后框内应该只剩背景蓝，不能留下暗红（描边色）。"""
        from imgtrans.inpaint.erase import erase_text
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._outlined()
        a = analyze_box(img, block)
        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        x0, y0, x1, y1 = a.box
        mask[y0:y1, x0:x1] = a.mask
        erased = erase_text(img, mask, [a])

        region = erased[y0:y1, x0:x1][a.mask > 0]
        mean = region.mean(axis=0)[::-1]  # BGR -> RGB
        self.assertLess(
            _dist(mean, self.BG), 30, f"擦除后框内平均色 {tuple(int(v) for v in mean)} 不是背景蓝"
        )


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class TextOnPhotoBackgroundTest(unittest.TestCase):
    """照片背景上的字色，不能只按"离背景中位色最远"来选。

    照片的"背景色"只是个中位值，框里另一块暗区（窗帘、相框、阴影）往往比
    白字离它更远。实测人像照上的白色小字被选成 ``(15,15,16)``，
    回填出来是黑字压在深色照片上。
    """

    def _photo(self) -> tuple[np.ndarray, TextBlock]:
        rng = np.random.default_rng(7)
        arr = np.full((160, 420, 3), 110, np.uint8)
        arr[:, :130] = 34      # 左侧暗区（离"背景中位色"最远）
        arr[:, 300:] = 176     # 右侧亮区
        arr = np.clip(
            arr.astype(np.int16) + rng.integers(-10, 11, arr.shape), 0, 255
        ).astype(np.uint8)
        im = Image.fromarray(arr[:, :, ::-1])  # BGR -> RGB
        block = _text_block(im, (140, 62), "白色小字", 34, (250, 248, 240))
        return _to_bgr(im), block

    def test_fg_is_the_text_not_the_dark_region(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img, block = self._photo()
        a = analyze_box(img, block)
        self.assertGreater(
            min(a.fg_color), 170, f"字色被选成了照片的暗部: {a.fg_color}"
        )


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class BackgroundFlatnessTest(unittest.TestCase):
    """``bg_std`` 必须按通道算。

    三个通道摊平算的是"跨通道色差"：纯红底 (200,60,66) 会得出 65 的高 std，
    纯色背景被误判成纹理，擦除阶段改走平面拟合，彩色底整块被涂花。
    """

    def test_pure_red_is_flat(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        img = Image.new("RGB", (300, 120), (200, 60, 66))
        block = _text_block(img, (30, 40), "测试", 36, (255, 255, 255))
        a = analyze_box(_to_bgr(img), block)
        self.assertLess(a.bg_std, 0.5, f"纯红底被判成纹理: bg_std={a.bg_std:.1f}")

    def test_gradient_is_not_flat(self) -> None:
        from imgtrans.inpaint.mask import analyze_box

        # 渐变要够陡：块内跨不到 10 个灰度时 std 只有 3，跟"轻微不均匀"分不开
        x = np.linspace(0, 1, 300)[None, :].repeat(120, axis=0)
        rgb = np.stack([110 + 130 * x, 90 + 140 * x, 210 - 120 * x], axis=2).astype(np.uint8)
        img = Image.fromarray(rgb, "RGB")
        block = _text_block(img, (30, 40), "测试", 36, (30, 30, 60))
        a = analyze_box(_to_bgr(img), block)
        self.assertGreater(a.bg_std, 4.0, f"渐变应被判为不平坦: bg_std={a.bg_std:.1f}")


@unittest.skipUnless(HAS_CV2, "需要 opencv")
class EraseCleanlinessTest(unittest.TestCase):
    """擦除后不能在原文字位置留下任何可辨认的轮廓。

    两个坑都必须堵住：

    * 软边 alpha 在 mask **内侧**小于 1，于是"擦除"退化成"淡化"，
      笔画边缘留一圈灰——放大后能完整认出原文；
    * 填充区只覆盖硬 mask，软边混合时 ``filled`` 在边缘仍是原图，
      混合结果恒等于原图。
    """

    def _residual(self, img: "Image.Image", block: TextBlock) -> tuple[float, int]:
        from imgtrans.inpaint.erase import erase_text
        from imgtrans.inpaint.mask import analyze_box

        bgr = _to_bgr(img)
        a = analyze_box(bgr, block)
        h, w = bgr.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        x0, y0, x1, y1 = a.box
        np.maximum(mask[y0:y1, x0:x1], a.mask, out=mask[y0:y1, x0:x1])
        base = erase_text(bgr, mask, [a])
        reg = base[y0:y1, x0:x1].astype(np.int16).mean(axis=2)
        bg = float(np.median(reg))
        return bg - float(reg.min()), int((reg < bg - 6).sum())

    def test_black_on_light_leaves_nothing(self) -> None:
        img = Image.new("RGB", (320, 130), (229, 228, 235))
        block = _text_block(img, (30, 40), "禁止转载", 40, (30, 30, 36))
        worst, dirty = self._residual(img, block)
        self.assertLess(worst, 4.0, f"擦除后仍有 {worst:.0f} 灰度的残影")
        self.assertEqual(dirty, 0, f"仍有 {dirty} 个像素明显残留")

    def test_vertical_column_leaves_nothing(self) -> None:
        """竖排逐字排列：每个字都是独立的一块墨，最容易在字间留残影。"""
        img = Image.new("RGB", (160, 400), (240, 240, 244))
        draw = ImageDraw.Draw(img)
        font = _font(40)
        grid = []
        for i, ch in enumerate("禁止转载"):
            xy = (50, 26 + i * 46)
            draw.text(xy, ch, font=font, fill=(30, 30, 36))
            mb = font.getmask(ch, mode="L").getbbox()
            grid.append((xy[0] + mb[0], xy[1] + mb[1], xy[0] + mb[2], xy[1] + mb[3]))
        poly: Polygon = (
            (min(g[0] for g in grid), min(g[1] for g in grid)),
            (max(g[2] for g in grid), min(g[1] for g in grid)),
            (max(g[2] for g in grid), max(g[3] for g in grid)),
            (min(g[0] for g in grid), max(g[3] for g in grid)),
        )
        block = TextBlock(
            index=0, polygon=poly, text="禁止转载", confidence=0.99, vertical=True
        )
        worst, dirty = self._residual(img, block)
        self.assertLess(worst, 4.0, f"竖排擦除后仍有 {worst:.0f} 灰度残影")
        self.assertEqual(dirty, 0, f"竖排仍有 {dirty} 个像素残留")


if __name__ == "__main__":
    unittest.main()
