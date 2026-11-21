"""把 OCR 的**行级**块聚合成**段落级**块。

PaddleOCR 的检出粒度是文本行：一段话折成几行，就是几个互不相干的框。
若直接按行翻译再按行回填，会同时坏掉三件事：

1. **语义**——一句被切成 ``…becomes liquid and can greatly`` /
   ``Wet the interface thoroughly…``，分块送给翻译器，跨块的主谓宾与从句
   全断，译文在断点处必然失真（实测一段 4 行被译成 4 个残缺句子）。
2. **字号**——每块都以自身行高起步再按需缩字号，同一段内会出现
   9px 与 25px 并存，视觉上像一句话里混了标题和脚注。
3. **行距**——每块在自己的小框里独立对齐，行间距忽大忽小。

聚合分两步，顺序不能反：

1. **行内拼接**（:func:`join_row_fragments`）：检测器会把一条长行切成左右两块，
   先按"同一行 + 间隙很小"把碎片拼回一条完整的行；
2. **跨行聚合**（:func:`group_blocks`）：再把行合成段。

第 1 步是后加的一道补丁，起因是 2026-11 线上反馈"大字号窄条幅上排版错乱"：
一张 980px 宽的促销条幅，三行中文被切成 8 块，第二步只认"上下行"，
于是每个左碎片都成了新段落的起点、右碎片谁也合不进去。8 个碎片各自
按自己的框定字号，回填出来互相压字。同一张图放大到 1500px 就正常——
检测颗粒度随尺寸变，代码不该跟着变。

跨行聚合的判断是"这一行是不是上一行的延续"，四个条件同时满足才合并：
竖直间距小、确实在下一行、左右大致对齐、行高接近。这些都是保守判据，
**宁可少合并也不误合并**——误合并会把两段不相干的话拼成一句，
比不合并难看得多，且无法通过调字号挽回。

在此之上还有两条否决项：下一行以列表编号开头（``2.`` / ``①`` / ``•``）必是
新条目；上一行以句末标点结尾即视为段末——但"段内行距"是从每张图自己的间距
分布里量出来的（:func:`_gap_profile`），量得出双峰的图里句号不再必然断段，
否则一段话里每写完一句就会被拆成一块。

**不写死任何与具体图相关的常量。** 行距、段间距、字号都是排版者定的，
一份阈值不可能同时适配松排和密排：800x600 的告示图段内 0.94、段间 2.8，
而紧凑长图段内 0.68、段间 0.73——前者需要"放宽"，后者需要"收紧"。
所以判据从图里量，量不出来才退回保守值。这条思路的回归靠
``tests/test_layout_generalization.py`` 生成的版式真值，而不是攒真图。

多栏版式由"各段各自生长"处理：读序会把左右栏的块交替送进来，只和上一个
块比较的话，每栏的第二行永远接不上第一行。因此循环里保留多条正在生长的段，
每个块去找**自己那一栏**的末行（横向重叠把别的栏挡在外面）。

合并后 ``TextBlock.line_height`` 记住**单行**行高，回填时用它推导初始字号；
若用整段高度起步，多行段落会得到一个大到荒谬的字号然后一路缩到底。
"""

from __future__ import annotations

import bisect
import logging
import re
import statistics

from ..types import Box, Polygon, TextBlock

logger = logging.getLogger(__name__)

#: 量不出"段内行距"时的兜底上限（相对单行行高）。
#:
#: 这是**兜底值，不是调优值**：紧凑排版里段内 0.68、段间 0.73，几何上
#: 根本分不开，这时只能靠 :data:`_SENTENCE_END` 判断段末，几何退回粗筛，
#: 所以取偏紧的 0.90——宁可少合并。能量出两簇时由 :func:`_gap_profile`
#: 用这张图自己的数据取代它，代码里不需要为某种排版改常量。
MAX_GAP_RATIO = 0.90

#: 双峰判定所需的最小间距样本数。少于 4 个样本看不出"段内/段间"两簇。
GAP_SAMPLE_MIN = 4

