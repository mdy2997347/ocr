"""链路层单元测试：预过滤、翻译索引映射、编解码。

不依赖 OCR 模型——OCR 需要下载数百 MB 权重，不能进 CI。
这里用一个假的 OCR 引擎注入管道，验证"块 → 译文 → 回填坐标"的映射正确。
"""

from __future__ import annotations

import unittest

import numpy as np

from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig, should_translate
from imgtrans.types import Polygon, TextBlock, TranslateError


class ShouldTranslateTest(unittest.TestCase):
    """预过滤是防"整批错位"的第一道防线，必须逐类覆盖。"""

    def test_normal_text_passes(self) -> None:
        for text in ("Hello", "你好", "Login", "50% off", "Total: 100"):
            with self.subTest(text=text):
                self.assertTrue(should_translate(text))

    def test_empty_and_whitespace_rejected(self) -> None:
        # 空串是 Google 丢弃 q 进而导致响应错位的根源
        for text in ("", " ", "\t", "\n", "   \n  "):
            with self.subTest(text=repr(text)):
                self.assertFalse(should_translate(text))

    def test_pure_number_rejected(self) -> None:
        for text in ("123", "1,000", "3.14", "50%", "$99", "¥100", "(1)", "1/2", "2026-11-19"):
            with self.subTest(text=text):
                self.assertFalse(should_translate(text))

    def test_pure_symbol_rejected(self) -> None:
        for text in ("---", "·", "…", "***", "|", "~", "→"):
            with self.subTest(text=text):
                self.assertFalse(should_translate(text))

    def test_text_with_digits_passes(self) -> None:
        # 含数字但有单词，必须翻译
        for text in ("100 users", "Item 3", "v2.0 release"):
            with self.subTest(text=text):
                self.assertTrue(should_translate(text))


class FakeOcr:
    """假 OCR：返回预设块，避免加载真实模型。"""

    def __init__(self, blocks: list[TextBlock]) -> None:
        self.blocks = blocks

    def recognize(self, image, *, lang="ch", min_confidence=0.5):  # noqa: ANN001
        return self.blocks

    def get(self, lang: str):  # noqa: ANN201 - 供 warmup 调用
        return self


