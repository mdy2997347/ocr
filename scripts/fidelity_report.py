#!/usr/bin/env python
"""版式还原自检：一张图，量出"聚合判据在这张图上到底取了什么值"。

用途不是替代肉眼，而是把"这张图为什么排得不对"变成可读的数字，省掉
来回传截图。输出三块：

1. **间距分布**：相邻块的间距按行高归一化后，段内与段间各自落在哪；
   聚合适用的阈值就取在两者之间（取不出双峰会明确写出来）。
2. **段落划分**：每段几行、文本开头，用来对照原图的段落结构。
3. **尺寸不变性**：把图缩放到 0.6× / 1× / 1.5× 再跑一遍，段落划分应当
   完全一致。不一致说明这一版的判据在依赖输入尺寸，属于实现缺陷，
   不是这张图的问题——这也是最能提前暴露"过拟合某张图"的地方。

不调用翻译、不写文件，只做识别与聚合，可以放心在线上机器上跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.device import DeviceError, resolve_device
from imgtrans.env import apply_model_dir, load_env

load_env()
apply_model_dir()

import cv2  # noqa: E402

from imgtrans.ocr.engine import OcrEngine  # noqa: E402
from imgtrans.ocr.group import (  # noqa: E402
    MAX_GAP_RATIO,
    _gap_profile,  # 诊断工具必须和流水线用同一个判据，否则报告没意义
    _x_overlap,
    group_blocks,
    join_row_fragments,
)


def _gaps(rows) -> list[tuple[float, float]]:
    """相邻块的 (归一化间距, 原始px)。只取左右有重叠的上下相邻对。"""
    ordered = sorted(rows, key=lambda b: (b.box[1], b.box[0]))
    out = []
    for a, b in zip(ordered, ordered[1:]):
        if _x_overlap(a.box, b.box) < 0.45:
            continue
        lh = min(a.height, b.height)
        if lh <= 0:
            continue
        gap = b.box[1] - a.box[3]
        if gap >= 0:
            out.append((gap / lh, float(gap)))
    return out


def analyze(path: Path, engine: OcrEngine, lang: str, scale: float = 1.0) -> dict:
    image = cv2.imread(str(path))
    if image is None:
        raise SystemExit(f"读不到图片: {path}")
    if scale != 1.0:
        image = cv2.resize(
            image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
    rows = join_row_fragments(engine.recognize(image, lang=lang), gap_ratio=0.45)
    paras = group_blocks(rows)
    return {"rows": len(rows), "blocks": rows, "paras": paras, "gaps": _gaps(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="版式还原自检（不翻译）")
    parser.add_argument("--input", required=True, help="本地图片路径")
    parser.add_argument("--lang", default="ch", help="OCR 语言，默认 ch")
    parser.add_argument("--device", default=None, help="cpu（默认）/ gpu / gpu:0 / auto")
    parser.add_argument(
        "--scales",
        default="0.6,1.0,1.5",
        help="尺寸不变性检查用的缩放倍率，逗号分隔；给 1.0 则跳过",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    try:
        device = resolve_device(args.device or "cpu")
    except DeviceError as exc:
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2

    engine = OcrEngine(device=device)
    path = Path(args.input)
    scales = [float(s) for s in args.scales.split(",") if s.strip()]

    base = analyze(path, engine, args.lang, 1.0)
    # 用**行级块**算间距判据，和 group_blocks 内部同一份输入
    profile = _gap_profile(sorted(base["blocks"], key=lambda b: b.box[1]))

    signatures = {}
    for scale in scales:
        if abs(scale - 1.0) < 1e-6:
            signatures[scale] = [b.line_count for b in base["paras"]]
            continue
        res = analyze(path, engine, args.lang, scale)
        signatures[scale] = [b.line_count for b in res["paras"]]

    stable = len({tuple(v) for v in signatures.values()}) == 1

    if args.json:
        print(
            json.dumps(
                {
                    "rows": base["rows"],
                    "paras": [
                        {
                            "lines": b.line_count,
                            "box": [round(v, 1) for v in b.box],
                            "text": b.text[:60],
                        }
                        for b in base["paras"]
                    ],
                    "gaps": [round(g[0], 2) for g in base["gaps"]],
                    "gap_limit": None if profile[0] is None else round(profile[0], 2),
                    "sentence_merge": profile[1],
                    "signatures": {str(k): v for k, v in signatures.items()},
                    "scale_stable": stable,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"图片 {path.name}  行级块 {base['rows']}  段落 {len(base['paras'])}")
    print(f"每段行数: {[b.line_count for b in base['paras']]}")

    gaps = [g[0] for g in base["gaps"]]
    if gaps:
        print(
            "相邻间距(×行高): "
            + " ".join(f"{g:.2f}" for g in sorted(gaps))
        )
    if profile[0] is None:
        print(f"间距判据: 取不出双峰 -> 退回 {MAX_GAP_RATIO:.2f} 倍行高 + 句号断段")
    else:
        print(
            f"间距判据: 段内上限 {profile[0]:.2f} 倍行高"
            f"，{'允许' if profile[1] else '不允许'}跨句号合并"
        )

    print("\n段落:")
    for i, b in enumerate(base["paras"]):
        print(f"  [{i:2d}] {b.line_count} 行  {b.text[:70]!r}")

    print("\n尺寸不变性:")
    for scale, sig in signatures.items():
        print(f"  ×{scale:<4g} {len(sig)} 段  行数 {sig}")
    print(f"  -> {'一致' if stable else '不一致（判据依赖输入尺寸，是 bug）'}")
    return 0 if stable else 1


if __name__ == "__main__":
    sys.exit(main())