#: 归一化间距排序后，相邻两个样本的差达到这个量级才算"断层"。
#:
#: 0.35 = 三分之一行高。断层比这更小说明两个簇互相咬合，不该硬分。
GAP_SPLIT_MIN = 0.35

#: 每簇至少要有这么多样本，才认断层不是个别离群值造成的。
GAP_CLUSTER_MIN = 2

#: 段首编号 / 项目符号。命中即视为**新段落起点**，不参与跨行合并。
#:
#: 列表内每一行的间距都一样，纯几何分不开"第 1 条的最后一行"和"第 2 条"；
#: 而 "2." / "①" / "•" 这种开头是不可能出现在折行延续处的（除非硬折）。
#: 只认 1~2 位数字 + 分隔符，避免把 "2024年"、"13528863010" 当成编号。
_LIST_MARKER = re.compile(
    r"^\s*(?:[（(]?\d{1,2}\s*[.、．)）]|[①-⑳]|[•·▪◦●○◆■□▶►]|[-–—*]\s)"
)

#: 下一行的顶部至少要比上一行顶部低这么多（相对行高）。
#: 用来排除"同一行被切成左右两块"——那属于横向拼接，由
#: :func:`join_row_fragments` 在跨行聚合**之前**处理掉，走到这里还剩下的
#: 就意味着不该拼。
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


#: 行内碎片拼接允许的最大水平间隙（相对行高）。
#:
#: 取 0.45（约半个汉字宽）是一次刻意的取舍：
#:
#: * 太小（0.25）接不住真实切分——实测条幅上的断点间隙在 0.3~0.45 之间，
#:   保守到 0.25 等于没修；
#: * 太大（0.8+）会把同一行里并排的两个独立标签（"计算机" / "大功率LED"）
#:   拼成一句。
#:
#: 两种误判的代价不对称：漏拼会让整段散成碎片、字号乱跳、互相压字；
#: 误拼只是把一个标签多带几个字，译文仍然读得通。所以宁可放开一点。
ROW_JOIN_GAP_RATIO = 0.45

#: 左碎片以逗号/顿号/冒号结尾时放宽到的间隙（相对行高）。
#:
#: "说明：系统价格默认304材质，" + "如需316材质请" 这种断点，检测器
#: 常常正好断在逗号后面，留出大半个字的间隙。逗号是"话没说完"的强信号，
#: 值得再放宽到 0.8；句号结尾且间隙大则不拼（那是跨行聚合的判据）。
ROW_JOIN_COMMA_GAP_RATIO = 0.80

#: 行内拼接的行高容忍度。同一行的碎片字号一致，差异大说明不是同一条行。
ROW_JOIN_HEIGHT_TOLERANCE = 0.30

#: 行内拼接的基线容忍度（y 中心差 / 行高）。
ROW_JOIN_CENTER_TOLERANCE = 0.25

#: 允许的最大重叠（相对行高）。重叠过多更像"同一处文字被重复检出"，
#: 拼起来会把同一句话写两遍。
ROW_JOIN_MAX_OVERLAP = 0.50

#: 这些标点出现在左碎片末尾时，说明句子还在继续，可以放宽间隙。
_CONTINUATION = "，、,：:"


def _cy(block: TextBlock) -> float:
    return (block.box[1] + block.box[3]) / 2


