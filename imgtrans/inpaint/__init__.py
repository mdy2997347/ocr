"""擦除子包：文字 mask 生成与背景修复。"""

from .erase import (
    FLAT_STD,
    GRADIENT_STD,
    INPAINT_RADIUS,
    erase_text,
)
from .mask import (
    DILATE_PX,
    MIN_CONTRAST,
    RING_WIDTH,
    BoxAnalysis,
    analyze_box,
    build_text_mask,
    estimate_bg_color,
)

__all__ = [
    "DILATE_PX",
    "FLAT_STD",
    "GRADIENT_STD",
    "INPAINT_RADIUS",
    "MIN_CONTRAST",
    "RING_WIDTH",
    "BoxAnalysis",
    "analyze_box",
    "build_text_mask",
    "erase_text",
    "estimate_bg_color",
]
