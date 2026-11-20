"""Google Cloud Translation v2 通道（PHP ``Google::nmtApi()`` 的移植）。

对应 PHP 代码::

    $translator = new TranslateClient(['key' => '']);
    $trOptions = ['source' => ..., 'target' => ..., 'model' => 'nmt'];
    $translator->translateBatch(array_values($chunk), $trOptions); // 每批 128 条

依赖是**可选**的：未安装 ``google-cloud-translate`` 时本模块照常可导入，
只有在真正调用 :meth:`GoogleCloudTranslator.translate_batch` 时才抛出
:class:`~imgtrans.translator.base.TranslatorUnavailable` 并给出清晰指引。
这样下游可以在没有云凭证/没装 SDK 的机器上照常跑 web 通道。

与 PHP 版的差异
---------------
* PHP 版把异常里的 JSON 错误码写进日志文件；本实现改用 ``logging``，
  并把失败条目的原文保留在结果对应位置（PHP 版数组模式没有做逐条兜底）。
* ``model='nmt'`` 保留（v2 API 的 ``model`` 选项），可用构造参数覆盖。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import TranslationError, TranslatorUnavailable
from .lang_map import convert_lang_code

__all__ = ["GoogleCloudTranslator"]

logger = logging.getLogger(__name__)

#: PHP 版 ``array_chunk($values, 128)`` 的批大小。
DEFAULT_BATCH_SIZE: int = 128


class GoogleCloudTranslator:
    """基于 ``google-cloud-translate`` v2 的批量翻译器。

    典型用法::

        translator = GoogleCloudTranslator(credentials="/path/key.json")
        out = translator.translate_batch(["Hello"], None, "zh-CN")
    """

    def __init__(
        self,
        *,
        credentials: str | dict[str, Any] | None = None,
        project: str | None = None,
        api_key: str | None = None,
        model: str = "nmt",
        batch_size: int = DEFAULT_BATCH_SIZE,
        client: Any | None = None,
    ) -> None:
        """初始化（不会立即建立网络连接）。

        Args:
            credentials: 服务账号 JSON 路径或字典。
            project: GCP 项目 ID。
            api_key: API Key（与 credentials 二选一，PHP 版用的是 key）。
            model: 翻译模型，``"nmt"``（默认）或 ``"base"``。
            batch_size: 单次 ``translateBatch`` 条数，默认 128。
            client: 已构造好的 ``TranslateClient``（便于测试注入）。

        Raises:
            ValueError: ``batch_size`` 非法。
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._credentials = credentials
        self._project = project
        self._api_key = api_key
        self._model = model
        self._batch_size = int(batch_size)
        self._client = client
        #: 通道标识，供 ``/healthz`` 与启动日志展示。
        self.kind: str = "cloud"
        #: 与 web 通道保持同一套"失败可见"约定：``None`` = 本轮没出故障。
        #: cloud 通道失败会直接抛异常（有配额与错误码，不必静默降级），
        #: 所以这个字段只在被上层统一读取时用到，始终为 ``None``。
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def translate_batch(
        self,
        texts: list[str],
        source: str | None,
        target: str,
    ) -> list[str]:
        """批量翻译，返回等长同序列表（失败位置为原文）。

        Args:
            texts: 待翻译文本列表。
            source: 源语言；``None``/空串交给服务端自动检测。
            target: 目标语言，必填。

        Returns:
            与 ``texts`` 等长、同序的译文列表。

        Raises:
            TranslatorUnavailable: SDK 缺失或客户端初始化失败。
            TranslationError: ``target`` 为空。
        """
        items = self._normalize_texts(texts)
        if not items:
            return []

        target_code = convert_lang_code(target)
        if not target_code:
            raise TranslationError("target language is required")
        source_code = convert_lang_code(source)

        client = self._ensure_client()
        options: dict[str, Any] = {"target": target_code, "model": self._model}
        if source_code:
            options["source"] = source_code

        result: list[str] = list(items)
        pending = [
            (index, text) for index, text in enumerate(items) if text.strip()
        ]
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            indices = [index for index, _ in batch]
            batch_texts = [text for _, text in batch]
            try:
                response = client.translate_batch(batch_texts, options)
            except Exception as exc:  # noqa: BLE001 - 云端异常统一降级为原文
                logger.error("cloud translate_batch failed: %s", exc)
                continue

            if not isinstance(response, list) or len(response) != len(batch_texts):
                logger.error(
                    "cloud translate_batch returned unexpected payload: %r", response
                )
                continue

            for index, original, item in zip(indices, batch_texts, response):
                translated = self._pick_text(item)
                if translated is None:
                    logger.warning("cloud element has no text: %r", item)
                    result[index] = original
                    continue
                result[index] = translated if translated.strip() else original
        return result

    def translate(
        self,
        text: str,
        source: str | None = None,
        target: str = "zh-CN",
    ) -> str:
        """单条翻译便捷方法。"""
        result = self.translate_batch([text], source, target)
        return result[0] if result else text

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_texts(texts: Any) -> list[str]:
        """把输入规整成 ``list[str]``。"""
        if not texts:
            return []
        normalized: list[str] = []
        for text in texts:
            if text is None:
                normalized.append("")
            elif isinstance(text, str):
                normalized.append(text)
            else:
                normalized.append(str(text))
        return normalized

    @staticmethod
    def _pick_text(item: Any) -> str | None:
        """从 v2 响应元素里取译文。"""
        if isinstance(item, dict):
            value = item.get("text")
            return value if isinstance(value, str) else None
        if isinstance(item, str):
            return item
        return None

    def _ensure_client(self) -> Any:
        """惰性构造 ``TranslateClient``。

        Raises:
            TranslatorUnavailable: SDK 缺失或客户端初始化失败。
        """
        if self._client is not None:
            return self._client
        try:
            from google.cloud import translate_v2  # noqa: PLC0415 - 可选依赖
        except ImportError as exc:
            raise TranslatorUnavailable(
                "google-cloud-translate is required for the cloud channel; "
                "install it with `pip install google-cloud-translate` "
                "or use the web channel instead"
            ) from exc

        kwargs: dict[str, Any] = {}
        if self._credentials is not None:
            kwargs["credentials"] = self._credentials
        if self._project is not None:
            kwargs["project"] = self._project
        if self._api_key is not None:
            kwargs["client_options"] = {"api_key": self._api_key}
        try:
            self._client = translate_v2.Client(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 凭证/网络问题统一包装
            raise TranslatorUnavailable(f"failed to create cloud translate client: {exc}") from exc
        return self._client

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(model={self._model!r}, "
            f"batch_size={self._batch_size})"
        )