def _median_sorted(vals: list[float]) -> float:
    """已排序列表的中位数，等价于 ``statistics.median`` 但不重排不复制。

    行内拼接每接一个碎片都要问一次"当前这条行有多高"，而 ``statistics.median``
    每次都排序+复制，一条行被切成 n 块时就是 O(n² log n)。
    """
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _can_join_row(
    pieces: list[TextBlock],
    nxt: TextBlock,
    gap_ratio: float,
    comma_gap_ratio: float,
    line_height: float,
) -> bool:
    """``nxt`` 是否紧接在 ``pieces`` 这条行的右端（同一行被切开的下一块）。

    ``line_height`` 由调用方维护（见 :func:`_median_sorted`），语义就是
    ``pieces`` 各行高的中位数。
    """
    if nxt.vertical:
        return False
    last = pieces[-1]
    lh = line_height
    if lh <= 0:
        return False

    gap = nxt.box[0] - last.box[2]
    if gap < -ROW_JOIN_MAX_OVERLAP * lh:
        return False
    if gap > comma_gap_ratio * lh:
        return False

    # 间隙落在"紧挨着"区间内就直接拼：同一行上句末标点后面还接着字是常态
    # （"咨询店主！厂家直销，…"），这里不能拿句末标点当断句依据——那是
    # 跨行聚合的判据。间隙偏大时才要求左碎片以逗号/顿号/冒号收尾。
    if gap > gap_ratio * lh:
        tail = last.text.rstrip()
        if not tail or tail[-1] not in _CONTINUATION:
            return False

    if abs(_cy(nxt) - _cy(pieces[0])) > ROW_JOIN_CENTER_TOLERANCE * lh:
        return False

    h_last, h_next = float(last.height), float(nxt.height)
    if h_last <= 0 or h_next <= 0:
        return False
    return abs(h_next - h_last) / max(h_last, h_next) <= ROW_JOIN_HEIGHT_TOLERANCE


def _merge_row(pieces: list[TextBlock]) -> TextBlock:
    """把同一行的若干碎片合成一块：外接矩形 + 无缝拼接文本 + 行内行高。"""
    boxes = [p.box for p in pieces]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return TextBlock(
        index=0,
        polygon=(
            (float(x0), float(y0)),
            (float(x1), float(y0)),
            (float(x1), float(y1)),
            (float(x0), float(y1)),
        ),
        text=join_lines([p.text for p in pieces]),
        confidence=min(p.confidence for p in pieces),
        vertical=False,
        line_height=float(statistics.median(p.height for p in pieces)),
        # 拼完仍然只是**一条**视觉行：行数保持 1，否则字号会按多行段落推导
        line_count=1,
    )


def join_row_fragments(
    blocks: list[TextBlock],
    *,
    gap_ratio: float = ROW_JOIN_GAP_RATIO,
    comma_gap_ratio: float = ROW_JOIN_COMMA_GAP_RATIO,
) -> list[TextBlock]:
    """把被检测器切成左右几块的**同一行**拼回一条。

    必须在 :func:`group_blocks` **之前**调用：跨行聚合只跟"上一行"比较，
    一行碎成两块时右碎片谁也合不进去，整段会散成碎片。

    Args:
        blocks: 行级块（顺序不限，内部按阅读顺序重排）。
        gap_ratio: 允许的最大水平间隙（相对行高）。``<= 0`` 关闭拼接。
        comma_gap_ratio: 左碎片以逗号结尾时放宽到的间隙。

    Returns:
        拼接后的块列表，``index`` 已重排为 ``0..n-1``。竖排块原样保留。
    """
    if not blocks:
        return []
    if len(blocks) < 2 or gap_ratio <= 0:
        return _renumber(sorted(blocks, key=lambda b: (b.box[1], b.box[0])))

    horizontal = [b for b in blocks if not b.vertical]
    out: list[TextBlock] = [b for b in blocks if b.vertical]

    # 先按 y 聚成行。这里用"行高比例"作容差，与 group_blocks 的聚类同源：
    # 固定像素阈值在缩略图和海报上会一个太松一个太紧。
    #
    # 块本来就按 y 升序进来，所以"逐行比较"会退化成 O(n²)：320 个块的长图上
    # 这一步实测 258ms，而它本该是微秒级。改成按首块中心 y 排序 + bisect 取
    # 窗口，落到窗口里的行通常只有 1~3 条。
    keys: list[float] = []  # 每行的中心 y（行内首块），升序
    rows: list[list[TextBlock]] = []
    tallest = 1.0
    for block in sorted(horizontal, key=lambda b: (b.box[1], b.box[0])):
        center = _cy(block)
        # 用已出现过的最大行高当作容差基：窗口必须覆盖任何一条可能匹配的行，
        # 精确判据仍在下面用该行自己的行高再算一次。
        tallest = max(tallest, float(block.height))
        tol = ROW_JOIN_CENTER_TOLERANCE * tallest
        hit: list[TextBlock] | None = None
        i = bisect.bisect_left(keys, center - tol)
        while i < len(keys) and keys[i] <= center + tol:
            row = rows[i]
            lh = float(statistics.median([r.height for r in row])) or 1.0
            if abs(center - keys[i]) <= ROW_JOIN_CENTER_TOLERANCE * lh:
                hit = row
                break
            i += 1
        if hit is not None:
            hit.append(block)
        else:
            j = bisect.bisect_left(keys, center)
            keys.insert(j, center)
            rows.insert(j, [block])

    for row in rows:
        row.sort(key=lambda b: b.box[0])
        pieces: list[TextBlock] = [row[0]]
        heights: list[float] = [float(row[0].height)]  # 保持升序，中位数 O(1)
        for nxt in row[1:]:
            if _can_join_row(
                pieces, nxt, gap_ratio, comma_gap_ratio, _median_sorted(heights)
            ):
                pieces.append(nxt)
                bisect.insort(heights, float(nxt.height))
            else:
                out.append(_merge_row(pieces))
                pieces = [nxt]
                heights = [float(nxt.height)]
        out.append(_merge_row(pieces))

    out.sort(key=lambda b: (b.box[1], b.box[0]))
    joined = len(blocks) - len(out)
    if joined > 0:
        logger.info("行内拼接: %d 块 -> %d 块（拼回 %d 个碎片）", len(blocks), len(out), joined)
    return _renumber(out)


