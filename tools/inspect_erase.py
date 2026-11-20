#!/usr/bin/env python
"""诊断工具：把每块的文字 mask 与擦除结果可视化出来。

用来回答一个只有看图才能回答的问题——"为什么擦完还有残影"：

* mask 覆盖不全  -> 残影是被漏掉的笔画像素；
* mask 覆盖完整  -> 残影是填充色估错（平面拟合把字迹当成了背景梯度）。

输出 ``out/debug/mask_overlay.png``（红色描出 mask 边界）与
``out/debug/erase_<i>.png``（每块的擦除前后对比，横向拼接）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.inpaint.erase import erase_text
from imgtrans.inpaint.mask import analyze_box
from imgtrans.ocr.engine import OcrEngine
from imgtrans.ocr.group import group_blocks


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "out/src_0.jpg")
    lang = sys.argv[2] if len(sys.argv) > 2 else "ch"
    out_dir = Path("out/debug")
    out_dir.mkdir(parents=True, exist_ok=True)

    img = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"无法读取 {src}")

    ocr = OcrEngine()
    blocks = group_blocks(ocr.recognize(img, lang=lang, min_confidence=0.5))
    print(f"共 {len(blocks)} 块")

    analyses = []
    for b in blocks:
        try:
            a = analyze_box(img, b)
        except ValueError as exc:
            print(f"[{b.index}] 分析失败: {exc}")
            continue
        analyses.append(a)
        print(
            f"[{b.index}] box={a.box} ink={a.ink_ratio:.3f} bg_std={a.bg_std:.1f} "
            f"fg={a.fg_color} bg={a.bg_color} text={b.text[:16]!r}"
        )

    # 全局 mask 叠加图：红色边界
    h, w = img.shape[:2]
    full = np.zeros((h, w), np.uint8)
    for a in analyses:
        x0, y0, x1, y1 = a.box
        region = full[y0:y1, x0:x1]
        if region.shape == a.mask.shape:
            np.maximum(region, a.mask, out=region)

    overlay = img.copy()
    contours, _ = cv2.findContours(full, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(overlay, contours, -1, (0, 0, 255), 1)
    cv2.imwrite(str(out_dir / "mask_overlay.png"), overlay)
    cv2.imwrite(str(out_dir / "mask_only.png"), full)

    erased = erase_text(img, full, analyses) if full.any() else img

    # 每块的擦除前后对比
    for i, a in enumerate(analyses):
        x0, y0, x1, y1 = a.box
        pad = 6
        ex0, ey0 = max(0, x0 - pad), max(0, y0 - pad)
        ex1, ey1 = min(w, x1 + pad), min(h, y1 + pad)
        before = img[ey0:ey1, ex0:ex1]
        after = erased[ey0:ey1, ex0:ex1]
        gap = np.full((before.shape[0], 4, 3), 255, np.uint8)
        combo = np.hstack([before, gap, after])
        # 放大 2 倍便于肉眼检查笔画残留
        combo = cv2.resize(combo, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
        cv2.imwrite(str(out_dir / f"erase_{i:02d}.png"), combo)

    print(f"已写入 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
