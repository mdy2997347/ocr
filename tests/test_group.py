"""段落聚合的单元测试。

判据都是保守的阈值，这里逐条覆盖"该合并"与"不该合并"两侧——
误合并（把两段拼成一句）比不合并难看得多，所以边界必须双向测。
"""

from __future__ import annotations

import unittest

from imgtrans.ocr.group import group_blocks, join_lines, join_row_fragments
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

    def test_taobao_notice_splits_into_items_not_lines(self) -> None:
        """淘宝告示图（800x600）实测坐标：8 个行级块应聚成 6 段。

        这张图的 OCR 框紧贴字形（h≈16~17，小于字号），段内 gap 15/17、
        段间 gap 47~54。原阈值 0.90 把段内比值 0.94/1.00 挡在外面，两段
        都被拆成"第一行缩到 12~13px + 第二行 18px"，看着像两段不同层级。
        另：第 3 条的折行恰好落在句号后面，必须靠"图内间距双峰"才能认出来。
        """
        blocks = [
            make("温馨提示：", 17, 69, 114, 94),
            make("淘宝店铺：深圳市名优气动接头批发", 353, 105, 735, 127),
            make("1.客服电话：周小姐 13528863010", 20, 181, 342, 197),
            make("2.关于发货：当天下午6点前付款的订单，都可以当天发货（订货订单除外）", 19, 247, 731, 263),
            make("晚上8点左右上传快递单号。", 19, 278, 271, 298),
            make("3.关于物流：小件默认发“圆通快递”，重货默认发“中通快运”。", 19, 345, 640, 362),
            make("更改其它快递请与客服联系。", 20, 379, 279, 396),
            make("开发票、收据下单前务必联系客服", 76, 445, 727, 485),
        ]
        out = group_blocks(blocks)
        self.assertEqual(len(out), 6)
        # 第 2、3 条各自合成一段；编号仍留在段首，不会被上一条吞掉
        self.assertEqual(out[3].line_count, 2)
        self.assertTrue(out[3].text.startswith("2.关于发货："))
        self.assertTrue(out[3].text.endswith("晚上8点左右上传快递单号。"))
        self.assertEqual(out[4].line_count, 2)
        self.assertTrue(out[4].text.startswith("3.关于物流："))
        self.assertTrue(out[4].text.endswith("更改其它快递请与客服联系。"))

    def test_list_marker_starts_new_item(self) -> None:
        """以 "2." 开头的行一定是新条目，间距再小也不能接在上一行后面。"""
        a = make("1.客服电话：周小姐 13528863010", 20, 181, 342, 197)
        b = make("2.关于发货：当天下午6点前付款", 20, 199, 700, 215)  # gap = 2
        out = group_blocks([a, b])
        self.assertEqual(len(out), 2)

    def test_sentence_break_kept_when_gaps_unimodal(self) -> None:
        """间距分布不成双峰时，句号仍等于段末（老行为不能被新判据动摇）。

        紧凑排版里段内 0.68 × 行高、段间 0.73 × 行高，几何分不开，
        只能靠标点；这时不许出现"图级阈值"把它放过去。
        """
        blocks = [
            make("第一句结束。", 24, 100, 720, 118),
            make("第二段第一行，", 24, 130, 720, 148),
            make("第二段第二行。", 24, 160, 720, 178),
            make("第三段第一行，", 24, 190, 720, 208),
            make("第三段第二行。", 24, 220, 720, 238),
            make("第四段。", 24, 250, 720, 268),
        ]
        out = group_blocks(blocks)
        self.assertEqual([b.line_count for b in out], [1, 2, 2, 1])

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


