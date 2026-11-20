"""回填沙盘：用合成图集离线验证"擦除 + 样式 + 回填"的通用性。

为什么不走完整管线：OCR + 翻译一次要 60 秒以上，而且每次输入都不同，
无法稳定复现某一种排版失败。这里直接构造 :class:`TextBlock`，
把"擦除 → 样式 → 回填"三段单独跑起来，秒级出图、场景完全可控。

每个场景声明：源图、文字块、译文、以及**期望**（对齐/方向/是否该保住单行）。
沙盘只负责出图和打印实测参数，判定交给人眼和 :mod:`tests.test_render_general`。

用法::

    python scripts/render_lab.py                 # 跑全部场景
    python scripts/render_lab.py slanted vertical  # 只跑名字含这些词的场景
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgtrans.inpaint.erase import erase_text  # noqa: E402
from imgtrans.inpaint.mask import analyze_box  # noqa: E402
from imgtrans.render.fonts import FontBook  # noqa: E402
from imgtrans.render.style import analyze_style  # noqa: E402
from imgtrans.render.text import render_blocks  # noqa: E402
from imgtrans.types import TextBlock  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "lab"
FONTS = FontBook()


# --------------------------------------------------------------------------
# 画字工具
# --------------------------------------------------------------------------


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _rotated_poly(
    x: float, y: float, w: float, h: float, angle_deg: float, cx: float, cy: float
) -> tuple:
    """把轴对齐矩形绕 (cx, cy) 旋转，返回四点（顺时针，与 PaddleOCR 一致）。"""
    rad = math.radians(angle_deg)
    cos, sin = math.cos(rad), math.sin(rad)
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    out = []
    for px, py in pts:
        dx, dy = px - cx, py - cy
        out.append((cx + dx * cos - dy * sin, cy + dx * sin + dy * cos))
    return tuple(out)


def put_text(
    img: Image.Image,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: tuple[int, int, int],
    *,
    lang: str = "zh",
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int] | None = None,
    angle: float = 0.0,
    alpha: int = 255,
    idx: int = 0,
) -> TextBlock:
    """在图上画一段文字并返回对应的 TextBlock。

    ``angle`` 非 0 时先在透明层绘制再旋转贴回，polygon 用旋转后的真实四点
    ——这样才能覆盖"倾斜文字"这一大类输入（海报斜标题、水印、拍摄件）。
    """
    font = FONTS.get_font(lang, size)

    if abs(angle) < 0.01:
        draw = ImageDraw.Draw(img)
        if alpha < 255:
            layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).text(
                xy, text, font=font, fill=(*color, alpha),
                stroke_width=stroke_width, stroke_fill=stroke_fill,
            )
            img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))
        else:
            draw.text(
                xy, text, font=font, fill=color,
                stroke_width=stroke_width, stroke_fill=stroke_fill,
            )
        sw = stroke_width
        # 用 textbbox 而不是 font.getmask().getbbox()：后者的坐标是**位图内**
        # 的相对位置，不带基线与 ascender 偏移，当成绝对坐标用会让整个框
        # 上移一个 ascent（实测 8px），框底露出框外、字高也被量小，
        # 于是字号一路偏小 20%。textbbox 与实际绘制范围吻合（48→84 vs 48→83）。
        bbox = draw.textbbox(xy, text, font=font, stroke_width=sw)
        ix0, iy0, ix1, iy1 = bbox
        poly = ((ix0, iy0), (ix1, iy0), (ix1, iy1), (ix0, iy1))
    else:
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        tb = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad = stroke_width + 4
        layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(
            (pad - tb[0], pad - tb[1]), text, font=font, fill=(*color, alpha),
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )
        rotated = layer.rotate(-angle, resample=Image.BICUBIC, expand=True)
        cx, cy = xy[0] + tw / 2, xy[1] + th / 2
        left = int(round(cx - rotated.width / 2))
        top = int(round(cy - rotated.height / 2))
        base = img.convert("RGBA")
        base.alpha_composite(rotated, (left, top))
        img.paste(base.convert("RGB"), (0, 0))
        # polygon 必须是**旋转后的四点**，而不是展开图的外接框。
        # 真实 OCR（PaddleOCR）对倾斜文字返回的就是旋转四边形，
        # 用外接框会把 angle 抹成 0，测出来的"倾斜支持"就是假的。
        poly = _rotated_poly(cx - tw / 2, cy - th / 2, tw, th, angle, cx, cy)

    return TextBlock(
        index=idx,
        polygon=poly,  # type: ignore[arg-type]
        text=text,
        confidence=0.99,
        line_height=float(size * 1.2),
    )


def new_canvas(w: int, h: int, color=(229, 228, 235)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


# --------------------------------------------------------------------------
# 场景定义
# --------------------------------------------------------------------------


@dataclass
class Scene:
    name: str
    image: Image.Image
    blocks: list[TextBlock]
    translations: list[str]

    def run(self, lang: str = "en"):
        img = cv2.cvtColor(np.array(self.image.convert("RGB")), cv2.COLOR_RGB2BGR)
        h, w = img.shape[:2]
        analyses, keep_b, keep_t = [], [], []
        for b, t in zip(self.blocks, self.translations):
            try:
                a = analyze_box(img, b)
            except ValueError as exc:
                print(f"    ! block {b.index} 分析失败: {exc}")
                continue
            if a.ink_ratio <= 0.0:
                print(f"    ! block {b.index} 未分割出文字像素 ({b.text[:12]!r})")
                continue
            analyses.append(a)
            keep_b.append(b)
            keep_t.append(t)
        if not analyses:
            return img, img, []

        mask = np.zeros((h, w), np.uint8)
        for a in analyses:
            x0, y0, x1, y1 = a.box
            region = mask[y0:y1, x0:x1]
            np.maximum(region, a.mask, out=region)
        base = erase_text(img, mask, analyses)
        styles = [analyze_style(img, b, a) for b, a in zip(keep_b, analyses)]
        rendered, reports = render_blocks(base, keep_b, keep_t, styles, FONTS, lang)
        return base, rendered, reports


def scene_flat_light() -> Scene:
    im = new_canvas(360, 130)
    b = put_text(im, (24, 40), "限时优惠", 40, (28, 28, 32))
    return Scene("flat_light", im, [b], ["Limited time offer"])


def scene_dark_card() -> Scene:
    im = new_canvas(360, 130, (58, 58, 62))
    b = put_text(im, (24, 40), "温馨提示", 34, (240, 240, 245))
    return Scene("dark_card", im, [b], ["Gentle reminder"])


def scene_red_button() -> Scene:
    im = new_canvas(360, 130, (200, 60, 66))
    b = put_text(im, (38, 42), "立即购买", 36, (255, 255, 255))
    return Scene("red_button", im, [b], ["Buy now"])


def scene_gradient() -> Scene:
    x = np.linspace(0, 1, 360)[None, :].repeat(130, axis=0)
    rgb = np.stack([210 + 30 * x, 200 + 40 * x, 235 - 20 * x], axis=2).astype(np.uint8)
    im = Image.fromarray(rgb, "RGB")
    b = put_text(im, (24, 42), "新品上市", 38, (30, 30, 60))
    return Scene("gradient", im, [b], ["New arrival"])


def scene_photo() -> Scene:
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 26, (130, 360, 3)).astype(np.int16) + 128
    blur = cv2.GaussianBlur(noise.astype(np.uint8), (0, 0), 3)
    im = Image.fromarray(cv2.cvtColor(blur, cv2.COLOR_BGR2RGB), "RGB")
    b = put_text(im, (24, 42), "实拍图片", 38, (250, 250, 250))
    return Scene("photo", im, [b], ["Real shot"])


def scene_centered_title() -> Scene:
    """宽框、文字居中——真实海报最常见，也最容易被画成左对齐。"""
    im = new_canvas(420, 120)
    draw = ImageDraw.Draw(im)
    font = FONTS.get_font("zh", 42)
    text = "商品参数"
    tb = draw.textbbox((0, 0), text, font=font)
    tw = tb[2] - tb[0]
    x = (420 - tw) // 2
    # 块框比文字宽得多：对齐信息就藏在两侧留白里
    poly = ((60, 34), (360, 34), (360, 88), (60, 88))
    draw.text((x, 38), text, font=font, fill=(25, 25, 30))
    block = TextBlock(index=0, polygon=poly, text=text, confidence=0.99, line_height=50.0)
    return Scene("centered_title", im, [block], ["Product parameters"])


def scene_right_price() -> Scene:
    """右对齐的金额：左对齐回填会让数字离开货币符号的位置。"""
    im = new_canvas(400, 110)
    draw = ImageDraw.Draw(im)
    font = FONTS.get_font("zh", 34)
    draw.text((250, 34), "￥1,299", font=font, fill=(190, 40, 40))
    block = TextBlock(
        index=0,
        polygon=((236, 30), (396, 30), (396, 80), (236, 80)),
        text="￥1,299",
        confidence=0.99,
        line_height=41.0,
    )
    return Scene("right_price", im, [block], ["$1,299"])


def scene_tiny() -> Scene:
    im = new_canvas(300, 90)
    b = put_text(im, (16, 34), "样品仅供参考", 13, (60, 60, 66))
    return Scene("tiny", im, [b], ["Sample only"])


def scene_huge_title() -> Scene:
    im = new_canvas(760, 240)
    b = put_text(im, (30, 50), "夏 日 特 惠", 96, (20, 20, 26))
    return Scene("huge_title", im, [b], ["SUMMER SALE"])


def scene_slanted8() -> Scene:
    """倾斜 8° 的手写体/贴纸风格标题。"""
    im = new_canvas(420, 170)
    b = put_text(im, (70, 55), "今日特价", 44, (30, 30, 36), angle=8.0)
    return Scene("slanted8", im, [b], ["Today's deal"])


def scene_watermark45() -> Scene:
    """45° 半透明水印。"""
    im = new_canvas(400, 240, (240, 240, 244))
    b = put_text(im, (90, 80), "样张", 48, (150, 150, 160), angle=45.0, alpha=170)
    return Scene("watermark45", im, [b], ["SAMPLE"])


def scene_vertical_zh() -> Scene:
    """竖排中文：每个字单独一行，自上而下。"""
    im = new_canvas(150, 420)
    draw = ImageDraw.Draw(im)
    font = FONTS.get_font("zh", 40)
    text = "禁止转载"
    grid = []
    for i, ch in enumerate(text):
        xy = (50, 26 + i * 46)
        draw.text(xy, ch, font=font, fill=(30, 30, 36))
        # 用每个字的**实际排版框**并集作 polygon（textbbox 与绘制范围一致，
        # getmask 的相对坐标不含基线偏移，会让框整体上移）。
        grid.append(draw.textbbox(xy, ch, font=font))
    x0 = min(g[0] for g in grid)
    y0 = min(g[1] for g in grid)
    x1 = max(g[2] for g in grid)
    y1 = max(g[3] for g in grid)
    block = TextBlock(
        index=0,
        polygon=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        text=text,
        confidence=0.99,
        vertical=True,
        line_height=float(y1 - y0) / len(text),
    )
    return Scene("vertical_zh", im, [block], ["No repost"])


def scene_outline() -> Scene:
    """描边字（游戏/促销 UI 常见）。"""
    im = new_canvas(420, 160, (40, 90, 170))
    b = put_text(
        im, (40, 40), "超级折扣", 52, (255, 230, 60),
        stroke_width=4, stroke_fill=(120, 20, 20),
    )
    return Scene("outline", im, [b], ["MEGA SALE"])


def scene_paragraph() -> Scene:
    """多行段落：4 行合并成一块。行距 1.7 倍字号——真实正文的常见值。"""
    im = new_canvas(460, 210)
    draw = ImageDraw.Draw(im)
    font = FONTS.get_font("zh", 21)
    lines = ["拍摄时受光线角度显示器色彩偏", "差及个人感知差异影响，实物颜色", "可能与图片存在轻微色差，请以", "收到的实物为准。"]
    pitch = 36
    for i, ln in enumerate(lines):
        draw.text((24, 18 + i * pitch), ln, font=font, fill=(45, 45, 50))
    block = TextBlock(
        index=0,
        polygon=((20, 14), (440, 14), (440, 14 + 3 * pitch + 26), (20, 14 + 3 * pitch + 26)),
        text="".join(lines),
        confidence=0.99,
        line_height=26.0,
        line_count=4,
    )
    return Scene(
        "paragraph", im, [block],
        ["Colors may differ slightly from the actual product due to lighting, "
         "screen settings and personal perception. Please refer to the item received."],
    )


def scene_dense_grid() -> Scene:
    """密集表格：标签与值两列，检验栏间距与不越界。"""
    im = new_canvas(520, 200)
    draw = ImageDraw.Draw(im)
    font_l = FONTS.get_font("zh", 19)
    font_v = FONTS.get_font("zh", 19)
    rows = [("产品名称", "决明子磁石护颈枕"), ("产品等级", "合格品"), ("安全类别", "B类")]
    blocks = []
    for i, (k, v) in enumerate(rows):
        y = 20 + i * 58
        draw.text((24, y), k, font=font_l, fill=(50, 50, 56))
        draw.text((220, y), v, font=font_v, fill=(30, 30, 36))
        blocks.append(
            TextBlock(
                index=len(blocks),
                polygon=((22, y - 2), (140, y - 2), (140, y + 26), (22, y + 26)),
                text=k, confidence=0.99, line_height=23.0,
            )
        )
        blocks.append(
            TextBlock(
                index=len(blocks),
                polygon=((218, y - 2), (470, y - 2), (470, y + 26), (218, y + 26)),
                text=v, confidence=0.99, line_height=23.0,
            )
        )
        draw.line((16, y + 40, 504, y + 40), fill=(210, 210, 216), width=1)
    trans = [
        "Product name", "Cassia Seed Magnet Neck Pillow",
        "Product grade", "Qualified product",
        "Security category", "Category B",
    ]
    return Scene("dense_grid", im, blocks, trans)


def scene_crowded_row() -> Scene:
    """同一行两个紧邻的块，译文都比原文长得多——最容易互相盖住的情形。"""
    im = new_canvas(430, 110)
    draw = ImageDraw.Draw(im)
    font = FONTS.get_font("zh", 26)
    draw.text((16, 36), "新品上市", font=font, fill=(35, 35, 40))
    draw.text((132, 36), "立即抢购", font=font, fill=(35, 35, 40))
    blocks = [
        TextBlock(
            index=0,
            polygon=((14, 34), (122, 34), (122, 66), (14, 66)),
            text="新品上市", confidence=0.99, line_height=31.0,
        ),
        TextBlock(
            index=1,
            polygon=((130, 34), (238, 34), (238, 66), (130, 66)),
            text="立即抢购", confidence=0.99, line_height=31.0,
        ),
    ]
    return Scene(
        "crowded_row", im, blocks,
        ["New arrivals just landed", "Buy now before it's gone"],
    )


def scene_tiny_image() -> Scene:
    """200px 小图：所有绝对像素常量在这种尺度下都过大。"""
    im = new_canvas(200, 80, (255, 255, 255))
    b = put_text(im, (8, 22), "店铺公告", 16, (40, 40, 46))
    return Scene("tiny_image", im, [b], ["Shop notice"])


def scene_huge_image() -> Scene:
    """2000px 大图：9px 下限、6px 边距在这个尺度下等于没有。"""
    im = new_canvas(2000, 400, (245, 245, 248))
    b = put_text(im, (120, 120), "全场包邮 满减优惠", 90, (30, 30, 36))
    return Scene("huge_image", im, [b], ["Free shipping storewide with tiered discounts"])


SCENES = [
    scene_flat_light,
    scene_dark_card,
    scene_red_button,
    scene_gradient,
    scene_photo,
    scene_centered_title,
    scene_right_price,
    scene_tiny,
    scene_huge_title,
    scene_slanted8,
    scene_watermark45,
    scene_vertical_zh,
    scene_outline,
    scene_paragraph,
    scene_dense_grid,
    scene_crowded_row,
    scene_tiny_image,
    scene_huge_image,
]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _stack(images: list[np.ndarray]) -> np.ndarray:
    """等高并排（不同高度时按最大高度补白）。"""
    h = max(im.shape[0] for im in images)
    out = []
    for im in images:
        if im.shape[0] < h:
            pad = np.full((h - im.shape[0], im.shape[1], 3), 255, np.uint8)
            im = np.vstack([im, pad])
        out.append(im)
        out.append(np.full((h, 6, 3), 120, np.uint8))
    return np.hstack(out[:-1])


def main(argv: list[str]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    picked = [s for s in SCENES if not argv or any(k in s.__name__ for k in argv)]
    if not picked:
        print(f"没有匹配的场景: {argv}")
        return 1

    failures = 0
    for factory in picked:
        scene = factory()
        base, rendered, reports = scene.run()
        name = scene.name
        panel = _stack([cv2.cvtColor(np.array(scene.image.convert("RGB")), cv2.COLOR_RGB2BGR),
                        base, rendered])
        cv2.imwrite(str(OUT_DIR / f"{name}.png"), panel)
        print(f"\n[{name}]  {scene.image.size[0]}x{scene.image.size[1]}")
        if not reports:
            print("    !! 无任何回填报告")
            failures += 1
            continue
        for r in reports:
            flag = " " if r.painted else "!"
            if not r.painted:
                failures += 1
            print(
                f"  {flag} #{r.index:<2} size={r.font_size:<3} lines={r.lines} "
                f"fit={r.fit_mode:<18} {r.source[:10]!r} -> {r.target[:34]!r} {r.note}"
            )
    print(f"\n输出目录: {OUT_DIR}")
    print(f"未回填块数: {failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
