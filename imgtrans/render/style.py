"""从原图反推文字样式。

要还原的样式里，颜色来自 :mod:`imgtrans.inpaint.mask` 的 mask 统计，
这里补三样位图上能估但容易估错的东西：

* **描边**：海报/游戏 UI 大量使用描边字。描边在擦除时一起被擦掉，
  回填不重绘的话，译文会"变细变淡"，与周围其他文字明显不一致。
  判据是"离字色和底色都远、且**不落在两者混色线上**"的像素簇——有描边时
  存在这样一种颜色，没有描边时抗锯齿像素都落在那条混色线上。
* **字重**：用最大内切圆半径（距离变换）估计笔画粗细，再除以字高。
  粗体与常规体的比值差异明显，但阈值只能是经验值。
* **对齐**：单行文字本无法判断对齐方式，只有当检测框明显宽于文字时，
  左右留白比例才携带信息。框紧贴字形时统一按左对齐处理。

所有估计都**允许失败**：拿不准时返回保守值（无描边、常规体、左对齐），
因为"少还原一样"比"还原错一样"视觉上更容易被接受。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from ..types import Align, BlockStyle, RGB, TextBlock, Weight
from ..inpaint.mask import BoxAnalysis, color_clusters

logger = logging.getLogger(__name__)

#: 描边色与前景色的最小距离（RGB 空间欧氏）。低于此值认为是抗锯齿混色而非描边。
STROKE_MIN_DELTA = 45.0

#: 描边色与背景色的最小距离。描边必须同时区别于文字和背景才有意义。
#:
#: 取 55 而不是 30：太接近背景的"描边"画上去也看不出来，只会让字形边缘发虚。
#: 实测黄底黑字的标题块被量出一条 ``(239,210,112)`` 的"描边"——那其实就是
#: 黄底本身（离背景 43），画成 1px 只会让字缘发脏。
STROKE_MIN_DELTA_BG = 55.0

#: 描边色偏离「字色—底色」混色线的最小距离。
#:
#: 这是区分"真描边"与"抗锯齿"的关键。抗锯齿像素永远是字色与底色的线性混合，
#: 在 RGB 空间里落在两者的连线上；描边是另一种独立颜色，一般明显偏离那条线
#: （实测黄字 + 暗红描边 + 蓝底，暗红偏离连线 162，抗锯齿偏离接近 0）。
#: 没有这条判据，50/50 的过渡色会被误判成描边——它离字色和底色都够远。
STROKE_MIN_OFFLINE = 30.0

#: 描边像素里最大的那个颜色簇，至少要占候选像素的这么多。
#: 描边是整圈均匀色块，占比很高；零散的过渡色不是。
STROKE_MIN_COHERENCE = 0.4

#: 描边宽度上限（原图像素）。超过这个值基本是把别的元素认成了描边。
STROKE_MAX_WIDTH = 8

#: 认定描边所需的背景平坦度上限（``BoxAnalysis.bg_std``）。
#:
#: 描边是一条**紧贴字形、颜色均匀**的窄带。判据里"离字色和底色都远"这一条
#: 在纯色/渐变背景上很有力，但在照片背景上几乎必然被满足——照片里到处都是
#: 与"背景中位色"无关的颜色。实测人像照上的白字：紧邻的暗色区域被认成
#: 黑色描边（宽 2），金色标题的**字色本身**被认成描边（宽 6），回填后
#: 每个字都套了一圈白边。
#:
#: 所以背景不干净时直接放弃描边——这与本模块"拿不准就返回保守值"的原则一致：
#: 少画一圈描边只是"变细了"，画错一圈描边是肉眼立刻能看出来的。
STROKE_MAX_BG_STD = 12.0

#: 笔画宽度 / 字高 超过该比例判定为粗体。经验值：常规体约 0.08~0.11，
#: 粗体 0.14~0.20。取 0.125 作为分界。
BOLD_RATIO = 0.125

#: 判断"框明显宽于文字"的阈值。留白小于该比例时对齐信息不可靠。
ALIGN_MIN_MARGIN = 0.08


def _point_segment_distance(c: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    """颜色 ``c`` 到线段 ``a-b`` 的最短距离。"""
    ab = b - a
    denom = float(np.dot(ab, ab))
    if denom <= 1e-6:
        return float(np.linalg.norm(c - a))
    t = float(np.dot(c - a, ab)) / denom
    t = min(1.0, max(0.0, t))
    return float(np.linalg.norm(c - (a + t * ab)))


def _stroke_width(
    roi_bgr: np.ndarray,
    mask: np.ndarray,
    fg_bgr: np.ndarray,
    area: int,
) -> int:
    """由描边像素面积和字形轮廓周长反推描边宽度。

    描边是贴着字形外围的一圈带子，面积 ≈ 宽度 × 轮廓周长，所以
    ``宽度 = 面积 / 周长``。周长取"字面"轮廓——即 mask 内与前景色接近的像素
    的外轮廓，它正好是描边的内边界。
    """
    if area <= 0:
        return 1
    diff = np.linalg.norm(roi_bgr.astype(np.float32) - fg_bgr, axis=2)
    core = ((mask > 0) & (diff <= STROKE_MIN_DELTA)).astype(np.uint8)
    if not core.any():
        return 1
    contours, _ = cv2.findContours(core, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    perim = sum(cv2.arcLength(c, True) for c in contours)
    if perim <= 0:
        return 1
    return int(min(STROKE_MAX_WIDTH, max(1, round(area / perim))))


def _detect_stroke(
    roi_bgr: np.ndarray, mask: np.ndarray, fg: RGB, bg: RGB
) -> tuple[RGB | None, int]:
    """检测描边颜色与宽度。

    做法：在 mask 内找"离字色和底色都远"的像素，它们的最大颜色簇若是
    **偏离字色—底色连线**的均匀色块，那就是描边。三步缺一不可：

    1. **离字色、离底色都远**：排除"字面"与"背景"本身；
    2. **最大簇占比够高**：描边是整圈均匀色，过渡色是零散分布；
    3. **偏离混色线**：抗锯齿像素落在连线上，真描边不会。

    **不能用 mask 的边缘带**（旧做法）：mask 做过 ``DILATE_PX`` 膨胀，
    它的最外一圈按构造就在字形外侧的**背景**上——实测边缘带中位色与背景色
    距离为 0，于是"有没有描边"永远判成没有，描边字回填后一律变细变淡。
    """
    if mask.size == 0 or not (mask > 0).any():
        return None, 0

    pts = roi_bgr[mask > 0].astype(np.float32)  # BGR
    fg_bgr = np.array(fg[::-1], dtype=np.float32)
    bg_bgr = np.array(bg[::-1], dtype=np.float32)

    d_fg = np.linalg.norm(pts - fg_bgr, axis=1)
    d_bg = np.linalg.norm(pts - bg_bgr, axis=1)
    cand = pts[(d_fg > STROKE_MIN_DELTA) & (d_bg > STROKE_MIN_DELTA_BG)]
    if len(cand) < 16:
        return None, 0

    clusters = color_clusters(cand, STROKE_MIN_COHERENCE)
    if not clusters:
        return None, 0
    color_bgr, share = max(clusters, key=lambda item: item[1])
    color = np.asarray(color_bgr, dtype=np.float32)

    if _point_segment_distance(color, fg_bgr, bg_bgr) < STROKE_MIN_OFFLINE:
        return None, 0

    area = int(round(share * len(cand)))
    width = _stroke_width(roi_bgr, mask, fg_bgr, area)
    logger.debug(
        "stroke: color=%s width=%d 占候选 %.0f%%（偏离混色线 %.0f）",
        color_bgr[::-1],
        width,
        share * 100,
        _point_segment_distance(color, fg_bgr, bg_bgr),
    )
    return (int(color_bgr[2]), int(color_bgr[1]), int(color_bgr[0])), width


def _estimate_weight(mask: np.ndarray, text_height: float) -> Weight:
    """用距离变换估计笔画粗细，判断是否粗体。"""
    if mask.size == 0 or not (mask > 0).any() or text_height <= 0:
        return "regular"

    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    # 最大内切圆半径 ≈ 笔画半宽
    stroke_half = float(dist.max())
    if stroke_half <= 0:
        return "regular"
    stroke_width = stroke_half * 2.0
    ratio = stroke_width / text_height

    logger.debug("weight: stroke=%.1f height=%.1f ratio=%.3f", stroke_width, text_height, ratio)
    return "bold" if ratio > BOLD_RATIO else "regular"


def _estimate_align(mask: np.ndarray, box_width: int) -> Align:
    """根据 mask 在框内的左右留白判断对齐方式。

    框紧贴文字时（两侧留白都 < ALIGN_MIN_MARGIN）无法判断，按左对齐——
    这也是绝大多数 UI/海报的实际选择。
    """
    if mask.size == 0 or not (mask > 0).any() or box_width <= 0:
        return "left"

    cols = np.where((mask > 0).any(axis=0))[0]
    if len(cols) == 0:
        return "left"

    left_margin = float(cols[0]) / box_width
    right_margin = float(box_width - 1 - cols[-1]) / box_width

    if left_margin < ALIGN_MIN_MARGIN and right_margin < ALIGN_MIN_MARGIN:
        return "left"
    if abs(left_margin - right_margin) <= ALIGN_MIN_MARGIN:
        return "center"
    return "left" if left_margin < right_margin else "right"


def analyze_style(
    image_bgr: np.ndarray,
    block: TextBlock,
    analysis: BoxAnalysis,
) -> BlockStyle:
    """综合 mask 分析结果，产出该块的样式估计。"""
    x0, y0, x1, y1 = analysis.box
    roi = image_bgr[y0:y1, x0:x1]

    if roi.size == 0 or analysis.mask.size == 0 or not (analysis.mask > 0).any():
        # 分割失败：不可渲染，只擦除
        return BlockStyle(
            fg_color=analysis.fg_color,
            bg_color=analysis.bg_color,
            renderable=False,
        )

    stroke_color, stroke_width = _detect_stroke(
        roi, analysis.mask, analysis.fg_color, analysis.bg_color
    )
    if analysis.bg_std > STROKE_MAX_BG_STD:
        # 背景是照片/纹理时，描边判据无法与"旁边的另一块颜色"区分，
        # 宁可不画（见 STROKE_MAX_BG_STD 的说明）。
        stroke_color, stroke_width = None, 0
    # 笔画粗细比的分母必须是**字形高度**：整段高会稀释笔画比，行框高又会引入
    # 上下留白。analysis.text_height 是 mask 实测的墨迹高度，最接近真值。
    stroke_ref_height = (
        analysis.text_height
        or (block.line_height if block.line_height > 0 else block.height)
    )
    weight = _estimate_weight(analysis.mask, stroke_ref_height)
    align = _estimate_align(analysis.mask, x1 - x0)

    # 竖排块现在可以竖排回填了（见 render.text._layout_vertical），
    # 不再需要"只擦不画"。留 ``renderable`` 这个开关给真正的特例：
    # 样式完全无法估计、或者艺术字/手写体这种重绘必然更丑的情况。
    renderable = True

    return BlockStyle(
        fg_color=analysis.fg_color,
        bg_color=analysis.bg_color,
        stroke_color=stroke_color,
        stroke_width=stroke_width,
        align=align,
        weight=weight,
        renderable=renderable,
        text_height=analysis.text_height,
    )