class JoinRowFragmentsTest(unittest.TestCase):
    """行内碎片拼接：检测器把一条长行切成左右两块时要拼回去。

    背景：980px 宽的促销条幅上三行中文被切成 8 块，跨行聚合只跟上一行比较，
    右碎片谁也合不进去，于是每块各自定字号、回填互相压字；同一张图放大到
    1500px 就正常。所以这一步修的是"检测颗粒度随尺寸变"，而阈值必须两边都测——
    拼得太狠会把并排的两个独立标签连成一句。
    """

    def test_tight_gap_joined(self) -> None:
        a = make("说明：系统价格默认304材质，", 10, 20, 560, 70)
        b = make("如需316材质请", 570, 20, 960, 70)  # gap 10 = 0.2 lh
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "说明：系统价格默认304材质，如需316材质请")
        self.assertEqual(out[0].box, (10, 20, 960, 70))
        # 拼完仍是一条视觉行：行数必须是 1，否则字号会按整段高度推导
        self.assertEqual(out[0].line_count, 1)
        self.assertAlmostEqual(out[0].line_height, 50.0)

    def test_wide_gap_labels_kept_apart(self) -> None:
        """间隙 0.67 行高、左标签又不以逗号收尾，就该当成两个独立标签。"""
        a = make("计算机", 560, 700, 610, 730)
        b = make("大功率LED", 630, 700, 700, 730)
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_comma_tail_allows_wider_gap(self) -> None:
        """以逗号收尾 = 话没说完，间隙放宽到 0.8 行高仍然拼。"""
        a = make("厂家直销，", 340, 95, 700, 145)
        b = make("特殊规格可订做", 733, 95, 900, 145)  # gap 33 = 0.66 lh
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 1)

    def test_same_gap_without_comma_kept_apart(self) -> None:
        """同样的 0.66 间隙，左碎片不以逗号收尾就不拼——放宽只给"话没说完"。"""
        a = make("厂家直销。", 340, 95, 700, 145)
        b = make("特殊规格可订做", 733, 95, 900, 145)
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_comma_tail_still_limited(self) -> None:
        """放宽不是无限放：1.2 行高已经是大间隔了。"""
        a = make("厂家直销，", 340, 95, 700, 145)
        b = make("特殊规格可订做", 760, 95, 900, 145)  # gap 60 = 1.2 lh
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_three_fragments_chain(self) -> None:
        parts = [
            make("甲", 10, 100, 200, 150),
            make("乙", 210, 100, 400, 150),
            make("丙", 410, 100, 600, 150),
        ]
        out = join_row_fragments(parts)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "甲乙丙")

    def test_height_mismatch_not_joined(self) -> None:
        a = make("正文一行", 10, 100, 300, 150)      # 50px
        b = make("小字注解", 310, 115, 500, 145)     # 30px，差 40%
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_baseline_mismatch_not_joined(self) -> None:
        a = make("上一行", 10, 100, 300, 150)
        b = make("下一行", 310, 160, 500, 210)
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_latin_fragments_get_space(self) -> None:
        a = make("phase change", 10, 100, 300, 130)
        b = make("materials", 306, 100, 500, 130)  # gap 6，latin 断词处
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "phase change materials")

    def test_vertical_untouched(self) -> None:
        a = make("縦書き", 100, 100, 130, 300, vertical=True)
        b = make("続き", 140, 100, 170, 300, vertical=True)
        out = join_row_fragments([a, b])
        self.assertEqual(len(out), 2)

    def test_indices_renumbered(self) -> None:
        parts = [
            make("甲", 10, 100, 200, 150),
            make("乙", 210, 100, 400, 150),
            make("丙", 10, 300, 200, 350),
        ]
        out = join_row_fragments(parts)
        self.assertEqual([b.index for b in out], list(range(len(out))))

    def test_disabled_by_zero(self) -> None:
        a = make("甲", 10, 100, 200, 150)
        b = make("乙", 202, 100, 400, 150)
        self.assertEqual(len(join_row_fragments([a, b], gap_ratio=0)), 2)

    def test_empty_and_single(self) -> None:
        self.assertEqual(join_row_fragments([]), [])
        one = make("唯一", 10, 100, 90, 140)
        self.assertEqual(len(join_row_fragments([one])), 1)

    def test_input_not_mutated(self) -> None:
        a = make("原文本", 10, 100, 200, 150)
        b = make("续接", 205, 100, 400, 150)
        join_row_fragments([a, b])
        self.assertEqual(a.text, "原文本")
        self.assertEqual(a.index, 0)

    def test_banner_three_lines_collapse_to_one_paragraph(self) -> None:
        """回归：这条幅曾把 3 行切成 8 块，最终回填出 8 个互相压字的碎片。"""
        fragments = [
            make("说明：系统价格默认304材质，", 10, 20, 560, 70),
            make("如需316材质请", 575, 20, 960, 70),
            make("咨询店主！", 10, 95, 330, 145),
            make("厂家直销，", 340, 95, 700, 145),
            make("特殊规格可订做。所有常用规格均有现货！量大包邮！", 10, 170, 950, 220),
        ]
        joined = join_row_fragments(fragments)
        self.assertEqual(len(joined), 3)  # 3 条视觉行
        out = group_blocks(joined)
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0].text,
            "说明：系统价格默认304材质，如需316材质请咨询店主！"
            "厂家直销，特殊规格可订做。所有常用规格均有现货！量大包邮！",
        )
        self.assertEqual(out[0].line_count, 3)


