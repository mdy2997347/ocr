"""端到端编排：URL -> 译文回填后的图片。

流程（顺序很重要，尤其是擦除与翻译的先后）：

1. 下载 & 校验（SSRF、体积、格式）
2. 解码（EXIF 方向归正、保留 ICC）
3. 文字检测 + 识别（PaddleOCR）
4. 预过滤：空串 / 纯数字 / 纯符号不送翻译（详见 :func:`should_translate`）
5. 批量翻译（一次请求带多个 ``q``，按 index 回填，绝不错位）
6. **只对"确实要重绘的块"生成 mask 并擦除**
7. 样式分析 → 字号自适应 → 超采样回填
8. 编码输出（默认 PNG 无损）

第 6 步的顺序是刻意的：如果先擦除再翻译，一旦某些块翻译失败，
那些块的原文已经被擦掉了，画面上会留下**空白**——比"没翻译"糟得多。
所以只擦"确定有译文要画上去"的区域，翻译失败的块**保留原文不动**。
这条约束换来的代价是 mask 要算两遍（这里只算一遍，因为擦除与样式分析
共用同一次 :func:`analyze_box` 结果）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps

from .downloader import fetch_image
from .inpaint.erase import erase_text
from .inpaint.mask import BoxAnalysis, analyze_box
from .ocr.engine import OcrEngine, resolve_ocr_lang
from .ocr.group import group_blocks
from .render.fonts import FontBook, font_dirs_from_env
from .render.style import analyze_style
from .render.text import render_blocks
from .translator.base import BaseTranslator
from .translator.lang_map import SUPPORTED_LANGS, normalize_lang
from .types import (
    BlockStyle,
    ImageTranslateError,
    PipelineResult,
    TextBlock,
    UnsupportedLangError,
)

logger = logging.getLogger(__name__)

#: 纯数字（含千分位、小数、货币符号前后缀）。这类内容翻译无意义，
#: 送进去还可能被改写格式（例如把 ``1,000`` 变成 ``1000``）。
_NUMBER_RE = re.compile(r"^[\s\d.,:%+\-/*×xX°()\[\]{}¥$€£#@&]*$")

#: 纯符号/标点。OCR 常把装饰性线条识别成标点。
_SYMBOL_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


def should_translate(text: str) -> bool:
    """判断一段 OCR 文本是否值得送翻译。

    过滤三类：

    1. **空白**：Google 对空 ``q`` 会直接丢弃，导致响应数组比入参短一条，
       从该位置起整批错位——这是最危险的失败模式，必须在源头掐掉。
    2. **纯数字**：``1,000``、``50%`` 这类翻不动，且可能被改写格式。
    3. **纯符号**：装饰性线条被 OCR 误识别成标点。
    """
    stripped = text.strip()
    if not stripped:
        return False
    if _NUMBER_RE.match(stripped):
        return False
    if _SYMBOL_RE.match(stripped):
        return False
    return True


@dataclass(slots=True)
class PipelineConfig:
    """链路配置。生产环境应从 YAML/Pydantic Settings 装配。"""

    #: 输出格式：``png`` 无损（默认）/ ``jpeg`` / ``webp``
    output_format: str = "png"
    #: JPEG/WebP 质量。95 以上 + subsampling=0 才谈得上"保持清晰度"。
    output_quality: int = 95
    #: 低于该置信度的 OCR 结果直接丢弃
    min_ocr_confidence: float = 0.5
    #: 原框高度低于此值（像素）的块**不翻译、不擦除**。
    #:
    #: 这类框几乎都是照片内嵌的小字（实测缩略图里 "Room" 高 7px、"Compound
    #: Softens" 高 11px）。它们放不下可读的译文：最小字号 9px 一个字母就顶满，
    #: 必然溢出压到照片内容上，把缩略图弄脏——比"不翻译"糟得多。
    #: 保留原文是这里唯一体面的选择。
    min_block_height: float = 12.0
    #: 未指定源语言时用哪个 OCR 模型。
    #:
    #: **绝不能按目标语言推断**：翻成韩语就用韩语模型去读中文原图，
    #: 结果是一个字都认不出来（实测：6 个块全丢，直接返回原图）。
    #: 默认 ``ch`` 是 PP-OCRv6 单模型覆盖最广的一档（中/英/日 + 46 种拉丁语系），
    #: 猜错也不至于全丢。韩/阿/泰/西里尔等需要调用方显式传 ``source_lang``。
    default_ocr_lang: str = "ch"
    #: 拉丁语系兜底 OCR 语言。默认模型认不出任何字时（很可能是纯西文图），
    #: 用 ``latin`` 模型再试一次。
    latin_fallback_lang: str = "latin"
    #: 是否把 OCR 的行级块聚合成段落。默认开启：按行翻译会让一段话在
    #: 折行处被切断（语义碎片化），且同段内字号忽大忽小。详见 ocr.group。
    merge_paragraphs: bool = True
    #: 是否回填译文（False 时只返回"擦干净的图 + 块信息"，对应百度 paste=3）
    render: bool = True
    #: 是否擦除原文（False 时返回原图，仅做 OCR + 翻译，用于调试）
    erase: bool = True
    #: 超大图像素数阈值，超过仅告警（分块处理尚未实现）
    big_image_pixels: int = 4000 * 4000
    #: 字体目录（生产建议显式配置）
    font_dirs: tuple[str, ...] = field(default_factory=tuple)
    #: 推理设备：``cpu``（默认）/ ``gpu`` / ``gpu:0`` / ``auto``。
    #:
    #: ``None`` 表示"读 ``IMGTRANS_DEVICE``，再没有就用 cpu"，归一化与可用性
    #: 校验在 :func:`imgtrans.device.resolve_device` 里做。放在配置里而不是
    #: 只认环境变量，是为了让库调用方（测试、批处理脚本）能显式钉死设备，
    #: 不被 shell 里残留的变量影响。
    device: str | None = None


class ImageTranslatePipeline:
    """把各环节串起来。实例应**进程级复用**（OCR 模型常驻）。"""

    def __init__(
        self,
        translator: BaseTranslator,
        *,
        ocr: OcrEngine | None = None,
        font_book: FontBook | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self._translator = translator
        self._config = config or PipelineConfig()
        # 先有 config 才能知道设备：配置里的 device 优先，None 才落到环境变量。
        self._ocr = ocr or OcrEngine(device=self._config.device)
        # 字体目录：配置显式给了就用配置，否则读 IMGTRANS_FONT_DIRS。
        # 容器里字体通常在 /opt/fonts 这类自定义路径，系统目录扫不到。
        self._font_book = font_book or FontBook(
            self._config.font_dirs or font_dirs_from_env()
        )

    @property
    def config(self) -> PipelineConfig:
        return self._config

    @property
    def device(self) -> str:
        """实际生效的推理设备（已归一化），用于启动日志与健康检查。"""
        return self._ocr.device

    def warmup(self) -> None:
        """预热：加载 OCR 模型，避免首图等待。

        OCR 模型加载是数百毫秒到数秒的重活，放在首个请求里会直接顶高 P95。
        """
        started = time.perf_counter()
        try:
            self._ocr.get("ch")
        except Exception as exc:  # noqa: BLE001 - 预热失败不应阻止服务启动
            logger.warning("OCR 预热失败（首次请求时会重试）: %s", exc)
        logger.info(
            "warmup 完成，耗时 %.2fs（device=%s）",
            time.perf_counter() - started,
            self._ocr.device,
        )

    # ---------------- 主入口 ----------------

    def translate_url(
        self,
        url: str,
        target_lang: str,
        source_lang: str | None = None,
        *,
        proxy: str | None = None,
    ) -> PipelineResult:
        """下载远程图片并翻译。"""
        # 先校验语言再下载。顺序反了会有两个坏结果：参数错却先发一次出网
        # 请求；URL 不通时 ``E_LANG`` 被下载失败掩盖成 ``E_IMAGE_FETCH``，
        # 调用方按错误码排查会查错方向（实测踩到：传 lang=zh + 404 的 URL）。
        target_lang = _require_target_lang(target_lang)
        fetched = fetch_image(url, proxy=proxy)
        return self.translate_bytes(
            fetched.content, target_lang, source_lang, mime=fetched.mime
        )

    def translate_bytes(
        self,
        data: bytes,
        target_lang: str,
        source_lang: str | None = None,
        *,
        mime: str | None = None,
    ) -> PipelineResult:
        """翻译内存中的图片字节。"""
        target_lang = _require_target_lang(target_lang)
        timings: dict[str, float] = {}
        warnings: list[str] = []

        t0 = time.perf_counter()
        image_bgr, icc = _decode_image(data)
        timings["decode"] = (time.perf_counter() - t0) * 1000

        h, w = image_bgr.shape[:2]
        if w * h > self._config.big_image_pixels:
            warnings.append(
                f"图片较大({w}x{h})，分块处理尚未实现，OCR 可能较慢"
            )

        # --- OCR ---
        t0 = time.perf_counter()
        # 关键：OCR 语言只看**源语言**。曾经按 target 推断，翻韩语时用韩语模型
        # 去读中文原图，一个字都认不出，整张图原样返回。
        ocr_lang = (
            resolve_ocr_lang(source_lang)
            if source_lang
            else self._config.default_ocr_lang
        )
        blocks = self._ocr.recognize(
            image_bgr,
            lang=ocr_lang,
            min_confidence=self._config.min_ocr_confidence,
        )
        if not blocks and ocr_lang != self._config.latin_fallback_lang:
            # 默认模型一个字都没认出来：很可能是纯西文图，用 latin 模型再试一次。
            # 只做一次兜底，不做多语言轮询——每换一个模型都要重新加载（秒级）。
            logger.info("OCR(%s) 未检出文字，改用 %s 重试一次", ocr_lang, self._config.latin_fallback_lang)
            blocks = self._ocr.recognize(
                image_bgr,
                lang=self._config.latin_fallback_lang,
                min_confidence=self._config.min_ocr_confidence,
            )
            if blocks:
                warnings.append(
                    f"默认 OCR 模型({ocr_lang})未检出文字，已用 {self._config.latin_fallback_lang} 重试"
                )
        timings["ocr"] = (time.perf_counter() - t0) * 1000

        # --- 丢弃贴边被裁的行 ---
        # 截图/长图拼接处常有"只剩半截"的文字行。OCR 只能靠字形猜，
        # 实测把"霍尼韦尔的 PTM7950 是 7900 系列中的高导热相变材料"读成
        # "霍尼书小的 PTM7950 是 790 余列中的高等热柏变材"，译出来是
        # "Honey Book ... hot cypress material"——错得离谱还没法救。
        # 判据：贴着上边缘 + 高度显著小于同图其他行（被裁掉了一部分）。
        clipped: list[TextBlock] = []
        if blocks:
            median_h = sorted(b.box[3] - b.box[1] for b in blocks)[len(blocks) // 2]
            if median_h > 0:
                keep: list[TextBlock] = []
                for b in blocks:
                    # 变量名别用 y1 - y0 = h：外层的 h 是**图像高度**，
                    # 一旦遮蔽，后面 np.zeros((h, w)) 会建成"一块文字那么高"的图。
                    bh = b.box[3] - b.box[1]
                    if b.box[1] <= 2 and bh < 0.7 * median_h:
                        clipped.append(b)
                    else:
                        keep.append(b)
                blocks = keep
                for b in clipped:
                    logger.info("跳过贴边被裁的行: %r", b.text)
                    warnings.append(f"顶部文字被裁切，仅擦除不回填: {b.text[:20]}")

        # --- 段落聚合（行级 -> 段落级） ---
        # 放在翻译之前：翻译的最小单位应该是"一段话"而不是"一行"，
        # 否则折行处的主谓宾被切断，译文必然失真。
        if blocks and self._config.merge_paragraphs:
            t0 = time.perf_counter()
            before = len(blocks)
            blocks = group_blocks(blocks)
            timings["group"] = (time.perf_counter() - t0) * 1000
            if len(blocks) < before:
                logger.info("段落聚合: %d 行 -> %d 段", before, len(blocks))

        if not blocks:
            warnings.append("未检测到文字，返回原图")
            out_bytes, out_mime = _encode_image(
                image_bgr, self._config.output_format, self._config.output_quality, icc
            )
            return PipelineResult(
                image=out_bytes,
                mime=out_mime,
                width=w,
                height=h,
                timings=timings,
                warnings=warnings,
            )

        # --- 翻译（含预过滤与 index 映射） ---
        t0 = time.perf_counter()
        translations = self._translate_blocks(blocks, source_lang, target_lang)
        timings["translate"] = (time.perf_counter() - t0) * 1000

        # 决定哪些块要重绘：有有效译文 且 与原文不同
        targets: list[int] = [
            i
            for i, (block, dst) in enumerate(zip(blocks, translations))
            if dst and dst.strip() and dst != block.text
        ]

        if not targets:
            warnings.append("所有块译文与原文一致或无有效译文，返回原图")
            out_bytes, out_mime = _encode_image(
                image_bgr, self._config.output_format, self._config.output_quality, icc
            )
            return PipelineResult(
                image=out_bytes,
                mime=out_mime,
                width=w,
                height=h,
                timings=timings,
                warnings=warnings,
            )

        selected = [blocks[i] for i in targets]
        selected_translations = [translations[i] for i in targets]

        # --- mask + 擦除（只对要重绘的块） ---
        t0 = time.perf_counter()
        analyses: list[BoxAnalysis] = []
        keep_blocks: list[TextBlock] = []
        keep_translations: list[str] = []
        for block, dst in zip(selected, selected_translations):
            try:
                analysis = analyze_box(image_bgr, block)
            except ValueError as exc:
                logger.warning("block %d 分析失败: %s", block.index, exc)
                warnings.append(f"块 {block.index} 分析失败，保持原文")
                continue
            if analysis.ink_ratio <= 0.0:
                logger.warning("block %d 未分割出文字像素，保持原文", block.index)
                warnings.append(f"块 {block.index} 未分割出文字像素，保持原文")
                continue
            analyses.append(analysis)
            keep_blocks.append(block)
            keep_translations.append(dst)
        timings["mask"] = (time.perf_counter() - t0) * 1000

        if not keep_blocks:
            warnings.append("无可擦除的文字区域，返回原图")
            out_bytes, out_mime = _encode_image(
                image_bgr, self._config.output_format, self._config.output_quality, icc
            )
            return PipelineResult(
                image=out_bytes,
                mime=out_mime,
                width=w,
                height=h,
                timings=timings,
                warnings=warnings,
            )

        base = image_bgr
        if self._config.erase:
            t0 = time.perf_counter()
            # 被裁切的贴边行不翻译，但要擦掉：一行"只剩半截的中文"留在
            # 英文图里比空白更扎眼，而它本身不可译，擦掉是唯一体面的处理。
            extra: list[BoxAnalysis] = []
            for block in clipped:
                try:
                    analysis = analyze_box(image_bgr, block)
                except ValueError:
                    continue
                if analysis.ink_ratio > 0.0:
                    extra.append(analysis)
            erase_analyses = analyses + extra

            mask = np.zeros((h, w), dtype=np.uint8)
            for analysis in erase_analyses:
                x0, y0, x1, y1 = analysis.box
                region = mask[y0:y1, x0:x1]
                np.maximum(region, analysis.mask, out=region)
            base = erase_text(image_bgr, mask, erase_analyses)
            timings["erase"] = (time.perf_counter() - t0) * 1000

        # --- 样式 + 回填 ---
        t0 = time.perf_counter()
        styles: list[BlockStyle] = [
            analyze_style(image_bgr, block, analysis)
            for block, analysis in zip(keep_blocks, analyses)
        ]
        rendered = base
        reports = []
        if self._config.render:
            rendered, reports = render_blocks(
                base,
                keep_blocks,
                keep_translations,
                styles,
                self._font_book,
                target_lang,
            )
        timings["render"] = (time.perf_counter() - t0) * 1000

        # --- 输出 ---
        t0 = time.perf_counter()
        out_bytes, out_mime = _encode_image(
            rendered, self._config.output_format, self._config.output_quality, icc
        )
        timings["encode"] = (time.perf_counter() - t0) * 1000

        return PipelineResult(
            image=out_bytes,
            mime=out_mime,
            width=w,
            height=h,
            blocks=reports,
            timings=timings,
            warnings=warnings,
        )

    # ---------------- 内部步骤 ----------------

    def _translate_blocks(
        self,
        blocks: list[TextBlock],
        source_lang: str | None,
        target_lang: str,
    ) -> list[str]:
        """翻译所有块，返回值与 ``blocks`` **等长同序**。

        不可翻译的块（数字/符号/空白）原样返回，不参与请求，
        也不占用批内位置——这样 Google 返回的数组才能与请求的 ``q`` 一一对应。
        """
        candidates: list[tuple[int, str]] = [
            (i, b.text)
            for i, b in enumerate(blocks)
            if should_translate(b.text)
            and (b.box[3] - b.box[1]) >= self._config.min_block_height
        ]
        too_small = [
            b
            for b in blocks
            if should_translate(b.text)
            and (b.box[3] - b.box[1]) < self._config.min_block_height
        ]
        if too_small:
            logger.info(
                "跳过 %d 个极小框（高 < %.0fpx），保留原文: %s",
                len(too_small),
                self._config.min_block_height,
                [b.text for b in too_small][:5],
            )

        results = [b.text for b in blocks]
        if not candidates:
            return results

        texts = [t for _, t in candidates]
        try:
            translated = self._translator.translate_batch(texts, source_lang, target_lang)
        except Exception as exc:  # noqa: BLE001 - 翻译层契约是"不抛"，但仍要兜住
            logger.exception("翻译层抛出异常，全部保留原文")
            return results

        if len(translated) != len(texts):
            # 契约被破坏：宁可全部保留原文，也不按顺序猜
            logger.error(
                "翻译返回数量不匹配: 期望 %d 实际 %d，全部保留原文",
                len(texts),
                len(translated),
            )
            return results

        for (idx, _), dst in zip(candidates, translated):
            results[idx] = dst
        return results


# ---------------- 编解码 ----------------


def _require_target_lang(code: str) -> str:
    """校验并归一目标语言码，非法直接抛 :class:`UnsupportedLangError`。

    放在解码**之前**：参数错就不该浪费一次解码 + 一次 15 秒的 OCR。
    错误信息里带上完整清单，调用方不必翻文档就知道该传什么。
    """
    normalized = normalize_lang(code)
    if normalized:
        return normalized
    raise UnsupportedLangError(
        f"不支持的目标语言: {code!r}；支持 {len(SUPPORTED_LANGS)} 种: "
        + ", ".join(SUPPORTED_LANGS)
    )


def _decode_image(data: bytes) -> tuple[np.ndarray, bytes | None]:
    """字节 -> BGR ndarray，并归正 EXIF 方向。

    Returns:
        ``(bgr, icc_profile)``。ICC 保留是为了输出时色彩不失真——
        丢掉 ICC 会让 sRGB/AdobeRGB 的图在浏览器里偏色。
    """
    try:
        with Image.open(BytesIO(data)) as im:
            icc = im.info.get("icc_profile")
            # EXIF 方向：手机拍的图常带 Orientation=6，不归正会导致
            # 文字框坐标与显示方向错位，回填全画在错误位置。
            oriented = ImageOps.exif_transpose(im)
            rgb = oriented.convert("RGB")
            arr = np.array(rgb, dtype=np.uint8)
    except Exception as exc:  # noqa: BLE001
        raise ImageTranslateError(f"图片解码失败: {exc}") from exc

    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return bgr, icc


def _encode_image(
    bgr: np.ndarray,
    fmt: str,
    quality: int,
    icc: bytes | None,
) -> tuple[bytes, str]:
    """BGR -> 字节。

    默认 PNG 无损：JPEG 即使 q=95 也会在文字边缘产生振铃，
    而图片翻译的输出主体就是文字，"清晰度"主要靠这一步保住。
    """
    fmt = (fmt or "png").lower()
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)

    # 只有真的有 ICC 才传：部分 Pillow 版本对 icc_profile=None 会抛
    # "object of type 'NoneType' has no len()"。
    extra: dict[str, object] = {}
    if icc:
        extra["icc_profile"] = icc

    buf = BytesIO()
    if fmt in ("jpg", "jpeg"):
        image.save(
            buf,
            format="JPEG",
            quality=max(1, min(100, quality)),
            subsampling=0,  # 4:4:4，不做色度下采样——这是"清晰度"的关键
            optimize=True,
            **extra,
        )
        return buf.getvalue(), "image/jpeg"

    if fmt == "webp":
        image.save(
            buf,
            format="WEBP",
            quality=max(1, min(100, quality)),
            method=4,
            **extra,
        )
        return buf.getvalue(), "image/webp"

    image.save(buf, format="PNG", optimize=True, **extra)
    return buf.getvalue(), "image/png"
