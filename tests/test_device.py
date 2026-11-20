"""推理设备解析的单元测试。

三条必须钉住的语义：

1. **默认 cpu**——不配置就不猜。机器有卡不代表这张卡能给你用（可能被训练
   任务占满），默认"有卡就占"是最容易出事故的设计。
2. **显式参数 > 环境变量**——shell 里残留的 ``IMGTRANS_DEVICE`` 不能盖掉
   命令行显式指定的值。
3. **配了 gpu 但用不了必须报错**——静默跑回 CPU 会让人以为在吃卡，
   实际慢了十几倍，这种"看起来正常"的故障最难发现。

所有 CUDA 探测都靠注入的假 probe，跑在有没有卡的机器上结果一致。
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

from imgtrans.device import (
    DEFAULT_DEVICE,
    ENV_VAR,
    DeviceError,
    normalize_device,
    probe_cuda,
    resolve_device,
)


def _probe(count: int, reason: str = "假探测结果"):
    """构造一个返回固定结果的探测函数。"""

    def _fn() -> tuple[int, str]:
        return count, reason

    return _fn


def _boom() -> tuple[int, str]:
    raise AssertionError("这个分支不该触发 CUDA 探测")


class NormalizeDeviceTest(unittest.TestCase):
    def test_empty_means_cpu(self) -> None:
        # .env 里写了 IMGTRANS_DEVICE= 这种手滑，不该让服务起不来
        for raw in (None, "", "   ", "default", "none"):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(normalize_device(raw), "cpu")

    def test_cpu_case_and_space_insensitive(self) -> None:
        for raw in ("cpu", "CPU", " Cpu ", "  cpu  "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(normalize_device(raw), "cpu")

    def test_gpu_shorthand_maps_to_zero(self) -> None:
        # 手写配置最常写 "gpu" / "cuda"，都要落到 0 号卡
        for raw in ("gpu", "GPU", "cuda", "CUDA"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_device(raw), "gpu:0")

    def test_index_is_preserved_and_normalized(self) -> None:
        cases = {
            "gpu:1": "gpu:1",
            "gpu:01": "gpu:1",
            "cuda:2": "gpu:2",
            "GPU : 3": "gpu:3",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_device(raw), expected)

    def test_invalid_value_raises(self) -> None:
        # 绝不猜："gpu0" 猜成 gpu:0 会掩盖真正的拼写错误
        for raw in ("gpu0", "gpu:abc", "gpu:-1", "gpu:", "tpu", "1", "gpu:1:2"):
            with self.subTest(raw=raw):
                with self.assertRaises(DeviceError):
                    normalize_device(raw)

    def test_device_error_is_translate_error(self) -> None:
        # 对外错误码要能被 API 层统一映射，必须挂在链路错误基类下
        self.assertEqual(DeviceError.code, "E_DEVICE")


class ProbeCudaTest(unittest.TestCase):
    """探测函数的失败路径比成功路径更重要——它决定了报错能不能指出病因。"""

    def test_missing_paddle(self) -> None:
        with mock.patch.dict(sys.modules, {"paddle": None}):
            count, reason = probe_cuda()
        self.assertEqual(count, 0)
        self.assertIn("未安装", reason)

    def test_cpu_build_explains_itself(self) -> None:
        fake = types.SimpleNamespace(
            __version__="9.9.9",
            device=types.SimpleNamespace(
                is_compiled_with_cuda=lambda: False,
                cuda=types.SimpleNamespace(device_count=lambda: 0),
            ),
        )
        with mock.patch.dict(sys.modules, {"paddle": fake}):
            count, reason = probe_cuda()
        self.assertEqual(count, 0)
        # 报错必须直接点明"你装的是 CPU 版"，而不是让用户去猜
        self.assertIn("CPU 版", reason)
        self.assertIn("9.9.9", reason)

    def test_gpu_build_reports_device_count(self) -> None:
        fake = types.SimpleNamespace(
            __version__="9.9.9",
            device=types.SimpleNamespace(
                is_compiled_with_cuda=lambda: True,
                cuda=types.SimpleNamespace(device_count=lambda: 2),
            ),
        )
        with mock.patch.dict(sys.modules, {"paddle": fake}):
            count, reason = probe_cuda()
        self.assertEqual(count, 2)
        self.assertIn("2", reason)

    def test_compiled_but_no_device(self) -> None:
        # 容器没透传 GPU 时就是这种情况：编译带 CUDA，运行时 0 张卡
        fake = types.SimpleNamespace(
            __version__="9.9.9",
            device=types.SimpleNamespace(
                is_compiled_with_cuda=lambda: True,
                cuda=types.SimpleNamespace(device_count=lambda: 0),
            ),
        )
        with mock.patch.dict(sys.modules, {"paddle": fake}):
            count, reason = probe_cuda()
        self.assertEqual(count, 0)
        self.assertIn("没检测到", reason)


class ResolveDeviceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop(ENV_VAR, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        os.environ.pop(ENV_VAR, None)
        if self._saved is not None:
            os.environ[ENV_VAR] = self._saved

    def test_defaults_to_cpu_without_any_config(self) -> None:
        self.assertEqual(resolve_device(probe=_boom), DEFAULT_DEVICE)

    def test_reads_env_when_no_explicit_value(self) -> None:
        os.environ[ENV_VAR] = "gpu:1"
        self.assertEqual(resolve_device(probe=_probe(2)), "gpu:1")

    def test_explicit_argument_beats_env(self) -> None:
        os.environ[ENV_VAR] = "gpu:1"
        self.assertEqual(resolve_device("cpu", probe=_boom), "cpu")

    def test_cpu_never_probes_cuda(self) -> None:
        # 默认路径（绝大多数部署）不该为此 import paddle 拖慢启动
        self.assertEqual(resolve_device("cpu", probe=_boom), "cpu")

    def test_auto_uses_gpu_when_available(self) -> None:
        self.assertEqual(resolve_device("auto", probe=_probe(1)), "gpu:0")

    def test_auto_falls_back_to_cpu(self) -> None:
        self.assertEqual(resolve_device("auto", probe=_probe(0)), "cpu")

    def test_gpu_unavailable_raises_instead_of_silent_fallback(self) -> None:
        with self.assertRaises(DeviceError) as ctx:
            resolve_device("gpu", probe=_probe(0, "当前 paddle 9.9.9 是 CPU 版"))
        message = str(ctx.exception)
        self.assertIn("CPU 版", message)
        # 报错要给退路，否则用户只能去翻源码
        self.assertIn("auto", message)
        self.assertIn(ENV_VAR, message)

    def test_out_of_range_index_raises(self) -> None:
        with self.assertRaises(DeviceError) as ctx:
            resolve_device("gpu:1", probe=_probe(1))
        self.assertIn("0~0", str(ctx.exception))

    def test_last_index_is_accepted(self) -> None:
        self.assertEqual(resolve_device("gpu:1", probe=_probe(2)), "gpu:1")

    def test_check_false_skips_validation(self) -> None:
        self.assertEqual(resolve_device("gpu:3", probe=_boom, check=False), "gpu:3")


class _FakePaddleOCR:
    """假 PaddleOCR：只记录构造参数，不加载任何模型。"""

    last_kwargs: dict | None = None

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        type(self).last_kwargs = kwargs


class OcrEngineDeviceTest(unittest.TestCase):
    """验证设备不只是"解析出来了"，而是真的传到了 PaddleOCR 的构造参数里。"""

    def setUp(self) -> None:
        self._saved = os.environ.pop(ENV_VAR, None)
        module = types.ModuleType("paddleocr")
        module.PaddleOCR = _FakePaddleOCR  # type: ignore[attr-defined]
        patcher = mock.patch.dict(sys.modules, {"paddleocr": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._restore_env)
        _FakePaddleOCR.last_kwargs = None

    def _restore_env(self) -> None:
        os.environ.pop(ENV_VAR, None)
        if self._saved is not None:
            os.environ[ENV_VAR] = self._saved

    def test_default_is_cpu_and_is_forwarded(self) -> None:
        from imgtrans.ocr.engine import OcrEngine

        engine = OcrEngine()
        self.assertEqual(engine.device, "cpu")
        engine.get("ch")
        assert _FakePaddleOCR.last_kwargs is not None
        # 恒显式传：不传的话 GPU 版 paddle 会自行占卡，与"默认 cpu"矛盾
        self.assertEqual(_FakePaddleOCR.last_kwargs["device"], "cpu")

    def test_env_is_read_by_engine(self) -> None:
        from functools import partial

        import imgtrans.ocr.engine as engine_mod
        from imgtrans.ocr.engine import OcrEngine

        os.environ[ENV_VAR] = "gpu:1"
        # 只替换探测，保留 resolve_device 的真实解析逻辑：这样才测到
        # "引擎自己会去读 IMGTRANS_DEVICE"这条链路，而不是把结果 mock 掉。
        with mock.patch.object(
            engine_mod, "resolve_device", partial(resolve_device, probe=_probe(4))
        ):
            engine = OcrEngine()
        self.assertEqual(engine.device, "gpu:1")

    def test_gpu_forces_mkldnn_off(self) -> None:
        from imgtrans.ocr.engine import OcrEngine

        with mock.patch("imgtrans.ocr.engine.resolve_device", return_value="gpu:0"):
            engine = OcrEngine(enable_mkldnn=True)
        engine.get("ch")
        assert _FakePaddleOCR.last_kwargs is not None
        self.assertEqual(_FakePaddleOCR.last_kwargs["device"], "gpu:0")
        # oneDNN 是 CPU 后端，GPU 上传 True 只会误导
        self.assertFalse(_FakePaddleOCR.last_kwargs["enable_mkldnn"])

    def test_device_error_propagates_from_constructor(self) -> None:
        from imgtrans.ocr.engine import OcrEngine

        # 构造期就炸：配置错误不该表现成"第一次请求特别慢"
        with mock.patch(
            "imgtrans.ocr.engine.resolve_device",
            side_effect=DeviceError("配了 gpu 但没卡"),
        ):
            with self.assertRaises(DeviceError):
                OcrEngine()


class _StubOcr:
    """假 OCR：记录收到的 device，验证配置层到引擎层的透传。"""

    last_device: object = "unset"

    def __init__(self, device: str | None = None) -> None:
        type(self).last_device = device
        self.device = device or "cpu"


class PipelineDeviceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop(ENV_VAR, None)
        self.addCleanup(self._restore_env)
        patcher = mock.patch("imgtrans.pipeline.OcrEngine", _StubOcr)
        patcher.start()
        self.addCleanup(patcher.stop)
        _StubOcr.last_device = "unset"

    def _restore_env(self) -> None:
        os.environ.pop(ENV_VAR, None)
        if self._saved is not None:
            os.environ[ENV_VAR] = self._saved

    def test_config_device_reaches_engine(self) -> None:
        from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig

        pipeline = ImageTranslatePipeline(
            object(),  # translator 此处不参与
            font_book=types.SimpleNamespace(),
            config=PipelineConfig(device="gpu:2"),
        )
        self.assertEqual(_StubOcr.last_device, "gpu:2")
        self.assertEqual(pipeline.device, "gpu:2")

    def test_unset_config_leaves_decision_to_engine(self) -> None:
        from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig

        ImageTranslatePipeline(
            object(),
            font_book=types.SimpleNamespace(),
            config=PipelineConfig(),
        )
        # 不硬塞 cpu：让 OcrEngine 去读 IMGTRANS_DEVICE，否则 .env 配置失效
        self.assertIsNone(_StubOcr.last_device)


if __name__ == "__main__":
    unittest.main()
