"""渲染子包：字体、样式还原、排版绘制。"""

from .fonts import (
    FONT_CANDIDATES,
    FontBook,
    family_for_lang,
    has_raqm,
)
from .style import (
    BOLD_RATIO,
    STROKE_MIN_DELTA,
    analyze_style,
)
from .text import (
    LINE_HEIGHT_RATIO,
    MAX_VERTICAL_OVERFLOW,
    MIN_FONT_SIZE,
    SUPERSAMPLE,
    Layout,
    draw_block,
    fit_block,
    render_blocks,
    wrap_text,
)

__all__ = [
    "BOLD_RATIO",
    "FONT_CANDIDATES",
    "LINE_HEIGHT_RATIO",
    "MAX_VERTICAL_OVERFLOW",
    "MIN_FONT_SIZE",
    "STROKE_MIN_DELTA",
    "SUPERSAMPLE",
    "FontBook",
    "Layout",
    "analyze_style",
    "draw_block",
    "family_for_lang",
    "fit_block",
    "has_raqm",
    "render_blocks",
    "wrap_text",
]
