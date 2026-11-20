#!/usr/bin/env python
"""OCR 档位耗时诊断：确认 fast 是否真的生效，并拆分检测/识别的开销。

为什么需要这个脚本
------------------
``/healthz`` 里的 ``ocr_profile`` 只证明**配置被读到**，不证明**模型真的换了**：
fast 档的检测模型下载失败时，代码会静默回退 quality 并继续服务，此时
healthz 照样显示 ``fast``。耗时才是唯一可信的证据。

它同时回答第二个问题：OCR 那 4~5 秒里，检测（det）和识别（rec）各占多少。
只有知道这个比例，才能判断"换检测模型"这个方向还剩多少空间。

用法（在项目根目录，用装好依赖的解释器跑）::

    .venv/bin/python scripts/ocr_bench.py --image tests/fixtures/demo_zh.png --lang ch

线上建议用真实业务图，并且 ``--lang`` 与业务一致（韩语图传 korean），
因为不同语言的识别模型不同，耗时也不同。
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build(lang: str, det: str | None = None, rec: str | None = None):
    """按给定的检测/识别模型建 PaddleOCR。

    这里刻意直接调 PaddleOCR 而不是走 OcrEngine：OcrEngine 只提供
    quality/fast 两档，而诊断需要"只换 rec"这个第三组配置来反推占比。
    参数与 OcrEngine._build 保持一致，避免测出来的数不对应线上。
    """
    from paddleocr import PaddleOCR

    kwargs = {
        "lang": lang,
        "device": "cpu",
        # 必须显式关：paddleocr 3.7 的默认值是 True，而 paddle 3.3.1 的 PIR
        # 执行器不支持 mkldnn，开了会直接崩。
        "enable_mkldnn": False,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
    }
    if det:
        kwargs["text_detection_model_name"] = det
    if rec:
        kwargs["text_recognition_model_name"] = rec
    return PaddleOCR(**kwargs)


def measure(engine, image, repeat: int) -> tuple[float, int]:
    """返回（最快一次耗时，识别出的非空文本块数）。

    第一次 predict 用来预热——模型加载、子图缓存、内存池分配都在这一步，
    计进去会把"首次请求慢"误判成"档位没生效"。取多次里的**最小值**而非
    平均值：我们要衡量的是这条路径的下限，偶发的系统抖动不该算在模型头上。
    """
    list(engine.predict(image))

    best = float("inf")
    count = 0
    for _ in range(repeat):
        start = time.perf_counter()
        result = list(engine.predict(image))
        best = min(best, time.perf_counter() - start)
        if result:
            texts = result[0].json["res"]["rec_texts"]
            count = len([t for t in texts if t.strip()])
    return best, count


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 档位耗时诊断")
    parser.add_argument("--image", required=True, help="测试图路径")
    parser.add_argument("--lang", default="ch", help="OCR 语言，与业务一致（如 korean）")
    parser.add_argument("--repeat", type=int, default=3, help="每档重复次数，取最快")
    args = parser.parse_args()

    import cv2

    image = cv2.imread(args.image)
    if image is None:
        print(f"读不到图片：{args.image}", file=sys.stderr)
        return 2

    height, width = image.shape[:2]
    print(f"图片 {width}x{height}  lang={args.lang}  每档重复 {args.repeat} 次取最快\n")

    cases = [
        ("quality    v6_medium_det + v6_medium_rec", None, None),
        ("fast       v5_mobile_det + v6_medium_rec", "PP-OCRv5_mobile_det", None),
        ("rec 也降档 v5_mobile_det + v5_mobile_rec", "PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"),
    ]

    timings: dict[str, float] = {}
    for label, det, rec in cases:
        try:
            engine = build(args.lang, det, rec)
            elapsed, count = measure(engine, image, args.repeat)
            timings[label] = elapsed
            print(f"{label:42s} {elapsed:7.2f}s   {count:3d} 块")
        except Exception as exc:  # noqa: BLE001 - 诊断脚本，失败要继续跑下一组
            print(f"{label:42s} 失败 {type(exc).__name__}: {str(exc)[:70]}")
        finally:
            # 每个实例都持有完整模型，同时驻留会吃掉 1.5GB 以上；
            # 线上机器别让诊断脚本自己把内存撑爆。
            engine = None
            gc.collect()

    if len(timings) < 2:
        print("\n可用的档位不足两组，无法比较。先看上面的失败原因。", file=sys.stderr)
        return 1

    ordered = list(timings.items())
    base_label, base = ordered[0]
    print("\n相对 quality：")
    for label, elapsed in ordered[1:]:
        delta = elapsed - base
        ratio = (elapsed / base - 1) * 100 if base else 0.0
        print(f"  {label.split()[0]:12s} {delta:+6.2f}s  ({ratio:+.0f}%)")

    print(
        "\n怎么读：\n"
        "  · quality 与 fast 差距 < 10%  → fast 很可能没生效（被回退），看 journalctl 里的\n"
        "    \"OCR 档位 fast 的检测模型加载失败\"；也可能是该图文字太少，det 本就不是瓶颈\n"
        "  · fast 明显快但总数仍偏大   → 瓶颈已转到 rec，需要看第三组（rec 也降档）的差值\n"
        "  · 第三组比 fast 快很多      → rec 是主要开销，可考虑新增只换 rec 的档位"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
