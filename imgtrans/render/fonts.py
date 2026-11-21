"""字体发现与回退链。

回填译文的字体必须按**目标语言**选，否则会出一整片"豆腐块"（tofu）。
这个模块做两件事：

1. 在常见系统字体目录里找可用字体，按语言给出优先级链；
2. 缓存 ``ImageFont`` 实例（构造大字号字体是系统调用，很贵）。

**为什么不打包字体**：字体授权各不相同，随代码分发有法律风险。
生产环境建议由部署方显式提供字体目录（``font_dirs`` 参数），
本模块只保证"没配也能跑起来"。

回退顺序的设计原则：宁可字形不美观，也不能出 tofu 或问号。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

from PIL import ImageFont

logger = logging.getLogger(__name__)

#: 字体目录最多往下找几层。
#:
#: Linux 的字体目录**天生是多层的**（``/usr/share/fonts/truetype/dejavu/xx.ttf``），
#: 只扫一层会一个字体都找不到，整张图的译文退化成 Pillow 的 10px 位图字体。
#: 这是把服务从 Windows 搬到 Linux 时最容易漏掉、后果又最严重的一处。
#: 深度上限是为了防病态目录：字体树再深也不该有 5 层。
_MAX_SCAN_DEPTH = 4

def _default_font_dirs() -> tuple[str, ...]:
    """按平台给出默认字体目录。

    分平台而不是把所有路径堆在一起：Windows 路径在 Linux 上
    ``expanduser(r"~\\AppData\\...")`` 会展开成 ``/home/u/\\AppData\\...``
    这种垃圾值（虽然 ``is_dir()`` 会挡住，但日志里很难看，也让人误以为扫过）。
    """
    if sys.platform.startswith("win"):
        return (
            r"C:\Windows\Fonts",
            os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
        )
    if sys.platform == "darwin":
        return (
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        )
    return (
        # 发行版字体根目录（Debian/Ubuntu/RHEL/Alpine 都在这里，且是**多层**的）
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/usr/share/local/fonts",
        # 用户级（freedesktop 标准，``fc-cache`` 也认）
        os.path.expanduser("~/.local/share/fonts"),
        os.path.expanduser("~/.fonts"),  # 旧标准，仍有人用
    )


#: 各平台的常见字体目录。测试会整体替换它来模拟 Linux 布局。
_SYSTEM_FONT_DIRS: tuple[str, ...] = _default_font_dirs()

#: 环境变量：额外字体目录，优先级高于系统目录。
#:
#: 容器部署几乎一定要用它——镜像里通常把字体放 ``/opt/fonts``，
#: 而不是往 ``/usr/share/fonts`` 里塞（那个目录可能被基础镜像重置）。
ENV_FONT_DIRS = "IMGTRANS_FONT_DIRS"

_FONT_SUFFIXES = frozenset((".ttf", ".ttc", ".otf", ".otc"))


def _split_dir_list(raw: str) -> list[str]:
    """切分目录列表，同时认 ``;`` 与 ``:``，且不切坏 Windows 盘符。

    不直接用 ``os.pathsep``：配置经常是跨平台复制粘贴的，Linux 上写
    ``/a;/b``、Windows 上写 ``/a:/b`` 都会发生。规则：

    * 出现 ``;`` 就按 ``;`` 切（两种系统的路径里都不会有 ``;``）；
    * 否则按 ``:`` 切，再把"单字母 + 反斜杠开头"的两段拼回盘符。
    """
    if ";" in raw:
        return [p.strip() for p in raw.split(";")]

    merged: list[str] = []
    for part in (p.strip() for p in raw.split(":")):
        if merged and len(merged[-1]) == 1 and merged[-1].isalpha():
            merged[-1] = f"{merged[-1]}:{part}"  # 还原 C:\fonts 这类盘符路径
        else:
            merged.append(part)
    return merged


def font_dirs_from_env() -> tuple[str, ...]:
    """从 ``IMGTRANS_FONT_DIRS`` 读额外字体目录（空则返回空元组）。

    容器部署几乎一定要用它——镜像里字体常放 ``/opt/fonts``，
    而不是塞进 ``/usr/share/fonts``（那个目录可能被基础镜像重置）。
    """
    raw = os.environ.get(ENV_FONT_DIRS, "").strip()
    if not raw:
        return ()
    return tuple(p for p in _split_dir_list(raw) if p)


def _iter_font_files(root: Path) -> Iterator[Path]:
    """递归产出字体文件。

    * 不跟随符号链接：``/usr/share/fonts`` 里常有指回自身的链接，
      跟随会绕成环。
    * 目录与文件名都排序：同一字体在多个子目录出现时，命中结果必须**确定**，
      否则两台机器跑同一张图会选到不同字体。
    * 超过 :data:`_MAX_SCAN_DEPTH` 层就停止下钻，防病态字体树拖慢启动。
    """
    def _onerror(exc: OSError) -> None:  # pragma: no cover - 权限问题少见
        logger.debug("扫描字体目录失败 %s: %s", getattr(exc, "filename", "?"), exc)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_onerror):
        base = Path(dirpath)
        try:
            depth = len(base.relative_to(root).parts)
        except ValueError:  # pragma: no cover - 防御性
            depth = 0
        if depth >= _MAX_SCAN_DEPTH:
            dirnames[:] = []
        dirnames.sort()
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in _FONT_SUFFIXES:
                yield base / name


#: 按语言族给出的候选字体文件名（不含目录），按优先级排列。
#: 文件名而非完整路径，便于跨平台查找。
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    # 中日韩统一表意文字：优先无衬线黑体（UI/海报最常见）
    "cjk": (
        "msyh.ttc",  # 微软雅黑（Win）
        "msyhbd.ttc",
        "simhei.ttf",  # 黑体
        "SourceHanSansSC-Regular.otf",  # 思源黑体
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
        "PingFang.ttc",  # macOS
        "Hiragino Sans GB.ttc",
        "wqy-zenhei.ttc",  # Linux 文泉驿正黑
        "wqy-microhei.ttc",  # Linux 文泉驿微米黑（更常见的轻量版）
        "simsun.ttc",  # 宋体（保底，有衬线但覆盖全）
    ),
    # 日文：字形与中文有别，能用专用字体更好
    "ja": (
        "YuGothR.ttc",
        "msgothic.ttc",
        "SourceHanSansJP-Regular.otf",
        "NotoSansJP-Regular.otf",
        "meiryo.ttc",
        "msyh.ttc",  # 回退到中文字体（日文假名覆盖 OK）
    ),
    # 韩文
    "ko": (
        "malgun.ttf",
        "malgunbd.ttf",
        "SourceHanSansKR-Regular.otf",
        "NotoSansKR-Regular.otf",
        "gulim.ttc",
        "msyh.ttc",
    ),
    # 拉丁/西里尔/希腊
    "latin": (
        "arial.ttf",
        "Arial.ttf",
        "arialbd.ttf",
        "DejaVuSans.ttf",
        "NotoSans-Regular.ttf",
        "LiberationSans-Regular.ttf",
        "segoeui.ttf",
        "msyh.ttc",  # 兜底：雅黑也含拉丁字形
    ),
    # 阿拉伯（RTL，需要整形；Pillow 需 raqm 支持）
    "ar": (
        "arial.ttf",
        "tahoma.ttf",
        "NotoSansArabic-Regular.ttf",
        "NotoNaskhArabic-Regular.ttf",
        "DejaVuSans.ttf",
    ),
    # 泰文
    "th": (
        "leelawui.ttf",
        "tahoma.ttf",
        "NotoSansThai-Regular.ttf",
        "Garuda.ttf",
    ),
    # 天城文（印地语等）
    # 注意 Nirmala 的文件名是 .ttf 而不是 .ttc——把它写成 .ttc 会让整个
    # 索引查不中，然后静默回退到中文字体，印地语全画成方块。
    "devanagari": (
        "nirmala.ttf",
        "mangal.ttf",
        "NotoSansDevanagari-Regular.ttf",
    ),
    # 孟加拉文（bn）。Nirmala 是 Windows 上覆盖南亚诸文字最全的一款，
    # Vrinda 是更老的孟加拉专用字体，两者都缺才回落到 cjk（会出方块）。
    "bengali": (
        "nirmala.ttf",
        "vrinda.ttf",
        "NotoSansBengali-Regular.ttf",
        "NotoSansBengaliUI-Regular.ttf",
    ),
    # 格鲁吉亚文（ka）。中文/拉丁字体几乎没有这套字形，
    # 不单独列出来必然画成一排豆腐块。Sylfaen 是 Windows 自带。
    "georgian": (
        "sylfaen.ttf",
        "NotoSansGeorgian-Regular.ttf",
        "DejaVuSans.ttf",
    ),
    # 希伯来文（he）。**必须独立成族**，不能挂 latin：
    # 它和拉丁语共用族时，get_font 只拿到族名"latin"，无法判断这段文字
    # 需要 RTL 整形，于是永远走 basic layout、整行文字被反过来画。
    # Arial/Tahoma 自带希伯来字形（质量一般但覆盖全），Noto 更规整。
    "hebrew": (
        "arial.ttf",
        "tahoma.ttf",
        "NotoSansHebrew-Regular.ttf",
        "DejaVuSans.ttf",
    ),
}

#: 目标语言码 -> 字体族。未覆盖的回落到 cjk（覆盖面最广）。
_LANG_TO_FAMILY: dict[str, str] = {
    "zh": "cjk",
    "zh-cn": "cjk",
    "zh-tw": "cjk",
    "zh-hk": "cjk",
    "zh-hans": "cjk",
    "zh-hant": "cjk",
    "chinese": "cjk",
    "ja": "ja",
    "jp": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kr": "ko",
    "korean": "ko",
    "ar": "ar",
    "arabic": "ar",
    "fa": "ar",
    "ur": "ar",
    "th": "th",
    "thai": "th",
    "hi": "devanagari",
    "mr": "devanagari",
    "ne": "devanagari",
    "en": "latin",
    "de": "latin",
    "fr": "latin",
    "es": "latin",
    "it": "latin",
    "pt": "latin",
    "ru": "latin",
    "uk": "latin",
    "pl": "latin",
    "tr": "latin",
    "vi": "latin",
    "id": "latin",
    "nl": "latin",
    "sv": "latin",
    # 以下都是"拉丁字母表 + 少量附加符号"，Arial/DejaVu 已覆盖：
    # 希腊、西里尔（保加利亚语）、克罗地亚/捷克/斯洛伐克/斯洛文尼亚的
    # 变音符（č ć ž š đ）、匈牙利语双锐音符、波罗的海与北欧诸语。
    "el": "latin",
    "bg": "latin",
    "hr": "latin",
    "cs": "latin",
    "sk": "latin",
    "sl": "latin",
    "hu": "latin",
    "ro": "latin",
    "da": "latin",
    "no": "latin",
    "fi": "latin",
    "et": "latin",
    "ms": "latin",
    # 希伯来：独立族。挂在 latin 上会让"是否需要 RTL 整形"无从判断
    # （get_font 只拿到族名），整行会被反向绘制。
    "he": "hebrew",
    "bn": "bengali",
    "ka": "georgian",
}


#: 各族用来"自检本机字体能不能画这种文字"的样例。
#:
#: 部署自检（:meth:`FontBook.check_langs`）拿它当探针：语言码能映射到族，
#: 不代表本机真有能画出这些字的字体——这正是 ka 在只装了中文字体的机器上
#: 变成一排方框的原因。样例只取各文字的代表字，不做可信度评估。
FONT_PROBE_TEXT: dict[str, str] = {
    "cjk": "中文测试",
    "ja": "日本語のテスト",
    "ko": "한국어 테스트",
    "ar": "اختبار",
    "th": "ทดสอบ",
    "devanagari": "परीक्षण",
    "bengali": "পরীক্ষা",
    "georgian": "გამარჯობა",
    "hebrew": "שלום",
    "latin": "Test 123",
}


def family_for_lang(lang: str | None) -> str:
    """把目标语言码映射到字体族名。

    也接受**已经是族名**的输入（``georgian`` / ``latin`` / ``cjk``…）：
    :meth:`FontBook.resolve_family` 会把"按字形覆盖选出来的族"继续传给
    :meth:`FontBook.get_font`，这里必须原样透传，否则 ``georgian`` 会被
    当成未知语言码再映射回 ``cjk``，等于白选。
    ``ja`` / ``ko`` / ``ar`` / ``th`` 既是语言码又是族名，且映射到自身，无歧义。
    """
    if not lang:
        return "cjk"
    key = lang.lower()
    if key in FONT_CANDIDATES:
        return key
    return _LANG_TO_FAMILY.get(key, "cjk")


#: 回退到 **latin 家族**字体时，这些目标族必定缺字形（直接画成 □）。
#:
#: 判据必须落在"回退到哪个族的字体"上，而不是"目标族重不重要"：
#: 日文回退到 NotoSansCJK 是**完全正常**的（CJK 字体含假名），
#: 但中文/泰文/天城文回退到 DejaVu/Arial 就是铁定豆腐块。
#: DejaVu 恰好带格鲁吉亚与阿拉伯字形，所以 ka/ar 只在 warning 一档。
_LATIN_FALLBACK_LACKS = frozenset({"cjk", "ja", "ko", "th", "devanagari", "bengali"})

#: 必须经过 **整形（shaping）** 才能画对的字体族。
#:
#: 这些文字不是"一个字符一个字形"：孟加拉/天城的元音符号要重排到辅音左右、
#: 辅音之间要合成连字（ক্ষ）、reph 要挪位；阿拉伯文要按词首/词中/词末选形并
#: 从右往左排；泰文的上下元音要叠在辅音上下。FreeType 只做字形光栅化，
#: 不做这些，所以用 basic layout 画出来**每个字都有**、但拼写是错的——
#: 比缺字形更隐蔽：当地用户一眼看出错，中文用户只会觉得"字有点怪"。
#:
#: 正确路径只有一条：Pillow 带 Raqm（HarfBuzz + FriBiDi）时按 RAQM 排版。
#: 注意键名必须与 :data:`FONT_CANDIDATES` 一致（泰文族是 ``th`` 不是 ``thai``，
#: 写错就整族漏判，且不会有任何报错）。
_SHAPING_FAMILIES = frozenset({"bengali", "devanagari", "th", "ar", "hebrew"})

#: 还需要整形的**语言码**，与 :data:`_SHAPING_FAMILIES` 互为兜底。
#:
#: 主判据是族名（:meth:`FontBook.get_font` 收到的可能就是族名）；语言码这
#: 一层补上映射表将来拆族的情况（mr/ne 天城小语种、fa/ur 阿拉伯语支）。
_SHAPING_LANGS = frozenset({"bn", "hi", "mr", "ne", "th", "ar", "fa", "ur", "he"})


def needs_shaping(lang: str | None) -> bool:
    """该目标语言是否必须整形才能画对（不看当前环境有没有 Raqm）。"""
    if not lang:
        return False
    key = lang.strip().lower().replace("_", "-")
    if key.split("-")[0] in _SHAPING_LANGS:
        return True
    return family_for_lang(key) in _SHAPING_FAMILIES

#: .ttc 里按 face 名挑字形所需的 token。
#:
#: Linux 的 ``NotoSansCJK-Regular.ttc`` 把 JP/KR/SC/TC/HK 五个字面打进同一个
#: 文件，**face 0 是日文**。中文目标直接用 face 0 会得到日式字形——「直」「骨」
#: 「关」这些字的写法与中文不同，用户看不出原因，只会觉得"字有点怪"。
#: Windows 的 msyh.ttc 只有简体字面，探测不到 token 就回落到 index 0，
#: 与旧行为完全一致，所以这个优化最坏情况等于没做，不会引入回归。
_FACE_TOKENS: dict[str, frozenset[str]] = {
    "cjk": frozenset({"sc", "gb", "hans", "simplified"}),
    "ja": frozenset({"jp", "japan"}),
    "ko": frozenset({"kr", "korea"}),
}

#: ttc 里最多探测多少个 face（Noto CJK 是 5 个，16 足够且不会在坏字体上空转）。
_MAX_FACE_PROBE = 16

#: 读 cmap 时最多看多少个子表（真实字体 1~3 个，留余量只为防坏文件空转）。
_MAX_CMAP_SUBTABLES = 8

#: 报告"缺字形"时最多列几个字（日志和块报告都不该被一长串汉字撑爆）。
_MAX_MISSING_REPORT = 24


def _sfnt_tables(data: bytes, face_index: int = 0) -> dict[bytes, tuple[int, int]] | None:
    """读 sfnt 表目录，返回 ``tag -> (offset, length)``。

    同时支持裸 TTF/OTF 与 TTC/OTC（``ttcf`` 头 + face 偏移表）。解析失败返回
    ``None``——**不抛异常**：字体文件可能是空占位、截断文件或另一种容器，
    这条路径的原则是"读不出就当不知道"，而不是让整张图生成失败。
    """
    if len(data) < 12:
        return None
    if data[:4] == b"ttcf":
        entry = 12 + 4 * face_index
        if len(data) < entry + 4:
            return None
        base = int.from_bytes(data[entry : entry + 4], "big")
    else:
        base = 0
    if len(data) < base + 12:
        return None
    num = int.from_bytes(data[base + 4 : base + 6], "big")
    if num <= 0 or num > 512:
        return None
    tables: dict[bytes, tuple[int, int]] = {}
    for i in range(num):
        rec = base + 12 + 16 * i
        if len(data) < rec + 16:
            return None
        tables[data[rec : rec + 4]] = (
            int.from_bytes(data[rec + 8 : rec + 12], "big"),
            int.from_bytes(data[rec + 12 : rec + 16], "big"),
        )
    return tables


def _cmap_format4(data: bytes, off: int) -> frozenset[int]:
    """format 4：BMP 的段式映射，绝大多数中文字体用它。"""
    seg2 = int.from_bytes(data[off + 6 : off + 8], "big")
    seg = seg2 // 2
    if seg <= 0:
        return frozenset()
    ends_at = off + 14
    starts_at = ends_at + seg2 + 2  # 跳过 reservedPad
    deltas_at = starts_at + seg2
    ranges_at = deltas_at + seg2
    if len(data) < ranges_at + seg2:
        return frozenset()
    cps: set[int] = set()
    for i in range(seg):
        end = int.from_bytes(data[ends_at + 2 * i : ends_at + 2 * i + 2], "big")
        start = int.from_bytes(data[starts_at + 2 * i : starts_at + 2 * i + 2], "big")
        delta = int.from_bytes(
            data[deltas_at + 2 * i : deltas_at + 2 * i + 2], "big", signed=True
        )
        range_offset = int.from_bytes(data[ranges_at + 2 * i : ranges_at + 2 * i + 2], "big")
        if start > end:
            continue
        if range_offset == 0:
            # 整段按 delta 平移，没有逐字表
            for cp in range(start, end + 1):
                if (cp + delta) & 0xFFFF:
                    cps.add(cp)
            continue
        for cp in range(start, end + 1):
            addr = ranges_at + 2 * i + range_offset + 2 * (cp - start)
            if addr + 2 > len(data):
                break
            gid = int.from_bytes(data[addr : addr + 2], "big")
            if gid and ((gid + delta) & 0xFFFF):
                cps.add(cp)
    return frozenset(cps)


def _cmap_format12(data: bytes, off: int) -> frozenset[int]:
    """format 12：分组式，能表达 BMP 之外的字符（Noto 系列常用）。"""
    groups = int.from_bytes(data[off + 12 : off + 16], "big")
    cps: set[int] = set()
    for i in range(min(groups, 200_000)):
        rec = off + 16 + 12 * i
        if len(data) < rec + 12:
            break
        start = int.from_bytes(data[rec : rec + 4], "big")
        end = int.from_bytes(data[rec + 4 : rec + 8], "big")
        gid = int.from_bytes(data[rec + 8 : rec + 12], "big")
        if end < start or start > 0x10FFFF:
            continue
        for cp in range(start, min(end, 0x10FFFF) + 1):
            if gid + (cp - start):
                cps.add(cp)
    return frozenset(cps)


def _cmap_codepoints(data: bytes, face_index: int = 0) -> frozenset[int] | None:
    """读字体 cmap，返回它真正有字形的码位集合。读不出返回 ``None``。"""
    tables = _sfnt_tables(data, face_index)
    if not tables or b"cmap" not in tables:
        return None
    cmap_off = tables[b"cmap"][0]
    if len(data) < cmap_off + 4:
        return None

    count = int.from_bytes(data[cmap_off + 2 : cmap_off + 4], "big")
    best_rank = 0
    best_off: int | None = None
    for i in range(min(count, _MAX_CMAP_SUBTABLES)):
        rec = cmap_off + 4 + 8 * i
        if len(data) < rec + 8:
            break
        platform = int.from_bytes(data[rec : rec + 2], "big")
        encoding = int.from_bytes(data[rec + 2 : rec + 4], "big")
        sub = cmap_off + int.from_bytes(data[rec + 4 : rec + 8], "big")
        # 优先级：Unicode 全平面(3,10) > Unicode(0,*) > BMP(3,1)
        if (platform, encoding) == (3, 10):
            rank = 3
        elif platform == 0:
            rank = 2
        elif (platform, encoding) == (3, 1):
            rank = 1
        else:
            rank = 0
        if rank > best_rank:
            best_rank, best_off = rank, sub

    if best_off is None or len(data) < best_off + 2:
        return None
    fmt = int.from_bytes(data[best_off : best_off + 2], "big")
    if fmt == 12 and len(data) >= best_off + 16:
        return _cmap_format12(data, best_off)
    if fmt == 4 and len(data) >= best_off + 14:
        return _cmap_format4(data, best_off)
    return None



class FontBook:
    """字体文件索引与 :class:`ImageFont.FreeTypeFont` 缓存。"""

    def __init__(self, font_dirs: list[str] | tuple[str, ...] | None = None) -> None:
        """
        Args:
            font_dirs: 额外的字体目录（优先于系统目录搜索）。
                生产环境建议显式传入，避免依赖系统装了哪些字体。
        """
        self._extra_dirs = [Path(d) for d in (font_dirs or [])]
        self._index: dict[str, Path] | None = None
        # 键第三项是"是否启用 Raqm"：同一族在不同语言下可能要走不同排版引擎
        self._cache: dict[tuple[str, int, bool], ImageFont.FreeTypeFont] = {}
        self._face_cache: dict[Path, int] = {}
        #: 字体文件 -> 它 cmap 里的码位集合（``None`` 表示读不出）。
        #: 一个字体只解析一次：中文字体两万多个码位，逐块重解析会明显变慢。
        self._cmap_cache: dict[tuple[Path, int], frozenset[int] | None] = {}

    def _build_index(self) -> dict[str, Path]:
        """递归扫描字体目录，建立 ``小写文件名 -> 路径`` 的索引。

        为什么必须递归：Linux 发行版把字体放在 ``/usr/share/fonts/<格式>/<厂商>/``
        **两层子目录**里（如 ``truetype/dejavu/DejaVuSans.ttf``）。只扫顶层时
        索引会是 0 个文件（实测），回填阶段所有译文都退化成 Pillow 内置位图
        字体——字能"出来"，但全是 10px 的粗糙字形，整张图等于废掉。
        Windows 的字体目录是扁平的，所以这个坑只在 Linux/macOS 上暴露。
        """
        index: dict[str, Path] = {}
        dirs = [*self._extra_dirs, *(Path(d) for d in _SYSTEM_FONT_DIRS)]
        scanned: list[str] = []
        for directory in dirs:
            if not directory.is_dir():
                continue
            scanned.append(str(directory))
            for entry in _iter_font_files(directory):
                index.setdefault(entry.name.lower(), entry)
        logger.info(
            "字体索引建立完成，共 %d 个字体文件（扫描 %d 个目录：%s）",
            len(index),
            len(scanned),
            ", ".join(scanned) or "无可用目录",
        )
        return index

    @property
    def index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _family_font(self, family: str) -> Path | None:
        """只在该族自己的候选文件名里找，**不回退**。

        :meth:`find` 要的是"能用的字体"（可以跨族回退），
        而按字形覆盖选族时要的是"这个族到底有没有"，回退会把答案搅混。
        """
        for name in FONT_CANDIDATES.get(family, ()):
            hit = self.index.get(name.lower())
            if hit is not None:
                return hit
        return None

    def find(self, lang: str | None) -> Path | None:
        """为指定语言找一个可用字体文件，找不到返回 ``None``。"""
        family = family_for_lang(lang)
        hit = self._family_font(family)
        if hit is not None:
            return hit

        # 该族没命中时，按 cjk -> latin 顺序回退（覆盖面优先）
        for fallback_family in ("cjk", "latin"):
            if fallback_family == family:
                continue
            for name in FONT_CANDIDATES.get(fallback_family, ()):
                hit = self.index.get(name.lower())
                if hit is not None:
                    self._report_fallback(lang, family, fallback_family, name)
                    return hit

        # 最后手段：索引里任意一个字体
        if self.index:
            name, path = next(iter(self.index.items()))
            logger.error("未找到匹配字体，使用任意可用字体 %s（字形很可能全部错乱）", name)
            return path
        return None

    def codepoints(self, path: Path, family: str | None = None) -> frozenset[int] | None:
        """该字体 cmap 覆盖的码位集合；读不出返回 ``None``（当作"不知道"）。"""
        face = self._face_index(path, family or "cjk")
        key = (path, face)
        if key in self._cmap_cache:
            return self._cmap_cache[key]
        cps: frozenset[int] | None
        try:
            cps = _cmap_codepoints(path.read_bytes(), face)
        except OSError as exc:
            logger.warning("读不出字体 %s 的 cmap：%s", path.name, exc)
            cps = None
        self._cmap_cache[key] = cps
        return cps

    def missing_chars(self, lang: str | None, sample: str) -> str:
        """``sample`` 里该语言字体覆盖不到的字（去重保序，最多 24 个）。

        返回空串有两种含义，调用方都不需要区分：全都有字形，或者字体文件
        读不出来（此时不敢断言缺字，宁可不报）。
        """
        if not sample:
            return ""
        family = family_for_lang(lang)
        unique = list(dict.fromkeys(sample))
        path = self._family_font(family)
        if path is None:
            # 连字体都没有：这些字必然全画成方框，如实报出来
            return "".join(c for c in unique if not c.isspace())[:_MAX_MISSING_REPORT]
        cps = self.codepoints(path, family)
        if cps is None:
            return ""
        missing = [c for c in unique if not c.isspace() and ord(c) not in cps]
        return "".join(missing[:_MAX_MISSING_REPORT])

    def _family_order(self, base: str) -> list[str]:
        """按"最适合顶替"的顺序排列字体族。

        latin（DejaVu）排第二：它带希腊、西里尔、格鲁吉亚、亚美尼亚，
        是多数非 CJK 文字的合理替代；CJK 排最后——它虽然覆盖拉丁，但缺
        上述所有文字，而且拿中文字形画外文很难看。
        """
        order = [base]
        for family in ("latin", *FONT_CANDIDATES):
            if family == "cjk" or family in order:
                continue
            order.append(family)
        if "cjk" not in order:
            order.append("cjk")
        return order

    def resolve_family(self, lang: str | None, sample: str = "") -> str:
        """按**译文实际用到的字符**挑字体族，而不是只看语言码。

        原因：语言码→族的映射是张静态表，猜错时 Pillow 不会报错，只会把
        OCR 不认识的码位全画成 ``.notdef`` 方框（实测：缺格鲁吉亚字体时
        ``გამარჯობა`` 的 9 个字渲染成 9 个一模一样的 □）。日志里那句
        "字形可能不理想"也完全掩盖了严重性。

        所以拿到译文后核对一遍覆盖：
        1. 本族字体覆盖全部字符 -> 用本族（绝大多数情况，零变化）；
        2. 否则按 :meth:`_family_order` 找第一个能全覆盖的族（并说明换了谁）；
        3. 都不全覆盖 -> 选覆盖最多的族，把缺的字**明确报出来**，
           交给上层写进块报告（调用方看得见，而不是只在日志里）。
        """
        base = family_for_lang(lang)
        needs = [c for c in dict.fromkeys(sample) if not c.isspace()]
        if not needs:
            return base

        for family in self._family_order(base):
            path = self._family_font(family)
            if path is None:
                continue
            cps = self.codepoints(path, family)
            if cps is None:
                # 读不出 cmap 就别拦：宁可保持原行为，也不要因为解析器失手换字体
                return family
            if all(ord(c) in cps for c in needs):
                if family != base:
                    logger.warning(
                        "语言 %s 的字体族 %s 缺字形，改用 %s（%s）绘制",
                        lang,
                        base,
                        family,
                        path.name,
                    )
                return family

        best_family, best_path, best_cps, best_covered = base, None, None, -1
        for family in self._family_order(base):
            path = self._family_font(family)
            if path is None:
                continue
            cps = self.codepoints(path, family)
            if cps is None:
                continue
            covered = sum(1 for c in needs if ord(c) in cps)
            if covered > best_covered:
                best_family, best_path, best_cps, best_covered = family, path, cps, covered

        missing = "".join(
            c for c in needs if best_cps is not None and ord(c) not in best_cps
        )[:_MAX_MISSING_REPORT]
        logger.error(
            "语言 %s 有 %d/%d 个字符没有任何字体覆盖（用 %s 绘制，缺：%s）。"
            "这些字会被画成方框，请安装对应字体后重启"
            "（Linux: apt install fonts-noto-core；或设 IMGTRANS_FONT_DIRS）",
            lang,
            len(needs) - max(best_covered, 0),
            len(needs),
            best_path.name if best_path else "无",
            missing or "未知",
        )
        return best_family

    def check_langs(self, langs: Iterable[str]) -> list[str]:
        """部署自检：这些目标语言在本机能不能画出来。

        只对**每种族的代表字**做一次覆盖判断，不做逐字检查（真实译文在
        :func:`render_blocks` 里还会再核对一遍）。这样启动时就能看出
        "这台机器缺哪个文字的字体"，而不是等用户传一张格鲁吉亚语的图、
        拿到一排方框才发现。

        Returns:
            画不出来的语言的说明列表；空列表表示全部可用。
        """
        problems: list[str] = []
        for lang in langs:
            family = family_for_lang(lang)
            probe = FONT_PROBE_TEXT.get(family, "")
            if not probe:
                continue  # 没有探针的族不判（宁可不报，也不要误报）
            resolved = self.resolve_family(lang, probe)
            missing = self.missing_chars(resolved, probe)
            if missing:
                problems.append(
                    f"{lang}（{family}）：没有字体能画 {probe!r}，缺 {missing}"
                )
        return problems

    @staticmethod
    def _report_fallback(lang: str | None, family: str, fallback: str, name: str) -> None:
        """按"会不会出豆腐块"分级告警回退事件。

        三档而不是一律 warning：
        * 日/韩回退到 CJK 字体 —— 正常（CJK 含假名/谚文），debug；
        * 中/泰/天城文/孟加拉回退到 latin —— 铁定 □，error；
        * 其余 —— 字形可能不理想，warning。
        级别混在一起，Linux 上少装字体的真问题就会被当成"字体不好看"忽略掉。
        """
        if fallback == "cjk" and family in ("ja", "ko"):
            logger.debug("语言 %s 无专用字体，回退到 %s（CJK 字体含该文字，正常）", lang, name)
        elif fallback == "latin" and family in _LATIN_FALLBACK_LACKS:
            logger.error(
                "语言 %s 缺专用字体，已回退到 %s —— 极可能出现豆腐块(□)。"
                "请安装对应字体（Linux: fonts-noto-cjk / fonts-noto-core）",
                lang,
                name,
            )
        else:
            logger.warning("语言 %s 无专用字体，回退到 %s（字形可能不理想）", lang, name)

    def _face_index(self, path: Path, family: str) -> int:
        """在 .ttc/.otc 里挑出与目标语言族匹配的 face 序号。

        非 ttc、非 CJK 族、探测失败、找不到匹配 —— 一律返回 ``0``
        （取第一个字面，即旧行为）。所以这个函数**最坏等于不生效**，
        不会把能跑的机器搞坏。
        """
        if family not in _FACE_TOKENS or path.suffix.lower() not in (".ttc", ".otc"):
            return 0
        cached = self._face_cache.get(path)
        if cached is not None:
            return cached

        wanted = _FACE_TOKENS[family]
        chosen = 0
        for idx in range(_MAX_FACE_PROBE):
            try:
                probe = ImageFont.truetype(str(path), size=12, index=idx)
                names = " ".join(probe.getname()).lower()
            except (OSError, ValueError):
                break  # index 越界即到底
            if wanted & set(re.split(r"[\s\-_]+", names)):
                chosen = idx
                break

        if chosen:
            logger.debug(".ttc 字面选择：%s -> index %d（族=%s）", path.name, chosen, family)
        self._face_cache[path] = chosen
        return chosen

    def get_font(self, lang: str | None, size: int) -> ImageFont.FreeTypeFont:
        """取字号为 ``size`` 的字体对象（带缓存）。

        极端情况下没有任何字体文件，退回 Pillow 内置位图字体——
        它无法缩放，只保证"能画出来不崩"，字形会很粗糙。
        """
        # 缓存键带上"是否整形"：整形与否由**语言**决定，而不同语言可能落在
        # 同一族（例如 he 若因换族落到 latin，就与 en 共用族名）。只用
        # (族, 字号) 时，先渲染的那个会把字体对象缓存下来，后来者直接命中——
        # 该整形的拿到 basic 对象（RTL 永远排不对），不该整形的拿到 RAQM 对象
        # （多出 kerning，宽度偏移）。
        use_raqm = needs_shaping(lang) and has_raqm()
        key = (family_for_lang(lang), max(1, int(size)), use_raqm)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self.find(lang)
        font: ImageFont.FreeTypeFont
        if path is None:
            logger.error("系统无可用 TrueType 字体，回退到 Pillow 默认位图字体")
            font = ImageFont.load_default()  # type: ignore[assignment]
        else:
            family = key[0]
            index = self._face_index(path, family)
            # 只对"必须整形"的语言启用 RAQM：Raqm 会额外做 kerning 与连字替换，
            # 中文/日文/拉丁的排版宽度会有微小变化，而这几类的回填效果是逐像素
            # 调过的，没必要为了不需要的功能去动它。南亚/泰/阿拉伯/希伯来则
            # 相反——不开 RAQM 画出来的拼写就是错的。
            kwargs: dict[str, object] = {}
            if use_raqm:
                kwargs["layout_engine"] = ImageFont.Layout.RAQM
            elif needs_shaping(lang):
                logger.warning(
                    "语言 %s 需要文本整形，但当前 Pillow 没有 Raqm："
                    "字母顺序/元音位置/连字都会不正确（不是缺字形，"
                    "图上仍有字）。Linux 装 libraqm0 libfribidi0 后重启可修复。",
                    lang,
                )
            try:
                font = ImageFont.truetype(str(path), size=key[1], index=index, **kwargs)
            except OSError as exc:
                logger.warning("加载字体 %s 失败(%s)，回退默认字体", path, exc)
                font = ImageFont.load_default()  # type: ignore[assignment]

        self._cache[key] = font
        return font


#: ``has_raqm()`` 的结果缓存。形态判定在进程生命周期内不变，但
#: ``features.check`` 每次都要去 dlopen 一遍 FriBiDi，单次 0.05ms，渲染
#: 循环里按块调用就是白白浪费。测试可把它置回 ``None`` 重新探测。
_RAQM_AVAILABLE: bool | None = None


def has_raqm() -> bool:
    """Pillow 是否编译了 Raqm（复杂文字整形）。

    阿拉伯/泰文/天城文/孟加拉文**必须**有 Raqm 才能正确连字与排列，
    否则字母会各自孤立、元音符号错位、RTL 不镜像。

    注意 Pillow 的两个陷阱：

    1. 请求 ``layout_engine=Layout.RAQM`` 而 Raqm 不可用时**只发一条
       UserWarning 就退回 basic**，不抛错——不主动 check 会以为整形生效了；
    2. Raqm 在**运行时**才去加载 C 库，形态随 wheel 而变：有的把 raqm 静态
       编进去、只差 ``fribidi``（Windows ``fribidi-0.dll`` / Linux
       ``libfribidi.so.0``）；有的动态链接 ``libraqm``（本机实测 Pillow 13
       的 win_amd64 wheel 导入表里两者都有）。缺任何一个 check 都是 False，
       而精简镜像恰好两样都没装。
    """
    global _RAQM_AVAILABLE
    if _RAQM_AVAILABLE is None:
        try:
            from PIL import features

            _RAQM_AVAILABLE = bool(features.check("raqm"))
        except Exception:  # noqa: BLE001
            _RAQM_AVAILABLE = False
    return _RAQM_AVAILABLE
