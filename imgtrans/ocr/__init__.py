"""OCR 子包：文字检测与识别。"""

from .engine import (
    LANG_ALIASES,
    OcrEngine,
    normalize_ocr_output,
    resolve_ocr_lang,
    sort_reading_order,
)

__all__ = [
    "LANG_ALIASES",
    "OcrEngine",
    "normalize_ocr_output",
    "resolve_ocr_lang",
    "sort_reading_order",
]
