"""译文排版与回填绘制。

三个必须解决的问题：

**1. 译文长度膨胀。** 中→德/俄通常涨 30~40%，日→英可能翻倍。处理优先级
（按视觉损失从小到大）：缩字号 → 增加行数 → 允许纵向轻微溢出。
横向永不溢出：横向溢出会压到画面其他元素上，比缩字难看得多。

**2. 清晰度。** 直接在小字号上绘制，CJK 笔画会糊成一团。这里用
**局部 4 倍超采样**：只对单块的目标区域建 4x 画布，绘制后 LANCZOS 降采样
再合成回原图。整图放大 4 倍会爆内存，局部放大则代价可忽略。

**3. 背景透明。** 不画任何底板——译文直接"浮"在已经擦干净的背景上。
这是"保持文字背景透明"的第一种语义，也是百度 ``paste=1`` 的做法。
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from dataclasses import dataclass, replace

import cv2
import numpy as np
from PIL import Image, ImageDraw

from ..types import BlockStyle, RenderedBlock, TextBlock
from .fonts import FontBook

logger = logging.getLogger(__name__)

#: 超采样倍数。4 倍对 CJK 小字提升明显，8 倍收益递减且内存翻倍。
SUPERSAMPLE = 4

#: 最小可用字号。再小就不可读了，此时宁可纵向溢出。
MIN_FONT_SIZE = 9

#: 允许的纵向溢出倍数（相对原框高）。超过这个值说明译文实在太长。
MAX_VERTICAL_OVERFLOW = 1.5

#: 行距系数（相对字号）。原文行距无从得知，1.2 是常见排版的折中。
LINE_HEIGHT_RATIO = 1.2

#: 字形墨迹高度 / 字号（em）。
#:
#: 汉字方块字约占 em 的 0.86~0.90，但 mask 是在 Otsu 二值化后量的，
#: 抗锯齿边缘每边多算约 1px，两个偏移几乎抵消，实测比值接近 1.0。
#: 这个数只能给到 ±15% 的精度，所以同图内还要做一次字号归一化
#: （见 :func:`_stabilize_sizes`），别指望单块测量自己就准。
GLYPH_HEIGHT_RATIO = 1.0

#: 同图字号归一化的容差窗口。落在中位数这个范围内的块视为"同一级字号"，
#: 统一到中位数——OCR 框不同、抗锯齿不同会让同字号量出 ±20% 的差异，
#: 表现就是"同一段正文一行大一行小"。
SIZE_CLUSTER_LOW = 0.82
SIZE_CLUSTER_HIGH = 1.20

#: 判断 CJK 的字符范围（含扩展 A、兼容表意文字、假名、谚文）。
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)

#: 允许向右扩展的最大倍数（相对原框宽）。放太宽会破坏版面平衡，
#: 而且译文拉成一条长线后与周围文字的比例关系也会失真。
MAX_EXPAND_RATIO = 3.0

#: 某一列的边缘像素占比超过该值，即认为该列"有内容"，扩展到此为止。
#: 渐变背景的 Canny 边缘占比通常 < 1%，文字/图形边缘会到 10% 以上。
EDGE_COL_RATIO = 0.06

#: 除了比例，还要有**绝对像素数**下限。
#:
#: 比例阈值在矮框上会算出不到 2 个像素（行高 20px × 0.06 = 1.2），
#: 于是擦除残留的一个小墨点就能把扩展彻底挡死——实测"决明子磁石护颈枕"
#: 右侧 2px 处残留 2x3px 墨迹，译文一格都扩不出去，只能折成两行。
#:
#: 取 4 的依据：残留墨点、JPEG 振铃在一列上最多占 2~3 个像素；
#: 而真正需要躲开的东西（竖分隔线、图标、照片边缘、边框）都能穿过整列，
#: 边缘计数是十几到几十。两者差一个数量级，中间留出 4 这个安全分界。
#:
#: 注意**不能用"连续几列都超阈值"来滤噪**：粗边框（4px 纯黑）只有最外侧
#: 两列会产生 Canny 边缘，中间两列是完全均匀的黑、没有边缘——
#: 按"连续"判定会把这条边框判成"空的"，文字径直穿过边框画到框外去。
EDGE_MIN_COUNT = 4

#: 扩宽时与障碍物保持的距离（像素）。贴着边界画会让文字"顶到边框"，
#: 视觉上比缩一点字号更难受。
EXPAND_PADDING = 5

#: 距离画布边缘的最小留白。RTL 文字向左扩时如果没有这个下限，
#: 长译文会一路顶到 x=0，看起来像被裁掉了一截。
EDGE_MARGIN = 6

#: 上述留白的**绝对下限**（像素）。:data:`EDGE_MARGIN` 会随 :func:`_visual_unit`
#: 按字高等比缩放，小字图上会缩到 2~4px——这个量级的留白视觉上等于没有，
#: 实测一张参数图的右栏译文右端距画布只剩 3px，末字的降部几乎被切掉。
#: 留白是为"看起来没有被裁"服务的，它不该跟着字号一起缩。
EDGE_MARGIN_MIN = 8

#: 判定"原文在整幅图里水平居中"时允许的**左右留白差**（像素）。
#:
#: 居中的定义就是左右留白相等，所以直接量留白差，而不是量"中心离画布中心
#: 多远"——后者等价于留白差的 1/2，但没法区分"真居中"与"块太宽所以中心
#: 必然在中间"。实测反例：一张图顶部有一行**左对齐**的正文，宽 656（图宽
#: 790），中心 404，离画布中心只差 9px，按中心判据会被当成居中；但它的
#: 左右留白是 76 / 58，差 18px，按留白差判据一眼就排除。
#:
#: 下限 6px 是为了容忍 OCR 框的抖动；随图宽放宽，4K 图上按 1% 走。
CANVAS_CENTER_MIN_PAD = 6.0
CANVAS_CENTER_PAD_RATIO = 0.01

#: 适用上述判定的块宽上限（占图宽比例）。接近铺满整行的块（正文、表格首行）
#: 居中与否看不出差别，纳入判定只会平白增加误判面。
CANVAS_CENTER_MAX_W = 0.9

#: 纵向扩展的最大倍数（相对原框高）。只在框下方是干净背景时生效，
#: 主力场景是"原文一行、译文两行"——不扩就只能把字号压到 11px。
MAX_VERTICAL_EXPAND_RATIO = 2.2

#: 某一行的边缘像素占比超过该值，认为该行有内容，纵向扩展到此为止。
#: 比横向的 0.06 更低：横向是"列"，一列穿过整段文字行必然高；纵向是"行"，
#: 一行里只要有少量笔画就说明到下一段了。
EDGE_ROW_RATIO = 0.03

#: 纵向扩展后与下方障碍保持的距离。
EXPAND_PADDING_V = 6


#: 短标签允许的最大字符数（纯 CJK）。超过此长度才认为"是一句话"而不是"标签"。
SHORT_LABEL_CJK = 4
#: 向外扩框时与**相邻块**之间必须留出的间隙（相对行高）。
#:
#: 原先只把限制收到邻居的 x0，等于允许译文一路顶到邻居的左边缘。实测：
#: "决明子磁石护颈枕"译成 ``Cassia Seed Magnet Neck Pillow`` 后正好铺满到
#: 右列"产品等级"的起点，两段英文粘成一个 ``PillowProduct grade``。
#: 扩框是为了"别缩太多字号"，不是为了"把空白吃掉"，所以必须留出栏间距。
#: 0.5 倍行高约等于一个词的空格宽度，是排版里最自然的间隙量级。
NEIGHBOUR_GAP_RATIO = 0.5

#: 保持单行时允许缩到的**绝对**字号下限（像素）。
#:
#: 折行会破坏原有版面网格：参数表的一行、图标下的注释、按钮上的文字，
#: 都是按单行设计的，多出一行会把整行撑高、把右侧的值挤走。
#: 但一味要求单行会把长句压成蚂蚁字，所以要有下限。
#:
#: 下限用**绝对像素**而不是"原字号的比例"：比例阈值会误伤表格列——
#: ``安全类别`` → ``security category`` 在 120px 的列里单行只需 13px
#: （原字号 18 的 72%），按 78% 的比例阈值会被判为"太小"而折行，
#: 结果整行被撑高、右边"B类"错位。而 13px 本身完全可读。
SINGLE_LINE_MIN_PX = 11

#: 大字号的块（标题）还要额外受比例下限约束，避免标题被压成正文大小。
SINGLE_LINE_MIN_RATIO = 0.6

#: 只有"短"译文才争取单行。
#:
#: 单行优先是为了保住**标签/短语**所在的行结构；一整句说明文字本来就该折行，
#: 硬压成一行只会得到一个 9px 的蚂蚁字行——比折行糟得多。
#: 40 字符约等于 5~7 个英文词，是"短语"与"句子"的经验分界。
SINGLE_LINE_MAX_CHARS = 40

#: 短标签允许的最大字符数（单个拉丁词）。
SHORT_LABEL_LATIN = 12

#: 竖排列宽 / 字号。汉字在竖排里字身宽约等于字号，留 5% 免得列间粘连。
VERTICAL_COL_RATIO = 1.06

#: 竖排最多拆成几列。再多说明"竖排块本来就装不下这段话"，
#: 与其挤成一堆小字，不如让它退回横排、借用横向空间。
VERTICAL_MAX_COLS = 3

#: 从多行块反推的行距比（行距 / 字号）的接受区间。
#:
#: 低于 1.0 说明测到的"行距"比字号还小，只可能是把行框量宽了；
#: 高于 2.6 通常是块里混进了图注或分隔线，不是真正的段落。
LINE_PITCH_MIN = 1.0
LINE_PITCH_MAX = 2.6

#: 小于这个角度（度）就按水平绘制。
#:
#: OCR 的四边形在水平文字上也有 ±0.5° 的抖动，这种角度去旋转只会引入
#: 双线性插值的模糊，没有任何视觉收益。
MIN_TILT_DEG = 1.5

#: 折行宽度的安全系数。
#:
#: 折行在 1x 下测量、绘制在超采样下进行，而 Pillow 的字形宽度随字号非线性
#: 变化（hinting + 整数舍入）：同一行 4x 下比 1x 宽 2~3%。若折行正好贴到
#: ``box_width`` 上限，patch 又被画布边缘卡住，行尾就会溢出被裁——
#: 实测 ``(3 types of`` 被裁成 ``(3 type``。
#:
#: 留 3% 余量，让"折出来的行"一定装得进 patch。
WRAP_SAFETY = 0.97

#: 视觉间距的基准字高（像素）。
#:
#: :data:`EDGE_MARGIN` / :data:`EXPAND_PADDING` 都是**绝对像素**，只在
#: 40px 上下的正文图上合适：同一个 6px 留白，放在 90px 的标题图上等于没有
#: （字高才 90，留白 6 看着就是贴边），放在 13px 的小图上又显得过于空旷。
#: 一张代码要覆盖从 200px 缩略图到 4K 海报，必须统一按"图中位字高"等比缩放。
BASE_GLYPH = 40.0

#: 缩放系数的夹紧范围。极端尺寸的图不该把间距推到荒谬的量级。
UNIT_MIN = 0.55
UNIT_MAX = 2.6


def _visual_unit(seed: float | None) -> float:
    """把"图中位字高"换算成视觉间距的缩放系数。"""
    if not seed or seed <= 0:
        return 1.0
    return max(UNIT_MIN, min(UNIT_MAX, float(seed) / BASE_GLYPH))


def _supersample(font_size: int) -> int:
    """按字号选超采样倍数。

    4x 是给小字号准备的：12px 汉字在 1x 下笔画会糊成一团。
    而 90px 的标题用 4x 会把 patch 面积放大 16 倍（2000px 宽的标题块
    单张就上百 MB），收益几乎为零——大字号本身的笔画已经很宽，
    缩采样带来的抗锯齿平滑反而会把它糊掉。
    """
    if font_size >= 64:
        return 2
    if font_size >= 32:
        return 3
    return SUPERSAMPLE


def _is_short_label(text: str) -> bool:
    """是否是"图二""写真""Room"这类短标签——它们从不折行。

    折行的代价很直观：``图二``→``写真`` 在 25px 宽的框里被拆成上下两行，
    看起来像排版事故；而缩短到能单行放下的字号只需 12px，可读性完全够。
    真实排版里这类标签要么单行、要么整体换位，绝不会把两个字符竖着摞起来。
    """
    t = text.strip()
    if not t or "\n" in t:
        return False
    if len(t.split()) > 1:
        return False
    if not _CJK_RE.search(t):
        return len(t) <= SHORT_LABEL_LATIN
    cjk = sum(1 for ch in t if _CJK_RE.match(ch))
    return cjk == len(t) and cjk <= SHORT_LABEL_CJK


@dataclass(slots=True)
class Layout:
    """一次排版的完整结果。"""

    font_size: int
    lines: list[str]
    line_height: float
    text_width: float
    text_height: float
    #: ``none`` 原尺寸放下 / ``shrink_font`` 缩过字号 / ``overflow`` 纵向溢出
    fit_mode: str = "none"
    #: ``True`` 表示这是竖排布局：``lines`` 里每个元素是**一列**而非一行。
    vertical: bool = False


def _measure(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[float, float]:
    """测量一行文本的渲染尺寸（不含描边）。"""
    if not text:
        return 0.0, 0.0
    bbox = draw.textbbox((0, 0), text, font=font)
    return float(bbox[2] - bbox[0]), float(bbox[3] - bbox[1])


#: 阿拉伯文 / 希伯来文等从右向左书写的字符区间。
_RTL_RANGES = (
    (0x0590, 0x05FF),  # 希伯来
    (0x0600, 0x06FF),  # 阿拉伯
    (0x0700, 0x074F),  # 叙利亚
    (0x0750, 0x077F),  # 阿拉伯补充
    (0x08A0, 0x08FF),  # 阿拉伯扩展 A
    (0xFB1D, 0xFDFF),  # 希伯来/阿拉伯呈现形式 A
    (0xFE70, 0xFEFF),  # 阿拉伯呈现形式 B
)


def is_rtl_text(text: str) -> bool:
    """判断文本是否以从右向左书写的文字为主。"""
    if not text:
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    rtl_count = sum(1 for ch in letters if any(lo <= ord(ch) <= hi for lo, hi in _RTL_RANGES))
    return rtl_count * 2 >= len(letters)


def shape_rtl_text(text: str) -> str:
    """把 RTL 文本转成"可以直接按 LTR 绘制"的显示序列。

    没有 libraqm 时 Pillow 完全不懂阿拉伯文，会按字符原样从左到右画，
    结果就是整段反过来（实测：译文读起来是倒的），而且字母是孤立形态。

    两步修正：

    1. :mod:`arabic_reshaper` 把每个字符换成正确的呈现形式（词首/词中/词尾/独立），
       这是"连写"的来源——FreeType 本身不做阿拉伯文整形；
    2. :func:`bidi.algorithm.get_display` 做双向算法的重排，得到视觉顺序。

    之后按 LTR 绘制就是正确的视觉结果。缺依赖时原样返回（宁可显示不对，
    也不要在渲染环节抛异常把整张图废掉）。
    """
    if not text:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        logger.warning("缺少 arabic-reshaper / python-bidi，RTL 文本将按原顺序绘制")
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("RTL 整形失败(%s)，按原文本绘制", exc)
        return text


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: float,
) -> list[str]:
    """把文本折成若干行，每行宽度不超过 ``max_width``。

    策略：先按空白切词，再贪心装入；单个词本身超宽时退化为**逐字符**切分。
    这样拉丁文本优先在空格处断行（可读性好），CJK 长串也能正常折行。
    """
    text = text.strip()
    if not text:
        return []

    # 保留原有换行
    paragraphs = text.split("\n")
    lines: list[str] = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 用正则切分，保留空格作为独立标记
        tokens = re.findall(r"\S+\s*|\s+", para)
        current = ""

        for token in tokens:
            candidate = current + token
            width, _ = _measure(draw, candidate.rstrip(), font)
            if width <= max_width or not current:
                # 单个 token 本身就超宽，且不是 CJK：整词独占一行，允许横向溢出。
                # 曾经这里逐字符切开，结果 "Originator" 被劈成 "Originato" + "r"，
                # 断词在视觉上比溢出难看得多——拉丁词被拦腰截断是可读性问题，
                # 溢出只是美观问题，而图片翻译里位置本来就会微调。
                if width > max_width and not current and not _CJK_RE.search(token):
                    lines.append(token.rstrip())
                    current = ""
                    continue
                # CJK 长串没有词边界，逐字符切是唯一办法
                if width > max_width and not current:
                    for ch in token:
                        trial = current + ch
                        w2, _ = _measure(draw, trial, font)
                        if w2 > max_width and current:
                            lines.append(current)
                            current = ch
                        else:
                            current = trial
                    continue
                current = candidate
            else:
                lines.append(current.rstrip())
                current = token.lstrip()

        if current.strip():
            lines.append(current.rstrip())

    return [ln for ln in lines if ln.strip()] or [text]


def _layout_at_size(
    text: str,
    size: int,
    box_width: float,
    box_height: float,
    font_book: FontBook,
    lang: str | None,
    *,
    no_wrap: bool = False,
    line_height_ratio: float = LINE_HEIGHT_RATIO,
) -> Layout | None:
    """在指定字号下尝试排版，放不下返回 ``None``。

    ``no_wrap`` 用于短标签：不允许折行，只能靠缩字号去适配宽度。
    ``line_height_ratio`` 由调用方从原图实测得到（见 :func:`_line_pitch_ratio`），
    缺省用 :data:`LINE_HEIGHT_RATIO`。
    """
    font = font_book.get_font(lang, size)
    # 用一个临时 draw 对象测量（Pillow 要求 draw 与 image 绑定，
    # 这里只为拿 textbbox，1x1 画布足够）
    scratch = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(scratch)

    lines = (
        [text.strip()]
        if no_wrap
        else wrap_text(draw, text, font, box_width * WRAP_SAFETY)
    )
    if not lines:
        return None

    line_height = size * line_height_ratio
    total_height = line_height * len(lines)

    widest = max((_measure(draw, ln, font)[0] for ln in lines), default=0.0)

    if widest > box_width + 0.5:
        return None
    if total_height > box_height + 0.5:
        return None

    return Layout(
        font_size=size,
        lines=lines,
        line_height=line_height,
        text_width=widest,
        text_height=total_height,
    )


def _edge_map(image_bgr: np.ndarray) -> np.ndarray:
    """整图边缘掩码（uint8，0/255）。

    **只对灰度图做 Canny 是不够的**：红底 ``(220,60,70)`` 与深蓝背景
    ``(69,99,162)`` 的灰度分别是 109 和 97，只差 12 —— 灰度 Canny 完全看不到这条边，
    结果是按钮上的译文会画出按钮外。

    所以补上 LAB 的两个色度通道：``a``/``b`` 对"等亮度但不同色"的边界很敏感，
    红蓝交界在那里是强边缘。三张边缘图取并集。
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    edges = cv2.bitwise_or(edges, cv2.Canny(lab[:, :, 1], 40, 120))
    edges = cv2.bitwise_or(edges, cv2.Canny(lab[:, :, 2], 40, 120))
    return edges


