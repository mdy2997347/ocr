"""Google 翻译 web 通道（PHP ``Google::api()`` / ``Google::free()`` 的移植）。

两条通道
--------
* **api**（主通道，PHP ``api()``）：``POST /translate_a/t``，
  单次请求可携带多个 ``q`` 参数，响应是与 ``q`` 顺序一一对应的 JSON 数组。
  批量翻译时 ``tk`` 用 **所有文本拼接串** 计算（PHP ``implode("", $text)``）。
* **free**（降级通道，PHP ``free()``）：``POST /translate_a/single``，
  伪装 Android 客户端；**整批文本用 ``\\n`` 拼成一个 ``q``，一次请求发完**，
  回包按 ``sentences[].orig`` 对齐回各段（与 PHP 一致）。

与 PHP 版的行为差异（均为有意为之，已与主理人确认）
--------------------------------------------------
1. **不再** 对译文做 ``ucfirst(strtolower($text))``。该后处理会把
   ``Hello WORLD`` 变成 ``Hello world``，摧毁德语名词大写、全大写标题与
   品牌名，对 CJK 目标语言更是无意义。本实现只做 ``html.unescape()``。
2. 不移植 ``cn()`` 通道：``translate.google.cn`` 已于 2022 年停止服务。
3. ``multi_ip``（多网卡出口 IP 随机轮换）不移植。**注意：不要以为
   ``local_address`` 参数能等价替代它** —— httpx 无法绑定源地址，本模块
   传 ``local_address`` 会直接抛 :class:`NotImplementedError`（见
   :data:`LOCAL_ADDRESS_UNSUPPORTED`）。多出口需求请走代理池或系统策略路由，
   或注入自定义 transport。

契约（见 :mod:`imgtrans.translator.base`）
-----------------------------------------
``translate_batch`` 返回等长、同序列表；单条失败放原文。
"""

from __future__ import annotations

import html
import inspect
import json
import logging
import random
import re
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import quote_plus, urlencode

from .base import TranslationError
from .google_tk import DEFAULT_TKK, TKKSnapshot, build_tk

__all__ = ["GoogleWebTranslator"]

logger = logging.getLogger(__name__)

#: ``api`` 通道域名。PHP 用 http，这里统一改用 https。
API_DOMAIN: str = "https://translate.googleapis.com"
#: ``api`` 批量端点。
API_PATH: str = "/translate_a/t"

#: ``free`` 通道域名（同时用于抓 TKK）。
FREE_DOMAIN: str = "https://translate.google.com"
#: ``free`` 端点。
FREE_PATH: str = "/translate_a/single"

#: ``free`` 通道要求的 Android 客户端 UA（PHP 里硬编码的那串）。
FREE_USER_AGENT: str = (
    "AndroidTranslate/5.3.0.RC02.130475354-53000263 5.1 phone TRANSLATE_OPM5_TEST_1"
)

#: 需要退避重试的 HTTP 状态码。
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: 触发 ``free`` 通道 cookie 刷新的状态码。
FREE_RETRY_STATUS: int = 503

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


def _normalize_for_match(text: str) -> str:
    """对齐 ``sentences[].orig`` 与入参文本时用的归一化（PHP 同款）。

    PHP ``getTransTexts()`` 的写法是：去空白 → 合并连续逗号 → 去不换行空格
    → 去句点，再互相 ``stristr``。Google 回包里的 ``orig`` 会在标点、空格、
    换行上与入参有细微差异，不归一化就永远对不上。
    """
    return re.sub(r",+", ",", re.sub(r"\s+", "", text)).replace("\u00a0", "").replace(".", "")


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
        """在接下来 ``seconds`` 秒内禁止取令牌（用于 429/503 降速）。"""
        if seconds <= 0:
            return
        with self._lock:
            self._not_before = max(self._not_before, time.monotonic() + seconds)


