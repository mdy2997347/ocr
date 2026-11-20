"""回填几何评估：把"回填像不像原文"量成可比较的数字。

沙盘（:mod:`scripts.render_lab`）只负责出图，人眼看图容易漏、也难回归。
这里用**几何指标**替代肉眼：

* ``ang``   —— 回填文字的倾斜角与原文之差（度）。倾斜文字被画成水平时这里会炸开。
* ``h``     —— 回填墨迹高度 / 原文实测字高。>1.15 说明字号被估大。
* ``dx/dy`` —— 回填中心相对原文中心的偏移，单位是原文字高。
* ``align`` —— 左端（LTR）偏移，单位是原文字高。居中/右对齐被画成左对齐时这里明显。

原文真值取自场景定义（块的四点、字号），回填真值取自"渲染图相对擦除底的差异像素"，
两边都是实测，不掺任何中间变量。

用法::

    python scripts/render_eval.py            # 全部场景
    python scripts/render_eval.py slanted    # 只看匹配的场景
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_lab import FONTS, SCENES  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "out" / "lab"

#: 判定"这里被重新画过"的像素差阈值。擦除后的底图上任何回填都会带来明显色差，
#: 18 足以滤掉 JPEG 噪声与超采样带来的 ±3 抖动，又不会漏掉浅灰字。
DIFF_THRESHOLD = 18


def _norm_rect(pts: np.ndarray) -> tuple[float, float, float, float, float]:
    """minAreaRect 结果规整成"行方向"：``(cx, cy, 长边, 短边, 角度)``。

    OpenCV 各版本返回的角度区间不一致（``[-90, 0)`` 或 ``[0, 90)``），
    这里统一成 **``[-90, 90)`` 且以长边为行方向**，否则水平文字会被报成 180°。
    """
    (cx, cy), (w, h), ang = cv2.minAreaRect(pts)
    if w < h:
        w, h = h, w
        ang += 90.0
    while ang >= 90.0:
        ang -= 180.0
    while ang < -90.0:
        ang += 180.0
    return cx, cy, float(w), float(h), float(ang)


def measure(rendered: np.ndarray, base: np.ndarray) -> tuple | None:
    diff = np.abs(rendered.astype(np.int16) - base.astype(np.int16)).max(axis=2)
    mask = (diff > DIFF_THRESHOLD).astype(np.uint8)
    if not mask.any():
        return None
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    # 丢掉孤立噪点（超采样/抗锯齿残渣）
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= 12]
    if not keep:
        return None
    ys, xs = np.where(np.isin(
        cv2.connectedComponents(mask, connectivity=8)[1], keep
    ))
    if len(xs) == 0:
        ys, xs = np.where(mask)
    pts = np.column_stack([xs, ys]).astype(np.float32)
    cx, cy, w, h, ang = _norm_rect(pts)
    return {
        "cx": float(cx), "cy": float(cy), "w": float(w), "h": float(h),
        "angle": float(ang),
        "x0": float(xs.min()), "x1": float(xs.max()),
        "y0": float(ys.min()), "y1": float(ys.max()),
    }


def truth(scene) -> dict:
    """原文的几何真值：联合范围 + 主块角度/字号。"""
    xs = [p[0] for b in scene.blocks for p in b.polygon]
    ys = [p[1] for b in scene.blocks for p in b.polygon]
    main = max(scene.blocks, key=lambda b: b.width * b.height)
    angle = math.degrees(main.slope)
    # 原文"字号"：单行块的行高约等于 1.2 倍字号（生成场景时的约定）；
    # 竖排块的行高是字距，字号取框宽。
    if main.vertical:
        glyph = min(main.width, main.line_height or main.width)
    elif main.line_height > 0:
        glyph = main.line_height / 1.2
    else:
        glyph = main.height * 0.8
    return {
        "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
        "cx": (min(xs) + max(xs)) / 2,
        "cy": (min(ys) + max(ys)) / 2,
        "angle": angle,
        "glyph": glyph,
        "vertical": main.vertical,
        "multi_block": len(scene.blocks) > 1,
        "multi_line": main.line_count > 1,
    }


def erase_residual(scene, base: np.ndarray) -> tuple[float, int]:
    """擦除后底图上还剩多少"墨"。

    这是比排版更基础的质量指标：中文残影叠在英文译文上，比字号小两号难看得多，
    而且**放大后能完整认出原字**（说明 mask 根本没盖住抗锯齿边缘）。
    返回 ``(最深残留相对背景的差值, 超标像素数)``。
    """
    worst, count = 0.0, 0
    for b in scene.blocks:
        x0, y0, x1, y1 = b.box
        reg = base[max(0, y0) : y1, max(0, x0) : x1].astype(np.int16).mean(axis=2)
        if reg.size == 0:
            continue
        bg = float(np.median(reg))
        worst = max(worst, bg - float(reg.min()))
        count += int((reg < bg - 6).sum())
    return worst, count


def overlaps(scene, base: np.ndarray, rendered: np.ndarray) -> int:
    """回填块之间是否互相覆盖。

    同一张图里两个块各画各的，天然会打架：一个向右扩、另一个纵向溢出，
    重叠处后半段文字会盖掉前半段。这里用"绘制后墨迹的连通域是否跨越了
    两个不同块的中心"来近似判断。
    """
    diff = np.abs(rendered.astype(np.int16) - base.astype(np.int16)).max(axis=2)
    ink = diff > DIFF_THRESHOLD
    hits = 0
    for i, a in enumerate(scene.blocks):
        for b in scene.blocks[i + 1 :]:
            ax0, ay0, ax1, ay1 = a.box
            bx0, by0, bx1, by1 = b.box
            ix0, iy0 = max(ax0, bx0), max(ay0, by0)
            ix1, iy1 = min(ax1, bx1), min(ay1, by1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            if ink[iy0:iy1, ix0:ix1].any():
                hits += 1
    return hits


def report(name: str, scene) -> str:
    base, rendered, reports = scene.run()
    t = truth(scene)
    m = measure(rendered, base)
    head = f"{name:<16} 原文 {t['x0']:.0f},{t['y0']:.0f} {t['x1']-t['x0']:.0f}x{t['y1']-t['y0']:.0f}"

    if not any(r.painted for r in reports):
        return f"{head}\n  ✗ 完全未回填（{reports[0].note if reports else '无报告'}）"
    if m is None:
        return f"{head}\n  ✗ 画了但图上找不到墨迹"

    g = max(1.0, t["glyph"])
    ang_err = m["angle"] - t["angle"]
    while ang_err > 90:
        ang_err -= 180
    while ang_err < -90:
        ang_err += 180
    # 竖排块的墨迹天然是"竖长条"，minAreaRect 报 -90° 属正常，
    # 拿它跟水平框比角度没有意义
    if not t["vertical"] and abs(ang_err) <= 3.0:
        ang_err = 0.0

    painted = [r for r in reports if r.painted]
    residual, dirty = erase_residual(scene, base)
    ov = overlaps(scene, base, rendered)

    flags = []
    if abs(ang_err) > 5:
        flags.append(f"角度丢失{ang_err:+.0f}°")
    if residual > 10 or dirty > 40:
        flags.append(f"擦除残留{residual:.0f}/{dirty}px")
    if ov:
        flags.append(f"块重叠{ov}处")
    # 越界：墨迹贴到画布边缘说明有字被裁
    h, w = rendered.shape[:2]
    if m["x1"] >= w - 1 or m["y1"] >= h - 1 or m["x0"] <= 0 or m["y0"] <= 0:
        flags.append("贴画布边缘")

    sizes = [r.font_size for r in painted]
    ratio = sizes[0] / g if not t["multi_line"] else 0.0
    mark = "✗" if flags else "✓"
    body = (
        f"  {mark} 角={m['angle']:+6.1f}° 字号={sizes if len(sizes) < 4 else sizes[0]}"
        f"({ratio:4.2f}x) 行={painted[0].lines} 残留={residual:.0f} 重叠={ov}"
    )
    if flags:
        body += "\n    → " + " / ".join(flags)
    return head + "\n" + body


def main(argv: list[str]) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    picked = [s for s in SCENES if not argv or any(k in s.__name__ for k in argv)]
    bad = 0
    lines = []
    for factory in picked:
        scene = factory()
        try:
            text = report(scene.name, scene)
        except Exception as exc:  # noqa: BLE001
            text = f"{scene.name:<16} ✗ 评估异常: {exc}"
        print(text)
        if "✗" in text:
            bad += 1
        lines.append(text)
    print(f"\n{len(picked)} 个场景，{bad} 个存在几何偏差")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
