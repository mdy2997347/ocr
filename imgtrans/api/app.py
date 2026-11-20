"""FastAPI 服务层。

对外两个入口：

* ``POST /v1/translate``：JSON 传 URL，**默认直接返回图片二进制**——
  与百度图片翻译的形态对齐，PHP 侧 `file_get_contents` 就能拿到图。
* ``POST /v1/translate/upload``：multipart 传文件，供不走 URL 的场景。
* ``POST /v1/translate/img``：**纯 JSON 出入口**（``{"lang","img"}``），
  返回 JSON（base64 图片 + 每块坐标/原文/译文/字号）。这是给 PHP 侧用的主形态：
  不用 multipart，也不依赖二进制响应头，``json_decode`` 一把梭。

第二个入口的 ``img`` 同时接受 http(s) 地址与 base64（可带 ``data:`` 前缀）。
支持两种写法是刻意的：同一个参数名在不同调用方那里语义不同（有人手里只有
URL、有人手里已经是字节流），与其逼调用方自己判断，不如这里判一次。

鉴权用请求头 ``X-Api-Key``（也接受 ``Authorization: Bearer <key>``），
从环境变量 ``IMGTRANS_API_KEY`` 读（可由项目根目录的 ``.env`` 提供）；
未配置时**只在 ``IMGTRANS_DEV=1`` 下放行**，避免默认裸奔上线。
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import secrets
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..downloader import MAX_BYTES
from ..pipeline import ImageTranslatePipeline
from ..translator.lang_map import SUPPORTED_LANGS
from ..types import ImageFetchError, ImageTranslateError, OcrError

logger = logging.getLogger(__name__)

#: 错误码 -> HTTP 状态码。对外统一用 ``{"error": {"code","message"}}`` 结构。
_ERROR_STATUS: dict[str, int] = {
    "E_IMAGE_FETCH": 400,
    "E_LANG": 400,  # 目标语言码不受支持：属于调用方参数错误
    "E_PARAM": 400,  # 参数本身不合法（img 既不是 URL 也不是合法 base64 等）
    "E_OCR": 500,
    "E_INTERNAL": 500,
    "E_AUTH": 401,
    "E_PAYLOAD": 413,
}


class TranslateRequest(BaseModel):
    url: str = Field(..., description="远程图片地址（http/https）")
    target: str = Field(..., description="目标语言码，见 GET /v1/languages；如 en / ja / ko")
    source: str | None = Field(None, description="源语言码，不传则按默认 OCR 语言（中文）识别")
    render: bool = Field(True, description="是否把译文回填到图中")


class ImgTranslateRequest(BaseModel):
    """``POST /v1/translate/img`` 的请求体。

    字段名按业务方口径定（``lang`` / ``img``），不做成 ``target`` / ``url``：
    接口是给 PHP 侧直接调的，字段名跟对方文档一致比"我们内部命名更优雅"重要。
    """

    lang: str = Field(..., description="目标语言码，见 GET /v1/languages；如 en / ja / hr")
    img: str = Field(
        ...,
        description="图片：http(s) 地址，或 base64（可带 data:image/png;base64, 前缀）",
    )
    source: str | None = Field(None, description="源语言码，不传则按默认 OCR 语言（中文）识别")


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_ERROR_STATUS.get(code, 500),
        content={"error": {"code": code, "message": message}},
    )


def _block_dict(b: Any) -> dict[str, Any]:
    """把 RenderedBlock 转成可序列化 dict。

    不用 ``asdict()``：``RenderedBlock`` 是 ``slots=True``，且字段顺序
    对外是契约，显式列出比反射更稳（加字段时不会悄悄改 API 形状）。
    """
    return {
        "index": b.index,
        "source": b.source,
        "target": b.target,
        "painted": b.painted,
        "font_size": b.font_size,
        "lines": b.lines,
        "fit_mode": b.fit_mode,
        "note": b.note,
    }


def _detail_payload(result: Any, elapsed_ms: float) -> dict[str, Any]:
    """把流水线结果摊平成对外 JSON。

    三个入口（``?detailed=1`` / ``/upload?detailed=1`` / ``/img``）共用同一形状，
    调用方只写一套解析逻辑。``image_base64`` 而不是 ``image``：JSON 里放不下
    二进制，字段名带 ``_base64`` 能省掉一轮"这串是什么"的沟通。
    """
    return {
        "image_base64": base64.b64encode(result.image).decode("ascii"),
        "mime": result.mime,
        "width": result.width,
        "height": result.height,
        "blocks": [_block_dict(b) for b in result.blocks],
        "timings": result.timings,
        "total_ms": round(elapsed_ms, 1),
        "warnings": result.warnings,
    }


def _binary_response(result: Any, elapsed_ms: float) -> Response:
    """二进制出口：图片本体 + 三个 ``X-ImgTrans-*`` 元信息头。

    ``/v1/translate`` 与 ``/v1/translate/upload`` 共用。原先两个端点各写一份，
    上传端少了 ``Blocks`` / ``Painted``——PHP 客户端解析头部时就只拿到耗时，
    看起来像"元信息丢了"。同一个形状只该有一处定义。
    """
    return Response(
        content=result.image,
        media_type=result.mime,
        headers={
            "X-ImgTrans-Blocks": str(len(result.blocks)),
            "X-ImgTrans-Painted": str(result.translated_count),
            "X-ImgTrans-Total-Ms": f"{elapsed_ms:.0f}",
        },
    )


#: ``data:image/png;base64,xxxx`` 前缀。调用方从前端 ``FileReader`` 里直接
#: 拿到的字符串就长这样，剥掉再解码，否则 base64 解码必然失败。
_DATA_URI_PREFIX = re.compile(r"^data:[^;,]+;base64,", re.IGNORECASE)


def _decode_img_field(value: str) -> bytes:
    """把 ``img`` 字段的 base64 解成字节。

    只接受 base64（URL 走另一条分支）。空格与换行要清掉——base64 按 76 字符
    折行是常规操作，带着换行直接 ``b64decode`` 会抛 ``binascii.Error``。
    """
    text = _DATA_URI_PREFIX.sub("", value.strip())
    text = re.sub(r"\s+", "", text)
    if not text:
        raise ValueError("img 为空")
    try:
        # validate=True：容忍脏字符比"静默解出一段垃圾字节"好得多
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"img 不是合法的 base64: {exc}") from exc


def create_app(
    pipeline: ImageTranslatePipeline,
    *,
    api_key: str | None = None,
    proxy: str | None = None,
    warmup: bool = True,
) -> FastAPI:
    """构造应用。``pipeline`` 由调用方注入，便于测试替换。

    Args:
        pipeline: 已装配的流水线。
        api_key: 鉴权 key；``None`` 时读环境变量 ``IMGTRANS_API_KEY``。
        proxy: 下载图片用的代理；``None`` 时读 ``IMGTRANS_PROXY``。
            只在 ``/v1/translate/img`` 传 URL 时用到（Google 出网代理在
            翻译器里配，这里只管图片下载）。
        warmup: 是否在 startup 阶段预热 OCR 模型。默认 ``True``。做成开关是
            因为启动即预热会让"只想验证接口通不通"的调试**卡住几秒到几十秒**
            （GPU 上还要等显存分配），``serve.py --no-warmup`` 依赖它。
    """
    app = FastAPI(
        title="imgtrans",
        version="0.1.0",
        description="图片文字翻译：OCR → 翻译 → 擦除 → 原位回填",
    )

    expected_key = api_key if api_key is not None else os.environ.get("IMGTRANS_API_KEY")
    dev_mode = os.environ.get("IMGTRANS_DEV") == "1"
    download_proxy = proxy if proxy is not None else os.environ.get("IMGTRANS_PROXY")

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request, exc: HTTPException) -> JSONResponse:
        """把 FastAPI 的 ``{"detail": ...}`` 统一成 ``{"error": {code, message}}``。

        对外只能有一种错误形状：PHP 侧 / 前端只写一套解析逻辑。
        鉴权失败和业务失败长得不一样，是很常见的集成踩坑点。
        """
        code = {
            401: "E_AUTH",
            413: "E_PAYLOAD",
            429: "E_RATE_LIMIT",
        }.get(exc.status_code, "E_INTERNAL")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
        )

    def require_auth(
        x_api_key: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """校验 API Key。

        同时认 ``X-Api-Key`` 和 ``Authorization: Bearer <key>``：前者是这里
        既有的约定，后者是大多数 HTTP 客户端/网关的默认写法。只认一种的话，
        接入方很容易"明明传了 key 却 401"，然后来问为什么会话不通。

        比较用 :func:`secrets.compare_digest`，避免逐字节短路带来的时序侧信道
        ——key 校验是暴力破解的入口，这类细节不该省。
        """
        if expected_key is None:
            if not dev_mode:
                raise HTTPException(
                    status_code=500,
                    detail="服务未配置 IMGTRANS_API_KEY，且未开启 IMGTRANS_DEV",
                )
            return

        provided = x_api_key
        if not provided and authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                provided = token.strip()

        if not provided or not secrets.compare_digest(provided, expected_key):
            raise HTTPException(status_code=401, detail="invalid api key")

    auth = Depends(require_auth)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """健康检查。带 ``device`` 字段。

        "这套服务到底跑在 CPU 还是 GPU 上"是最常被问、也最容易被 ``.env``
        与真实 paddle 版本不一致搞混的一件事。放在不需要鉴权的探活接口里，
        运维 curl 一下就清楚，不用翻日志。
        ``getattr`` 保底：单测注入的假 pipeline 不一定有 ``device``。
        """
        device = getattr(pipeline, "device", None)
        payload: dict[str, Any] = {"ok": True}
        if device:
            payload["device"] = device
        return payload

    @app.get("/v1/languages")
    def languages() -> dict[str, Any]:
        """返回支持的目标语言码。

        把白名单做成接口，PHP 侧就能在后台下拉框里直接拉，不用两边各维护
        一份语言列表——那种双写迟早会不一致。
        """
        return {"targets": list(SUPPORTED_LANGS), "count": len(SUPPORTED_LANGS)}

    @app.post("/v1/translate", dependencies=[auth])
    def translate_url(
        body: TranslateRequest,
        detailed: Annotated[int, Query(description="1=返回 JSON 详情")] = 0,
    ) -> Response:
        started = time.perf_counter()
        try:
            result = pipeline.translate_url(body.url, body.target, body.source)
        except ImageFetchError as exc:
            return _error("E_IMAGE_FETCH", str(exc))
        except OcrError as exc:
            return _error("E_OCR", str(exc))
        except ImageTranslateError as exc:
            return _error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 500 堆栈外泄
            logger.exception("translate 失败")
            return _error("E_INTERNAL", f"{type(exc).__name__}: {exc}")

        total_ms = (time.perf_counter() - started) * 1000

        if detailed:
            return JSONResponse(_detail_payload(result, total_ms))

        return _binary_response(result, total_ms)

    @app.post("/v1/translate/upload", dependencies=[auth])
    def translate_upload(
        file: Annotated[UploadFile, File(description="图片文件")],
        target: Annotated[str, Form(description="目标语言码，见 GET /v1/languages")],
        source: Annotated[str | None, Form()] = None,
        detailed: Annotated[int, Query()] = 0,
    ) -> Response:
        data = file.file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return _error("E_PAYLOAD", f"文件超过 {MAX_BYTES} 字节")
        if not data:
            return _error("E_PAYLOAD", "文件为空")

        started = time.perf_counter()
        try:
            result = pipeline.translate_bytes(
                data, target, source, mime=file.content_type
            )
        except OcrError as exc:
            return _error("E_OCR", str(exc))
        except ImageTranslateError as exc:
            return _error(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("upload translate 失败")
            return _error("E_INTERNAL", f"{type(exc).__name__}: {exc}")

        total_ms = (time.perf_counter() - started) * 1000
        if detailed:
            return JSONResponse(_detail_payload(result, total_ms))

        return _binary_response(result, total_ms)

    @app.post("/v1/translate/img", dependencies=[auth])
    def translate_img(body: ImgTranslateRequest) -> Response:
        """JSON 出入口：``{"lang": "en", "img": "<url 或 base64>"}`` -> JSON。

        与两个老入口的区别：

        * 请求体是 JSON，**不用 multipart**——PHP 侧 ``curl`` 传 JSON 比
          构造 multipart 简单；
        * 响应固定 JSON（不做二进制/JSON 双形态），调用方不需要看 ``?detailed``
          这类开关决定怎么解析。

        成功返回 200 + :func:`_detail_payload`；失败返回 4xx/5xx +
        ``{"error": {"code", "message"}}``，与其余接口保持同一错误形状。
        """
        started = time.perf_counter()
        raw = (body.img or "").strip()
        if not raw:
            return _error("E_PARAM", "img 不能为空")

        try:
            if raw.lower().startswith(("http://", "https://")):
                # 走 translate_url 而不是自己下载：SSRF 防护、体积限制、
                # Content-Type 校验都在 downloader 里，重复实现必然漏一条。
                result = pipeline.translate_url(
                    raw, body.lang, body.source, proxy=download_proxy
                )
            else:
                data = _decode_img_field(raw)
                if not data:
                    return _error("E_PARAM", "img base64 解码后为空")
                if len(data) > MAX_BYTES:
                    return _error("E_PAYLOAD", f"图片超过 {MAX_BYTES} 字节")
                result = pipeline.translate_bytes(data, body.lang, body.source)
        except ImageFetchError as exc:
            return _error("E_IMAGE_FETCH", str(exc))
        except OcrError as exc:
            return _error("E_OCR", str(exc))
        except ImageTranslateError as exc:
            # 含 UnsupportedLangError -> E_LANG(400)
            return _error(exc.code, str(exc))
        except ValueError as exc:
            # 图片解码失败（字节流不是图片）也走这里，算调用方参数问题
            return _error("E_PARAM", str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，避免 500 堆栈外泄
            logger.exception("translate_img 失败")
            return _error("E_INTERNAL", f"{type(exc).__name__}: {exc}")

        return JSONResponse(
            _detail_payload(result, (time.perf_counter() - started) * 1000)
        )

    @app.on_event("startup")
    def _warmup() -> None:
        # OCR 模型加载很贵，放启动阶段，别摊到首图延迟上。
        # 但 --no-warmup 必须真的跳过：本函数曾被无条件调用，
        # 导致 serve.py 的开关形同虚设（调试时照样卡十几秒）。
        if not warmup:
            logger.info("跳过模型预热（warmup=False）")
            return
        try:
            pipeline.warmup()
        except Exception:  # noqa: BLE001
            logger.exception("预热失败，服务仍启动（首次请求会重试）")

    return app
