"""imgtrans —— 多语言图片翻译服务。

分层结构：

* :mod:`imgtrans.translator` —— 翻译层（Google 免费通道 + 可选 Cloud）
* :mod:`imgtrans.ocr` —— 文字检测识别（PaddleOCR）
* :mod:`imgtrans.inpaint` —— 文字 mask 与擦除
* :mod:`imgtrans.render` —— 样式还原、排版、回填
* :mod:`imgtrans.pipeline` —— 端到端编排
* :mod:`imgtrans.api` —— FastAPI 服务

用法::

    from imgtrans import get_translator, ImageTranslatePipeline

    pipe = ImageTranslatePipeline(get_translator("web", proxy="http://127.0.0.1:10809"))
    result = pipe.translate_url("https://example.com/a.jpg", "en")
    open("out.png", "wb").write(result.image)

**惰性导出**：``ImageTranslatePipeline`` 会连带导入 OpenCV/Pillow。
只用翻译层（例如跑翻译单测）时不该被迫装图像库，所以这里用
``__getattr__`` 延迟到真正访问时才 import。
"""

from __future__ import annotations

from .translator import (
    AUTO_LIST,
    SUPPORTED_LANGS,
    BaseTranslator,
    GoogleCloudTranslator,
    GoogleWebTranslator,
    TranslationError,
    TranslatorUnavailable,
    build_tk,
    convert_lang,
    convert_lang_code,
    get_translator,
    is_supported_lang,
    lookup_auto,
    normalize_lang,
)

__version__ = "0.2.0"

#: 需要图像栈才能导入的名字，惰性加载。
_LAZY = {
    "ImageTranslatePipeline",
    "PipelineConfig",
    "PipelineResult",
    "TextBlock",
    "BlockStyle",
    "RenderedBlock",
    "fetch_image",
    "FetchedImage",
    "should_translate",
}

__all__ = [
    "AUTO_LIST",
    "BaseTranslator",
    "BlockStyle",
    "FetchedImage",
    "GoogleCloudTranslator",
    "GoogleWebTranslator",
    "ImageTranslatePipeline",
    "PipelineConfig",
    "PipelineResult",
    "RenderedBlock",
    "SUPPORTED_LANGS",
    "TextBlock",
    "TranslationError",
    "TranslatorUnavailable",
    "__version__",
    "build_tk",
    "convert_lang",
    "convert_lang_code",
    "fetch_image",
    "get_translator",
    "is_supported_lang",
    "lookup_auto",
    "normalize_lang",
    "should_translate",
]


def __getattr__(name: str):
    if name in ("ImageTranslatePipeline", "PipelineConfig", "should_translate"):
        from . import pipeline

        return getattr(pipeline, name)
    if name in ("PipelineResult", "TextBlock", "BlockStyle", "RenderedBlock"):
        from . import types

        return getattr(types, name)
    if name in ("fetch_image", "FetchedImage"):
        from . import downloader

        return getattr(downloader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