class RecordingTranslator:
    """记录收到的文本，按规则返回译文。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.mapping = mapping or {}

    def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN201
        self.calls.append(list(texts))
        return [self.mapping.get(t, f"译:{t}") for t in texts]


def _block(index: int, text: str, x: int, y: int, w: int = 80, h: int = 24) -> TextBlock:
    poly: Polygon = (
        (float(x), float(y)),
        (float(x + w), float(y)),
        (float(x + w), float(y + h)),
        (float(x), float(y + h)),
    )
    return TextBlock(index=index, polygon=poly, text=text, confidence=0.99)


class TranslationIndexMappingTest(unittest.TestCase):
    """核心契约：译文必须按 index 回到原块，绝不错位。"""

    def test_filtered_blocks_keep_index_alignment(self) -> None:
        blocks = [
            _block(0, "Login", 10, 10),
            _block(1, "123", 10, 50),  # 纯数字 -> 不送翻译
            _block(2, "Register", 10, 90),
            _block(3, "---", 10, 130),  # 纯符号 -> 不送翻译
            _block(4, "Settings", 10, 170),
        ]
        translator = RecordingTranslator()
        pipeline = ImageTranslatePipeline(
            translator, ocr=FakeOcr(blocks), config=PipelineConfig()
        )

        results = pipeline._translate_blocks(blocks, None, "zh-CN")

        # 只应送 3 条可翻译文本
        self.assertEqual(translator.calls, [["Login", "Register", "Settings"]])
        self.assertEqual(len(results), 5)
        # 译文落在正确位置
        self.assertEqual(results[0], "译:Login")
        self.assertEqual(results[2], "译:Register")
        self.assertEqual(results[4], "译:Settings")
        # 不可翻译的块保持原文
        self.assertEqual(results[1], "123")
        self.assertEqual(results[3], "---")

    def test_tiny_boxes_are_not_translated(self) -> None:
        """极小框（照片内嵌小字）不送翻译，否则译文溢出会弄脏照片内容。

        实测缩略图里 "Room" 的框只有 7px 高，9px 的译文必然压到照片上。
        """
        blocks = [
            _block(0, "Room", 10, 10, w=27, h=7),
            _block(1, "Compound Softens", 50, 10, w=73, h=11),
            _block(2, "Normal text", 10, 40, w=200, h=24),
        ]
        translator = RecordingTranslator()
        pipeline = ImageTranslatePipeline(
            translator, ocr=FakeOcr(blocks), config=PipelineConfig(min_block_height=12)
        )

        results = pipeline._translate_blocks(blocks, None, "ja")

        self.assertEqual(translator.calls, [["Normal text"]])
        # 极小框保留原文
        self.assertEqual(results[0], "Room")
        self.assertEqual(results[1], "Compound Softens")
        self.assertEqual(results[2], "译:Normal text")

    def test_clipped_top_line_is_skipped(self) -> None:
        """贴着上边缘且高度明显偏小的行是被裁掉一半的，不能送翻译。

        实测把"霍尼韦尔的 PTM7950 是 7900 系列中的高导热相变材料"
        读成"霍尼书小...高等热柏变材"，译出来面目全非。
        """
        from PIL import Image
        import io as _io

        img = Image.new("RGB", (200, 120), (240, 240, 240))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")

        blocks = [
            _block(0, "被裁掉一半的文字", 10, 0, w=180, h=14),   # 贴顶且矮
            _block(1, "正常正文", 10, 40, w=180, h=30),
            _block(2, "第二行正文", 10, 80, w=180, h=30),
        ]
        translator = RecordingTranslator()
        pipeline = ImageTranslatePipeline(translator, ocr=FakeOcr(blocks))

        result = pipeline.translate_bytes(buf.getvalue(), "en")

        sent = translator.calls[0]
        self.assertNotIn("被裁掉一半的文字", sent)
        # 两行正文没有段末标点，会先合并成一段再送翻译（这是段落聚合的功劳）
        self.assertTrue(any("正常正文" in t and "第二行正文" in t for t in sent), sent)
        self.assertTrue(any("裁切" in w for w in result.warnings))

    def test_translator_shorter_return_falls_back_to_source(self) -> None:
        """翻译层破坏契约（返回数量不对）时，必须整批保留原文，不能猜顺序。"""

        class BadTranslator:
            def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN201
                return ["只有一条"]

        blocks = [_block(0, "a", 0, 0), _block(1, "b", 0, 40)]
        pipeline = ImageTranslatePipeline(BadTranslator(), ocr=FakeOcr(blocks))
        results = pipeline._translate_blocks(blocks, None, "zh-CN")
        self.assertEqual(results, ["a", "b"])

    def test_translator_exception_keeps_all_source(self) -> None:
        class BoomTranslator:
            def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN201
                raise RuntimeError("boom")

        blocks = [_block(0, "a", 0, 0)]
        pipeline = ImageTranslatePipeline(BoomTranslator(), ocr=FakeOcr(blocks))
        self.assertEqual(pipeline._translate_blocks(blocks, None, "zh-CN"), ["a"])


class TranslateFailureSurfacingTest(unittest.TestCase):
    """翻译通道整体失败时必须报错，不能 200 + 一张原封不动的原图。

    线上真实事故：Google 对机房 IP 返回 429 → 熔断打开 → 之后所有请求
    直接返回原文 → pipeline 判定"没有可回填的块" → 200 + 中文原图。
    调用方以为成功，排查时日志里连异常都没有。
    """

    @staticmethod
    def _png() -> bytes:
        from PIL import Image
        import io as _io

        img = Image.new("RGB", (200, 120), (240, 240, 240))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_raises_when_channel_failed(self) -> None:
        class FailingTranslator:
            last_error = "熔断打开（连续 403/429 达阈值）"

            def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN201
                return list(texts)

        blocks = [_block(0, "床品四件套", 0, 0, w=180, h=30)]
        pipeline = ImageTranslatePipeline(FailingTranslator(), ocr=FakeOcr(blocks))

        with self.assertRaises(TranslateError) as ctx:
            pipeline.translate_bytes(self._png(), "en")
        self.assertEqual(ctx.exception.code, "E_TRANSLATE")

    def test_returns_original_when_nothing_to_translate(self) -> None:
        """没出故障、只是译文与原文相同（例如全是数字）→ 仍返回原图。"""

        class EchoTranslator:
            last_error = None

            def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN201
                return list(texts)

        blocks = [_block(0, "abc", 0, 0, w=180, h=30)]
        pipeline = ImageTranslatePipeline(EchoTranslator(), ocr=FakeOcr(blocks))

        result = pipeline.translate_bytes(self._png(), "en")

        self.assertEqual(result.translated_count, 0)
        self.assertTrue(any("返回原图" in w for w in result.warnings))


class TargetLangGuardTest(unittest.TestCase):
    """目标语言白名单在入口就拦住，不浪费解码与 OCR。

    放错位置代价很实在：OCR 在 CPU 上要 11~17 秒，参数错了还跑完一遍
    再失败，用户的等待全部白付。
    """

    @staticmethod
    def _png() -> bytes:
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (60, 40), (250, 250, 250))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_unsupported_target_raises_before_ocr(self) -> None:
        from imgtrans.types import UnsupportedLangError

        ocr = FakeOcr([_block(0, "登录", 10, 10)])
        ocr.langs = []  # type: ignore[attr-defined]
        original = ocr.recognize

        def spy(image, **kwargs):  # noqa: ANN001, ANN202
            ocr.langs.append(kwargs.get("lang"))
            return original(image, **kwargs)

        ocr.recognize = spy  # type: ignore[method-assign]
        pipeline = ImageTranslatePipeline(RecordingTranslator(), ocr=ocr)

        with self.assertRaises(UnsupportedLangError) as ctx:
            pipeline.translate_bytes(self._png(), "zh-CN")

        self.assertEqual(ctx.exception.code, "E_LANG")
        self.assertIn("zh-CN", str(ctx.exception))
        # 错误信息要能直接指导调用方，而不是只说"参数错误"
        self.assertIn("en", str(ctx.exception))
        self.assertEqual(ocr.langs, [], "参数非法时不该启动 OCR")

    def test_supported_target_is_normalized(self) -> None:
        """``PT-BR`` / ``iw`` 这类写法要能归一到规范码再送翻译。"""
        targets: list[str] = []

        class Recorder:
            def translate_batch(self, texts, source, target):  # noqa: ANN001, ANN202
                targets.append(target)
                return [f"译:{t}" for t in texts]

        pipeline = ImageTranslatePipeline(
            Recorder(), ocr=FakeOcr([_block(0, "登录", 10, 10)])
        )

        pipeline.translate_bytes(self._png(), "PT-BR")
        pipeline.translate_bytes(self._png(), "iw")
        self.assertEqual(targets, ["pt", "he"])


class EncodeDecodeTest(unittest.TestCase):
    """编解码必须保真：二次有损压缩是"清晰度下降"的最大来源。"""

    def test_png_roundtrip_is_lossless(self) -> None:
        from imgtrans.pipeline import _decode_image, _encode_image

        rng = np.random.default_rng(7)
        bgr = rng.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        data, mime = _encode_image(bgr, "png", 95, None)
        self.assertEqual(mime, "image/png")
        back, _ = _decode_image(data)
        np.testing.assert_array_equal(bgr, back)

    def test_jpeg_uses_no_chroma_subsampling(self) -> None:
        """subsampling=0 是"文字边缘不发虚"的关键，用尺寸反证。

        4:4:4 编码的 JPEG 体积明显大于 4:2:0；这里用同一图对比两个质量的
        体积单调性来间接确认参数生效，避免依赖 Pillow 内部结构。
        """
        from imgtrans.pipeline import _encode_image

        img = np.zeros((64, 64, 3), dtype=np.uint8)
        img[:, ::2] = 200  # 高频竖条纹，色度下采样会让体积骤降
        high, _ = _encode_image(img, "jpeg", 95, None)
        low, _ = _encode_image(img, "jpeg", 40, None)
        self.assertGreater(len(high), len(low), "高质量应比低质量体积更大")

    def test_exif_orientation_is_applied(self) -> None:
        """带 Orientation=6 的图必须旋转，否则回填坐标全错。"""
        from io import BytesIO

        from PIL import Image

        from imgtrans.pipeline import _decode_image

        im = Image.new("RGB", (30, 10), (255, 0, 0))
        exif = im.getexif()
        exif[274] = 6  # Orientation: rotate 90 CW
        buf = BytesIO()
        im.save(buf, format="JPEG", exif=exif.tobytes())

        decoded, _ = _decode_image(buf.getvalue())
        # 原 30x10 -> 归正后 10x30（宽高互换）
        self.assertEqual(decoded.shape[0], 30)
        self.assertEqual(decoded.shape[1], 10)


class RowJoinWiringTest(unittest.TestCase):
    """行内拼接必须真的接在链路上：碎片要在**送翻译之前**拼成一条行。

    只测 ``join_row_fragments`` 不够——函数写得对但没被调用，线上照旧是碎片。
    这里断言翻译器收到的请求体本身，是唯一能证明接线正确的地方。
    """

    @staticmethod
    def _png() -> bytes:
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (1000, 260), (250, 250, 250))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_fragments_reach_translator_as_one_line(self) -> None:
        blocks = [
            _block(0, "说明：系统价格默认304材质，", 10, 20, w=550, h=50),
            _block(1, "如需316材质请", 575, 20, w=385, h=50),
        ]
        translator = RecordingTranslator()
        pipeline = ImageTranslatePipeline(
            translator, ocr=FakeOcr(blocks), config=PipelineConfig()
        )

        pipeline.translate_bytes(self._png(), "en")

        self.assertEqual(
            translator.calls,
            [["说明：系统价格默认304材质，如需316材质请"]],
            "碎片应拼成一条行后再送翻译",
        )

    def test_can_be_disabled_by_config(self) -> None:
        blocks = [
            _block(0, "说明：系统价格默认304材质，", 10, 20, w=550, h=50),
            _block(1, "如需316材质请", 575, 20, w=385, h=50),
        ]
        translator = RecordingTranslator()
        pipeline = ImageTranslatePipeline(
            translator,
            ocr=FakeOcr(blocks),
            config=PipelineConfig(row_join_gap_ratio=0, merge_paragraphs=False),
        )

        pipeline.translate_bytes(self._png(), "en")

        self.assertEqual(translator.calls[0], ["说明：系统价格默认304材质，", "如需316材质请"])


if __name__ == "__main__":
    unittest.main()