class _CircuitBreaker:
    """连续错误熔断器（线程安全）。

    语义（与设计文档 §6 一致）：

    * **触发**：连续 ``threshold_403`` 次 403 或连续 ``threshold_429`` 次 429；
    * **打开期间**：:meth:`allow` 返回 ``False``，调用方**直接返回原文**、
      不抛异常、不发请求 —— 图片翻译是尽力而为链路，绝不能因翻译失败阻塞出图；
    * **半开**：``open_seconds`` 到期后只放行 **1 个探测请求**，成功则闭合，
      失败则重新打开并重新计时；
    * 任何非 403/429 的响应（含 200、400、5xx）都会清零连续计数。

    计数是"连续"语义：403 会清零 429 计数，反之亦然。
    """

    def __init__(
        self,
        *,
        threshold_403: int = 20,
        threshold_429: int = 15,
        open_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化。

        Args:
            threshold_403: 连续 403 多少次后熔断。
            threshold_429: 连续 429 多少次后熔断。
            open_seconds: 熔断保持时长（秒）。
            clock: 单调时钟注入点，默认 ``time.monotonic``；单测可注入假时钟
                来验证"60 秒后转半开"，无需真的 sleep。
        """
        self._threshold_403 = max(1, int(threshold_403))
        self._threshold_429 = max(1, int(threshold_429))
        self._open_seconds = max(0.0, float(open_seconds))
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._state = "closed"
        self._open_until = 0.0
        self._probe_in_flight = False
        self._consecutive_403 = 0
        self._consecutive_429 = 0
        self._trip_count = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        """当前状态：``"closed"`` / ``"open"`` / ``"half_open"``。"""
        with self._lock:
            return self._state

    @property
    def is_open(self) -> bool:
        """熔断是否正在生效（供监控埋点使用）。"""
        with self._lock:
            return self._state == "open"

    @property
    def trip_count(self) -> int:
        """累计熔断次数（供监控埋点使用）。"""
        with self._lock:
            return self._trip_count

    def allow(self) -> bool:
        """本次请求是否放行。"""
        with self._lock:
            now = self._clock()
            if self._state == "open":
                if now < self._open_until:
                    return False
                # 到期 → 半开，只放行一个探测请求
                self._state = "half_open"
                self._probe_in_flight = True
                logger.warning("circuit half_open: sending 1 probe request")
                return True
            if self._state == "half_open":
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return True

    def record_status(self, status: int) -> None:
        """记录一次响应状态。

        Args:
            status: HTTP 状态码。
        """
        with self._lock:
            if status == 403:
                self._consecutive_403 += 1
                self._consecutive_429 = 0
            elif status == 429:
                self._consecutive_429 += 1
                self._consecutive_403 = 0
            else:
                self._consecutive_403 = 0
                self._consecutive_429 = 0
                if self._state == "half_open":
                    self._probe_in_flight = False
                    self._state = "closed"
                    logger.warning("circuit closed: probe request succeeded")
                return

            if self._state == "half_open":
                # 探测失败 → 立刻重新打开并重新计时
                self._probe_in_flight = False
                self._trip()
                return

            if self._state == "open":
                # 已经打开：只累计计数，不重复触发、不重置计时窗口。
                # 否则持续的 403/429 会把窗口无限延长，且 trip_count 与日志被刷爆。
                return

            if (
                self._consecutive_403 >= self._threshold_403
                or self._consecutive_429 >= self._threshold_429
            ):
                self._trip()

    def record_exception(self) -> None:
        """记录一次传输层异常（探测失败也要重新打开）。"""
        with self._lock:
            if self._state == "half_open":
                self._probe_in_flight = False
                self._trip()

    def reset(self) -> None:
        """人工复位（运维手动解除熔断用）。"""
        with self._lock:
            self._state = "closed"
            self._open_until = 0.0
            self._probe_in_flight = False
            self._consecutive_403 = 0
            self._consecutive_429 = 0
            logger.warning("circuit reset manually")

    def _trip(self) -> None:
        """打开熔断（调用方需已持锁）。"""
        self._state = "open"
        self._open_until = self._clock() + self._open_seconds
        self._trip_count += 1
        logger.error(
            "circuit_open: tripped (consecutive 403=%d/%d, 429=%d/%d), "
            "requests will return originals for %.0fs",
            self._consecutive_403,
            self._threshold_403,
            self._consecutive_429,
            self._threshold_429,
            self._open_seconds,
        )


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


class GoogleWebTranslator:
    """Google 翻译免费 web 接口的批量翻译器。

    典型用法::

        translator = GoogleWebTranslator()
        out = translator.translate_batch(["Hello", "World"], None, "zh-CN")

    Attributes:
        MAX_ATTEMPTS: 单个 HTTP 请求的最大尝试次数（含首次）。
        BACKOFF_BASE: 指数退避基数（秒）。
        BACKOFF_CAP: 单次退避上限（秒）。
        BACKOFF_JITTER: 退避随机抖动上限（秒）。
        RATE_LIMIT_PENALTY: 429/503 后的额外降速秒数。
    """

    MAX_ATTEMPTS: int = 3
    BACKOFF_BASE: float = 0.5
    BACKOFF_CAP: float = 5.0
    BACKOFF_JITTER: float = 0.3
    RATE_LIMIT_PENALTY: float = 1.0
    #: 连续多少次 403 触发熔断。
    CIRCUIT_403_THRESHOLD: int = 20
    #: 连续多少次 429 触发熔断。
    CIRCUIT_429_THRESHOLD: int = 15
    #: 熔断保持时长（秒）。
    CIRCUIT_OPEN_SECONDS: float = 60.0

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_chars_per_request: int = 4000,
        max_items_per_request: int = 50,
        qps: float = 4.0,
        proxy: str | None = None,
        local_address: str | None = None,
        tkk_ttl: int = 21600,
        prefer: str = "api",
        transport: Transport | None = None,
        fetch_tkk: bool = False,
    ) -> None:
        """初始化。

        Args:
            timeout: 单次 HTTP 请求超时秒数（读取阶段；连接阶段固定 3s）。
            max_chars_per_request: 单次请求的字符数上限（按拼接串长度计）。
            max_items_per_request: 单次请求的文本条数上限。
            qps: 每秒请求数上限，``<= 0`` 不限流。
            proxy: 代理地址，例如 ``"http://127.0.0.1:7890"``。注入失败会直接抛错，
                **不会**静默降级为直连。
            local_address: **不支持**。httpx 无法绑定源地址，传非空值即抛
                :class:`NotImplementedError`（见 :data:`LOCAL_ADDRESS_UNSUPPORTED`）。
            tkk_ttl: TKK 缓存有效期（秒），默认 6 小时。
            prefer: 首选通道，``"api"`` 或 ``"free"``。
            transport: 自定义传输层，便于测试注入；``None`` 用 httpx 实现。

        Raises:
            ValueError: 参数非法。
            NotImplementedError: 传入了 ``local_address``。
            TranslationError: 代理无法注入（配置了 ``proxy`` 但 httpx 不支持）。
        """
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if max_chars_per_request < 1:
            raise ValueError("max_chars_per_request must be >= 1")
        if max_items_per_request < 1:
            raise ValueError("max_items_per_request must be >= 1")
        if prefer not in ("api", "free"):
            raise ValueError("prefer must be 'api' or 'free'")
        if local_address:
            # 无论是否注入自定义 transport，这里都不能"收下但不生效"。
            raise NotImplementedError(LOCAL_ADDRESS_UNSUPPORTED)

        self._timeout = float(timeout)
        self._max_chars = int(max_chars_per_request)
        self._max_items = int(max_items_per_request)
        self._prefer = prefer
        self._transport: Transport = (
            transport
            if transport is not None
            else _HttpXTransport(proxy=proxy, local_address=local_address)
        )
        self._bucket = _TokenBucket(qps)
        self._breaker = _CircuitBreaker(
            threshold_403=self.CIRCUIT_403_THRESHOLD,
            threshold_429=self.CIRCUIT_429_THRESHOLD,
            open_seconds=self.CIRCUIT_OPEN_SECONDS,
        )
        self._tkk = TKKSnapshot(
            self._transport,
            url=FREE_DOMAIN,
            ttl=float(tkk_ttl),
            timeout=self._timeout,
            initial=DEFAULT_TKK,
            allow_fetch=fetch_tkk,
        )
        #: 最近一次 api 通道的 HTTP 状态码，供"是否值得重试"的判断使用。
        self._last_api_status: int | None = None
        #: 最近一次 ``translate_batch`` 的整体失败原因；成功后清空。
        #: ``None`` 表示"这一轮翻译没出故障"——注意与"译不出来"不同：
        #: 图里没有可译文字时它也是 ``None``。上层据此区分"正常返回原图"
        #: 和"通道挂了却返回原图"，后者必须报错。
        self.last_error: str | None = None
        #: 通道标识，供 ``/healthz`` 展示。
        self.kind: str = "free" if prefer == "free" else "web"

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
            source: 源语言；``None``/空串表示自动检测（``auto``）。
            target: 目标语言，必填。

        Returns:
            与 ``texts`` 等长、同序的译文列表。

        Raises:
            ValueError: ``target`` 为空。
        """
        normalized_target = self._web_lang(target)
        if not normalized_target:
            raise ValueError("target language is required")
        normalized_source = self._web_lang(source, default="auto") or "auto"
        # 每次批量翻译开头清空：上一次的失败原因不能留在这一次的结果里，
        # 否则一张正常的图会被上一次的 429 误判成故障（反之亦然）。
        self.last_error = None

        items = self._normalize_texts(texts)
        if not items:
            return []

        result: list[str] = list(items)
        pending: list[tuple[int, str]] = [
            (index, text) for index, text in enumerate(items) if text.strip()
        ]
        if not pending:
            return result

        for batch in self._make_batches(pending):
            indices = [index for index, _ in batch]
            batch_texts = [text for _, text in batch]
            try:
                translated = self._translate_chunk(
                    batch_texts, normalized_source, normalized_target
                )
            except Exception:  # noqa: BLE001 - 契约要求永不抛出，失败即保留原文
                logger.exception(
                    "unexpected error while translating %d text(s); keeping originals",
                    len(batch_texts),
                )
                continue
            for index, text in zip(indices, translated):
                result[index] = text
        return result

    def translate(
        self,
        text: str,
        source: str | None = None,
        target: str = "zh-CN",
    ) -> str:
        """单条翻译便捷方法。

        Args:
            text: 原文。
            source: 源语言；``None`` 自动检测。
            target: 目标语言。

        Returns:
            译文；失败时返回原文。
        """
        result = self.translate_batch([text], source, target)
        return result[0] if result else text

    def close(self) -> None:
        """释放传输层持有的连接资源。"""
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()

    @property
    def circuit_open(self) -> bool:
        """熔断是否正在生效（只读，供监控埋点）。"""
        return self._breaker.is_open

    @property
    def circuit_trip_count(self) -> int:
        """累计熔断次数（只读，供监控埋点）。"""
        return self._breaker.trip_count

    def warmup(self) -> str:
        """预热 TKK，把首页抓取的延迟从"首图"挪到"服务启动"。

        建议在进程启动时调用一次（失败也不抛异常，仍回到内置常量）::

            translator.warmup()

        不调用也能正常工作：第一次 ``translate_batch`` 会同步抓一次，
        若出口不通则会承担一次连接超时（connect 3s），并计入失败退避。

        Returns:
            当前生效的 TKK 字符串。
        """
        tkk = self._tkk.get()
        logger.debug("warmup done, TKK=%s", tkk)
        return tkk

    def __enter__(self) -> "GoogleWebTranslator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(prefer={self._prefer!r}, "
            f"max_chars={self._max_chars}, max_items={self._max_items})"
        )

    # ------------------------------------------------------------------ #
    # 内部：切批 / 通道调度
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_texts(texts: Any) -> list[str]:
        """把输入规整成 ``list[str]``（``None`` → 空串，其它类型 ``str()``）。"""
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
    def _web_lang(code: str | None, default: str = "") -> str:
        """web 通道语言码处理：去空白 + 保留调用方大小写。

        Google 对语言码大小写不敏感，但 ``zh-CN`` / ``zh-TW`` 这类
        "语言-地区" 组合必须保持原样；仅把旧码 ``he`` 换成 Google 仍在
        使用的 ``iw``（对齐 PHP ``convertLang()`` 的 HE→IW 映射）。
        """
        if not code or not isinstance(code, str):
            return default
        cleaned = code.strip()
        if not cleaned:
            return default
        if cleaned.lower() == "he":
            return "iw"
        return cleaned

    def _make_batches(
        self, pending: list[tuple[int, str]]
    ) -> list[list[tuple[int, str]]]:
        """按字符数与条数双维度切批。

        超长单条文本（长度 > ``max_chars_per_request``）不会被截断，
        会独占一批发送 —— 截断会改变译文语义，宁可超限。
        """
        batches: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        current_chars = 0
        for index, text in pending:
            length = len(text)
            if current and (
                current_chars + length > self._max_chars or len(current) >= self._max_items
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append((index, text))
            current_chars += length
        if current:
            batches.append(current)
        return batches

    def _translate_chunk(self, chunk: list[str], source: str, target: str) -> list[str]:
        """翻译一批文本（已切批），保证返回等长列表。"""
        if not self._breaker.allow():
            # 熔断打开：不抛异常、不发请求，直接原文占位（绝不能阻塞出图）。
            logger.error(
                "circuit_open: skipping %d text(s) (source=%s target=%s), returning originals",
                len(chunk),
                source,
                target,
            )
            # 记下原因，上层据此把"通道挂了"和"图里没字"区分开。
            if self.last_error is None:
                self.last_error = (
                    f"熔断打开（连续 403/429 达阈值，已累计 {self._breaker.trip_count} 次），"
                    f"本次未发起请求；出口 IP 可能已被 Google 限流"
                )
            return list(chunk)

        primary, secondary = ("free", "api") if self._prefer == "free" else ("api", "free")

        result = self._try_channel(primary, chunk, source, target)
        if result is not None:
            return result

        logger.warning(
            "%s channel failed for %d text(s) (source=%s target=%s), falling back to %s",
            primary,
            len(chunk),
            source,
            target,
            secondary,
        )
        result = self._try_channel(secondary, chunk, source, target)
        if result is not None:
            return result

        logger.error(
            "all channels failed for %d text(s) (source=%s target=%s); keeping originals",
            len(chunk),
            source,
            target,
        )
        if self.last_error is None:
            status = f"，api 通道最后 HTTP {self._last_api_status}" if self._last_api_status else ""
            self.last_error = (
                f"{primary}/{secondary} 两个通道均失败{status}；"
                f"429 表示出口 IP 被限流，403 表示被判定为机器人"
            )
        return list(chunk)

    def _try_channel(
        self, channel: str, chunk: list[str], source: str, target: str
    ) -> list[str] | None:
        """尝试某个通道；整体失败返回 ``None``。"""
        if channel == "api":
            return self._api_chunk(chunk, source, target)
        return self._free_chunk(chunk, source, target)

    # ------------------------------------------------------------------ #
    # 内部：api 通道（PHP api()）
    # ------------------------------------------------------------------ #

    def _api_chunk(self, chunk: list[str], source: str, target: str) -> list[str] | None:
        """``api`` 通道：失败后刷新 TKK 重试一次，仍失败返回 ``None``。

        如果刷新拿不到新 TKK（``fetch_tkk=False``，或者 Google 首页依旧没有
        ``tkk`` 变量），重试用同一个签名必然同样失败——这种重试只是白花一次
        跨境往返，直接降级到 ``free`` 通道更划算。
        """
        before = self._tkk.value
        result = self._call_api(chunk, source, target)
        if result is not None:
            return result

        logger.info("api channel failed, refreshing TKK and retrying once")
        self._tkk.invalidate()
        # 必须用 get() 而不是 value：invalidate 只是标记过期，
        # value 读到的仍是旧值，那样"是否变化"的判断永远成立。
        after = self._tkk.get()
        if after == before:
            # 只有"403 且签名没变"才值得放弃重试——那种情况重试必然同样被拒。
            # 长度不匹配、超时、5xx 都可能是偶发，重试仍有意义。
            if self._last_api_status == 403:
                logger.info("403 with unchanged TKK; skipping retry, falling back")
                return None
            logger.info("TKK unchanged after refresh; retrying anyway")
        return self._call_api(chunk, source, target)

    def _call_api(self, chunk: list[str], source: str, target: str) -> list[str] | None:
        """发一次 ``/translate_a/t`` 请求并解析。"""
        body = "&".join(f"q={quote_plus(text)}" for text in chunk)
        tk = build_tk("".join(chunk), self._tkk.get())
        query = urlencode(
            [
                ("client", "te"),
                ("format", "html"),
                ("v", "1.0"),
                ("key", ""),
                ("sl", source),
                ("tl", target),
                ("tc", "3"),
                ("ctt", "1"),
                ("tk", tk),
            ]
        )
        url = f"{API_DOMAIN}{API_PATH}?{query}"

        try:
            status, text = self._request(
                "POST",
                url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=body.encode("utf-8"),
            )
        except TranslationError as exc:
            logger.warning("api request error: %s", exc)
            self._last_api_status = None
            return None

        self._last_api_status = status
        if status != 200:
            logger.warning("api channel http %s", status)
            if status in (429, 503):
                self._bucket.penalize(self.RATE_LIMIT_PENALTY)
            return None

        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            logger.warning("api channel returned non-JSON payload: %s", exc)
            return None

        if not isinstance(payload, list):
            logger.warning("api channel returned unexpected payload type: %s", type(payload))
            return None
        if len(payload) != len(chunk):
            logger.warning(
                "api channel result length mismatch: expected %d, got %d",
                len(chunk),
                len(payload),
            )
            return None

        translated: list[str] = []
        for original, item in zip(chunk, payload):
            segment = self._pick_text(item)
            if segment is None:
                logger.warning("api channel element is not a string: %r", item)
                translated.append(original)
                continue
            # 只做实体反转义，绝不做大小写变换（见模块 docstring 差异 1）。
            decoded = html.unescape(segment)
            translated.append(decoded if decoded.strip() else original)
        return translated

    @staticmethod
    def _pick_text(item: Any) -> str | None:
        """从响应元素里取出译文字符串（兼容 ``str`` 与 ``[[text], ...]``）。"""
        if isinstance(item, str):
            return item
        if isinstance(item, list) and item and isinstance(item[0], str):
            return item[0]
        return None

    # ------------------------------------------------------------------ #
    # 内部：free 通道（PHP free()）
    # ------------------------------------------------------------------ #

    def _free_chunk(self, chunk: list[str], source: str, target: str) -> list[str] | None:
        """``free`` 通道：**整批一次请求**（对齐 PHP ``free()``）。

        逐个文本发请求是移植时留下的性能陷阱：PHP 把所有文本用 ``\\n``
        拼成一个 ``q`` 发一次，回包再按 ``sentences[].orig`` 拆回各行。
        一张 72 个文本块的图逐条发就是 72 次 HTTP —— 实测立刻吃 429，
        随后熔断打开、整批返回原文，表现成"图片还是中文"。
        """
        if not chunk:
            return []
        try:
            return self._translate_free_batch(chunk, source, target)
        except TranslationError as exc:
            logger.warning("free channel error: %s", exc)
            return None

    def _translate_free_batch(
        self, chunk: list[str], source: str, target: str
    ) -> list[str]:
        """一次 POST 翻译整批，返回等长同序列表。"""
        sentences = self._post_free("\n".join(chunk), source, target)
        return self._align_free_sentences(chunk, sentences)

    def _translate_free(self, text: str, source: str, target: str) -> str:
        """单条翻译。仍走整批路径，保证"单条也只有一次请求"。"""
        if not text.strip():
            return text
        parts = [
            s["trans"] for s in self._post_free(text, source, target) if isinstance(s.get("trans"), str)
        ]
        return "".join(parts).rstrip()

    def _post_free(self, text: str, source: str, target: str) -> list[dict[str, Any]]:
        """发一次 ``/translate_a/single``，返回 ``sentences`` 里的字典列表。"""
        last_status = 0
        for attempt in range(2):
            query = urlencode(
                [
                    ("client", "at"),
                    ("dt", "t"),
                    ("dt", "ld"),
                    ("dt", "qca"),
                    ("dt", "rm"),
                    ("dt", "bd"),
                    ("dj", "1"),
                    ("hl", "zh-CN"),
                    ("ie", "UTF-8"),
                    ("oe", "UTF-8"),
                    ("inputm", "2"),
                    ("otf", "2"),
                    ("iid", str(uuid.uuid4())),
                ]
            )
            url = f"{FREE_DOMAIN}{FREE_PATH}?{query}"
            body = urlencode({"sl": source, "tl": target, "q": text})

            status, payload_text = self._request(
                "POST",
                url,
                headers={
                    "User-Agent": FREE_USER_AGENT,
                    "Referer": FREE_DOMAIN,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                data=body.encode("utf-8"),
            )
            if status == 200:
                return self._free_sentences(payload_text)

            last_status = status
            if status == FREE_RETRY_STATUS and attempt == 0:
                logger.info("free channel 503, refreshing cookie and retrying once")
                self._refresh_free_cookie()
                continue
            break

        raise TranslationError(f"free channel http {last_status}")

    @staticmethod
    def _free_sentences(payload_text: str) -> list[dict[str, Any]]:
        """解析 ``{"sentences": [...]}``，只留字典项。"""
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError) as exc:
            raise TranslationError(f"free channel returned non-JSON payload: {exc}") from exc

        if not isinstance(payload, dict):
            raise TranslationError("free channel returned unexpected payload type")

        sentences = payload.get("sentences")
        if not isinstance(sentences, list):
            raise TranslationError("free channel response has no sentences")

        return [s for s in sentences if isinstance(s, dict)]

    @classmethod
    def _align_free_sentences(
        cls, texts: list[str], sentences: list[dict[str, Any]]
    ) -> list[str]:
        """把回包的句子对齐回入参文本（PHP ``getTransTexts()`` 的移植）。

        为什么不能按下标硬取：Google 会把 ``a\\nb`` 拆成几句是不确定的 ——
        可能 1:1，也可能两行并成一句或一行拆成两句。对齐不上的位置保留
        原文，宁可少翻一段，也不能把 A 段的译文画到 B 段上（那比不翻更糟）。
        """
        if len(texts) == 1:
            parts = [s["trans"] for s in sentences if isinstance(s.get("trans"), str)]
            return ["".join(parts).rstrip()]

        # 快速路径：句数与行数相等，说明 Google 按行返回，直接一一对应。
        # 注意每句译文尾部普遍带 "\n"（实测 40/40 都有），不去掉会把换行
        # 带进回填，行高与折行全乱——PHP 那边是 ``rtrim($trans, "\n ")``。
        if len(sentences) == len(texts):
            out: list[str] = []
            for i, sentence in enumerate(sentences):
                trans = sentence.get("trans")
                cleaned = trans.rstrip("\n ") if isinstance(trans, str) else ""
                out.append(cleaned if cleaned.strip() else texts[i])
            return out

        # 慢速路径：按 orig 做前缀对齐（与 PHP 同款归一化后比较）
        result: list[str | None] = [None] * len(texts)
        index = 0
        for sentence in sentences:
            if index >= len(texts):
                break
            orig = sentence.get("orig")
            trans = sentence.get("trans")
            if not isinstance(orig, str) or not isinstance(trans, str):
                continue
            expect = _normalize_for_match(texts[index])
            got = _normalize_for_match(orig)
            if not got or not expect:
                continue
            if got in expect:
                segment = trans.rstrip("\n ")
                result[index] = f"{result[index]} {segment}" if result[index] else segment
                # 期望的那一行以这句原文结尾 → 这行翻完了，换下一行
                if expect.endswith(got):
                    index += 1
        return [r if r else texts[i] for i, r in enumerate(result)]

    def _refresh_free_cookie(self) -> None:
        """``free`` 通道 503 后的"刷新 cookie"动作。

        传输层契约只返回 ``(status, text)``，拿不到 ``Set-Cookie``，因此这里
        无法真正维护 cookie 罐。实际做法是：给令牌桶加一点冷却，并在下一次
        尝试时换用新的 ``iid``（见 :meth:`_translate_free`）。若需要真实的
        cookie 复用，请注入一个自行维护 cookie 的 transport。
        """
        self._bucket.penalize(0.5)

    # ------------------------------------------------------------------ #
    # 内部：HTTP
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> tuple[int, str]:
        """带限流与重试的请求。

        Args:
            method: HTTP 方法。
            url: 完整 URL。
            headers: 请求头。
            data: 请求体。

        Returns:
            ``(status, text)``；可重试状态码在最后一次尝试后原样返回。

        Raises:
            TranslationError: 传输层异常且重试用尽。
        """
        last_error: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            self._bucket.acquire()
            try:
                status, text = self._transport(
                    method, url, None, headers, data, self._timeout
                )
            except Exception as exc:  # noqa: BLE001 - 传输层异常统一降级
                last_error = exc
                self._breaker.record_exception()
                logger.warning(
                    "transport error (attempt %d/%d): %s", attempt + 1, self.MAX_ATTEMPTS, exc
                )
                if attempt + 1 >= self.MAX_ATTEMPTS:
                    break
                self._sleep_backoff(attempt)
                continue

            self._breaker.record_status(status)
            if status in RETRYABLE_STATUS and attempt + 1 < self.MAX_ATTEMPTS:
                delay = self._backoff_delay(attempt)
                if status in (429, 503):
                    # 被限流/过载时，除了本次退避，还要让令牌桶整体冷却。
                    self._bucket.penalize(self.RATE_LIMIT_PENALTY)
                    delay += self.RATE_LIMIT_PENALTY
                logger.warning(
                    "http %s (attempt %d/%d), retrying after %.2fs",
                    status,
                    attempt + 1,
                    self.MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                continue
            return status, text

        raise TranslationError(f"request failed after {self.MAX_ATTEMPTS} attempts: {last_error}")

    def _backoff_delay(self, attempt: int) -> float:
        """计算第 ``attempt`` 次退避的秒数（**不睡眠**，便于单测断言）。

        序列（``attempt`` 从 0 起）：``0.5s`` / ``1.0s`` / ``2.0s`` / ……，
        每次叠加 ``0~BACKOFF_JITTER`` 的随机抖动，单次不超过 ``BACKOFF_CAP``。
        """
        delay = min(self.BACKOFF_CAP, self.BACKOFF_BASE * (2**attempt))
        return delay + random.uniform(0.0, self.BACKOFF_JITTER)

    def _sleep_backoff(self, attempt: int) -> float:
        """退避并睡眠，返回实际休眠秒数。"""
        delay = self._backoff_delay(attempt)
        time.sleep(delay)
        return delay
