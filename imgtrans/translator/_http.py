"""翻译通道共用的 HTTP 基础设施（传输层 + 令牌桶）。

为什么单独一个模块
------------------
``google_web`` 与 ``baidu`` 两个通道的**业务**毫无共同点（一个要算 TKK 签名、
一个要算 MD5 签名），但**基础设施**完全一致：连接复用、代理注入、连接阶段
单独限时、请求速率限制。这些代码写在任一通道里再让另一个 import 私有名，
或者复制一份，都是坏味道，所以抽到这里。

模块名以 ``_`` 开头表示不对外：外部只该依赖
:mod:`imgtrans.translator.base` 里的契约。

.. note::
   ``_TokenBucket`` / ``_HttpXTransport`` 这两个名字保留了下划线前缀——
   它们是 :mod:`imgtrans.translator.google_web` 里的历史名字，单测直接按
   这个名字导入。改名的收益为零，破坏测试的成本为正，所以原样搬过来。
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Any, Callable

from .base import TranslationError

__all__ = [
    "LOCAL_ADDRESS_UNSUPPORTED",
    "Transport",
    "_HttpXTransport",
    "_TokenBucket",
]

logger = logging.getLogger(__name__)

#: ``local_address`` 的显式不支持说明。httpx 没有 ``CURLOPT_INTERFACE`` 等价物，
#: 这里宁可报错也不能"记个 warning 继续跑"——那会让多网卡部署以为绑了源 IP，
#: 实际用的是默认出口。
LOCAL_ADDRESS_UNSUPPORTED: str = (
    "local_address (source IP binding, PHP CURLOPT_INTERFACE equivalent) is not "
    "supported: httpx has no API for binding a source address. Use an HTTP proxy "
    "pool, OS-level policy routing (ip route / SO_BINDTODEVICE), or inject a custom "
    "transport that implements binding."
)

#: 传输层签名：``transport(method, url, params, headers, data, timeout) -> (status, text)``。
#: ``data`` 为 bytes 或 None；实现方不得抛异常以外的方式失败。
Transport = Callable[
    [str, str, "dict[str, Any] | None", "dict[str, str] | None", "bytes | None", float],
    "tuple[int, str]",
]


class _TokenBucket:
    """简单令牌桶限流器（线程安全）。

    容量取 ``max(1, qps)``，即允许 ``qps`` 个请求的瞬时突发；
    :meth:`penalize` 用于 429/503 后的额外降速。
    """

    def __init__(self, qps: float) -> None:
        self._qps = max(0.0, float(qps))
        self._capacity = max(1.0, self._qps) if self._qps > 0 else 0.0
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._not_before = 0.0
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        """取一个令牌，必要时阻塞等待。``qps <= 0`` 表示不限流。"""
        if self._qps <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._not_before:
                    wait = self._not_before - now
                else:
                    self._tokens = min(
                        self._capacity,
                        self._tokens + (now - self._updated) * self._qps,
                    )
                    self._updated = now
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return
                    wait = (tokens - self._tokens) / self._qps
            time.sleep(min(max(wait, 0.001), 1.0))

    def penalize(self, seconds: float) -> None:
        """在接下来 ``seconds`` 秒内禁止取令牌（用于限流降速）。"""
        if seconds <= 0:
            return
        with self._lock:
            self._not_before = max(self._not_before, time.monotonic() + seconds)


class _HttpXTransport:
    """基于 ``httpx`` 的默认传输层（连接复用 + 可选代理）。

    代理与源地址绑定是**两件独立的事**，本类对它们彻底解耦：

    * ``proxy`` 通过 httpx 的 ``proxy``（新版本）或 ``proxies``（旧版本）关键字
      注入，注入失败会**立即抛错**，绝不静默降级 —— 本服务必须走代理，
      "悄悄变直连"等于整条链路全超时。
    * ``local_address``（等价 PHP ``CURLOPT_INTERFACE``）**httpx 无法实现**，
      传入即抛 :class:`NotImplementedError`，不做"记个 warning 继续跑"。
    """

    def __init__(self, proxy: str | None = None, local_address: str | None = None) -> None:
        """初始化。

        Args:
            proxy: 代理地址，例如 ``"http://127.0.0.1:7890"``。
            local_address: 源地址绑定；**本实现不支持**，传非空值即抛错。

        Raises:
            NotImplementedError: 传入了 ``local_address``。
            TranslationError: 装了 httpx 但没有任何代理关键字可用。
        """
        if local_address:
            raise NotImplementedError(LOCAL_ADDRESS_UNSUPPORTED)

        self._proxy = proxy
        self._proxy_kwarg = self._resolve_proxy_kwarg() if proxy else None
        self._client: Any = None
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_proxy_kwarg() -> str:
        """解析当前 httpx 版本使用的代理关键字名。

        Returns:
            ``"proxy"``（httpx >= 0.26）或 ``"proxies"``（更早版本）。

        Raises:
            TranslationError: 已装 httpx 但无法注入代理。
        """
        try:
            import httpx  # noqa: PLC0415 - 延迟导入
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise TranslationError(
                "httpx is required for the default transport; "
                "install it with `pip install httpx` or inject a custom transport"
            ) from exc

        parameters = inspect.signature(httpx.Client.__init__).parameters
        for keyword in ("proxy", "proxies"):
            if keyword in parameters:
                return keyword
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            return "proxy"
        raise TranslationError(
            f"installed httpx {httpx.__version__} exposes no proxy keyword; "
            "refusing to run without proxy (configure a custom transport instead)"
        )

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                import httpx  # noqa: PLC0415 - 延迟导入
            except ImportError as exc:  # pragma: no cover - 依赖缺失分支
                raise TranslationError(
                    "httpx is required for the default transport; "
                    "install it with `pip install httpx` or inject a custom transport"
                ) from exc

            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(10.0),
                "follow_redirects": True,
            }
            if self._proxy_kwarg:
                kwargs[self._proxy_kwarg] = self._proxy
            try:
                self._client = httpx.Client(**kwargs)
            except TypeError as exc:
                # 绝不静默丢代理：宁可让部署失败，也不能悄悄变直连。
                raise TranslationError(
                    f"failed to create httpx client with proxy={self._proxy!r} "
                    f"(keyword={self._proxy_kwarg!r}): {exc}"
                ) from exc
            return self._client

    @staticmethod
    def _build_timeout(seconds: float) -> Any:
        """跨境请求超时拆分：connect 3s，read/write 用整体超时。

        PHP 原码统一 ``timeout = 3``（连接+读取共 3s），跨境场景必然超时；
        这里按设计文档把连接阶段独立限制在 3s，读取阶段用调用方给的 ``timeout``。

        Args:
            seconds: 读取阶段超时秒数。

        Returns:
            ``httpx.Timeout`` 实例（未安装 httpx 时返回原浮点值）。
        """
        try:
            import httpx  # noqa: PLC0415 - 与 _get_client 共用惰性导入
        except ImportError:  # pragma: no cover - 依赖缺失分支
            return seconds
        return httpx.Timeout(float(seconds), connect=min(3.0, float(seconds)))

    def __call__(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, str]:
        client = self._get_client()
        response = client.request(
            method,
            url,
            params=params,
            headers=headers,
            content=data,
            timeout=self._build_timeout(timeout),
        )
        return int(response.status_code), response.text

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不影响主流程
                    logger.debug("close httpx client failed: %s", exc)
                finally:
                    self._client = None
