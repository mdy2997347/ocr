"""FastAPI 服务层测试。

用假 OCR + 假翻译器，**不联网、不加载模型**：这里要验证的是
"HTTP 契约"（鉴权、状态码、响应形状、错误映射），不是识别或翻译质量。
真实的 OCR/翻译链路由 ``test_pipeline`` 与手工端到端覆盖。

之所以要单独测这一层：契约一旦变了，PHP 调用方会在生产上直接拿到
500 或解析失败，而单元测试全绿也发现不了。
"""

from __future__ import annotations

import base64
import io
import os
import unittest
from unittest import mock

import numpy as np

try:
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    HAS_DEPS = True
except ImportError:  # pragma: no cover
    HAS_DEPS = False

try:
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    HAS_FASTAPI = False

from imgtrans.types import Polygon, TextBlock


class FakeOcr:
    """固定返回一个文字块的假 OCR。"""

    def __init__(self, text: str = "登录") -> None:
        self._text = text
        self.calls: list[str] = []

    def recognize(self, image_bgr, *, lang: str = "ch", min_confidence: float = 0.5):
        self.calls.append(lang)
        poly: Polygon = (
            (20.0, 20.0),
            (80.0, 20.0),
            (80.0, 50.0),
            (20.0, 50.0),
        )
        return [TextBlock(index=0, polygon=poly, text=self._text, confidence=0.99)]


