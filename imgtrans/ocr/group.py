"""把 OCR 的**行级**块聚合成**段落级**块。

PaddleOCR 的检出粒度是文本行：一段话折成几行，就是几个互不相干的框。
若直接按行翻译再按行回填，会同时坏掉三件事：

1. **语义**——一句被切成 ``…becomes liquid and can greatly`` /
   ``Wet the interface thoroughly…``，分块送给翻译器，跨块的主谓宾与从句
   全断，译文在断点处必然失真（实测一段 4 行被译成 4 个残缺句子）。
2. **字号**——每块都以自身行高起步再按需缩字号，同一段内会出现
   9px 与 25px 并存，视觉上像一句话里混了标题和脚注。
3. **行距**——每块在自己的小框里独立对齐，行间距忽大忽小。

聚合的核心判断是"这一行是不是上一行的延续"，四个条件同时满足才合并：
竖直间距小、确实在下一行、左右大致对齐、行高接近。这些都是保守判据，
**宁可少合并也不误合并**——误合并会把两段不相干的话拼成一句，
比不合并难看得多，且无法通过调字号挽回。

合并后 ``TextBlock.line_height`` 记住**单行**行高，回填时用它推导初始字号；
若用整段高度起步，多行段落会得到一个大到荒谬的字号然后一路缩到底。
"""

from __future__ import annotations

import logging
import statistics

from ..types import Box, Polygon, TextBlock

logger = logging.getLogger(__name__)

#: 段内行间距上限（相对单行行高）。
#:
#: 这个阈值只能放宽到"紧凑排版的段间距"附近，再大就会把相邻段落也吞进来。
#: 实测电商长图：行距 1.7 倍（gap ≈ 0.7 × 行高）时段内间距 0.68、
#: 段间距 0.73 —— **两者几乎重合**，纯几何判据分不开。所以真正的段落边界
#: 交给 :data:`_SENTENCE_END` 判断，这里只做粗筛。
MAX_GAP_RATIO = 0.90

#: 下一行的顶部至少要比上一行顶部低这么多（相对行高）。
#: 用来排除"同一行被切成左右两块"，那属于横向拼接而非换行。
MIN_DROP_RATIO = 0.30

#: 行高差异容忍度。同一段落的行高几乎一致，超过这个比例说明字号不同，
#: 多半是正文里嵌了小字注释或标题。
HEIGHT_TOLERANCE = 0.40

#: 水平重叠比例下限（相对于较窄的一行）。段落折行时左右边界基本对齐，
#: 即使首行缩进两格，重叠比例也很高；跨栏的两块则几乎不重叠。
MIN_X_OVERLAP = 0.45

#: 单段最多合并多少行。防止图片边框、表格线把整页串成一段。
MAX_LINES = 14

#: 行尾出现这些标点即视为**段末**，不再与下一行合并。
#:
#: 紧凑排版里段内行距与段间距可以只差 1~2 像素，几何上无法区分，但
#: "上一行以句号结尾"是可靠的语言学信号：中文折行时标点跟字走，
#: 句号几乎只会出现在段的最后一行（逗号、顿号才是行中断点）。
#: 误判的方向是安全的——少合并只是没优化，多合并会把两段话拼成一句。
_SENTENCE_END = "。．！？!?；;…"


def _x_overlap(a: Box, b: Box) -> float:
    """两框的水平重叠比例，分母取较窄者宽度。"""
    inter = min(a[2], b[2]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    narrower = min(a[2] - a[0], b[2] - b[0])
    if narrower <= 0:
        return 0.0
    return inter / float(narrower)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角
    )


def join_lines(lines: list[str]) -> str:
    """把多行原文拼成一段，尽量不引入原文没有的字符。

    * 中文/日文/韩文换行**不产生空格**（这两个字符之间本来就没有词分隔）；
    * 西文换行补一个空格，否则 ``phase`` + ``change`` 会粘成 ``phasechange``；
    * 行尾是连字符时视为断词，去掉连字符直接接上。
    """
    out = ""
    for raw in lines:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + piece
        elif _is_cjk(out[-1]) and _is_cjk(piece[0]):
            out += piece
        else:
            # 任一侧是西文/数字都要补空格，否则 "材料" + "phase" 会粘成 "材料phase"
            out += " " + piece
    return out


def _can_merge(last: TextBlock, nxt: TextBlock, line_height: float) -> bool:
    """``nxt`` 是否紧接在 ``last`` 之后（同一段的下一行）。"""
    if last.vertical or nxt.vertical:
        # 竖排的"下一行"是另一列，方向语义完全不同，交给竖排逻辑处理
        return False

    # 段末标点优先于一切几何判据
    tail = last.text.rstrip()
    if tail and tail[-1] in _SENTENCE_END:
        return False

    lh = line_height if line_height > 0 else max(last.height, nxt.height)
    if lh <= 0:
        return False

    la, lb = last.box, nxt.box
    gap = lb[1] - la[3]
    if gap > MAX_GAP_RATIO * lh:
        return False
    if gap < -0.8 * lh:
        # 与上一行重叠太多，不是折行而是重复检出
        return False
    if lb[1] < la[1] + MIN_DROP_RATIO * lh:
        return False

    h_last, h_next = last.height, nxt.height
    if h_last <= 0 or h_next <= 0:
        return False
    if abs(h_next - h_last) / max(h_last, h_next) > HEIGHT_TOLERANCE:
        return False

    return _x_overlap(la, lb) >= MIN_X_OVERLAP


def _merge(lines: list[TextBlock], next_index: int) -> TextBlock:
    """把同一段的若干行合成一个块：外包矩形 + 拼接文本 + 单行行高。"""
    boxes = [b.box for b in lines]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    polygon: Polygon = (
        (float(x0), float(y0)),
        (float(x1), float(y0)),
        (float(x1), float(y1)),
        (float(x0), float(y1)),
    )
    return TextBlock(
        index=next_index,
        polygon=polygon,
        text=join_lines([b.text for b in lines]),
        confidence=min(b.confidence for b in lines),
        vertical=False,
        line_height=float(statistics.median(b.height for b in lines)),
        line_count=len(lines),
    )


def group_blocks(blocks: list[TextBlock]) -> list[TextBlock]:
    """按阅读顺序聚合段落，返回重新编号（0..n-1）的块列表。

    不修改入参，返回全新的 :class:`TextBlock`。单行段落会被原样保留
    （``line_count`` 为 1，``line_height`` 为该行高度）。
    """
    if not blocks:
        return []

    # 阅读顺序：先上后下、再左后右。对多栏布局这个顺序会让左右栏交替出现，
    # 而它们水平不重叠，自然会各自成段，不会串段。
    ordered = sorted(blocks, key=lambda b: (b.box[1], b.box[0]))
    heights = [b.height for b in ordered if b.height > 0]
    global_lh = float(statistics.median(heights)) if heights else 0.0

    groups: list[list[TextBlock]] = []
    current: list[TextBlock] = [ordered[0]]

    for blk in ordered[1:]:
        last = current[-1]
        # 用段内已出现的行高的中位数判断，避免被某一行异常高的框带偏
        lh = float(statistics.median([b.height for b in current])) or global_lh
        if len(current) < MAX_LINES and _can_merge(last, blk, lh):
            current.append(blk)
        else:
            groups.append(current)
            current = [blk]
    groups.append(current)

    out = [_merge(g, i) for i, g in enumerate(groups)]
    changed = sum(1 for b in out if b.line_count > 1)
    if changed:
        logger.info(
            "段落合并: %d 行 -> %d 块（其中 %d 个多行段）",
            len(blocks),
            len(out),
            changed,
        )
    return out