def _renumber(blocks: list[TextBlock]) -> list[TextBlock]:
    for i, block in enumerate(blocks):
        block.index = i
    return blocks


def _can_merge(
    last: TextBlock,
    nxt: TextBlock,
    line_height: float,
    profile: tuple[float | None, bool] = (None, False),
) -> bool:
    """``nxt`` 是否紧接在 ``last`` 之后（同一段的下一行）。

    ``profile`` 来自 :func:`_gap_profile`，是 ``(段内行距上限, 能否跨句号)``：
    上限取代 :data:`MAX_GAP_RATIO`；能跨句号意味着本图量出了两簇间距，
    此时"上一行以句号结尾"不再等于段末——一段话里句子之间本来就有句号。
    """
    gap_limit, sentence_ok = profile

    if last.vertical or nxt.vertical:
        # 竖排的"下一行"是另一列，方向语义完全不同，交给竖排逻辑处理
        return False

    # 列表编号优先于几何判据：同一个列表里条目间距与段内行距一样，
    # 只有"下一行以编号开头"能可靠地指出新条目边界。
    if _LIST_MARKER.match(nxt.text):
        return False

    lh = line_height if line_height > 0 else max(last.height, nxt.height)
    if lh <= 0:
        return False

    limit = gap_limit if gap_limit is not None else MAX_GAP_RATIO
    la, lb = last.box, nxt.box
    gap = lb[1] - la[3]

    tail = last.text.rstrip()
    if tail and tail[-1] in _SENTENCE_END:
        if not sentence_ok or gap > limit * lh:
            return False

    if gap > limit * lh:
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


