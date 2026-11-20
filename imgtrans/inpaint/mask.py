"""文字像素 mask 生成。

这是整套擦除质量的根源。核心取舍：

**不要直接把检测框填成实心矩形再擦掉**——那样等于把文字的"底板"一起挖掉，
纯色背景上看不出来，但有纹理/渐变/阴影的背景上会留下一块明显的补丁。

正确做法是在框内做一次局部分割，只把**文字笔画像素**标成待擦除区域：

1. 取框外一圈像素作为"背景色"参考（PaddleOCR 的框通常紧贴字形，
   框内几乎没有纯背景，所以参考环必须取在**框外**）；
2. 计算每个像素与背景参考的最大通道差，得到"离背景多远"；
3. 对该差值图做 Otsu 自适应阈值——不同图片的对比度差异极大，
   固定阈值在浅色文字上会漏、在深色背景上会糊；
4. 加一个最小对比度下限，避免在近乎纯色的区域把噪声当文字；
5. 形态学闭运算 + 膨胀，覆盖抗锯齿边缘（少擦 1px 就会留下文字"描边"）。

输出是 ``uint8`` 的 0/255 图，与原图同尺寸。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

from ..types import RGB, Box, TextBlock

logger = logging.getLogger(__name__)

#: 背景参考环的宽度（像素）。太窄容易采到文字抗锯齿像素，太宽会跨到别的元素。
RING_WIDTH = 3

#: 膨胀像素数。抗锯齿边缘通常 1~2px，取 2 能覆盖且不过度侵蚀背景。
DILATE_PX = 2

#: 最小对比度（0~255）。低于此值认为"这片区域没有可靠文字边缘"。
#: 15 是经验值：能过滤 JPEG 噪声，又不至于漏掉浅灰文字。
MIN_CONTRAST = 15

#: 判定"某个候选色是背景"时，它必须至少占据框内这么多面积。
#:
#: 背景的定义就是"字周围那片更大的颜色"。取 0.5 而不是 0.75 是有原因的：
#: 原先的 0.75 是"反了才纠正"的事后补丁，而这里是在**选**背景——一旦选错，
#: mask、填充色、前景色这些下游结果会一起错，靠事后反转救不回来。
#: 实测标题块：错选成黑色边框时框内 74.9% 都"不是背景"，0.75 的阈值刚好漏过。
#:
#: **这个判据单独用会被描边字骗过去**：字面 + 4px 描边的墨迹能占到框内 73%，
#: 于是"真实背景"因为占比不足被否掉；而此时框内中位色恰好落在字色与描边色
#: **之间**，对三簇都有一定距离，分出来的墨水占比反而只有 48%，看起来"更像背景"。
#: 那个混色在图上根本不存在，填进去就是一块脏斑。所以现在它只作为
#: :func:`_choose_bg` 覆盖参考环时的**附加校验**，主判据是框周一致性。
MAX_INK_RATIO_FOR_BG = 0.5

#: 框周一致性判定的通道容差（0~255）。
BORDER_TOL = 24

#: 参考环颜色要"包住文字"到多少比例，才算被验证过的背景。
BG_TRUST_SHARE = 0.35

#: 别的颜色要顶掉参考环，在框周上的占比必须领先这么多。
#:
#: 取这么大的间距是刻意的：它只在"参考环颜色几乎不出现在框周、而另一色几乎
#: 铺满框周"时才成立，也就是描边字/紧包边框这类明确的信号。取小值会让
#: 紧贴字形的框（真实图上框周一致性能低到 0.21）被墨迹颜色顶掉。
BG_OVERRIDE_MARGIN = 0.35

#: 参与背景评选的框内颜色簇至少要占这么大比例，零星噪点不参与。
BG_CLUSTER_MIN_SHARE = 0.10

#: 用"框内非笔画像素"算背景平坦度/残差时，至少要有这么多像素才可信。
#: 太少说明 mask 把整块都盖住了（分割失败），此时只能退回参考环统计。
MIN_BG_SAMPLES = 64

#: 分析区域相对 OCR 框的外扩量（左/上/右/下，像素）。
#:
#: PaddleOCR 的框基本贴合字形，但**中文的字形下缘常被截掉 1~4px**（"原""理"
#: 这类字的竖笔收尾、以及带描边的字）。被截掉的像素根本不在 ROI 里，
#: mask 自然是 0，擦完就在字脚留下一排断续黑点——实测标题块每字下缘都有。
#: 向下多给几像素是因为中文字形的视觉重心偏上，收笔基本在下方；
#: 左右各 1px 覆盖抗锯齿横向溢出。
#:
#: 外扩的副作用是 ROI 可能跨出背景块（例如黄底标题带扩到蓝底），
#: 但接下来仍是"与背景色的差值 + Otsu"，跨出去的部分与背景差异大反而
#: 会被当成前景擦掉。所以仅靠背景色无法兜住，外扩量必须保持很小。
MASK_PAD_LEFT = 1
MASK_PAD_TOP = 1
MASK_PAD_RIGHT = 1
MASK_PAD_BOTTOM = 4


@dataclass(slots=True)
class BoxAnalysis:
    """单个文字框的分析结果，供擦除与样式还原共用。"""

    box: Box
    bg_color: RGB
    fg_color: RGB
    #: 框内文字像素占比。极低（<2%）说明分割失败或框内没字。
    ink_ratio: float
    #: 背景复杂度：背景环像素的标准差。低 = 纯色，高 = 纹理。
    bg_std: float
    mask: np.ndarray
    #: **字形**实测高度（像素），取自膨胀前的笔画像素行范围。
    #:
    #: OCR 框高含上下留白，比真实字高大 25~35%；拿框高当字号会一律偏大。
    #: 这里量的是字形本身的墨迹高度，是反推字号唯一可靠的依据。
    text_height: float = 0.0


def _ring_pixels(roi_bgr: np.ndarray, padding: int = RING_WIDTH) -> np.ndarray:
    """取 ROI 最外一圈像素（形状 ``(N, 3)``）。

    单独抽出来是因为两处都要用：估计背景色，以及计算背景复杂度
    ``bg_std``。**必须只统计这一圈**——如果对整块 ROI 求标准差，
    框内的文字会把它顶到几十，纯色背景会被误判成纹理背景，
    进而错误地触发 inpaint（慢且有色差）。
    """
    h, w = roi_bgr.shape[:2]
    if h <= 2 * padding or w <= 2 * padding:
        return roi_bgr.reshape(-1, 3)
    return np.concatenate(
        [
            roi_bgr[:padding, :, :].reshape(-1, 3),
            roi_bgr[-padding:, :, :].reshape(-1, 3),
            roi_bgr[:, :padding, :].reshape(-1, 3),
            roi_bgr[:, -padding:, :].reshape(-1, 3),
        ]
    )


def estimate_bg_color(roi_bgr: np.ndarray, padding: int = RING_WIDTH) -> RGB:
    """用图像最外一圈像素的中位数估计背景色（**返回 RGB**）。

    用中位数而非均值：环上可能压着别的元素或阴影，中位数对少量异常值免疫。
    """
    flat = _ring_pixels(roi_bgr, padding)
    median = np.median(flat, axis=0)
    return _bgr_to_rgb((int(median[0]), int(median[1]), int(median[2])))


def _bgr_to_rgb(color: RGB) -> RGB:
    """BGR 通道顺序 -> RGB 通道顺序。

    :class:`~imgtrans.types.BoxAnalysis` 的颜色字段对外一律是 **RGB**，
    但内部所有取色都发生在 OpenCV 的 BGR 图上，所以每次"出圈"都要翻转一次。
    这条约定一旦破了，黄色会被画成蓝色（实测踩过），而且因为三个通道都是
    0~255 的整数，**不会有任何异常抛出**，只能靠肉眼看出来。
    """
    return (color[2], color[1], color[0])


#: 前景色聚类时的量化步长（0~255 的 1/10 左右）。
#: 足以把"黄字 + 红描边"分成两簇，又不会被抗锯齿的过渡色碎片化。
FG_CLUSTER_STEP = 24

#: 参与"前景色"评选的簇至少要有这么大占比，避免选到零星噪点。
FG_CLUSTER_MIN_SHARE = 0.08

#: 量化之后再把重心相近的桶合并起来的距离。
#: 同一个字色会被抗锯齿和照片光照摊到多个桶里，不合并会各自过不了占比门槛。
CLUSTER_MERGE_TOL = 45.0


def _core_color(
    roi_bgr: np.ndarray, mask: np.ndarray, bg_bgr: tuple[int, int, int]
) -> RGB | None:
    """从笔画像素里选出真正的字色（RGB）。

    判据是**离背景色最远的那一簇**，而不是中位数、最大簇、或笔画内部——

    这几种朴素做法在实测里都翻过车：

    * **中位数**：黄字配暗红描边时直接得出暗红；
    * **最大簇**：描边沿周长铺开，4px 描边 + 6px 笔画时描边面积反而更大；
    * **笔画内部**（距离变换）：细笔画的"内部"就是抗锯齿过渡色本身，
      黑字会被量成 ``(103,103,108)`` 的浅灰，白字被量成粉色。

    而"离背景最远"对所有情形都成立：文字之所以能被看见，就是因为它与背景
    对比度最高。描边色可能比字色更浅、更接近背景，字色却一定是距离最远的那个。

    **前提是字色本身得在候选里**——这正是 :func:`color_clusters` 要先合并
    相近色桶的原因：白色文字在照片上会被摊成好几个 5%~7% 的小桶，
    每个都过不了占比门槛，于是"最远"只能从旁边的背景色里挑。

    试过再叠加"局部对比度 × 占比"加权，实测**更差**：浅色底上的
    ``B类`` 被选成中灰、纯蓝底上的白字被选成淡蓝（抗锯齿过渡色簇反而得分高）。
    单纯的距离在对齐了候选之后已经够用，不必再加参数。
    """
    if mask.size == 0 or not (mask > 0).any():
        return None
    pts = roi_bgr[mask > 0].astype(np.float32)
    if len(pts) < 8:
        med = np.median(pts, axis=0)
        return _bgr_to_rgb((int(med[0]), int(med[1]), int(med[2])))

    bg = np.asarray(bg_bgr, dtype=np.float32)
    best: np.ndarray | None = None
    best_d = -1.0
    for color_bgr, _share in color_clusters(pts, FG_CLUSTER_MIN_SHARE):
        color = np.asarray(color_bgr, dtype=np.float32)
        d = float(np.linalg.norm(color - bg))
        if d > best_d:
            best_d, best = d, color
    if best is None:
        best = np.median(pts, axis=0)
    return _bgr_to_rgb((int(best[0]), int(best[1]), int(best[2])))


def _median_bgr(pixels: np.ndarray) -> tuple[int, int, int]:
    """一组 BGR 像素的中位色。"""
    med = np.median(pixels, axis=0)
    return (int(med[0]), int(med[1]), int(med[2]))


def _ink_binary(roi_bgr: np.ndarray, bg_bgr: tuple[int, int, int]) -> tuple[np.ndarray | None, float]:
    """按"离候选背景色多远"分割笔画，返回 ``(二值 mask, 原始墨水占比)``。

    **不做反转**：调用方要靠"原始占比"来判断这个候选色到底是不是背景。
    占比 > 0.5 意味着框内大部分像素都不像它——那它就不是背景。

    对比度过低时返回 ``(None, 0.0)``。
    """
    roi_i = roi_bgr.astype(np.int16)
    bg_arr = np.array(bg_bgr, dtype=np.int16)
    # 最大通道差：彩色文字在灰度图上可能与背景同亮度，但至少一个通道会差开
    diff = np.max(np.abs(roi_i - bg_arr), axis=2).astype(np.uint8)
    if int(diff.max()) < MIN_CONTRAST:
        return None, 0.0

    thr, _ = cv2.threshold(diff, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = max(float(thr), float(MIN_CONTRAST))
    binary = (diff >= thr).astype(np.uint8) * 255
    return binary, float(binary.mean() / 255.0)


def _border_share(
    roi_bgr: np.ndarray,
    color_bgr: tuple[int, int, int],
    tol: int = BORDER_TOL,
) -> float:
    """框内最外一圈像素里，有多大比例与给定颜色一致（最大通道差 ≤ tol）。

    这是"**这个颜色包住文字了吗**"的直接测量。背景的定义就是"围着字的那片
    颜色"，所以它必然出现在框的四周；文字再满也只会铺满某一个方向。
    """
    h, w = roi_bgr.shape[:2]
    if h == 0 or w == 0:
        return 0.0
    edge = np.concatenate([roi_bgr[0], roi_bgr[-1], roi_bgr[:, 0], roi_bgr[:, -1]])
    diff = np.abs(edge.astype(np.int16) - np.array(color_bgr, dtype=np.int16)).max(axis=1)
    return float((diff <= tol).mean())


def color_clusters(
    pixels_bgr: np.ndarray,
    min_share: float = BG_CLUSTER_MIN_SHARE,
    merge_tol: float = CLUSTER_MERGE_TOL,
) -> list[tuple[tuple[int, int, int], float]]:
    """把一批像素按颜色聚成簇，返回 ``[(中位色 BGR, 占比), ...]``（按占比降序）。

    只保留占比 ≥ ``min_share`` 的簇。低占比的簇多是抗锯齿过渡色与 JPEG 噪点，
    它们既不构成"背景"，也不构成"描边"。

    两步聚类的原因：先按 :data:`FG_CLUSTER_STEP` 量化成桶，再把**彼此相近**
    的桶合并。只做第一步会把一个字色拆成好几块——抗锯齿、JPEG 噪点、以及
    照片上轻微的光照变化都会把同一个字色摊到多个桶里。实测白色文字在照片上
    被拆成 7.2% + 7.2% 两个桶，各自都过不了"占比 ≥ 8%"的门槛，
    于是字色候选里根本没有它，最后选出来的是旁边深色的窗帘。

    候选必须是**真实存在的簇**而不是整体中位色：多色块里整体中位色会算出
    一个谁都不是的混色（实测描边字块得出 ``(54,90,120)``，是蓝底、黄字、
    暗红描边的混合）。它恰好离每一簇都不远，于是"墨水占比"显得很低，
    骗过了按占比选背景的判据——填进去就是一块图上原本不存在的脏斑。
    """
    pts = np.asarray(pixels_bgr).reshape(-1, 3)
    if len(pts) == 0:
        return []
    pts = pts.astype(np.float32)
    q = (pts // FG_CLUSTER_STEP).astype(np.int32)
    keys = (q[:, 0] << 16) | (q[:, 1] << 8) | q[:, 2]
    uniq, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)

    raw: list[list[float]] = []
    for i in range(len(uniq)):
        med = np.median(pts[inv == i], axis=0)
        raw.append([float(med[0]), float(med[1]), float(med[2]), float(counts[i])])
    if not raw:
        return []

    # 按像素数从大到小贪心合并：每个桶并入第一个"重心距离 ≤ merge_tol"的组
    raw.sort(key=lambda r: -r[3])
    groups: list[list[float]] = []
    for r in raw:
        center = np.array(r[:3], dtype=np.float32)
        for g in groups:
            if float(np.linalg.norm(center - np.array(g[:3], dtype=np.float32))) <= merge_tol:
                n = g[3] + r[3]
                for c in range(3):
                    g[c] = (g[c] * g[3] + r[c] * r[3]) / n
                g[3] = n
                break
        else:
            groups.append(list(r))

    total = float(len(pts))
    floor = max(1.0, total * min_share)
    out = [((int(g[0]), int(g[1]), int(g[2])), g[3] / total) for g in groups if g[3] >= floor]
    out.sort(key=lambda item: -item[1])
    return out


def _choose_bg(
    roi_bgr: np.ndarray,
    ring_bg: tuple[int, int, int],
) -> tuple[tuple[int, int, int], bool]:
    """选真正的背景色，返回 ``(背景色 BGR, 是否已被"包住文字"验证)``。

    参考环的取值方式（框外一圈的中位数）有一个前提：**框外一圈是背景**。
    这个前提在"文字被边框/色块紧紧包住"时会破——此时参考环整圈压在边框上，
    中位数直接变成边框色。实测电商参数图的标题"商品参数"：

    * OCR 框 ``(352,66)-(437,94)``，外面是一圈 3px 黑边框，恰好间隔 1px；
    * 参考环上黑色 558 px、浅灰 198 px，中位数取了黑色；
    * 于是"浅灰的底"被当成文字擦掉，整条标题被填成黑底浅字。

    判据是**框周一致性**（:func:`_border_share`）：背景围着字，所以它一定
    出现在框的四周；错采成边框时，边框色只出现在框外，框周上是底色。

    为什么不用"框内墨水占比 < 50%"当判据（旧做法）：它默认"墨迹占不到半块"，
    而**描边字的墨迹能到 73%**。实测蓝底黄字 + 4px 暗红描边：

    * 参考环是 100% 纯蓝（同一张图里最可靠的背景证据）；
    * 用它当背景，墨水占比 72.9% > 0.5，于是被判定"它不是背景"；
    * 退而求其次的"框内中位色"是个混色 ``(54,90,120)``，墨水占比 48.8% 通过；
    * 结果整块被填成那个不存在的混色——描边字是海报里最常见的一类，必须过。

    所以现在改成：参考环颜色在框周上出现得足够多（≥
    :data:`BG_OVERRIDE_MARGIN` 的领先优势都没有被别的颜色拿到）就采信它；
    只有当"参考环色几乎不在框周上、而另一个真实颜色几乎铺满框周"时才改判。
    """
    ring_share = _border_share(roi_bgr, ring_bg)

    best_color, best_share = ring_bg, ring_share
    for color, _share in color_clusters(roi_bgr.reshape(-1, 3)):
        s = _border_share(roi_bgr, color)
        if s > best_share:
            best_color, best_share = color, s

    # 领先幅度不够：参考环没有被别的颜色顶掉，采信参考环
    if best_share <= ring_share + BG_OVERRIDE_MARGIN:
        return ring_bg, ring_share >= BG_TRUST_SHARE

    # 要改判，还得确认这个颜色真能当背景（否则可能选中的是粗壮的描边）
    _, ink_ratio = _ink_binary(roi_bgr, best_color)
    if ink_ratio >= MAX_INK_RATIO_FOR_BG:
        return ring_bg, ring_share >= BG_TRUST_SHARE

    logger.debug(
        "参考环色 %s 在框周上只占 %.0f%%，改判为 %s（占 %.0f%%，墨水 %.0f%%）",
        ring_bg,
        ring_share * 100,
        best_color,
        best_share * 100,
        ink_ratio * 100,
    )
    return best_color, True


def _background_std(roi_bgr: np.ndarray, mask: np.ndarray, ring: np.ndarray) -> float:
    """背景复杂度：优先统计**框内非笔画像素**，样本不足时才看参考环。

    旧做法只量参考环的 std，前提同样是"环上是背景"。文字被边框紧包时
    环上是"黑边框 + 浅灰底"，std 顶到 99，纯色背景被误判成纹理，
    擦除阶段于是改走平面拟合——而拟合样本还是取自那条被污染的环，
    最终把整条标题填黑。

    笔画 mask 本身就是现成的地面真值：mask 之外的像素就是背景，量它们的
    std 既不会误碰边框，也不会误碰字形。只有 mask 盖住了大半块
    （说明分割本身不可信）时才退回参考环。
    """
    keep = mask == 0
    if int(keep.sum()) >= MIN_BG_SAMPLES:
        # **按通道分别**算 std 再取均值，不能把三通道摊平了算。
        #
        # 摊平算的是"跨通道色差"，不是"背景在空间上有多少起伏"：
        # 纯红底 (200,60,66) 摊平 std 高达 65，纯色背景被误判成纹理，
        # 擦除阶段于是走平面拟合而不是纯色填充——彩色底（红按钮、蓝底白字）
        # 全踩这个坑。按通道算，纯色的 std 是 0，渐变的 std 是真实梯度。
        vals = roi_bgr[keep].astype(np.float32)
        return float(vals.std(axis=0).mean())
    return float(ring.std())


def _clip_box(box: Box, width: int, height: int, pad: int = 0) -> Box:
    x0, y0, x1, y1 = box
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(width, x1 + pad),
        min(height, y1 + pad),
    )


def _grow_box(box: Box, width: int, height: int) -> Box:
    """按 :data:`MASK_PAD_*` 非对称外扩 OCR 框并裁进画布。"""
    x0, y0, x1, y1 = box
    return (
        max(0, x0 - MASK_PAD_LEFT),
        max(0, y0 - MASK_PAD_TOP),
        min(width, x1 + MASK_PAD_RIGHT),
        min(height, y1 + MASK_PAD_BOTTOM),
    )


def _single_line_height(core: np.ndarray) -> float:
    """从笔画 mask 估计**单行**字形高度。

    做法：按行投影切成连续的墨迹带，过滤掉明显偏矮的（标点、横线），
    取剩下的中位数。单行文本只会切出一段，直接就是字高。
    """
    if core.size == 0 or not (core > 0).any():
        return 0.0

    active = (core > 0).any(axis=1)
    bands: list[int] = []
    start: int | None = None
    for i, on in enumerate(active):
        if on and start is None:
            start = i
        elif not on and start is not None:
            bands.append(i - start)
            start = None
    if start is not None:
        bands.append(len(active) - start)

    if not bands:
        return 0.0
    tallest = max(bands)
    kept = [b for b in bands if b >= 0.4 * tallest]
    kept.sort()
    return float(kept[len(kept) // 2])


def analyze_box(image_bgr: np.ndarray, block: TextBlock) -> BoxAnalysis:
    """分析单个文字框：估计背景/前景色，并生成笔画 mask。"""
    img_h, img_w = image_bgr.shape[:2]

    # 分析区域先按 MASK_PAD_* 外扩（补回被框截掉的字形下缘），
    # 参考环再从**外扩后**的区域往外让出 RING_WIDTH——
    # 若环仍贴着原框，外扩进来的那几行字迹会落在环上，把背景色估计污染成文字色。
    inner = _grow_box(block.box, img_w, img_h)
    outer = _clip_box(inner, img_w, img_h, pad=RING_WIDTH)
    ox0, oy0, ox1, oy1 = outer
    if ox1 <= ox0 or oy1 <= oy0:
        raise ValueError(f"block {block.index} 的框超出图像范围: {block.box}")

    outer_roi = image_bgr[oy0:oy1, ox0:ox1]
    ring = _ring_pixels(outer_roi)

    ix0, iy0, ix1, iy1 = inner
    if ix1 <= ix0 or iy1 <= iy0:
        raise ValueError(f"block {block.index} 的框无效: {block.box}")
    roi = image_bgr[iy0:iy1, ix0:ix1]

    # 参考环中位色只是**候选**：文字被边框/色块紧包时，整圈环会压在边框上。
    bg_bgr, bg_surrounds = _choose_bg(roi, _median_bgr(ring))
    bg = _bgr_to_rgb(bg_bgr)  # 对外语义是 RGB

    binary, ink_ratio = _ink_binary(roi, bg_bgr)

    # Otsu 在全平坦图上会给出无意义阈值，先判对比度
    if binary is None:
        empty = np.zeros(roi.shape[:2], dtype=np.uint8)
        return BoxAnalysis(
            box=inner,
            bg_color=bg,
            fg_color=bg,
            ink_ratio=0.0,
            bg_std=float(ring.std()),
            mask=empty,
        )

    # ink_ratio 过高说明分反了（比如文字比背景面积大，或整块是深色底）。
    #
    # 但**背景已经被"包住文字"验证过**时不能反转：描边字/超粗体的墨迹本来
    # 就能占到 70% 以上，反转会把背景当字擦掉、把字留下——这是最坏的结果。
    # 只有背景来路不明（参考环在框周上都没多少像素，见 _choose_bg）时，
    # 才保留这条兜底逻辑。
    if ink_ratio > 0.75 and not bg_surrounds:
        # 反转：说明"离背景远"的反而是背景主体，取反更合理
        binary = 255 - binary
        ink_ratio = 1.0 - ink_ratio
        logger.debug("block %d ink_ratio 过高，已反转 mask", block.index)


    # 闭运算填掉笔画内部小孔，再膨胀覆盖抗锯齿。
    # 注意：**前景色必须用膨胀前的 mask 统计**——膨胀后覆盖面积可能到 70%，
    # 其中大量是背景像素，中位数会被拉向背景色，反推出的文字颜色变成浅灰。
    core = binary.copy()
    if DILATE_PX > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (DILATE_PX * 2 + 1, DILATE_PX * 2 + 1)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        binary = cv2.dilate(binary, kernel, iterations=1)

    # 字形高度：用**膨胀前**的笔画像素量（膨胀会把上下各吃进 2px）。
    #
    # 多行段落不能直接取"墨迹总高"——那里面还含着行间距，反推出的字号会偏大
    # 一整倍。按行投影切出连续的墨迹带，取**单行**带高中位数：
    # 标点符号会形成很矮的独立带，先按最大带高过滤掉。
    glyph_h = _single_line_height(core)

    # 前景色：选"离背景最远的颜色簇"，见 _core_color 的说明。
    fg_rgb = _core_color(roi, core, bg_bgr) or bg

    bg_std = _background_std(roi, binary, ring)

    return BoxAnalysis(
        box=inner,
        bg_color=bg,
        fg_color=fg_rgb,
        ink_ratio=ink_ratio,
        bg_std=bg_std,
        mask=binary,
        text_height=glyph_h,
    )


def build_text_mask(
    image_bgr: np.ndarray,
    blocks: list[TextBlock],
    *,
    feather: int = 0,
) -> tuple[np.ndarray, list[BoxAnalysis]]:
    """为整张图生成文字 mask。

    Args:
        image_bgr: BGR 原图。
        blocks: 文字块列表。
        feather: >0 时对 mask 做高斯羽化（软边），用于让擦除过渡更自然。
            注意羽化后的值不再是 0/255，调用方应按"连续权重"处理。

    Returns:
        ``(mask, analyses)``。mask 与原图同尺寸（单通道 uint8）。
    """
    img_h, img_w = image_bgr.shape[:2]
    mask = np.zeros((img_h, img_w), dtype=np.uint8)
    analyses: list[BoxAnalysis] = []

    for block in blocks:
        try:
            analysis = analyze_box(image_bgr, block)
        except ValueError as exc:
            logger.warning("跳过 block %d: %s", block.index, exc)
            continue

        if analysis.mask.size == 0 or not (analysis.mask > 0).any():
            analyses.append(analysis)
            continue

        x0, y0, x1, y1 = analysis.box
        # 多个框可能重叠，用 max 合并（不能覆盖，否则先写的会被后来的空 mask 擦掉）
        region = mask[y0:y1, x0:x1]
        np.maximum(region, analysis.mask, out=region)
        analyses.append(analysis)

    if feather > 0 and mask.any():
        k = feather * 2 + 1
        mask = cv2.GaussianBlur(mask, (k, k), 0)

    return mask, analyses
