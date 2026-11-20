#!/usr/bin/env python
"""端到端命令行：图片 -> 译文回填图。

示例::

    # 远程图片
    python scripts/translate_image.py --url https://example.com/a.jpg --to en --out out.png

    # 本地图片（走 multipart 同一套逻辑，不经过下载器）
    python scripts/translate_image.py --input ./a.png --to ja --out out.png

    # 指定代理（本机 Google 需代理）
    python scripts/translate_image.py --input a.png --to ja --proxy http://127.0.0.1:10809

    # 只看识别与翻译结果，不落盘
    python scripts/translate_image.py --input a.png --to en --dry-run

设计上刻意不复用 FastAPI：这个脚本要能在没装 fastapi 的机器上跑，
用于排查"到底是 OCR 问题、翻译问题还是渲染问题"。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.device import DeviceError, resolve_device
from imgtrans.env import apply_model_dir, load_env
from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig
from imgtrans.translator import get_translator
from imgtrans.types import ImageTranslateError

load_env()
# 必须早于任何 paddle 导入（pipeline 里的引擎是惰性导入，此时尚未发生）
apply_model_dir()


def main() -> int:
    parser = argparse.ArgumentParser(description="图片文字翻译（端到端）")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="远程图片地址")
    src.add_argument("--input", help="本地图片路径")
    parser.add_argument(
        "--to",
        required=True,
        help="目标语言码（见 imgtrans.translator.SUPPORTED_LANGS），如 en / ja / hr",
    )
    parser.add_argument("--from", dest="source", default=None, help="源语言（默认按目标语言推断 OCR 语言）")
    parser.add_argument("--out", default=None, help="输出文件路径（默认 out.png）")
    parser.add_argument(
        "--proxy",
        default=os.environ.get("IMGTRANS_PROXY") or None,
        help="下载/翻译用的代理（默认取 .env 的 IMGTRANS_PROXY）",
    )
    parser.add_argument("--format", default="png", choices=["png", "jpeg", "webp"], help="输出格式")
    parser.add_argument(
        "--device",
        default=os.environ.get("IMGTRANS_DEVICE") or "cpu",
        help="推理设备：cpu（默认）/ gpu / gpu:0 / auto",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印块信息，不写文件")
    args = parser.parse_args()

    try:
        device = resolve_device(args.device)
    except DeviceError as exc:
        # 设备和参数错误同级：都是"配置不对，改一下就能跑"，不该甩堆栈。
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2

    translator = get_translator("web", proxy=args.proxy, timeout=20)
    pipeline = ImageTranslatePipeline(
        translator,
        config=PipelineConfig(output_format=args.format, device=device),
    )

    started = time.perf_counter()
    try:
        if args.url:
            result = pipeline.translate_url(args.url, args.to, args.source, proxy=args.proxy)
        else:
            data = Path(args.input).read_bytes()
            result = pipeline.translate_bytes(data, args.to, args.source)
    except ImageTranslateError as exc:
        # 参数错/下载失败这类"可预期错误"不该甩堆栈：命令行工具的输出
        # 是给人看的，堆栈只会把真正的原因埋掉。
        print(f"[{exc.code}] {exc}", file=sys.stderr)
        return 2
    total = (time.perf_counter() - started) * 1000

    print(f"图片: {result.width}x{result.height}  输出: {len(result.image)/1024:.1f}KB {result.mime}")
    print(f"总耗时: {total:.0f}ms")
    for stage, ms in result.timings.items():
        print(f"  {stage:10s} {ms:7.0f}ms")

    print(f"\n文字块 {len(result.blocks)} 个（已回填 {result.translated_count} 个）:")
    for b in result.blocks:
        flag = "✓" if b.painted else "·"
        detail = f"size={b.font_size} lines={b.lines} fit={b.fit_mode}" if b.painted else b.note
        print(f"  {flag} [{b.index:2d}] {b.source!r} -> {b.target!r}  {detail}")

    if result.warnings:
        print("\n警告:")
        for w in result.warnings:
            print(f"  - {w}")

    if not args.dry_run:
        out = Path(args.out or "out.png")
        out.write_bytes(result.image)
        print(f"\n已写入 {out.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