def _gap_profile(ordered: list[TextBlock]) -> tuple[float | None, bool]:
    """从**这张图自己的**行间距分布里量出 ``(段内行距上限, 能否跨句号)``。

    为什么不能写死常量：段内行距是排版者定的，不是我们定的。同一套阈值在
    800x600 的告示图（段内 0.94/1.00、段间 2.4~3.4）上会把段内两行拆开，
    在紧凑长图（段内 0.68、段间 0.73）上又会把两段合并——一份常量不可能
    同时满足，而为某张图调一次，换张图就在别处更糟。

    所以改成量：把"上下相邻、左右有重叠"的块间距按行高归一化，排序后看形状。

    1. **双峰**（最大断层 ≥ :data:`GAP_SPLIT_MIN`，两簇各 ≥
       :data:`GAP_CLUSTER_MIN` 个样本）：小簇是段内行距、大簇是段间距，
       阈值取断层中点，并允许跨句号合并——这是唯一"有证据说明两者不同"的情形。
    2. **单峰但有一簇明显更紧**（≥2 个样本、且比全图中位紧 15% 以上）：
       按"最紧的间距就是段内行距"取上限，但不允许跨句号——两簇咬合时，
       段末只能交给标点判断。
    3. 其余（样本太少、或全是段间距，例如每段只有一行）：返回 ``None``，
       退回 :data:`MAX_GAP_RATIO` 与"句号即段末"的保守路径。

    归一化用两行中**较矮**的行高做分母：OCR 框比字号紧，用真实字高会低估
    比值，宁可让比值偏大（偏大 = 更倾向于不合并 = 误判方向安全）。
    """
    samples: list[float] = []
    for a, b in zip(ordered, ordered[1:]):
        if _x_overlap(a.box, b.box) < MIN_X_OVERLAP:
            continue
        lh = min(a.height, b.height)
        if lh <= 0:
            continue
        gap = b.box[1] - a.box[3]
        if gap >= 0:
            samples.append(gap / lh)

    if len(samples) < GAP_SAMPLE_MIN:
        return None, False

    samples.sort()
    split = max(
        range(1, len(samples)), key=lambda i: samples[i] - samples[i - 1]
    )
    jump = samples[split] - samples[split - 1]
    if (
        jump >= GAP_SPLIT_MIN
        and split >= GAP_CLUSTER_MIN
        and len(samples) - split >= GAP_CLUSTER_MIN
    ):
        return (samples[split - 1] + samples[split]) / 2.0, True

    tight = _tight_upper(samples)
    if tight is not None:
        return max(MAX_GAP_RATIO, tight), False
    return None, False


def _tight_upper(samples: list[float]) -> float | None:
    """单峰分布里"最紧那一簇"的上界；不成立时返回 ``None``。

    判据：最紧的一簇要有至少 :data:`GAP_CLUSTER_MIN` 个样本（排除单个
    贴在一起的离群对），并且比全图中位紧 15% 以上（否则整张图都是段间距，
    例如每段只有一行——这时"最紧"没有意义）。
    """
    lo = samples[0]
    tol = max(lo * 0.15, 0.02)
    cluster = [s for s in samples if s <= lo + tol]
    if len(cluster) < GAP_CLUSTER_MIN:
        return None
    upper = cluster[-1]
    if upper >= 0.85 * statistics.median(samples):
        return None
    return upper


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

    # 阅读顺序：先上后下、再左后右。多栏布局下左右栏会交替出现——所以下面
    # 不能只和"上一个块"比，得保留每条正在生长的段，各自认自己的下一行。
    ordered = sorted(blocks, key=lambda b: (b.box[1], b.box[0]))
    heights = [b.height for b in ordered if b.height > 0]
    global_lh = float(statistics.median(heights)) if heights else 0.0
    # 图级判据：从本图间距分布里量出段内行距上界与"能否跨句号"
    profile = _gap_profile(ordered)

    groups: list[list[TextBlock]] = []
    open_groups: list[list[TextBlock]] = []
    # 段落的末行离当前块超过这么远就关掉：块按 y 递增遍历，后面的只会更远
    horizon = 6 * max(global_lh, 1.0)

    for blk in ordered:
        bx0, _, bx1, _ = blk.box
        target: list[TextBlock] | None = None
        # 从最新创建的段往回找：同栏的上一行总是最近才出现的，
        # 而别的栏的块即使纵向也贴着，也会被 _can_merge 的横向重叠挡住。
        for group in reversed(open_groups):
            if len(group) >= MAX_LINES:
                continue
            last = group[-1]
            # 横向不沾边就直接跳过。_can_merge 的最后一条判据就是横向重叠，
            # 提前判不改变结果，但省掉一次 median 和一串浮点比较——密网格
            # 表格里九成的候选都死在这里（12 列 x 40 行：48ms -> 6ms）。
            lx0, _, lx1, _ = last.box
            if lx1 <= bx0 or bx1 <= lx0:
                continue
            lh = float(statistics.median([b.height for b in group])) or global_lh
            if _can_merge(last, blk, lh, profile):
                target = group
                break
        if target is not None:
            target.append(blk)
        else:
            new_group = [blk]
            open_groups.append(new_group)
            groups.append(new_group)

        open_groups = [
            g for g in open_groups if blk.box[1] - g[-1].box[3] <= horizon
        ]

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
