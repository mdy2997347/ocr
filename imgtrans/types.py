"""图片翻译链路的公共数据类型。

设计原则：

* **纯数据**，不含行为、不依赖 numpy/PIL，便于单测与序列化；
* 坐标一律用**原图像素坐标系**（左上为原点），不做任何缩放折算——
  缩放只允许发生在 :mod:`imgtrans.render` 内部，且必须换算回去；
* 每个 :class:`TextBlock` 的 ``index`` 是**全局唯一且稳定**的，
  翻译结果按 ``index`` 回填，绝不按列表顺序——这是防错位的最后一道防线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: 四点顺序：左上、右上、右下、左下（顺时针，与 PaddleOCR 一致）。
Point = tuple[float, float]
Polygon = tuple[Point, Point, Point, Point]

#: 轴对齐包围盒 ``(x0, y0, x1, y1)``，右开下开语义。
Box = tuple[int, int, int, int]

RGB = tuple[int, int, int]

#: 字重判断结果。``regular`` 与 ``bold`` 之间没有中间态——
#: 真实字重无法从位图可靠还原，二分到"像不像粗体"已是上限。
Weight = Literal["regular", "bold"]

Align = Literal["left", "center", "right"]


@dataclass(slots=True)
class TextBlock:
    """一处待翻译的文字。

    ``polygon`` 保留四点而非只用轴对齐框：倾斜/透视文字需要四边形才能
    正确贴合，轴对齐框只作为快速路径（排版、mask 外扩）使用。
    """

    index: int
    polygon: Polygon
    text: str
    confidence: float

    #: 检测是否为竖排（自上而下）。竖排文字暂不参与回填，只擦除。
    vertical: bool = False

    #: 合并成段落块后，**单行**的行高（像素）。单行块为 0，表示未知。
    #:
    #: 回填时必须用它而不是 :attr:`height` 推导字号：多行段落的 ``height``
    #: 是整段高度，按它起步会得到一个大到荒谬的字号，然后一路缩到 9px。
    line_height: float = 0.0

    #: 该块由多少行合并而来（1 表示单行）。
    line_count: int = 1

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"index must be >= 0, got {self.index}")
        if not self.text:
            raise ValueError("text must be non-empty; 空块应在聚合阶段被丢弃")

    @property
    def box(self) -> Box:
        """轴对齐包围盒（向外取整，保证不裁掉抗锯齿边缘）。"""
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return (
            int(min(xs) // 1),
            int(min(ys) // 1),
            int(-(-max(xs) // 1)),
            int(-(-max(ys) // 1)),
        )

    @property
    def width(self) -> float:
        """四边形平均宽度（上底与下底均值），用于字号估算。"""
        tl, tr, br, bl = self.polygon
        top = ((tr[0] - tl[0]) ** 2 + (tr[1] - tl[1]) ** 2) ** 0.5
        bottom = ((br[0] - bl[0]) ** 2 + (br[1] - bl[1]) ** 2) ** 0.5
        return (top + bottom) / 2

    @property
    def height(self) -> float:
        """四边形平均高度（左右边均值）。"""
        tl, tr, br, bl = self.polygon
        left = ((bl[0] - tl[0]) ** 2 + (bl[1] - tl[1]) ** 2) ** 0.5
        right = ((br[0] - tr[0]) ** 2 + (br[1] - tr[1]) ** 2) ** 0.5
        return (left + right) / 2

    @property
    def slope(self) -> float:
        """文字行方向相对水平线的夹角（弧度）。0 表示水平。"""
        import math

        tl, tr = self.polygon[0], self.polygon[1]
        return math.atan2(tr[1] - tl[1], tr[0] - tl[0])


@dataclass(slots=True)
class BlockStyle:
    """从一个文字区域反推出的视觉样式。

    所有颜色都是**估计值**——位图上文字只有几个像素宽时，抗锯齿会让
    真实前景色不可知。用 mask 内像素的中位数而非均值，避免边缘混色拉偏。
    """

    fg_color: RGB
    bg_color: RGB
    stroke_color: RGB | None = None
    stroke_width: int = 0
    align: Align = "left"
    weight: Weight = "regular"
    #: 该块是否值得回填。False 时只擦除不绘制（如竖排、艺术字、无法估计样式）。
    renderable: bool = True

    #: 字形实测高度（像素），来自笔画 mask。0 表示无法估计。
    #:
    #: 回填字号由它反推——OCR 框高含上下留白，按框高定字号会偏大 25~35%，
    #: 表现为"译文比原文明显大一截"。
    text_height: float = 0.0


@dataclass(slots=True)
class RenderedBlock:
    """一个块的回填结果，用于对外汇报与调试。"""

    index: int
    source: str
    target: str
    #: 是否真的把译文画上去了。False 的原因见 ``note``。
    painted: bool
    font_size: int = 0
    lines: int = 0
    #: 为塞进原框所做的退让：``none`` / ``shrink_font`` / ``overflow``
    fit_mode: str = "none"
    note: str = ""


@dataclass(slots=True)
class PipelineResult:
    """一次完整翻译的产物。"""

    image: bytes
    mime: str
    width: int
    height: int
    blocks: list[RenderedBlock] = field(default_factory=list)
    #: 链路各阶段耗时（毫秒），键名见 :mod:`imgtrans.pipeline`
    timings: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def translated_count(self) -> int:
        return sum(1 for b in self.blocks if b.painted)


class ImageTranslateError(Exception):
    """链路级错误基类。``code`` 用于对外错误码映射。"""

    code = "E_INTERNAL"


class ImageFetchError(ImageTranslateError):
    """图片下载失败（含 SSRF 拦截、超限、格式不支持）。"""

    code = "E_IMAGE_FETCH"


class OcrError(ImageTranslateError):
    """文字检测/识别失败。"""

    code = "E_OCR"


class UnsupportedLangError(ImageTranslateError):
    """目标语言码不在支持清单内（见 ``SUPPORTED_LANGS``）。

    单独一个错误码，是为了让调用方能区分"参数错了"（改参数即可）
    和"链路坏了"（重试/告警）——都归到 ``E_INTERNAL`` 会把这两种情况混掉。
    """

    code = "E_LANG"