def _edge_threshold(length: int, ratio: float) -> float:
    """某一列/行的边缘像素数达到多少算"有内容"。

    比例项负责随框的尺寸自适应，绝对项负责滤掉残留墨点与振铃
    （见 :data:`EDGE_MIN_COUNT`）。
    """
    return max(float(EDGE_MIN_COUNT), length * ratio)


def _neighbour_gap(block: TextBlock, unit: float = 1.0) -> int:
    """扩框时与相邻块之间要留的间隙（像素）。"""
    lh = block.line_height if block.line_height > 0 else float(block.box[3] - block.box[1])
    return int(max(EXPAND_PADDING * unit, NEIGHBOUR_GAP_RATIO * lh))


def _available_width(
    block: TextBlock,
    blocks: list[TextBlock],
    edges: np.ndarray,
    image_width: int,
    *,
    direction: str = "right",
    unit: float = 1.0,
) -> int:
    """算这个块能向某侧扩展到多宽。

    原框是 OCR 给出的**紧贴原文**的框，而译文常常是原文的 2~3 倍长。
    只缩字号的话，"限时优惠"（4 字）译成 "Limited time offer"（18 字符）
    会被压到原字号的一半以下，视觉上明显"没翻译好"。
    所以先争取横向空间，实在没空间才缩字号。

    ``direction`` 决定往哪边扩：

    * ``right``（默认）：LTR 文字左对齐，向右扩，右端自由；
    * ``left``：RTL 文字（阿拉伯/希伯来）右对齐，**右端必须贴住原框右边缘**，
      只能往左扩。

    三重约束，取最严的那个：

    1. **图像边界**；
    2. **同层相邻块**：只挡住 y 方向有重叠的块，上下相邻的不算（它们不占同一行）；
    3. **视觉内容**：从原框边缘向外逐列看，一旦某列的边缘像素占比超过
       :data:`EDGE_COL_RATIO`，说明那里有图形/文字，立刻停。

    第 3 条是关键：OCR 认不出的东西（图标、装饰线、照片边缘）只有边缘能发现。
    只靠第 2 条会把这些内容压掉。

    提醒：``edges`` 必须来自**已擦除文字的底图**。原图上本块的文字会形成
    密集边缘，导致"向外一格就撞墙"，扩框永远不生效。
    """
    x0, y0, x1, y1 = block.box
    box_w = max(1, x1 - x0)
    going_left = direction == "left"
    gap = _neighbour_gap(block, unit)
    # 描边留白按图中位字高等比缩放（见 _visual_unit），但**画布边缘的留白
    # 有绝对下限**：它是"看起来有没有被裁"的问题，不该随字号一起缩。
    pad = int(round(EXPAND_PADDING * unit))
    edge = max(EDGE_MARGIN_MIN, int(round(EDGE_MARGIN * unit)))

    # --- 约束 2：同层最近块 ---
    if going_left:
        limit = 0
        for other in blocks:
            if other is block:
                continue
            ox0, oy0, ox1, oy1 = other.box
            if oy1 <= y0 or oy0 >= y1:
                continue
            if ox1 <= x0:
                limit = max(limit, ox1 + gap)
    else:
        # 画布右缘留白：贴到 x=width 的文字看起来像被裁掉了半个字
        limit = image_width - edge
        for other in blocks:
            if other is block:
                continue
            ox0, oy0, ox1, oy1 = other.box
            if oy1 <= y0 or oy0 >= y1:  # y 方向不重叠，不在同一行
                continue
            if ox0 >= x1:  # 在右边，留出栏间距再停
                limit = min(limit, ox0 - gap)

    # --- 最大扩展倍数 ---
    if going_left:
        # limit 是"能扩到的最左坐标"，越小空间越大；先压到 x0 以内
        limit = min(limit, x0)
        hard_cap = max(edge, x1 - int(box_w * MAX_EXPAND_RATIO))
        limit = max(limit, hard_cap)
        if limit >= x0:
            return box_w
    else:
        limit = max(limit, x1)
        hard_cap = min(image_width, x0 + int(box_w * MAX_EXPAND_RATIO))
        limit = min(limit, hard_cap)
        if limit <= x1:
            return box_w

    # --- 约束 3：逐列看边缘密度 ---
    row_slice = edges[max(0, y0) : min(edges.shape[0], y1), :]
    if row_slice.size == 0:
        return (limit - x0) if not going_left else (x1 - limit)
    col_edges = (row_slice > 0).sum(axis=0)
    threshold = _edge_threshold(max(1, row_slice.shape[0]), EDGE_COL_RATIO)
    if going_left:
        stop = limit
        for x in range(min(x0, col_edges.shape[0]) - 1, limit - 1, -1):
            if col_edges[x] > threshold:
                stop = x + 1
                break
        if stop > limit:
            stop = min(x0, stop + pad)
        return max(box_w, x1 - stop)

    stop = limit
    for x in range(x1, min(limit, col_edges.shape[0])):
        if col_edges[x] > threshold:
            stop = x
            break

    # 留一点边距，别让译文正好顶到障碍物
    if stop < limit:
        stop = max(x1, stop - pad)

    return max(box_w, stop - x0)


