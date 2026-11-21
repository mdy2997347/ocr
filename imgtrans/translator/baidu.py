"""百度翻译开放平台「通用文本翻译」通道。

接口
----
``POST https://fanyi-api.baidu.com/api/trans/vip/translate``，``form-urlencoded``：

============  ============================================================
``q``         待翻译文本，UTF-8。多段用 ``\\n`` 连接，回包按行一一返回。
``from``      源语言，``auto`` 为自动检测。
``to``        目标语言。
``appid``     开放平台「我的服务」里的 APP ID（纯数字）。
``salt``      随机数。
``sign``      ``MD5(appid + q + salt + 密钥)``，**对原文求 MD5**，
              不是对 URL 编码后的串——这点搞错只会得到 ``54001 签名错误``。
============  ============================================================

响应::

    {"from": "zh", "to": "en",
     "trans_result": [{"src": "第一行", "dst": "First line"}, ...]}

失败响应::

    {"error_code": "54001", "error_msg": "Invalid Sign"}

为什么这个通道对本项目特别合适
------------------------------
百度**严格按 ``q`` 里的 ``\\n`` 分行返回**，且每项带 ``src``（原文）。
一张图几十个文本块拼成一次请求，回来按下标就能对齐——比 Google 免费通道
（``sentences`` 会被任意合并/拆分、得靠归一化前缀匹配）稳得多，
每次翻译的网络往返也从"几十次"降到"一两次"。

配额与限速
----------
标准版：**1 QPS**、单次 ``q`` ≤ 6000 字节。所以 :data:`MAX_BYTES_PER_REQUEST`
取 5500 留余量，``qps`` 默认 ``1.0``。升级到高级版后可把 ``qps`` 调到 10。

安全
----
密钥只存成构造参数，不进日志、不进 ``repr()``、不进 ``/healthz``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode

from ._http import Transport, _HttpXTransport, _TokenBucket
from .base import TranslationError
from .lang_map import (
    BAIDU_STANDARD_CODES,
    BAIDU_STANDARD_LANGS,
    normalize_lang,
    to_baidu_lang,
)

__all__ = [
    "API_DOMAIN",
    "API_PATH",
    "ERROR_HINTS",
    "FATAL_ERRORS",
    "MAX_BYTES_PER_REQUEST",
    "RATE_LIMIT_ERROR",
    "RETRYABLE_ERRORS",
    "BaiduTranslator",
    "make_sign",
]

logger = logging.getLogger(__name__)

#: 接口域名。
API_DOMAIN: str = "https://fanyi-api.baidu.com"
#: 通用文本翻译路径。
API_PATH: str = "/api/trans/vip/translate"
#: 完整 URL。
TRANSLATE_URL: str = f"{API_DOMAIN}{API_PATH}"

#: 单次 ``q`` 的**字节**上限。官方标准版是 6000 字节，留 500 余量给
#: ``\n`` 连接符与多字节字符的估算误差（中文 3 字节/字，切批按真实编码算，
#: 余量只是防"刚好卡线"）。
MAX_BYTES_PER_REQUEST: int = 5500
#: 单次请求的文本段数上限。百度的瓶颈是字节数而不是条数，这个值只是
#: 防止"几百个短词"把 URL/body 撑得过长。
MAX_ITEMS_PER_REQUEST: int = 50

#: 可重试的错误码：超时、百度侧系统错误、频率受限、长 query 请求过于频繁。
#: 这类错误重试一次往往就好了，值得多花一次往返（``54003`` / ``54005``
#: 是"太快了"，退避之后大概率能过）。
RETRYABLE_ERRORS: frozenset[str] = frozenset({"52001", "52002", "54003", "54005"})

#: **重试无意义**的错误码：凭证、签名、余额、IP 白名单、服务未开通。
#: 继续请求只会白等，而且要按"通道整体不可用"上报，所以触发熔断。
FATAL_ERRORS: frozenset[str] = frozenset(
    {"52003", "54001", "54004", "58000", "58002", "90107"}
)

#: 频率受限。不熔断（等一会儿就能好），但要给令牌桶加冷却。
RATE_LIMIT_ERROR: str = "54003"

#: 错误码 → 中文解释。百度只回 ``Invalid Sign`` 这种，运维看了也不知道
#: 该去改哪个配置，这里补上"下一步该干什么"。
ERROR_HINTS: dict[str, str] = {
    "52000": "成功",
    "52001": "请求超时",
    "52002": "百度侧系统错误",
    "52003": "未授权用户：APP ID 不对，或该应用没有开通通用文本翻译",
    "54000": "必填参数为空",
    "54001": (
        "签名错误 / 未授权：APP ID 与密钥不匹配（密钥不是 APP ID，注意别抄漏）；"
        "该 APP ID 没开通通用文本翻译、或没开通这个语言方向时，百度也回这个码"
    ),
    "54003": "访问频率受限：标准版只有 1 QPS，把 IMGTRANS_BAIDU_QPS 调低或升级高级版",
    "54004": "账户余额不足：去开放平台充值",
    "54005": "长 query 请求过于频繁：稍后重试或降低批大小",
    "58000": "客户端 IP 非法：开放平台里设了 IP 白名单，把这台机器的出口 IP 加进去",
    "58001": (
        "译文语言方向不支持：个人标准版/高级版只认 28 个常见语种，"
        "非常用语种（tr/id/hi/ka/uk/he/hr/sl/ms/no/bn）要企业尊享版；"
        "若已开通尊享版，检查 IMGTRANS_BAIDU_PREMIUM 是否为 1"
    ),
    "58002": "服务当前已关闭：控制台里重新开启通用文本翻译",
    "90107": "认证未通过或未生效：确认服务已开通且审核通过",
}

#: 需要退避重试的 HTTP 状态码（百度正常只回 200，这几类属于链路问题）。
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def make_sign(appid: str, q: str, salt: str, secret: str) -> str:
    """计算百度要求的 ``sign``。

    ``MD5(appid + q + salt + 密钥)``，四段**直接拼接**，中间没有分隔符，
    且 ``q`` 用**原文**（含其中的 ``\\n``），不是 URL 编码后的形式。
    编码统一 UTF-8 后取小写十六进制。

    Args:
        appid: 应用 ID。
        q: 待翻译原文（与请求体里的 ``q`` 完全一致）。
        salt: 随机数（字符串形式）。
        secret: 开放平台密钥。

    Returns:
        32 位小写十六进制 MD5。
    """
    raw = f"{appid}{q}{salt}{secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class _BaiduError(TranslationError):
    """百度返回的业务错误。

    Attributes:
        fatal: 是否属于"重试也没用"的凭证/权限类错误。
        code: 百度错误码（HTTP 层失败时为 ``""``）。
    """

    def __init__(self, message: str, *, fatal: bool = False, code: str = "") -> None:
        super().__init__(message)
        self.fatal = fatal
        self.code = code


class _FatalCircuit:
    """针对**致命错误**的熔断器（线程安全）。

    为什么不用 ``google_web._CircuitBreaker``：那一个是按 HTTP 状态码
    （403/429）设计的，而百度的失败是 200 响应里的 ``error_code``。
    硬塞进去会写出 ``if status == "54001"`` 这种四不像，不如各写各的。

    语义：

    * **closed**：连续 ``threshold`` 次致命错误 → **open**；
    * **open**：不 ``allow()``，调用方直接返回原文，不再发请求；
    * **half_open**：``open_seconds`` 到期后放行 **1 个**探测请求，
      成功则闭合，失败则重新打开并重新计时。

    非致命失败（网络抖动、52001 超时）**不清零**计数也不累加：凭证错了
    不会因为中间夹了一次超时就自愈。
    """

    def __init__(
        self,
        *,
        threshold: int = 3,
        open_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """初始化。

        Args:
            threshold: 连续多少次致命错误后熔断。
            open_seconds: 熔断保持时长（秒）。凭证类错误不会自愈，
                所以比 web 通道的 60s 长得多。
            clock: 单调时钟注入点，默认 ``time.monotonic``。
        """
        self._threshold = max(1, int(threshold))
        self._open_seconds = max(0.0, float(open_seconds))
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._state = "closed"
        self._open_until = 0.0
        self._probe_in_flight = False
        self._consecutive = 0
        self._trip_count = 0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """熔断是否正在生效。"""
        with self._lock:
            return self._state == "open"

    @property
    def trip_count(self) -> int:
        """累计熔断次数。"""
        with self._lock:
            return self._trip_count

    def allow(self) -> bool:
        """本次请求是否放行。"""
        with self._lock:
            now = self._clock()
            if self._state == "open":
                if now < self._open_until:
                    return False
                self._state = "half_open"
                self._probe_in_flight = True
                logger.warning("百度翻译熔断转半开：放行 1 个探测请求")
                return True
            if self._state == "half_open":
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return True

    def record_success(self) -> None:
        """记录一次成功（半开探测成功即闭合）。"""
        with self._lock:
            self._consecutive = 0
            if self._state == "half_open":
                self._probe_in_flight = False
                self._state = "closed"
                logger.warning("百度翻译熔断已闭合：探测请求成功")

    def record_failure(self, *, fatal: bool) -> None:
        """记录一次失败。

        Args:
            fatal: 是否为凭证/权限类致命错误。
        """
        with self._lock:
            if self._state == "half_open":
                # 探测没成功 → 立刻重开，别让后续请求在"半开"里排队等
                self._probe_in_flight = False
                self._trip()
                return
            if self._state == "open":
                return
            if not fatal:
                return
            self._consecutive += 1
            if self._consecutive >= self._threshold:
                self._trip()

    def reset(self) -> None:
        """人工复位（运维手动解除熔断用）。"""
        with self._lock:
            self._state = "closed"
            self._open_until = 0.0
            self._probe_in_flight = False
            self._consecutive = 0
            logger.warning("百度翻译熔断已人工复位")

    def _trip(self) -> None:
        """打开熔断（调用方需已持锁）。"""
        self._state = "open"
        self._open_until = self._clock() + self._open_seconds
        self._trip_count += 1
        logger.error(
            "circuit_open: 百度翻译连续 %d 次致命错误，%.0f 秒内不再发起请求"
            "（多半是 APP ID / 密钥 / 余额 / IP 白名单问题，改配置后重启服务）",
            self._consecutive,
            self._open_seconds,
        )


class BaiduTranslator:
    """百度通用文本翻译的批量翻译器。

    典型用法::

        tr = BaiduTranslator(appid="2023xxxxxx", secret="8ugS_...")
        out = tr.translate_batch(["你好", "世界"], None, "en")

    Attributes:
        MAX_ATTEMPTS: 单个请求的最大尝试次数（含首次）。
        BACKOFF_BASE: 指数退避基数（秒）。
        BACKOFF_CAP: 单次退避上限（秒）。
        BACKOFF_JITTER: 退避随机抖动上限（秒）。
        RATE_LIMIT_PENALTY: 54003 / 429 后令牌桶的冷却秒数。
        FATAL_THRESHOLD: 连续多少次致命错误后熔断。
        FATAL_OPEN_SECONDS: 熔断保持时长（秒）。
    """

    MAX_ATTEMPTS: int = 3
    BACKOFF_BASE: float = 0.5
    BACKOFF_CAP: float = 5.0
    BACKOFF_JITTER: float = 0.3
    RATE_LIMIT_PENALTY: float = 1.5
    FATAL_THRESHOLD: int = 3
    FATAL_OPEN_SECONDS: float = 300.0

    def __init__(
        self,
        *,
        appid: str,
        secret: str,
        timeout: float = 10.0,
        qps: float = 1.0,
        max_bytes_per_request: int = MAX_BYTES_PER_REQUEST,
        max_items_per_request: int = MAX_ITEMS_PER_REQUEST,
        proxy: str | None = None,
        transport: Transport | None = None,
        salt_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        premium: bool = False,
    ) -> None:
        """初始化（不会立即建立网络连接）。

        Args:
            appid: 开放平台 APP ID（纯数字，不是密钥）。
            secret: 开放平台密钥。
            timeout: 单次 HTTP 请求超时秒数（连接阶段固定 3s）。
            qps: 每秒请求数上限。标准版 1 QPS，高级版可设 10；``<= 0`` 不限流。
            max_bytes_per_request: 单次请求 ``q`` 的字节上限。
            max_items_per_request: 单次请求的文本段数上限。
            proxy: 代理地址；百度国内可直连，一般不需要。
            transport: 自定义传输层，便于测试注入。
            salt_factory: ``salt`` 生成器，默认随机数（单测注入固定值）。
            clock: 熔断器时钟注入点。
            premium: 账号是否为**企业尊享版**。百度的语言能力按档位切：
                标准版/高级版只认 28 个常见语种，其余非常用语种（土耳其语、
                印尼语、印地语、格鲁吉亚语……共 11 个在本项目白名单内）一律
                回 ``58001``。默认 ``False``，即按标准版口径**前置拒绝**，
                让调用方看到"这个语言方向要尊享版"；升级后设
                ``IMGTRANS_BAIDU_PREMIUM=1`` 才放开。

        Raises:
            ValueError: 缺 APP ID / 密钥，或参数非法。**启动期就报**，
                好过跑完 OCR 才发现翻译必然失败。
        """
        if not appid or not str(appid).strip():
            raise ValueError(
                "百度翻译缺少 APP ID：设置 IMGTRANS_BAIDU_APPID"
                "（开放平台「管理控制台 → 我的服务」里的纯数字 ID，"
                "不是密钥）"
            )
        if not secret or not str(secret).strip():
            raise ValueError("百度翻译缺少密钥：设置 IMGTRANS_BAIDU_SECRET")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if max_bytes_per_request < 1:
            raise ValueError("max_bytes_per_request must be >= 1")
        if max_items_per_request < 1:
            raise ValueError("max_items_per_request must be >= 1")

        self._appid = str(appid).strip()
        # 密钥单独存，绝不进日志 / repr / 任何报错信息
        self._secret = str(secret).strip()
        self._timeout = float(timeout)
        self._max_bytes = int(max_bytes_per_request)
        self._max_items = int(max_items_per_request)
        self._salt_factory: Callable[[], str] = (
            salt_factory if salt_factory is not None else self._default_salt
        )
        self._transport: Transport = (
            transport if transport is not None else _HttpXTransport(proxy=proxy)
        )
        self._bucket = _TokenBucket(qps)
        self._circuit = _FatalCircuit(
            threshold=self.FATAL_THRESHOLD,
            open_seconds=self.FATAL_OPEN_SECONDS,
            clock=clock,
        )
        #: 通道标识，供 ``/healthz`` 与启动日志展示。
        self.kind: str = "baidu"
        #: 账号档位：``False`` = 标准版/高级版（28 个常见语种），``True`` = 企业尊享版。
        self.premium: bool = bool(premium)
        #: 最近一次 ``translate_batch`` 的整体失败原因；成功后清空。
        #: ``None`` = 这一轮没出故障（注意：图里没有可译文字时也是 ``None``）。
        #: 上层据此区分"正常返回原图"和"通道挂了却返回原图"。
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #

    def unsupported_target_reason(self, target: str | None) -> str | None:
        """当前账号档位能否翻这个目标语言；能翻返回 ``None``，否则返回原因。

        pipeline 在**解码图片之前**调它，参数不对就立刻 400，不浪费一次
        OCR（15 秒）和一次翻译。

        为什么不能等百度回错误码：非常用语种拿到的是 ``58001 译文语言方向
        不支持``，"方向不支持"字面上像通道故障或码写错，而真实原因只有一个
        ——账号档位不够。官方原文：*个人标准版和高级版支持 28 个常见语种，
        企业尊享版支持全部语种*。本方法把这个判断搬到本地，报错就能说人话。

        Args:
            target: 目标语言码（Google 风格或百度风格均可）。

        Returns:
            ``None`` 表示可用；否则是一句可直接展示给调用方的原因。
        """
        if not target:
            return None
        code = to_baidu_lang(target)
        if not code:
            return f"百度没有 {target!r} 这个语言码"
        if self.premium or code in BAIDU_STANDARD_CODES:
            return None
        canonical = normalize_lang(target) or target
        return (
            f"百度标准版/高级版不支持 {canonical}：只有 28 个常见语种可用，"
            "该语言属非常用语种，需开通企业尊享版；"
            "可改用 Google 通道，或换成 "
            + "/".join(sorted(BAIDU_STANDARD_LANGS))
        )

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
            target: 目标语言（项目规范码，如 ``en`` / ``ja``）。

        Returns:
            与 ``texts`` 等长、同序的译文列表。

        Raises:
            ValueError: ``target`` 为空、不在百度的语言表里，或当前账号档位
                不支持该语言方向（非常用语种要企业尊享版）。
        """
        reason = self.unsupported_target_reason(target)
        if reason:
            raise ValueError(f"baidu: {reason}")
        target_code = to_baidu_lang(target)
        if not target_code:
            raise ValueError(f"target language is not supported by baidu: {target!r}")
        source_code = to_baidu_lang(source) or "auto"

        # 每轮开头清空：上一次的失败原因不能留到这一次，
        # 否则一张正常的图会被上一轮的 54003 误判成故障（反之亦然）。
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
            translated = self._translate_chunk(batch_texts, source_code, target_code)
            for index, text in zip(indices, translated):
                result[index] = text
        return result

    def translate(
        self,
        text: str,
        source: str | None = None,
        target: str = "en",
    ) -> str:
        """单条翻译便捷方法（失败时返回原文）。"""
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
        return self._circuit.is_open

    @property
    def circuit_trip_count(self) -> int:
        """累计熔断次数（只读，供监控埋点）。"""
        return self._circuit.trip_count

    def __enter__(self) -> "BaiduTranslator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        # 只露 APP ID 后 4 位，密钥一个字都不露
        tail = self._appid[-4:] if len(self._appid) >= 4 else self._appid
        return (
            f"{self.__class__.__name__}(appid=***{tail}, "
            f"max_bytes={self._max_bytes}, max_items={self._max_items})"
        )

    # ------------------------------------------------------------------ #
    # 内部：切批
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
    def _default_salt() -> str:
        """默认 ``salt``：随机整数（对齐百度官方 PHP demo 的 ``mt_rand``）。"""
        return str(random.randint(32768, 65536))

    def _make_batches(
        self, pending: list[tuple[int, str]]
    ) -> list[list[tuple[int, str]]]:
        """按**字节数**与条数双维度切批。

        百度限制的是 ``q`` 的 UTF-8 **字节数**（6000），不是字符数：
        2000 个汉字的字符数是 2000 但字节数是 6000，按字符切会直接超限。
        所以这里必须 ``len(text.encode("utf-8"))``，另外每段之间还要算
        ``\\n`` 那 1 个字节。

        超长单条（> ``max_bytes_per_request``）不截断，独占一批——截断会
        改变译文语义，宁可让百度报错也不悄悄吃掉半句话。
        """
        batches: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        current_bytes = 0
        for index, text in pending:
            size = len(text.encode("utf-8"))
            # 段间连接符 "\n" 也占 1 字节
            extra = 1 if current else 0
            if current and (
                current_bytes + extra + size > self._max_bytes
                or len(current) >= self._max_items
            ):
                batches.append(current)
                current = []
                current_bytes = 0
                extra = 0
            if size > self._max_bytes:
                logger.warning(
                    "单段文本 %d 字节，超过单次请求上限 %d，独占一批发送（可能被百度拒绝）",
                    size,
                    self._max_bytes,
                )
            current.append((index, text))
            current_bytes += extra + size
        if current:
            batches.append(current)
        return batches

    # ------------------------------------------------------------------ #
    # 内部：请求与解析
    # ------------------------------------------------------------------ #

    def _translate_chunk(self, chunk: list[str], source: str, target: str) -> list[str]:
        """翻译一批文本，保证返回等长列表。"""
        if not self._circuit.allow():
            # 熔断打开：不发请求、不抛异常，直接原文占位。
            # 但必须记 last_error，否则上层会当成"图里没字"返回原图，
            # 表现就是"接口 200、图片还是中文"——这个坑在 Google 通道上踩过。
            logger.error(
                "circuit_open: 跳过 %d 段文本（source=%s target=%s），返回原文",
                len(chunk),
                source,
                target,
            )
            if self.last_error is None:
                self.last_error = (
                    f"百度翻译熔断中（连续 {self.FATAL_THRESHOLD} 次致命错误，"
                    f"已累计 {self._circuit.trip_count} 次），本轮未发起请求；"
                    f"上一次的原因见服务日志里的 circuit_open 那条"
                )
            return list(chunk)

        try:
            translated = self._post_batch(chunk, source, target)
        except _BaiduError as exc:
            self._circuit.record_failure(fatal=exc.fatal)
            self.last_error = f"百度翻译失败：{exc}"
            logger.error(
                "百度翻译失败（%d 段，source=%s target=%s）：%s",
                len(chunk),
                source,
                target,
                exc,
            )
            return list(chunk)
        except Exception:  # noqa: BLE001 - 契约要求永不抛出，失败即保留原文
            logger.exception("百度翻译出现未预期异常，保留原文（%d 段）", len(chunk))
            self.last_error = "百度翻译出现未预期异常，详见服务日志"
            return list(chunk)

        self._circuit.record_success()
        return translated

    def _post_batch(self, chunk: list[str], source: str, target: str) -> list[str]:
        """发一次（必要时带重试）请求，返回等长同序译文。

        Raises:
            _BaiduError: 重试用尽或遇到致命错误。
        """
        joined = "\n".join(chunk)
        last_error = ""

        for attempt in range(self.MAX_ATTEMPTS):
            self._bucket.acquire()
            salt = self._salt_factory()
            sign = make_sign(self._appid, joined, salt, self._secret)
            body = urlencode(
                {
                    "q": joined,
                    "from": source,
                    "to": target,
                    "appid": self._appid,
                    "salt": salt,
                    "sign": sign,
                }
            )

            try:
                status, text = self._transport(
                    "POST",
                    TRANSLATE_URL,
                    None,
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    body.encode("utf-8"),
                    self._timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 传输层异常统一降级
                last_error = f"传输层异常：{exc}"
                logger.warning(
                    "百度翻译传输层异常（第 %d/%d 次）：%s",
                    attempt + 1,
                    self.MAX_ATTEMPTS,
                    exc,
                )
                if attempt + 1 >= self.MAX_ATTEMPTS:
                    break
                time.sleep(self._backoff_delay(attempt))
                continue

            if status != 200:
                last_error = f"HTTP {status}"
                if status in RETRYABLE_STATUS and attempt + 1 < self.MAX_ATTEMPTS:
                    delay = self._backoff_delay(attempt)
                    if status == 429:
                        self._bucket.penalize(self.RATE_LIMIT_PENALTY)
                        delay += self.RATE_LIMIT_PENALTY
                    logger.warning(
                        "百度翻译 HTTP %s（第 %d/%d 次），%.2fs 后重试",
                        status,
                        attempt + 1,
                        self.MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise _BaiduError(last_error)

            try:
                payload = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise _BaiduError(f"返回非 JSON 内容：{exc}") from exc
            if not isinstance(payload, dict):
                raise _BaiduError(f"返回结构异常：{type(payload).__name__}")

            error_code = payload.get("error_code")
            if error_code is not None:
                code = str(error_code)
                hint = ERROR_HINTS.get(code, str(payload.get("error_msg", "")))
                last_error = f"错误码 {code}（{hint}）"
                if code == RATE_LIMIT_ERROR:
                    self._bucket.penalize(self.RATE_LIMIT_PENALTY)
                if code in RETRYABLE_ERRORS and attempt + 1 < self.MAX_ATTEMPTS:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "百度翻译返回 %s（第 %d/%d 次），%.2fs 后重试",
                        code,
                        attempt + 1,
                        self.MAX_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise _BaiduError(last_error, fatal=code in FATAL_ERRORS, code=code)

            rows = self._parse_rows(payload)
            if rows is None:
                raise _BaiduError("返回里没有 trans_result")
            return self._align(chunk, rows)

        raise _BaiduError(last_error or "未知错误")

    @staticmethod
    def _parse_rows(payload: dict[str, Any]) -> list[tuple[str, str]] | None:
        """把 ``trans_result`` 解析成 ``[(src, dst), ...]``。

        Returns:
            解析结果；结构不对返回 ``None``。
        """
        raw = payload.get("trans_result")
        if not isinstance(raw, list):
            return None
        rows: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            src = item.get("src")
            dst = item.get("dst")
            rows.append(
                (
                    src if isinstance(src, str) else "",
                    dst if isinstance(dst, str) else "",
                )
            )
        return rows

    @classmethod
    def _align(cls, texts: list[str], rows: list[tuple[str, str]]) -> list[str]:
        """把按行返回的 ``trans_result`` 对齐回各段文本。

        百度按 ``q`` 里的 ``\\n`` 分行返回，多数情况下 ``len(rows) == len(texts)``
        （OCR 分段后的块一般不含内嵌换行），直接 1:1。

        段数对不上，说明某些块的文本内部带了 ``\\n``（多行段落），
        百度把它拆成了多行。这时把 ``texts`` 按行展开再对齐，最后按段拼回。

        两种情形都对不上（百度丢了行 / 合并了行），**整体放弃并保留原文**：
        宁可这段不翻，也不能把 A 段的译文画到 B 段上——那比不译更糟。
        """
        if not rows:
            return list(texts)

        if len(rows) == len(texts):
            out: list[str] = []
            for original, (_src, dst) in zip(texts, rows):
                cleaned = dst.strip()
                out.append(dst if cleaned else original)
            return out

        expanded: list[tuple[int, str]] = []
        for index, text in enumerate(texts):
            for line in text.split("\n"):
                expanded.append((index, line))

        if len(rows) != len(expanded):
            logger.warning(
                "百度翻译行数对不上（段 %d / 行 %d / 回包 %d 行），本批保留原文",
                len(texts),
                len(expanded),
                len(rows),
            )
            return list(texts)

        merged: dict[int, list[str]] = {}
        for (index, original), (_src, dst) in zip(expanded, rows):
            merged.setdefault(index, []).append(dst if dst.strip() else original)
        return [
            "\n".join(merged[index]) if index in merged else texts[index]
            for index in range(len(texts))
        ]

    def _backoff_delay(self, attempt: int) -> float:
        """计算第 ``attempt`` 次退避的秒数（不睡眠，便于单测断言）。"""
        delay = min(self.BACKOFF_CAP, self.BACKOFF_BASE * (2**attempt))
        return delay + random.uniform(0.0, self.BACKOFF_JITTER)
