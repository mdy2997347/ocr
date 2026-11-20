"""段落聚合的单元测试。

判据都是保守的阈值，这里逐条覆盖"该合并"与"不该合并"两侧——
误合并（把两段拼成一句）比不合并难看得多，所以边界必须双向测。
"""

from __future__ import annotations

import unittest

from imgtrans.ocr.group import group_blocks, join_lines
from imgtrans.types import TextBlock


def make(text: str, x0: int, y0: int, x1: int, y1: int, **kw) -> TextBlock:
    """用轴对齐矩形快速构造块。"""
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
        **kw,
    )


class GroupBlocksTest(unittest.TestCase):
    def test_two_lines_of_one_paragraph_merge(self) -> None:
        # 行高 30，行距 10（gap = 0.33 * lh），左边界对齐
        a = make("随着温度升高，相变导热材料", 40, 100, 700, 130)
        b = make("变得越来越软。", 40, 140, 400, 170)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "随着温度升高，相变导热材料变得越来越软。")
        self.assertEqual(out[0].line_count, 2)
        self.assertAlmostEqual(out[0].line_height, 30.0)
        self.assertEqual(out[0].box, (40, 100, 700, 170))

    def test_paragraph_gap_does_not_merge(self) -> None:
        a = make("第一段结束。", 40, 100, 700, 130)
        b = make("第二段开始。", 40, 190, 700, 220)  # gap = 60 = 2.0 * lh
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_side_by_side_same_row_not_merged(self) -> None:
        # 同一行左右两块：top 相同，属于横向拼接而非折行
        a = make("计算机", 560, 700, 610, 730)
        b = make("大功率LED", 620, 700, 700, 730)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_different_line_height_not_merged(self) -> None:
        a = make("正文一行", 40, 100, 700, 130)       # 30px
        b = make("小字注释", 40, 140, 300, 155)       # 15px，差 50%
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_vertical_text_not_merged(self) -> None:
        a = make("縦書き", 100, 100, 130, 300, vertical=True)
        b = make("続き", 140, 100, 170, 300, vertical=True)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_compact_line_spacing_still_merges(self) -> None:
        """行距 1.7 倍（gap = 0.73 × 行高）的紧凑排版必须能合并。

        实测电商长图就是这个行距，早期阈值 0.62 导致全图零合并，
        六块正文的字号从 12px 跳到 30px。
        """
        a = make("基于一种新型聚合物PCM体系，这种材料在典型", 24, 144, 727, 174)
        b = make("度范围内表现出良好的界面润湿性，从而产生极低", 24, 197, 725, 227)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 1)

    def test_sentence_end_blocks_merge_even_if_gap_is_small(self) -> None:
        """上一行以句号结尾就是段末——即使段间距和行距一样小也不能合并。

        紧凑排版里段内间距 0.68、段间距 0.73，几何上分不开，
        只能靠标点。误合并会把两段话拼成一句，且字号被统一成错的值。
        """
        a = make("，通过可靠性测试并保持优异的性能。", 26, 92, 483, 122)
        b = make("基于一种新型聚合物PCM体系", 24, 144, 727, 174)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_no_horizontal_overlap_kept_apart(self) -> None:
        # 上下两行但分别是左右两栏的内容
        a = make("左栏第一行", 20, 100, 300, 130)
        b = make("右栏第二行", 500, 140, 760, 170)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_indented_first_line_still_merges(self) -> None:
        # 首行缩进两个字，重叠比例仍然很高
        a = make("　　缩进的首行文字", 84, 100, 700, 130)
        b = make("续行顶格写。", 40, 140, 700, 170)
        out = group_blocks([a, b])
        self.assertEqual(len(out), 1)

    def test_indices_renumbered_continuously(self) -> None:
        blocks = [
            make("一", 40, 100, 300, 130),
            make("二", 40, 140, 300, 170),
            make("三", 40, 300, 300, 330),
        ]
        out = group_blocks(blocks)
        self.assertEqual([b.index for b in out], list(range(len(out))))

    def test_empty_input(self) -> None:
        self.assertEqual(group_blocks([]), [])

    def test_input_not_mutated(self) -> None:
        a = make("原文本", 40, 100, 700, 130)
        b = make("续行", 40, 140, 400, 170)
        group_blocks([a, b])
        self.assertEqual(a.text, "原文本")
        self.assertEqual(a.line_count, 1)


class JoinLinesTest(unittest.TestCase):
    def test_cjk_no_space(self) -> None:
        self.assertEqual(join_lines(["相变材料", "的原理"]), "相变材料的原理")

    def test_latin_gets_space(self) -> None:
        self.assertEqual(join_lines(["phase change", "materials"]), "phase change materials")

    def test_hyphen_break(self) -> None:
        self.assertEqual(join_lines(["thermo-", "conductive"]), "thermoconductive")

    def test_cjk_then_latin_bridges_with_space(self) -> None:
        # 中英混排换行处补空格，避免 "材料phase" 粘连
        self.assertEqual(join_lines(["导热材料", "phase change"]), "导热材料 phase change")

    def test_blank_lines_skipped(self) -> None:
        self.assertEqual(join_lines(["甲", "  ", "乙"]), "甲乙")


if __name__ == "__main__":
    unittest.main()