def _available_height(
    block: TextBlock,
    blocks: list[TextBlock],
    edges: np.ndarray,
    image_height: int,
    *,
    unit: float = 1.0,
) -> int:
    """算这个块能向**下方**扩展到多高。

    最典型的场景：原文一行（"温度也开始下降。"），译文变两行，
    框高只够一行，于是字号被压到 11px——而它下方本来就是段落间距的空白。
    这时"往下借空间"比"缩字号"的视觉损失小得多。

    只往下扩、不往上扩：往上会压住上一段，且与阅读方向相反。

    三重约束，与 :func:`_available_width` 同理：

    1. **图像下边界**；
    2. **下方最近的块**：要求水平区间有重叠（上下堆叠的块才算障碍，
       左右并排的不算）；
    3. **视觉内容**：逐行看边缘像素占比，撞到图形/照片立刻停。

    另有一道硬上限 :data:`MAX_VERTICAL_EXPAND_RATIO`，防止某段在整页空白里
    一路长到画布底部，把版面比例彻底改掉。
    """
    x0, y0, x1, y1 = block.box
    box_h = max(1, y1 - y0)

    # 行高用来换算"至少该留多少段间距"，合并过的段落用单行行高
    lh = block.line_height if block.line_height > 0 else float(box_h)
    # 与下方障碍至少留这么多：比"贴着画"好得多。段落间距是排版的基本呼吸感，
    # 扩框把间距吃光会让相邻两段粘成一坨，比字号小一点更难受。
    gap_keep = max(EXPAND_PADDING_V * unit, 0.45 * lh)

    # --- 约束 1/2：块与画布 ---
    # 底部也要留白：允许顶到画布底边等于允许"最后一行被裁掉"。
    # 与横向同理，留白有绝对下限（见 EDGE_MARGIN_MIN）。
    limit = image_height - max(EDGE_MARGIN_MIN, int(round(EDGE_MARGIN * unit)))
    for other in blocks:
        if other is block:
            continue
        ox0, oy0, ox1, _oy1 = other.box
        if ox1 <= x0 or ox0 >= x1:  # 水平不重叠，不是上下关系
            continue
        if oy0 >= y1:
            limit = min(limit, oy0 - gap_keep)

    # --- 硬上限 ---
    limit = min(limit, y0 + int(box_h * MAX_VERTICAL_EXPAND_RATIO))
    if limit - y1 < 0.5 * box_h:
        # 只多出不到半行：扩了也装不下多一行，白白让文字贴近下一段
        return box_h
    limit = int(limit)

    # --- 约束 3：逐行看边缘密度 ---
    band = edges[y1:limit, max(0, x0) : min(edges.shape[1], x1)]
    if band.size == 0:
        return box_h
    row_edges = (band > 0).sum(axis=1)
    width = max(1, min(edges.shape[1], x1) - max(0, x0))
    threshold = _edge_threshold(width, EDGE_ROW_RATIO)

    for i, count in enumerate(row_edges):
        if count > threshold:
            # 撞到内容：留出呼吸空间再返回
            return max(box_h, (y1 + i) - y0 - int(gap_keep))

    return max(box_h, limit - y0)



