"""OCR 速度档位的装配测试。

这里**不加载任何模型**（PaddleOCR 被 mock 掉）：要验证的是"档位名 -> 构造
参数"的映射、非法档位是否在启动期就被拒绝、以及 fast 档的模型下载失败时
能不能自愈回 quality。

真实的提速效果（15.0s -> 7.5s）是实测出来的，不是在单测里断言耗时——
耗时断言在不同机器上必然飘，那种测试只会变成噪声。
"""

from __future__ import annotations

import unittest
from unittest import mock

from imgtrans.ocr.engine import DEFAULT_OCR_PROFILE, OCR_PROFILES, OcrEngine
from imgtrans.types import OcrError


class OcrProfileNameTest(unittest.TestCase):
    def test_default_is_quality(self) -> None:
        """默认必须是最准的档位：提速是使用者显式选择，不该替人做主。"""
        self.assertEqual(DEFAULT_OCR_PROFILE, "quality")
        self.assertEqual(OcrEngine(device="cpu").profile, "quality")

    def test_unknown_profile_rejected_at_construction(self) -> None:
        """档位名写错要在构造时报错，不能等到第一个请求。

        线上表现为整站 500，而且堆栈在 paddle 内部，很难看出是配置拼错。
        """
        with self.assertRaises(OcrError) as ctx:
            OcrEngine(device="cpu", profile="turbo")
        self.assertIn("turbo", str(ctx.exception))

    def test_profile_is_case_and_space_insensitive(self) -> None:
        self.assertEqual(OcrEngine(device="cpu", profile=" FAST ").profile, "fast")

    def test_fast_only_overrides_detection(self) -> None:
        """fast 档**只换检测**。识别模型必须保持默认。

        这是整个提速方案的关键约束：识别精度不降，省的时间全来自检测。
        一旦这里被改成同时换 text_recognition_model_name，中文小字的识别
        质量会明显下滑（实测 ``因测量方式不同`` -> ``因测量式不同``）。
        """
        fast = OCR_PROFILES["fast"]
        self.assertEqual(fast["text_detection_model_name"], "PP-OCRv5_mobile_det")
        self.assertNotIn("text_recognition_model_name", fast)


class OcrBuildArgsTest(unittest.TestCase):
    """验证构造 PaddleOCR 时实际传下去的参数。"""

    def _build(self, **kwargs) -> dict:
        seen: dict = {}

        def fake(**params):
            seen.update(params)
            return "engine"

        with mock.patch("paddleocr.PaddleOCR", side_effect=fake):
            OcrEngine(device="cpu", **kwargs)._build("ch")
        return seen

    def test_quality_passes_no_model_override(self) -> None:
        seen = self._build(profile="quality")
        self.assertNotIn("text_detection_model_name", seen)
        self.assertNotIn("text_recognition_model_name", seen)

    def test_fast_passes_mobile_det(self) -> None:
        seen = self._build(profile="fast")
        self.assertEqual(seen["text_detection_model_name"], "PP-OCRv5_mobile_det")
        self.assertNotIn("text_recognition_model_name", seen)

    def test_mkldnn_stays_off_for_both_profiles(self) -> None:
        """两个档位都必须显式关 mkldnn。

        paddleocr 3.7 的 enable_mkldnn 默认是 **True**，而 paddle 3.3.1 的
        PIR 执行器下开会直接崩（ConvertPirAttribute2RuntimeAttribute）。
        漏传就是服务起不来，所以这里锁死。
        """
        for profile in ("quality", "fast"):
            with self.subTest(profile=profile):
                self.assertIs(self._build(profile=profile)["enable_mkldnn"], False)

    def test_det_limit_passed_when_set(self) -> None:
        self.assertEqual(self._build(det_limit_side_len=640)["text_det_limit_side_len"], 640)

    def test_det_limit_omitted_by_default(self) -> None:
        self.assertNotIn("text_det_limit_side_len", self._build())

    def test_fast_falls_back_to_quality_when_model_unavailable(self) -> None:
        """fast 档的检测模型要额外下载，拿不到时不能把服务带崩。

        档位是"想更快"的优化项，退回慢档远好过整站不可用。
        """
        calls: list[dict] = []

        def fake(**params):
            calls.append(params)
            if "text_detection_model_name" in params:
                raise RuntimeError("model not found: PP-OCRv5_mobile_det")
            return "engine-quality"

        with mock.patch("paddleocr.PaddleOCR", side_effect=fake):
            built = OcrEngine(device="cpu", profile="fast")._build("ch")

        self.assertEqual(built, "engine-quality")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("text_detection_model_name", calls[1])

    def test_quality_failure_is_not_swallowed(self) -> None:
        """quality 档没有降级空间，构造失败必须原样抛出。"""

        def fake(**params):
            raise RuntimeError("boom")

        with mock.patch("paddleocr.PaddleOCR", side_effect=fake):
            with self.assertRaises(OcrError) as ctx:
                OcrEngine(device="cpu", profile="quality")._build("ch")
        self.assertIn("boom", str(ctx.exception))


class PipelineProfileWiringTest(unittest.TestCase):
    """配置项要能从 PipelineConfig 一路传到 OcrEngine。"""

    def test_config_reaches_engine(self) -> None:
        from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig

        pipeline = ImageTranslatePipeline(
            object(),
            font_book=mock.Mock(),
            config=PipelineConfig(device="cpu", ocr_profile="fast"),
        )
        self.assertEqual(pipeline.ocr_profile, "fast")

    def test_default_pipeline_is_quality(self) -> None:
        from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig

        pipeline = ImageTranslatePipeline(
            object(), font_book=mock.Mock(), config=PipelineConfig(device="cpu")
        )
        self.assertEqual(pipeline.ocr_profile, "quality")


if __name__ == "__main__":
    unittest.main()