class GroupingCostTest(unittest.TestCase):
    """判据的调用次数必须随块数线性增长，不能超线性。

    起因：支持多栏之后，"只和上一个块比"变成"和所有还开着的段比"，第一版
    没有做任何剪枝，320 块的长图上这一步从 4ms 涨到 264ms（57 倍）。判据本身
    没错，错在结构。而单纯断言耗时在 CI 上不可靠（机器快慢能差十倍），
    所以这里数调用次数：真正的 O(n²) 会让次数涨 4 倍，线性只涨 2 倍。
    """

    @staticmethod
    def _scene(paragraphs: int, lines: int = 8) -> list[TextBlock]:
        out: list[TextBlock] = []
        y = 20
        for _ in range(paragraphs):
            for _ in range(lines):
                out.append(make("一段正文里的一行", 40, y, 900, y + 22))
                y += 34
            y += 40
        return out

    def test_candidate_checks_grow_linearly(self) -> None:
        from unittest import mock

        import imgtrans.ocr.group as group_mod

        counts: list[int] = []
        sizes: list[int] = []
        for paragraphs in (40, 80):
            blocks = self._scene(paragraphs)
            sizes.append(len(blocks))
            with mock.patch.object(
                group_mod, "_can_merge", wraps=group_mod._can_merge
            ) as spy:
                group_blocks(blocks)
            counts.append(spy.call_count)

        # 块数翻倍：线性 -> 约 2 倍，平方 -> 约 4 倍。留一点常数项余量。
        self.assertLess(
            counts[1],
            counts[0] * 2.5,
            f"判据调用次数疑似超线性: {counts}（块数 {sizes}）",
        )

    def test_row_clustering_does_not_rescan_every_row(self) -> None:
        """行内拼接的 y 聚类不能对每个块都扫一遍已有行。

        旧写法是"每个块遍历全部已有行"，块块不同行时就是 O(n²)；现在按中心
        y 排序后只在使用 bisect 找到的窗口里比。``_cy`` 的调用次数正好卡在
        这两种结构中间：新写法每块只算一次，旧写法每块要算"已有行数"次。
        """
        from unittest import mock

        import imgtrans.ocr.group as group_mod

        blocks = self._scene(60)  # 480 个块，块块不同行
        with mock.patch.object(
            group_mod, "_cy", wraps=group_mod._cy
        ) as spy:
            join_row_fragments(blocks)
        self.assertLessEqual(
            spy.call_count,
            len(blocks) + 8,
            f"每块只该定位一次行，实际 {spy.call_count} 次 / {len(blocks)} 块",
        )


if __name__ == "__main__":
    unittest.main()