def fit_block(
    text: str,
    box_width: float,
    box_height: float,
    font_book: FontBook,
    lang: str | None,
    *,
    initial_size: int | None = None,
    prefer_single_line: bool = False,
    line_height_ratio: float = LINE_HEIGHT_RATIO,
) -> Layout:
    """为一段译文找出能塞进目标框的最大字号。

    实现是**从大到小**的线性扫描而非二分：``wrap_text`` 的结果随字号
    非单调变化（缩字号可能减少行数，也可能因为更宽而改变断词位置），
    二分会漏掉更优解。字号范围只有几十档，线性扫描的代价可以忽略。

    ``prefer_single_line`` 用于"原文本就是单行"的块：先在可读字号区间内
    试着保住单行，保不住再允许折行。折行本身不丢字，但它会破坏版面网格
    （参数表的一行被撑高、右侧的值被挤走），视觉上比字号小两号更糟。

    ``line_height_ratio`` 是行距 / 字号。原图是多行段落时必须传实测值：
    正文行距常见 1.5~1.8，固定用 1.2 会把段落压成"挤在一起的四行"，
    整段高度比原文矮三分之一，与上下元素的间距全乱。

    所有字号都放不下时，允许纵向溢出到 ``MAX_VERTICAL_OVERFLOW`` 倍框高，
    再不行就用最小字号硬排（标记 ``overflow``）——总比不画要好，
    因为"没翻译"比"翻译了但略挤"对用户更糟。
    """
    box_w = max(1.0, float(box_width))
    box_h = max(1.0, float(box_height))

    # 原文高度就是最好的起点：译文与原文同字号时视觉最连贯
    top = int(initial_size) if initial_size else int(box_h)
    top = max(MIN_FONT_SIZE, min(top, int(box_h * 1.4) + 2))

    # 短标签（"图二"/"写真"/"Room"）不允许折行：两个字符竖着摞起来像排版事故，
    # 宁可缩到更小的字号也要保持单行。
    no_wrap = _is_short_label(text)

    # 单行原文优先单行：只在"字号还看得过去"的区间里尝试
    if prefer_single_line and not no_wrap and len(text.strip()) <= SINGLE_LINE_MAX_CHARS:
        floor = max(
            MIN_FONT_SIZE,
            SINGLE_LINE_MIN_PX,
            int(round(top * SINGLE_LINE_MIN_RATIO)),
        )
        for size in range(top, floor - 1, -1):
            layout = _layout_at_size(
                text, size, box_w, box_h, font_book, lang, no_wrap=True,
                line_height_ratio=line_height_ratio,
            )
            if layout is not None:
                if size < top * 0.9:
                    layout.fit_mode = "shrink_font"
                return layout

    for size in range(top, MIN_FONT_SIZE - 1, -1):
        layout = _layout_at_size(
            text, size, box_w, box_h, font_book, lang, no_wrap=no_wrap,
            line_height_ratio=line_height_ratio,
        )
        if layout is not None:
            if size < top * 0.9:
                layout.fit_mode = "shrink_font"
            return layout

    # 框内放不下：允许纵向溢出
    relaxed_h = box_h * MAX_VERTICAL_OVERFLOW
    for size in range(top, MIN_FONT_SIZE - 1, -1):
        layout = _layout_at_size(
            text, size, box_w, relaxed_h, font_book, lang, no_wrap=no_wrap,
            line_height_ratio=line_height_ratio,
        )
        if layout is not None:
            layout.fit_mode = "overflow"
            return layout

    # 兜底：最小字号单行硬排
    font = font_book.get_font(lang, MIN_FONT_SIZE)
    scratch = Image.new("L", (1, 1))
    draw = ImageDraw.Draw(scratch)
    lines = wrap_text(draw, text, font, box_w * WRAP_SAFETY)
    line_height = MIN_FONT_SIZE * line_height_ratio
    return Layout(
        font_size=MIN_FONT_SIZE,
        lines=lines or [text],
        line_height=line_height,
        text_width=max((_measure(draw, ln, font)[0] for ln in lines), default=0.0),
        text_height=line_height * max(1, len(lines)),
        fit_mode="overflow",
    )


