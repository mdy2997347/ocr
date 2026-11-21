"""版式通用性回归：用**生成的版式**做真值，而不是拿几张真图调常量。

为什么这么做：真图数量有限，而且改一个阈值往往"这张好了、那张坏了"，
反馈慢且无法定位。这里把段落的真实结构直接画出来——每块属于哪一段是
已知的（真值），于是"行距 / 段间距 / 字号 / 段内行数 / 栏数"可以铺满，
group_blocks 的输出能逐字比对，改坏了立刻知道坏在哪一组参数上。

覆盖三类判据，对应 group.py 里的三级决策：
1. 间距分布**双峰**：段内与段间分得开，靠本图量出的断层；
2. 分布**单峰但有一簇更紧**：两簇咬合，靠"最紧那簇"定上限、靠句号定段末；
3. 样本太少或全是段间距：只有句号可用。

还有一条硬要求：**同一版式放大/缩小后结果必须一致**。检测颗粒度会随尺寸
变化，聚合逻辑不该跟着变。
"""

from __future__ import annotations

import unittest

from imgtrans.ocr.group import group_blocks
from imgtrans.types import TextBlock


def block(text: str, x0: int, y0: int, x1: int, y1: int) -> TextBlock:
    return TextBlock(
        index=0,
        polygon=(
            (float(x0), float(y0)),
            (float(x1), float(y0)),
            (float(x1), float(y1)),
            (float(x0), float(y1)),
        ),
        text=text,
        confidence=0.9,
    )


def build(
    *,
    line_h: int,
    intra_gap: int,
    para_gap: int,
    shapes: list[int],
    width: int = 760,
    x0: int = 40,
    y0: int = 40,
    numbered: bool = False,
) -> tuple[list[TextBlock], list[str]]:
    """按给定版式生成块，返回 ``(blocks, 期望的段落文本)``。

    版式参数就是排版者的笔：``line_h`` 字号行高、``intra_gap`` 段内行距、
    ``para_gap`` 段间距、``shapes`` 每段几行。文字也按中文排版习惯构造：
    段内行以逗号收尾、段末行以句号收尾——这是标点判据能生效的前提。
    """
    blocks: list[TextBlock] = []
    expected: list[str] = []
    y = y0
    for p, lines in enumerate(shapes):
        texts: list[str] = []
        for i in range(lines):
            tail = "，" if i < lines - 1 else "。"
            prefix = f"{p + 1}." if (numbered and i == 0) else ""
            text = f"{prefix}第{p + 1}段第{i + 1}行{tail}"
            texts.append(text)
            # 行宽略作抖动：真图里几乎不会每行等宽，避免测试依赖"完全对齐"
            right = x0 + width - (i * 37 % 120)
            blocks.append(block(text, x0, y, right, y + line_h))
            if i < lines - 1:
                y += line_h + intra_gap
        expected.append("".join(texts))
        y += line_h + para_gap
    return blocks, expected


class GeneratedLayoutTest(unittest.TestCase):
    """铺满参数，逐组比对还原出的段落。"""

    def test_paragraphs_recovered_across_geometry(self) -> None:
        checked = 0
        for line_h in (16, 22, 30):
            for intra in (2, 5, 9, 14):
                for para in (20, 30, 44, 60):
                    # 段间距不到行距 1.5 倍的版式，几何上是真的分不开
                    # （例如行距 14、段间距 20），这类交给标点判据，
                    # 还原度不保证，下面的 fuzzy 用例只要求"别串段"。
                    if para < 1.5 * intra:
                        continue
                    for shapes in ([1, 1, 1], [2, 2], [3, 2], [2, 2, 2], [4, 3, 2]):
                        with self.subTest(
                            line_h=line_h, intra=intra, para=para, shapes=shapes
                        ):
                            blocks, expected = build(
                                line_h=line_h,
                                intra_gap=intra,
                                para_gap=para,
                                shapes=list(shapes),
                            )
                            out = group_blocks(blocks)
                            self.assertEqual([b.text for b in out], expected)
                        checked += 1
        self.assertGreater(checked, 100, "参数网格没铺开，覆盖不足")

    def test_same_layout_scaled_gives_same_grouping(self) -> None:
        """同一版式放大倍率变化，段落划分必须一致（尺寸不该改变结果）。"""
        shapes = [2, 3, 1]
        reference: list[str] | None = None
        for scale in (1, 1.5, 2.5, 5, 8):
            line_h = round(16 * scale)
            blocks, expected = build(
                line_h=line_h,
                intra_gap=round(5 * scale),
                para_gap=round(30 * scale),
                shapes=shapes,
                width=round(760 * scale),
                x0=round(40 * scale),
                y0=round(40 * scale),
            )
            out = group_blocks(blocks)
            self.assertEqual([b.text for b in out], expected, msg=f"scale={scale}")
            texts = [b.text for b in out]
            if reference is None:
                reference = texts
            self.assertEqual(texts, reference, msg=f"scale={scale} 与基准不一致")

    def test_two_columns_never_cross_merge(self) -> None:
        """左右两栏各自成段：栏间没有水平重叠，任何阈值都不该跨栏。"""
        left, left_expected = build(
            line_h=18, intra_gap=6, para_gap=30, shapes=[2, 2], width=340, x0=30
        )
        right, right_expected = build(
            line_h=18, intra_gap=6, para_gap=30, shapes=[2, 2], width=340, x0=420
        )
        out = group_blocks(left + right)
        self.assertEqual(
            sorted(b.text for b in out), sorted(left_expected + right_expected)
        )

    def test_numbered_items_split_even_with_tiny_gaps(self) -> None:
        """编号列表：段间距和行距一样小也不许串条。"""
        blocks, expected = build(
            line_h=18, intra_gap=6, para_gap=6, shapes=[1, 1, 1], numbered=True
        )
        out = group_blocks(blocks)
        self.assertEqual([b.text for b in out], expected)

    def test_single_line_paragraphs_with_loose_spacing(self) -> None:
        """每段只有一行、间距又偏小时，只有句号能定段末。"""
        blocks, expected = build(
            line_h=30, intra_gap=0, para_gap=20, shapes=[1, 1, 1, 1, 1]
        )
        out = group_blocks(blocks)
        self.assertEqual([b.text for b in out], expected)

    def test_degenerate_gaps_never_merge_more_than_three_lines(self) -> None:
        """间距全等（几何完全无信息）时不许把整页串成一段。

        这时唯一可靠的信号是句号，所以每段都应保留；即使某些段被误并，
        也不能出现"六段并成一段"这种灾难性结果。
        """
        blocks, expected = build(
            line_h=20, intra_gap=9, para_gap=9, shapes=[2, 2, 2, 2, 2, 2]
        )
        out = group_blocks(blocks)
        self.assertEqual(len(out), len(expected), [b.text for b in out])


if __name__ == "__main__":
    unittest.main()
