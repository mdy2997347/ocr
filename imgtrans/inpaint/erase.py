"""文字擦除。

分三级处理，按每个文字框的背景复杂度自动选择——这一层用错方法，
结果就是"擦完留疤"：

* **L1 纯色背景**：直接填背景中位色。比 inpaint 更准（inpaint 会在纯色区
  制造轻微色差），且快一个数量级。海报、截图、UI 图大量属于这一类。
* **L2 渐变/平面背景**：对框外环像素做最小二乘平面拟合 ``c = a·x + b·y + k``，
  用拟合面填充。渐变、光照不均、轻微阴影都能贴合，比"填中位色"自然得多。
* **L3 纹理/复杂背景**：``cv2.inpaint``（Telea），从周围像素向内扩散。
  只在这一档才付出 inpaint 的代价（慢，且大面积时会有涂抹感）。

分级依据是**框外环像素的通道标准差** ``bg_std``：纯色接近 0，
渐变在个位数到十几之间，纹理通常 20 以上。

无论哪一档，最终都用羽化后的软 mask 做 alpha 混合，
避免硬边——硬边是"能看出被 P 过"最直接的原因。
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .mask import MIN_BG_SAMPLES, BoxAnalysis

logger = logging.getLogger(__name__)


def _rgb_to_bgr(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """RGB -> BGR。

    :class:`BoxAnalysis` 的颜色字段对外是 RGB（与 :mod:`imgtrans.types` 的
    ``RGB`` 标注一致），而本模块所有写入目标都是 OpenCV 的 BGR 图，
    所以每次落笔前都要翻转一次。漏掉这一层的结果不是报错，而是
    "纯色底被填成互补色"——比崩溃更难发现。
    """
    return (color[2], color[1], color[0])

#: 纯色阈值：低于此标准差按纯色填充，否则用平面拟合还原。
#:
#: 不能定得太松。合成渐变上实际测到 ``bg_std=7.2``——看着不高，但那是一个
#: 跨三行文字的大块，顶部与底部的背景色已经差了近 20 个灰度；按纯色填一条
#: 颜色，整块会出现明显色带，原文字位置留下 19 灰度、上千像素的残影。
#: 纯色背景的按通道 std 稳定在 1 以下，3.0 足够区分。
FLAT_STD = 3.0

#: 平面拟合残差阈值：低于此值认为背景可用解析面还原（纯色或渐变）。
#: 实测：合成渐变 + 高斯噪声(σ=4) 的残差约 3~5；木纹/照片纹理在 15 以上。
#: 这个值直接决定 inpaint 的使用比例——调高会省时间但可能留下"抹布感"。
RESIDUAL_STD = 8.0

#: 渐变阈值：低于此值按平面拟合填充，高于则走 inpaint。
#: 注意：这是**原始 std** 的旧阈值，现已被 :data:`RESIDUAL_STD` 取代，
#: 保留仅为兼容既有调用方，不要再用于分级判断。
GRADIENT_STD = 22.0

#: inpaint 的半径。3 是很稳的值：太小补不满笔画宽度，太大易糊成一片。
INPAINT_RADIUS = 3


def _fit_plane(values: np.ndarray, coords_x: np.ndarray, coords_y: np.ndarray) -> np.ndarray:
    """最小二乘拟合 ``c = a·x + b·y + k``，返回系数 ``[a, b, k]``（每通道一组）。

    Args:
        values: 形状 ``(N, 3)`` 的 BGR 采样值。
        coords_x: 形状 ``(N,)`` 的 x 坐标。
        coords_y: 形状 ``(N,)`` 的 y 坐标。

    Returns:
        形状 ``(3, 3)`` 的系数矩阵，行对应 B/G/R 通道。
    """
    a = np.stack([coords_x, coords_y, np.ones_like(coords_x)], axis=1).astype(np.float64)
    out = np.zeros((3, 3), dtype=np.float64)
    for ch in range(3):
        coef, *_ = np.linalg.lstsq(a, values[:, ch].astype(np.float64), rcond=None)
        out[ch] = coef
    return out


def _apply_plane(
    coef: np.ndarray, coords_x: np.ndarray, coords_y: np.ndarray
) -> np.ndarray:
    """按拟合系数算出每个采样点的预测值，形状 ``(N, 3)``。"""
    pred = np.empty((len(coords_x), 3), dtype=np.float64)
    for ch in range(3):
        a, b, k = coef[ch]
        pred[:, ch] = a * coords_x + b * coords_y + k
    return pred


def picture_residual_std(roi_bgr: np.ndarray, ring: int = 3) -> float:
    """平面拟合的残差标准差 —— 判断"这块背景能不能用解析式还原"。

    **不能用原始像素的 std**：线性渐变上下能差出几十个灰度，std 轻轻就上 30，
    但它其实是完美可拟合的（残差接近 0）。用原始 std 会把大片渐变误判成
    纹理，全部丢给 ``cv2.inpaint``——慢，而且大区域会有明显涂抹感。

    残差才反映"去掉线性趋势之后还剩多少结构"：纯色/渐变 → 接近噪声水平；
    木纹、大理石、照片 → 残差显著偏高。

    Returns:
        残差 std（对三个通道一起算）。样本不足时返回 ``inf``，
        让调用方走保守的 inpaint 分支。
    """
    vals, xs, ys = _ring_samples(roi_bgr, ring=ring)
    if len(vals) < 8:
        return float("inf")
    coef = _fit_plane(vals, xs.astype(np.float64), ys.astype(np.float64))
    pred = _apply_plane(coef, xs.astype(np.float64), ys.astype(np.float64))
    return float((vals.astype(np.float64) - pred).std())


def _ring_samples(roi_bgr: np.ndarray, ring: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """取 ROI 最外一圈像素及其坐标（坐标相对 ROI 左上角）。"""
    h, w = roi_bgr.shape[:2]
    ring = max(1, min(ring, h // 2, w // 2))

    ys, xs = [], []
    for x in range(w):
        ys.append(0)
        xs.append(x)
    for x in range(w):
        ys.append(h - 1)
        xs.append(x)
    for y in range(1, h - 1):
        ys.append(y)
        xs.append(0)
    for y in range(1, h - 1):
        ys.append(y)
        xs.append(w - 1)

    ys_arr = np.array(ys, dtype=np.int64)
    xs_arr = np.array(xs, dtype=np.int64)

    # 只用最外 ring 层，避免把紧贴边缘的抗锯齿像素当参考
    keep = (
        (ys_arr < ring)
        | (ys_arr >= h - ring)
        | (xs_arr < ring)
        | (xs_arr >= w - ring)
    )
    ys_arr, xs_arr = ys_arr[keep], xs_arr[keep]

    vals = roi_bgr[ys_arr, xs_arr].reshape(-1, 3)
    return vals, xs_arr, ys_arr


def mask_residual_std(roi_bgr: np.ndarray, mask: np.ndarray) -> float:
    """用**框内非笔画像素**做平面拟合，返回残差标准差。

    这是 :func:`picture_residual_std` 的"地面真值"版本。后者取 ROI 最外一圈
    当样本，前提是"外圈是背景"——文字被边框/色块紧包时整圈压在边框上，
    残差被顶到几十，纯色背景被误判成纹理丢给 inpaint；而 inpaint 的参考源
    同样是那圈边框，于是把整块往边框色上涂。

    笔画 mask 已经标出了哪些像素是字：剩下的就是背景，直接对它们拟合面，
    既不会误碰边框也不会误碰字形。

    Returns:
        残差 std。非笔画像素不足 :data:`~imgtrans.inpaint.mask.MIN_BG_SAMPLES`
        时返回 ``inf``，让调用方退回参考环版本。
    """
    if mask.shape != roi_bgr.shape[:2]:
        return float("inf")
    keep = mask == 0
    if int(keep.sum()) < MIN_BG_SAMPLES:
        return float("inf")

    ys, xs = np.nonzero(keep)
    xs_f = xs.astype(np.float64)
    ys_f = ys.astype(np.float64)
    vals = roi_bgr[ys, xs].astype(np.float64)
    coef = _fit_plane(vals, xs_f, ys_f)
    pred = _apply_plane(coef, xs_f, ys_f)
    return float((vals - pred).std())


def _fill_by_plane(
    image_bgr: np.ndarray,
    analysis: BoxAnalysis,
    hard_mask: np.ndarray,
    *,
    expand: int = 0,
) -> np.ndarray:
    """用平面拟合结果填充该框内的文字像素区域（返回整图副本的局部修改版）。

    ``expand`` 把填充区向外扩这么多像素。**这是软边擦除的关键**：
    笔画 mask 只覆盖 Otsu 判定为"墨"的像素，抗锯齿边缘（灰阶介于前景与背景
    之间）落在 mask 之外。若只替换 mask 内的像素，随后的 feather 混合
    在这圈边缘上是 ``原图×(1-a) + filled×a``，而 ``filled`` 那里还是原图，
    混合结果恒等于原图——**边缘的灰阶一点都没擦掉**。

    实测：竖排"禁止转载"擦除后，放大 3 倍能清楚看到完整字形轮廓，
    mask 外的那圈抗锯齿像素就是轮廓的来源。外扩 ``feather + 1`` 之后残影消失。
    """
    out = image_bgr.copy()
    x0, y0, x1, y1 = analysis.box
    roi = image_bgr[y0:y1, x0:x1]
    h, w = roi.shape[:2]
    if h == 0 or w == 0:
        return out

    # 采样必须用**框内非笔画像素**，不能用框内最外圈。
    #
    # 框是紧贴文字的，最外一圈本身就是笔画（或把字包住的那道边框）。
    # 用它当"背景样本"，拟合出来的是字与底的混合色：
    # 白字满宽的红按钮会把红底整块涂成浅粉，描边字会被涂成蓝黄渐变。
    # mask==0 的像素才是真正的背景，这里正好有现成的地面真值。
    keep = hard_mask == 0
    if int(keep.sum()) >= MIN_BG_SAMPLES:
        sy, sx = np.nonzero(keep)
        vals = roi[sy, sx]
        xs, ys = sx, sy
    else:
        vals, xs, ys = _ring_samples(roi, ring=3)
    if len(vals) < 8:
        out[y0:y1, x0:x1][hard_mask > 0] = _rgb_to_bgr(analysis.bg_color)
        return out

    if analysis.bg_std < FLAT_STD:
        fill = np.empty_like(roi)
        # BoxAnalysis 的颜色是 RGB，写进 BGR 图前必须翻转，否则纯色底会被填成互补色
        fill[:, :] = _rgb_to_bgr(analysis.bg_color)
    else:
        coef = _fit_plane(vals, xs.astype(np.float64), ys.astype(np.float64))
        gy, gx = np.mgrid[0:h, 0:w]
        fill = np.empty_like(roi)
        for ch in range(3):
            a, b, k = coef[ch]
            plane = a * gx + b * gy + k
            fill[:, :, ch] = np.clip(plane, 0, 255).astype(np.uint8)

    region = hard_mask
    if expand > 0:
        k = np.ones((expand * 2 + 1, expand * 2 + 1), np.uint8)
        region = cv2.dilate((hard_mask > 0).astype(np.uint8) * 255, k)

    target = out[y0:y1, x0:x1]
    target[region > 0] = fill[region > 0]
    return out


def erase_text(
    image_bgr: np.ndarray,
    mask: np.ndarray,
    analyses: list[BoxAnalysis],
    *,
    feather: int = 2,
) -> np.ndarray:
    """擦除所有文字像素。

    Args:
        image_bgr: BGR 原图（不会被就地修改）。
        mask: :func:`imgtrans.inpaint.mask.build_text_mask` 产出的硬 mask。
        analyses: 与 mask 对应的框分析结果，用于分级决策。
        feather: 软边宽度（像素）。0 表示硬边，不推荐。

    Returns:
        擦除后的新图。
    """
    if not mask.any():
        return image_bgr.copy()

    hard = (mask > 0).astype(np.uint8) * 255
    filled = image_bgr.copy()

    # --- L1/L2：平面或纯色填充（快、准） ---
    texture_mask = np.zeros_like(hard)
    for analysis in analyses:
        if analysis.mask.size == 0 or not (analysis.mask > 0).any():
            continue
        x0, y0, x1, y1 = analysis.box
        # 采样必须**外扩**再取环：OCR 框紧贴文字，直接拿框内的外圈采样，
        # 环上落的就是文字笔画本身，残差会被顶到 20+，把纯渐变误判成纹理。
        pad = 3
        ih, iw = filled.shape[:2]
        ex0, ey0 = max(0, x0 - pad), max(0, y0 - pad)
        ex1, ey1 = min(iw, x1 + pad), min(ih, y1 + pad)
        # 判据是**平面拟合残差**而不是原始 std：渐变背景的原始 std 能到 30+，
        # 但残差接近 0，完全可以用拟合面精确还原（比 inpaint 准且快一个数量级）。
        #
        # 优先用笔画 mask 之外的像素做拟合——那是真正的背景，
        # 不会像"框外环"那样在紧包边框时采样到边框。
        residual = mask_residual_std(filled[y0:y1, x0:x1], analysis.mask)
        if not np.isfinite(residual):
            residual = picture_residual_std(filled[ey0:ey1, ex0:ex1])
        if residual <= RESIDUAL_STD:
            filled = _fill_by_plane(
                filled, analysis, analysis.mask, expand=feather + 1
            )
            logger.debug(
                "block %d 走平面填充 (residual=%.2f, raw_std=%.1f)",
                getattr(analysis, "index", -1),
                residual,
                analysis.bg_std,
            )
        else:
            # L3 区域交给 inpaint，先累积 mask
            region = texture_mask[y0:y1, x0:x1]
            sub = analysis.mask
            if region.shape == sub.shape:
                np.maximum(region, sub, out=region)
            else:
                # 形状不一致（被裁切），退化为用全局 mask 对应区域
                region[:] = hard[y0:y1, x0:x1]

    # --- L3：纹理区 inpaint ---
    if texture_mask.any():
        # inpaint 需要"非零即修补"，但输入应包含所有文字像素，
        # 否则平面填充与 inpaint 交界处会露边。
        combined = np.maximum(hard, texture_mask)
        try:
            filled = cv2.inpaint(filled, combined, INPAINT_RADIUS, cv2.INPAINT_TELEA)
        except cv2.error as exc:  # noqa: BLE001
            logger.warning("inpaint 失败(%s)，该区域退化为平面填充", exc)
            # 退化为平面填充：有残差但残差小的情况（如轻度纹理 + 强渐变）仍然能看
            for analysis in analyses:
                if analysis.mask.size and (analysis.mask > 0).any():
                    filled = _fill_by_plane(
                filled, analysis, analysis.mask, expand=feather + 1
            )

    # --- 软边混合 ---
    #
    # alpha 必须是 ``max(hard, blurred)``，不能直接用 blurred：
    #
    # 高斯模糊把硬 mask 的阶跃变成斜坡，**边界内侧**的 alpha 也只有 0.5~0.9。
    # 那样"擦除"就变成了"淡化"——笔画边缘留下一圈灰，放大后整段文字轮廓
    # 清晰可见（实测竖排"禁止转载"能完整认出四个字）。
    # 取 max 之后，mask 内恒为 1（彻底擦掉），只在 mask 外侧 1~2px 有过渡，
    # 这才是抗锯齿该有的软化方向。
    if feather > 0:
        k = feather * 2 + 1
        soft = cv2.GaussianBlur(hard, (k, k), 0).astype(np.float32) / 255.0
        alpha = np.maximum((hard > 0).astype(np.float32), soft)
    else:
        alpha = (hard > 0).astype(np.float32)

    alpha3 = alpha[:, :, None]
    out = image_bgr.astype(np.float32) * (1.0 - alpha3) + filled.astype(np.float32) * alpha3
    return np.clip(out, 0, 255).astype(np.uint8)