def _line_pitch_ratio(block: TextBlock, seed: float) -> float:
    """从多行块反推原图的行距比（行距 / 字号）。

    为什么必须实测：正文行距常见 1.5~1.8，标题 1.0~1.1，而
    :data:`LINE_HEIGHT_RATIO` 只有一个 1.2。用固定值回填多行段落，
    整段会比原文矮三分之一——上下留白突然变大，与相邻元素的间距关系全乱，
    看起来像"文字被人捏扁了"。

    推导：段落框高 = 首行框高 + (行数-1) × 行距，所以

    ``行距 = (段落高 - 单行框高) / (行数 - 1)``

    单行块没有行距信息，返回默认值。
    """
    if block.line_count < 2:
        return LINE_HEIGHT_RATIO
    lh = block.line_height
    total = block.height
    if lh <= 0 or total <= 0 or seed <= 0:
        return LINE_HEIGHT_RATIO
    pitch = (total - lh) / (block.line_count - 1)
    if pitch <= 0:
        return LINE_HEIGHT_RATIO
    ratio = pitch / seed
    if not (LINE_PITCH_MIN <= ratio <= LINE_PITCH_MAX):
        return LINE_HEIGHT_RATIO
    return ratio


def _is_cjk_text(text: str) -> bool:
    """文本是否以 CJK 为主（决定竖排是否还讲得通）。"""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cjk = sum(1 for ch in letters if _CJK_RE.match(ch))
    return cjk * 2 >= len(letters)