class FakeTranslator:
    """把每条文本映射成固定译文。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}
        self.calls: list[tuple[list[str], str | None, str]] = []

    def translate_batch(self, texts, source, target):
        self.calls.append((list(texts), source, target))
        return [self._mapping.get(t, f"T:{t}") for t in texts]


def _png_bytes(w: int = 200, h: int = 80) -> bytes:
    im = Image.new("RGB", (w, h), (240, 240, 240))
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("msyh.ttc", 24)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 20), "登录", font=font, fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@unittest.skipUnless(HAS_DEPS and HAS_FASTAPI, "需要 opencv/pillow/fastapi")
class ApiContractTest(unittest.TestCase):
    def setUp(self) -> None:
        # 未配置 IMGTRANS_API_KEY 时服务会拒绝启动（防止默认裸奔上线）。
        # 非鉴权类用例通过 IMGTRANS_DEV=1 显式进入开发模式。
        patcher = mock.patch.dict(os.environ, {"IMGTRANS_DEV": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("IMGTRANS_API_KEY", None)

    def _client(self, *, api_key: str | None = None) -> "TestClient":
        from imgtrans.api.app import create_app
        from imgtrans.pipeline import ImageTranslatePipeline

        pipeline = ImageTranslatePipeline(
            FakeTranslator({"登录": "Login"}), ocr=FakeOcr()
        )
        return TestClient(create_app(pipeline, api_key=api_key))

    # ---------------- 鉴权 ----------------

    def test_missing_key_is_401_when_configured(self) -> None:
        client = self._client(api_key="secret")
        resp = client.post("/v1/translate/upload", files={"file": ("a.png", _png_bytes(), "image/png")})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "E_AUTH")

    def test_wrong_key_is_401(self) -> None:
        client = self._client(api_key="secret")
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            headers={"X-Api-Key": "wrong"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_correct_key_passes(self) -> None:
        client = self._client(api_key="secret")
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "en"},
            headers={"X-Api-Key": "secret"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # ---------------- 主链路 ----------------

    def test_upload_returns_image_binary(self) -> None:
        client = self._client()
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "en"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("image/"))
        self.assertGreater(len(resp.content), 100)
        # 返回的确实是一张能解码的图
        arr = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(arr)
        # 元信息头必须与 /v1/translate 一致：曾经上传口只给了耗时，
        # 二进制模式下调用方拿不到块数，看起来像"元信息丢了"。
        for name in ("X-ImgTrans-Blocks", "X-ImgTrans-Painted", "X-ImgTrans-Total-Ms"):
            self.assertIn(name, resp.headers)

    def test_detailed_returns_json_with_blocks(self) -> None:
        client = self._client()
        resp = client.post(
            "/v1/translate/upload?detailed=1",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "en"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertIn("image_base64", payload)
        self.assertIn("blocks", payload)
        self.assertEqual(payload["blocks"][0]["source"], "登录")
        self.assertEqual(payload["blocks"][0]["target"], "Login")
        # base64 能还原成图片
        raw = base64.b64decode(payload["image_base64"])
        self.assertGreater(len(raw), 100)

    def test_target_lang_reaches_translator(self) -> None:
        from imgtrans.api.app import create_app
        from imgtrans.pipeline import ImageTranslatePipeline

        translator = FakeTranslator()
        pipeline = ImageTranslatePipeline(translator, ocr=FakeOcr())
        client = TestClient(create_app(pipeline))

        client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "ja", "source": "zh-CN"},
        )
        self.assertTrue(translator.calls)
        _, source, target = translator.calls[0]
        self.assertEqual(target, "ja")
        self.assertEqual(source, "zh-CN")

    def test_ocr_lang_follows_source_not_target(self) -> None:
        """回归：OCR 语言必须看源语言。

        曾经按 target 推断，翻韩语时用韩语模型读中文原图，
        一个字都认不出来，整张图原样返回（用户看到的就是"没翻译"）。
        """
        from imgtrans.api.app import create_app
        from imgtrans.pipeline import ImageTranslatePipeline

        ocr = FakeOcr()
        pipeline = ImageTranslatePipeline(FakeTranslator(), ocr=ocr)
        client = TestClient(create_app(pipeline))

        client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "ko"},
        )
        self.assertEqual(ocr.calls[0], "ch", "未指定源语言时应使用广谱模型，而不是韩语模型")

    # ---------------- JSON 入口 /v1/translate/img ----------------

    def _img_client(self, *, api_key: str | None = None) -> "TestClient":
        from imgtrans.api.app import create_app
        from imgtrans.pipeline import ImageTranslatePipeline

        pipeline = ImageTranslatePipeline(
            FakeTranslator({"登录": "Login"}), ocr=FakeOcr()
        )
        return TestClient(create_app(pipeline, api_key=api_key))

    def test_img_json_accepts_base64(self) -> None:
        """img 传原始 base64：返回 JSON，图片能还原成真图。"""
        client = self._img_client()
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        resp = client.post("/v1/translate/img", json={"lang": "en", "img": b64})
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["mime"], "image/png")
        self.assertEqual(payload["blocks"][0]["target"], "Login")
        raw = base64.b64decode(payload["image_base64"])
        arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(arr, "返回的 base64 必须能解码成图片")

    def test_img_json_accepts_data_uri(self) -> None:
        """前端 FileReader 出来的 data:image/png;base64,xxx 也要能直接吃。"""
        client = self._img_client()
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        resp = client.post(
            "/v1/translate/img",
            json={"lang": "ja", "img": f"data:image/png;base64,{b64}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_img_json_tolerates_base64_line_breaks(self) -> None:
        """base64 按 76 字符折行是常规操作，带换行不能报错。"""
        client = self._img_client()
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        wrapped = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
        resp = client.post("/v1/translate/img", json={"lang": "en", "img": wrapped})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_img_json_requires_api_key(self) -> None:
        client = self._img_client(api_key="secret")
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        resp = client.post("/v1/translate/img", json={"lang": "en", "img": b64})
        self.assertEqual(resp.status_code, 401)
        ok = client.post(
            "/v1/translate/img",
            json={"lang": "en", "img": b64},
            headers={"X-Api-Key": "secret"},
        )
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_img_json_accepts_bearer_token(self) -> None:
        """Authorization: Bearer 是 HTTP 客户端的默认写法，只认 X-Api-Key 会让人踩坑。"""
        client = self._img_client(api_key="secret")
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        resp = client.post(
            "/v1/translate/img",
            json={"lang": "en", "img": b64},
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_img_json_rejects_unsupported_lang(self) -> None:
        client = self._img_client()
        b64 = base64.b64encode(_png_bytes()).decode("ascii")
        resp = client.post("/v1/translate/img", json={"lang": "zh-CN", "img": b64})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "E_LANG")

    def test_img_json_validates_lang_before_download(self) -> None:
        """URL 入口的语言校验必须在下载之前。

        顺序反了会踩到实测过的坑：传 ``lang=zh`` + 一个 404 的 URL，返回的是
        ``E_IMAGE_FETCH`` 而不是 ``E_LANG``，调用方照着错误码去查图片地址，
        永远修不好。这里同时断言"没有发出过下载请求"。
        """
        client = self._img_client()
        with mock.patch("imgtrans.pipeline.fetch_image") as fetch:
            resp = client.post(
                "/v1/translate/img",
                json={"lang": "zh-CN", "img": "https://example.com/a.jpg"},
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "E_LANG")
        fetch.assert_not_called()

    def test_img_json_rejects_bad_base64(self) -> None:
        client = self._img_client()
        resp = client.post("/v1/translate/img", json={"lang": "en", "img": "not-base64!!"})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "E_PARAM")

    def test_img_json_empty_is_400(self) -> None:
        client = self._img_client()
        resp = client.post("/v1/translate/img", json={"lang": "en", "img": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "E_PARAM")

    def test_img_json_url_is_ssrf_guarded(self) -> None:
        """img 传内网地址必须被拦在下载器，而不是真的去请求。"""
        client = self._img_client()
        resp = client.post(
            "/v1/translate/img",
            json={"lang": "en", "img": "http://169.254.169.254/latest/meta-data/"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "E_IMAGE_FETCH")

    # ---------------- 错误映射 ----------------

    def test_bad_image_is_400(self) -> None:
        client = self._client()
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", b"not-an-image", "image/png")},
            data={"target": "en"},
        )
        self.assertGreaterEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_ssrf_url_is_rejected(self) -> None:
        """URL 入口必须挡住内网地址（云元数据 169.254.169.254 是典型攻击面）。"""
        client = self._client()
        resp = client.post(
            "/v1/translate",
            json={"url": "http://169.254.169.254/latest/meta-data/", "target": "en"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "E_IMAGE_FETCH")

    def test_unsupported_target_is_400_with_lang_code(self) -> None:
        """目标语言不在白名单里：必须是 400 + E_LANG，而不是跑到 OCR 再 500。

        PHP 侧需要区分"参数传错"（改参数）和"链路坏了"（重试/告警），
        两者都落 E_INTERNAL 的话只能人肉翻日志。
        """
        client = self._client()
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
            data={"target": "zh-CN"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "E_LANG")
        self.assertIn("en", resp.json()["error"]["message"])

    def test_languages_endpoint_lists_targets(self) -> None:
        """白名单要能被调用方直接拉取，避免前后端各维护一份语言表。"""
        client = self._client()
        resp = client.get("/v1/languages")
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertEqual(payload["count"], len(payload["targets"]))
        for code in ("en", "ja", "hr", "vi"):
            self.assertIn(code, payload["targets"])
        self.assertNotIn("zh", payload["targets"])

    def test_missing_target_is_422(self) -> None:
        client = self._client()
        resp = client.post(
            "/v1/translate/upload",
            files={"file": ("a.png", _png_bytes(), "image/png")},
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