def _layout_vertical(
    text: str,
    box_width: float,
    box_height: float,
    font_book: FontBook,
    lang: str | None,
    *,
    initial_size: int | None = None,
) -> Layout | None:
    """竖排排版：逐字自上而下，列自右向左（CJK 传统排版）。

    只在目标语言是 CJK 时有意义。把 ``No repost`` 竖着摞成八行不是"保留原版式"，
    是把可读性彻底破坏——所以调用方要先判断 :func:`_is_cjk_text`。

    列数超过 :data:`VERTICAL_MAX_COLS` 时返回 ``None``，让调用方改走横排
    （横排能向右借空间，比在窄框里挤成三列小字好得多）。
    """
    chars = [c for c in text.strip() if not c.isspace()]
    if not chars:
        return None
    box_w = max(1.0, float(box_width))
    box_h = max(1.0, float(box_height))

    top = int(initial_size) if initial_size else int(min(box_w, box_h))
    # 竖排的列宽受框宽限制：字号不能超过框宽，否则一列都放不下
    top = max(MIN_FONT_SIZE, min(top, int(box_w / VERTICAL_COL_RATIO)))

    scratch = ImageDraw.Draw(Image.new("L", (1, 1)))
    for size in range(top, MIN_FONT_SIZE - 1, -1):
        col_w = size * VERTICAL_COL_RATIO
        per_col = max(1, int(box_h // size))
        cols = -(-len(chars) // per_col)  # 向上取整
        if cols > VERTICAL_MAX_COLS:
            continue
        if cols * col_w > box_w + 0.5:
            continue
        columns = [
            "".join(chars[i * per_col : (i + 1) * per_col]) for i in range(cols)
        ]
        font = font_book.get_font(lang, size)
        widest = max(
            (_measure(scratch, ch, font)[0] for ch in chars), default=0.0
        )
        return Layout(
            font_size=size,
            lines=columns,
            line_height=float(size),
            text_width=float(cols) * col_w,
            # 最高一列的墨迹高度（逐字堆叠，字距 = 字号）
            text_height=float(max(len(c) for c in columns)) * size,
            vertical=True,
            # 单字宽度超过列宽说明这个字体在这个字号下横向溢出，标出来便于排查
            fit_mode="none" if widest <= col_w else "shrink_font",
        )
    return None


def _superimpose_patch(
    canvas: np.ndarray,
    patch_rgba: Image.Image,
    left: int,
    top: int,
) -> None:
    """把 RGBA patch 按 alpha 合成到 BGR 画布上（原地修改）。"""
    arr = np.array(patch_rgba, dtype=np.uint8)
    if arr.size == 0:
        return

    h, w = arr.shape[:2]
    ch, cw = canvas.shape[:2]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(cw, left + w), min(ch, top + h)
    if x1 <= x0 or y1 <= y0:
        return

    # patch 内的对应偏移（处理贴边裁剪）
    px0, py0 = x0 - left, y0 - top
    sub = arr[py0 : py0 + (y1 - y0), px0 : px0 + (x1 - x0)]
    rgb = sub[:, :, :3].astype(np.float32)
    alpha = (sub[:, :, 3:4].astype(np.float32)) / 255.0

    region = canvas[y0:y1, x0:x1].astype(np.float32)
    # patch 是 RGB，画布是 BGR
    blended = rgb[:, :, ::-1] * alpha + region * (1.0 - alpha)
    canvas[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)


def _paint_horizontal(
    draw: ImageDraw.ImageDraw,
    canvas_size: tuple[int, int],
    layout: Layout,
    font,
    style: BlockStyle,
    *,
    box_w: int,
    box_x0: int,
    patch_left: int,
    ss: int,
    fill,
    stroke_w: int,
    stroke_fill,
) -> None:
    """在 patch 内按水平方向逐行绘制。

    对齐基准是**原框**（``box_x0`` / ``box_w``），不是 patch。patch 可能因为
    向右借空间而比原框宽，如果按 patch 居中，一个居中的标题会整段右移
    （实测 40px 字右移 28px，看起来像"标题跑到右边去了"）。
    """
    _patch_w, patch_h = canvas_size
    line_height = layout.line_height * ss
    total_h = line_height * len(layout.lines)
    start_y = (patch_h - total_h) / 2.0
    # 原框左缘在 patch 内的偏移（RTL 时 patch 向左扩，这个值会大于 0）
    dx = (box_x0 - patch_left) * ss

    for i, line in enumerate(layout.lines):
        line_w = _measure(draw, line, font)[0]
        if style.align == "center":
            line_x = dx + (box_w * ss - line_w) / 2.0
        elif style.align == "right":
            line_x = dx + box_w * ss - line_w
        else:
            line_x = dx
        # 以原框为基准算出的 line_x 可能是负的：居中的块、译文又比原框宽时，
        # ``(box_w - line_w)/2`` 会把行首推到 patch 左边之外，行首被裁掉
        # （实测 "No repost" 被裁成 "epost"）。夹紧到 patch 内，
        # 宁可让居中略微失准，也不能丢字。
        line_x = min(max(line_x, 0.0), max(0.0, _patch_w * ss - line_w))
        draw.text(
            (line_x, start_y + i * line_height),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_w,
            stroke_fill=stroke_fill,
        )


def _paint_vertical(
    draw: ImageDraw.ImageDraw,
    canvas_size: tuple[int, int],
    layout: Layout,
    font,
    *,
    ss: int,
    fill,
    stroke_w: int,
    stroke_fill,
) -> None:
    """竖排绘制：每字一格自上而下，列自右向左。

    竖排的"行"是列，所以 ``layout.lines[0]`` 画在**最右**一列。
    每列内部逐字居中，整列在框内垂直居中。

    ``stroke_w`` 是 patch 四周为描边预留的边距（超采样尺度）：列的位置从
    "patch 右缘"量起，所以必须先扣掉这段边距，否则整列会右移一个描边宽度。
    纵向不必扣——patch 高度也同步加了 ``2×stroke_w``，居中公式自动抵消。
    """
    patch_w, patch_h = canvas_size
    col_w = layout.font_size * VERTICAL_COL_RATIO * ss
    step = layout.font_size * ss

    for ci, col in enumerate(layout.lines):
        col_cx = patch_w - stroke_w - (ci + 0.5) * col_w
        total = len(col) * step
        top = (patch_h - total) / 2.0
        for ri, ch in enumerate(col):
            ch_w = _measure(draw, ch, font)[0]
            draw.text(
                (col_cx - ch_w / 2.0, top + ri * step),
                ch,
                font=font,
                fill=fill,
                stroke_width=stroke_w,
                stroke_fill=stroke_fill,
            )


def draw_block(
    canvas: np.ndarray,
    block: TextBlock,
    text: str,
    style: BlockStyle,
    layout: Layout,
    font_book: FontBook,
    lang: str | None,
    avail_w: int | None = None,
    direction: str = "right",
    top_anchor: bool = False,
) -> tuple[int, int, int, int] | None:
    """把一条译文绘制到画布上的原位置（原地修改 ``canvas``）。

    Returns:
        实际绘制区域的 ``(x0, y0, x1, y1)``，未绘制时 ``None``。
        调用方用它把新墨迹并进障碍图，避免后面的块压上来。

    ``avail_w`` 是"可用的横向空间"（见 :func:`_available_width`），默认等于原框宽。
    ``direction="left"`` 表示 RTL 文字：右端固定贴住原框右缘，向左扩。
    无论哪种方向，**贴住的那一侧永远对准原框对应边缘**——否则文字会"漂移"，
    与周围元素的关系全乱。

    两种非水平情形在这里一并处理：

    * ``layout.vertical``：竖排逐字绘制（见 :func:`_paint_vertical`）；
    * 块本身倾斜（``block.slope``）：先在轴对齐 patch 内正常绘制，
      再按角度旋转贴回。倾斜文字若强制画成水平，与原文的错位非常刺眼
      （实测 8° 的贴纸标题会横移到旁边、字号大 39%）。
    """
    x0, y0, x1, y1 = block.box
    box_w = x1 - x0
    box_h = y1 - y0
    if box_w <= 0 or box_h <= 0:
        return

    ss = _supersample(layout.font_size)
    canvas_w = canvas.shape[1]
    font = font_book.get_font(lang, layout.font_size * ss)
    # 按**实际绘制字号**量一遍行宽，用它兜住 patch 宽度。
    #
    # 必须这么做：Pillow 的字形宽度随字号非线性变化（hinting + 整数舍入），
    # 4x 超采样下同一行比 1x 测量宽 2~3%。而折行是在 1x 下算的，
    # patch 又按 1x 的 ``text_width`` 定宽，于是行尾总被裁掉一两个字符——
    # 实测 "and personal differences" 被裁成 "and personal difference;s"，
    # "The final color is" 被裁成 "The final color i"。
    # 宁可多占几像素（新建的栏间距就是为这种情况留的），也不能裁字。
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    drawn_w = max(
        (_measure(probe, ln, font)[0] for ln in layout.lines), default=0.0
    ) / ss
    need_w = int(np.ceil(max(float(layout.text_width), drawn_w))) + EXPAND_PADDING

    tilt = 0.0 if block.vertical else math.degrees(block.slope)
    tilted = abs(tilt) >= MIN_TILT_DEG

    # 倾斜块不能用 avail_w：旋转后 patch 的中心必须落在原框中心，
    # 一旦扩宽，中心就偏了，整段文字会绕着错误的支点转出去。
    want_w = box_w if tilted else max(box_w, avail_w or box_w, need_w)

    if direction == "left":
        # RTL：右端锚在 x1，向左扩，不能越过画布左缘
        patch_w = int(min(want_w, max(1, x1)))
        patch_left = x1 - patch_w
    else:
        patch_w = int(min(want_w, max(1, canvas_w - x0)))
        patch_left = x0
    patch_w = max(1, patch_w)

    overflow = layout.text_height - box_h
    if tilted:
        # 旋转会自己撑出空间，再加纵向 padding 只会让中心偏移
        pad_top = pad_bottom = 0
    elif top_anchor and overflow > 0:
        # 已向下扩过框：文字从原框顶部开始向下生长。绝不能像默认那样
        # "上下各溢出一半"，那会把译文顶到上一段的文字上。
        pad_top = 0
        pad_bottom = int(round(overflow)) + 2
    else:
        pad_top = pad_bottom = max(0, int(overflow / 2) + 2)
    patch_h = box_h + pad_top + pad_bottom
    patch_top = y0 - pad_top

    # 描边画在字形轮廓**之外**，patch 不为它留边距就会被自己的边界裁掉，
    # 回填出来的字比原来"细一圈"（描边字尤其明显）。
    # 四周各外扩 stroke_px，同时把 patch 原点一起外移：
    # 水平方向的平移已经被 ``_paint_horizontal`` 里的 ``dx``（原框左缘在
    # patch 内的偏移）自动吸收，纵向被"在 patch 内居中"吸收，
    # 所以字面的落位与没有描边时完全一致，只是多出一圈描边。
    stroke_px = (
        style.stroke_width if (style.stroke_color and style.stroke_width > 0) else 0
    )
    if stroke_px:
        patch_left -= stroke_px
        patch_top -= stroke_px
        patch_w += 2 * stroke_px
        patch_h += 2 * stroke_px

    big = Image.new("RGBA", (patch_w * ss, patch_h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)

    fill = (*style.fg_color, 255)
    stroke_w = 0
    stroke_fill = None
    if stroke_px:
        stroke_w = stroke_px * ss
        stroke_fill = (*style.stroke_color, 255)

    if layout.vertical:
        _paint_vertical(
            draw, big.size, layout, font,
            ss=ss, fill=fill, stroke_w=stroke_w, stroke_fill=stroke_fill,
        )
    else:
        _paint_horizontal(
            draw, big.size, layout, font, style,
            box_w=box_w, box_x0=x0, patch_left=patch_left, ss=ss,
            fill=fill, stroke_w=stroke_w, stroke_fill=stroke_fill,
        )

    if tilted:
        # 在超采样尺度上旋转，再缩回原尺寸：先缩小再旋转会把笔画插值糊掉
        rotated = big.rotate(-tilt, resample=Image.BICUBIC, expand=True)
        small = rotated.resize(
            (max(1, rotated.width // ss), max(1, rotated.height // ss)),
            Image.LANCZOS,
        )
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        left = int(round(cx - small.width / 2.0))
        top = int(round(cy - small.height / 2.0))
        _superimpose_patch(canvas, small, left, top)
        return (left, top, left + small.width, top + small.height)

    small = big.resize((patch_w, patch_h), Image.LANCZOS)
    _superimpose_patch(canvas, small, patch_left, patch_top)
    return (patch_left, patch_top, patch_left + patch_w, patch_top + patch_h)


def _seed_size(block: TextBlock, style: BlockStyle, box_h: float) -> float:
    """估计这一块原文字号，用于回填时的初始字号。

    三种块的"字高"来源完全不同，共用一套推导必然错：

    * **水平块**：用 mask 实测的墨迹高度，最准；
    * **倾斜块**：墨迹在轴对齐外接框里的高度等于"整行长度 × sin(倾角) + 行高"，
      一个 8° 的标题会量出 1.4 倍字高（实测字号 61 vs 真实 44）。
      必须改用四边形自身的高（``block.height``，即 OBB 短边）；
    * **竖排块**：字在列宽方向，所以取框宽。
    """
    if block.vertical:
        return block.width if block.width > 0 else float(box_h)
    if block.height > 0 and abs(block.slope) >= math.radians(MIN_TILT_DEG):
        return float(block.height)
    if style.text_height > 0:
        return style.text_height / GLYPH_HEIGHT_RATIO
    if block.line_height > 0:
        return float(block.line_height)
    return float(box_h)


def _is_canvas_centered(
    block: TextBlock, blocks: list[TextBlock], style: BlockStyle, image_w: int
) -> bool:
    """原文是否属于"整幅图里水平居中"的那类文字。

    典型形态是大标题、图注、页脚免责声明：它们**不属于任何一栏**，而是
    相对于画布居中。这类块的 OCR 框紧贴字形，框内左右都没有留白，
    :func:`_estimate_align` 无从判断，只能按左对齐回填——于是译文从原文
    左端一路向右铺开，中心整体右移。实测一张参数图的标题中心从 396 移到
    519（图宽 790），右端距画布只剩 29px，看起来整段标题"跑偏了"。

    判据用**框的左右留白是否相等**，而不是框内留白（框紧贴字形时框内没有
    留白可言），也不是"中心离画布中心多远"。居中的定义就是左右留白相等；
    而中心判据区分不了"真居中"与"块太宽、中心必然在中间"。实测反例：一张
    图顶部有一行**左对齐**的正文，宽 656（图宽 790），中心 404，离画布中心
    只差 9px，按中心判据会被误判；但它的左右留白是 76 / 58，差 18px，
    按留白差一眼排除。

    另有三道限制把误判面压到最低：

    * 只对原本判成左对齐的块生效（已判出 center/right 的不用管）；
    * 竖排、倾斜块不参与——它们的坐标系不是简单的水平居中；
    * **同一行还有别的块时不参与**。多栏版面（两栏参数、三栏表格）的中间
      那栏，左右留白可能恰好对称。它和左右栏是并列关系，必须按栏内对齐
      处理，居中回填会向两侧扩、压进左右栏。
    * 块宽超过图宽的 :data:`CANVAS_CENTER_MAX_W` 时不参与。
    """
    if style.align != "left" or block.vertical:
        return False
    if abs(block.slope) >= math.radians(MIN_TILT_DEG):
        return False
    x0, y0, x1, y1 = block.box
    box_w = x1 - x0
    if box_w <= 0 or box_w > image_w * CANVAS_CENTER_MAX_W:
        return False
    tol = max(CANVAS_CENTER_MIN_PAD, image_w * CANVAS_CENTER_PAD_RATIO)
    if abs(x0 - (image_w - x1)) > tol:
        return False
    for other in blocks:
        if other is block:
            continue
        oy0, oy1 = other.box[1], other.box[3]
        if oy1 <= y0 or oy0 >= y1:  # y 不重叠，不在同一行
            continue
        return False
    return True


def _centered_proxy(
    block: TextBlock,
    blocks: list[TextBlock],
    edges: np.ndarray,
    image_w: int,
    *,
    unit: float,
) -> TextBlock | None:
    """把居中块换成"以画布中心为对称轴"的代理块，并返回它可用的对称宽度。

    为什么要换块而不是在绘制时改对齐基准：居中的译文通常比原文宽得多，
    左右**两侧**都要借空间。而 :func:`_available_width` 只朝一个方向扩，
    patch 也只朝一侧长。把框直接平移成"左右等宽"的对称框后，原有的
    ``align="center"`` 逻辑（相对框居中）正好给出画布居中，不必改绘制代码。

    对称宽度取两侧净空的**较小值**：单侧空间大、另一侧没有时，居中会让
    文字压到障碍物上，还不如按原框宽度缩字号。
    """
    x0, y0, x1, y1 = block.box
    center = image_w / 2.0
    # 反推两侧净空的边界：两个方向都只返回"总可用宽"，各自减去框的同侧边
    left_stop = x1 - _available_width(
        block, blocks, edges, image_w, direction="left", unit=unit
    )
    right_stop = x0 + _available_width(
        block, blocks, edges, image_w, direction="right", unit=unit
    )
    half = min(center - left_stop, right_stop - center)
    if half < (x1 - x0) / 2.0:
        # 有一侧根本没有空间，居中会把文字推到障碍物上
        return None
    nx0 = int(round(center - half))
    nx1 = int(round(center + half))
    return replace(
        block,
        polygon=((float(nx0), float(y0)), (float(nx1), float(y0)),
                 (float(nx1), float(y1)), (float(nx0), float(y1))),
    )


def _stabilize_sizes(seeds: list[float]) -> list[float]:
    """把同图内"同一级"的字号统一到中位数。

    单块测出来的字高有 ±20% 的噪声（框贴合程度、抗锯齿、标点高度都会影响），
    直接当字号用会出现"同一段正文一行 20px 一行 25px"。而真实排版里
    同级的文字字号必然一致，所以把它们聚类后取中位。
    与中位差距超过窗口的（标题、脚注）保持原值，不动。
    """
    valid = [v for v in seeds if v > 0]
    if len(valid) < 3:
        return seeds
    med = statistics.median(valid)
    return [
        med if (SIZE_CLUSTER_LOW * med <= v <= SIZE_CLUSTER_HIGH * med) else v
        for v in seeds
    ]


def render_blocks(
    image_bgr: np.ndarray,
    blocks: list[TextBlock],
    translations: list[str],
    styles: list[BlockStyle],
    font_book: FontBook,
    lang: str | None,
) -> tuple[np.ndarray, list[RenderedBlock]]:
    """把译文批量回填到图中。

    Args:
        image_bgr: 已擦除文字的底图。
        blocks: 文字块（``index`` 为稳定主键）。
        translations: 与 ``blocks`` 等长、同序的译文。
        styles: 与 ``blocks`` 等长的样式。
        font_book: 字体库。
        lang: 目标语言，决定字体族。

    Returns:
        ``(新图, 每块的回填报告)``。
    """
    if not (len(blocks) == len(translations) == len(styles)):
        raise ValueError(
            f"长度不一致: blocks={len(blocks)} translations={len(translations)} "
            f"styles={len(styles)}"
        )

    canvas = image_bgr.copy()
    reports: list[RenderedBlock] = []

    # 边缘图只算一次：扩框判断要靠它，逐块算会重复付出 Canny 的代价。
    # 必须在**擦除之后**的图上算（这里 image_bgr 就是擦除后的底图），
    # 否则本块原文的边缘会把自己挡住。
    edges = _edge_map(image_bgr)
    image_h, image_w = image_bgr.shape[:2]

    seeds = _stabilize_sizes(
        [
            _seed_size(b, st, float(b.box[3] - b.box[1]))
            for b, st in zip(blocks, styles)
        ]
    )

    # 全图共用一把"视觉尺"：间距、留白都按图中位字高等比缩放，
    # 这样同一份代码在 200px 缩略图和 4K 海报上都成立（见 _visual_unit）。
    valid_seeds = [s for s in seeds if s > 0]
    unit = _visual_unit(statistics.median(valid_seeds) if valid_seeds else None)

    for block, target, style, seed in zip(blocks, translations, styles, seeds):
        if not target or target == block.text:
            reports.append(
                RenderedBlock(
                    index=block.index,
                    source=block.text,
                    target=target,
                    painted=False,
                    note="译文与原文相同或为空，跳过绘制",
                )
            )
            continue

        if not style.renderable:
            reports.append(
                RenderedBlock(
                    index=block.index,
                    source=block.text,
                    target=target,
                    painted=False,
                    note="该块判定为不可渲染（竖排或样式无法估计），仅擦除",
                )
            )
            continue

        x0, y0, x1, y1 = block.box
        box_w = max(1, x1 - x0)
        box_h = max(1, y1 - y0)

        # RTL 文字（阿拉伯/希伯来）必须整形 + 双向重排 + 右对齐，
        # 且扩展方向是向左（右端贴住原框右缘）。
        rtl = is_rtl_text(target)
        paint_text = shape_rtl_text(target) if rtl else target
        paint_style = style
        if rtl and style.align != "right":
            paint_style = replace(style, align="right")

        tilt_deg = 0.0 if block.vertical else abs(math.degrees(block.slope))
        tilted = tilt_deg >= MIN_TILT_DEG
        # 竖排只在"译文还是竖排语言"时保留。把 No repost 摞成八行不是
        # 保留版式，是把可读性毁掉——拉丁文竖排在任何语境下都不成立。
        want_vertical = block.vertical and _is_cjk_text(paint_text)
        # 多行段落继承原图行距；单行块没有行距信息，用默认值
        pitch = _line_pitch_ratio(block, seed)

        if tilted:
            # 倾斜块的轴对齐外接框里全是"斜穿的空隙"，逐列扫边缘毫无意义，
            # 只会被自己的笔画挡住。直接用原框尺寸，靠旋转保住版式。
            avail_w, avail_h = box_w, box_h
        else:
            # 先争取周围空间（旁边/下面是干净背景就能扩），再考虑缩字号。
            # 顺序很重要：先缩字号再扩框会用不上扩出来的空间，白留一截空白。
            avail_w = _available_width(
                block, blocks, edges, image_w,
                direction="left" if rtl else "right", unit=unit,
            )
            avail_h = _available_height(block, blocks, edges, image_h, unit=unit)

            # 整幅图居中的文字（大标题、图注、页脚）要**左右对称**借空间：
            # 原文框紧贴字形，align 只能判成左对齐，译文会一路向右铺、
            # 中心整体右移。换成对称代理框后，原有的 align="center" 正好
            # 落在画布中心上，不必改绘制代码。
            if not rtl and _is_canvas_centered(block, blocks, style, image_w):
                proxy = _centered_proxy(block, blocks, edges, image_w, unit=unit)
                if proxy is not None:
                    block = proxy
                    paint_style = replace(paint_style, align="center")
                    avail_w = max(1, block.box[2] - block.box[0])

        layout: Layout | None = None
        if want_vertical:
            layout = _layout_vertical(
                paint_text, avail_w, avail_h, font_book, lang,
                initial_size=int(seed),
            )
            if layout is not None:
                layout.fit_mode = "vertical"
        if layout is None:
            # 竖排放不下（列数超限）或译文不是 CJK：退回横排，
            # 横排能向右借空间，比在窄框里挤成三列小字好得多。
            layout = fit_block(
                paint_text,
                avail_w,
                avail_h,
                font_book,
                lang,
                initial_size=int(seed),
                # 原文本就是单行 -> 译文也尽量单行，除非字号已经小到影响可读
                prefer_single_line=block.line_count <= 1 and not want_vertical,
                line_height_ratio=pitch,
            )

        try:
            painted_box = draw_block(
                canvas,
                block,
                paint_text,
                paint_style,
                layout,
                font_book,
                lang,
                avail_w,
                "left" if rtl else "right",
                # 竖排块改横排时从原框顶部起排：竖排的阅读起点在最右上方，
                # 若像普通块那样上下居中，一小段横排文字会飘在整列高度的正中间，
                # 看起来像"标题掉到半空"。
                top_anchor=avail_h > box_h or (block.vertical and not want_vertical),
            )
        except Exception as exc:  # noqa: BLE001 - 单块失败不应中断整图
            logger.exception("block %d 绘制失败", block.index)
            reports.append(
                RenderedBlock(
                    index=block.index,
                    source=block.text,
                    target=target,
                    painted=False,
                    note=f"绘制异常: {exc}",
                )
            )
            continue

        # 把刚画上的译文并进障碍图：同一行里一个块向右扩、另一块纵向溢出时，
        # 两块 patch 天然会打架，后画的那个会把前一块的字盖掉一半。
        # 让后续块的扩框扫描"看见"已经画上的字，它就会自己停下。
        if painted_box is not None:
            px0, py0 = max(0, painted_box[0]), max(0, painted_box[1])
            px1, py1 = min(image_w, painted_box[2]), min(image_h, painted_box[3])
            if px1 > px0 and py1 > py0:
                edges[py0:py1, px0:px1] |= _edge_map(canvas[py0:py1, px0:px1])

        if avail_w > box_w:
            layout.fit_mode = f"{layout.fit_mode}+expand" if layout.fit_mode != "none" else "expand"
        if avail_h > box_h:
            layout.fit_mode = (
                f"{layout.fit_mode}+expand_v" if layout.fit_mode != "none" else "expand_v"
            )

        reports.append(
            RenderedBlock(
                index=block.index,
                source=block.text,
                target=target,
                painted=True,
                font_size=layout.font_size,
                lines=len(layout.lines),
                fit_mode=layout.fit_mode,
            )
        )

    return canvas, reports
