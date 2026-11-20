"""Google ``tk`` 令牌算法（PHP ``Google::TL()`` 的 Python 移植）。

算法来源: ``google.php`` 中的 ``TL()`` / ``RL()`` / ``shr32()`` / ``charCodeAt()``，
其数学语义与 Google 前端 JS 的 ``TKK`` 令牌算法完全一致，因此可用
``tools/tk_reference.js``（官方 JS 版实现）做交叉验证。

关于非 BMP 字符（emoji 等）：**有意与 PHP 不一致，禁止"修回去"**
------------------------------------------------------------------
PHP 用 ``mb_convert_encoding($char, "UTF-32BE", "UTF-8")`` 取码点，对
astral 平面字符（U+10000 以上）会拿到 ``> 0xFFFF`` 的码点，于是
``55296 == ($g & 64512)`` 这个代理对分支**永远不成立（死代码）**，
emoji 落入"三字节"分支，生成 **非法** 的 UTF-8 序列（如 😀 会产出
``[0xFF, 0x98, 0x80]``）。JS 参考实现走代理对分支，生成标准四字节
（``F0 9F 98 80``）。

本移植与 **JS 参考实现 / Google 服务端** 对齐，即 astral 字符编码为标准
UTF-8 四字节。理由：判定方是 Google 服务端，只有与服务端一致才有意义；
在"UTF-16 代理对 vs UTF-32 码点"这件事上 **PHP 才是错的那一方**（含 emoji
的文本在 PHP 版里发出的 tk 是错的）。

**因此：不要为了"逐位复刻 PHP"把这里改回三字节分支。** 一旦改回，
``tests/test_google_tk.py`` 的 200 条 golden（含 😀/🇨🇳/ZWJ 序列）会立刻失败。
详见 ``_utf8_bytes()``。

32 位运算
---------
所有位运算统一在无符号 32 位空间 ``[0, 0xFFFFFFFF)`` 内进行，
与 JS ``ToInt32`` / ``ToUint32`` 逐位等价（见 ``_rl()`` 注释）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

__all__ = [
    "DEFAULT_TKK",
    "FAILURE_BACKOFF_STEPS",
    "build_tk",
    "dec_tkk",
    "fetch_tkk",
    "TKKSnapshot",
    "TKK_PATTERN",
]

logger = logging.getLogger(__name__)

#: 内置 TKK 常量，等价于 PHP ``TKK()`` 的 ``406398 . "." . (561666268 + 1526272306)``。
DEFAULT_TKK: str = "406398.2087938574"

#: 首页里 ``tkk:'406398.2087938574'`` 形式；同时兼容 ``tkk="..."`` / ``tkk=...``（无引号）。
TKK_PATTERN: re.Pattern[str] = re.compile(
    r"""tkk\s*[:=]\s*['"]?(\d+)\.(\d+)""",
    re.IGNORECASE,
)

_MASK32: int = 0xFFFFFFFF

#: 抓取失败后的再试间隔（秒），按连续失败次数递增，用尽后保持最后一档。
#: 出口不通时这是关键：不能让每个翻译请求都白等一次 connect 超时。
FAILURE_BACKOFF_STEPS: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0, 86400.0)

#: Google 翻译首页是否仍带 ``tkk`` 变量。
#:
#: 2026-11-19 实测：首页（``cfb2h=boq_translate-webserver_20261117.04_p0``，
#: 3.1 MB）已 **完全不含** ``tkk`` 变量，``TKK_PATTERN`` 永远匹配不到。
#: 因此 :data:`DEFAULT_TKK` 目前是唯一可用来源，动态抓取逻辑保留仅为兼容
#: 未来 Google 恢复该变量。判据与处置见 :func:`fetch_tkk` 的日志分支。


def _shr32(value: int, bits: int) -> int:
    """无符号 32 位右移，等价于 PHP ``shr32()`` / JS ``>>>``。

    Args:
        value: 任意整数（内部按 ToUint32 截取低 32 位）。
        bits: 右移位数；``<= 0`` 原样返回，``>= 32`` 返回 0。

    Returns:
        右移结果，范围 ``[0, 0xFFFFFFFF]``。
    """
    if bits <= 0:
        return value & _MASK32
    if bits >= 32:
        return 0
    return (value & _MASK32) >> bits


def _rl(a: int, b: str) -> int:
    """PHP ``RL()`` / JS ``RL()``：按 3 字符一组的指令串变换整数。

    指令串格式为 ``[符号][方向][位移量]`` 三元组：

    * 第 1 个字符 ``+`` → 做加法（再掩 32 位），``^`` → 做异或；
    * 第 2 个字符 ``+`` → 右移（无符号），``-`` → 左移；
    * 第 3 个字符 ``a``-``f`` → 位移量 ``ord(c) - 87``（即 10..15），
      数字字符 → 位移量 ``int(c)``。

    所有中间结果都保持无符号 32 位，因此与 JS 的 ``ToInt32`` 结果
    在 **位模式** 上完全一致（区别仅在有符号/无符号解释，而最终输出
    只取位模式）。

    Args:
        a: 当前累加值。
        b: 指令串，例如 ``"+-a^+6"``。

    Returns:
        变换后的无符号 32 位整数。
    """
    length = len(b)
    c = 0
    while c < length - 2:
        shift_char = b[c + 2]
        distance = ord(shift_char) - 87 if "a" <= shift_char else int(shift_char)
        if b[c + 1] == "+":
            operand = _shr32(a, distance)
        else:
            # JS 的 `a << d` 会 ToInt32 截断到 32 位；Python 的 << 不截断，
            # 故显式掩 32 位，保证位模式一致。
            operand = (a << distance) & _MASK32
        if b[c] == "+":
            a = (a + operand) & _MASK32
        else:
            a = (a ^ operand) & _MASK32
        c += 3
    return a


def dec_tkk(tkk: str) -> tuple[int, int]:
    """把 ``"406398.2087938574"`` 拆成两个整数。

    Args:
        tkk: 形如 ``"<int>.<int>"`` 的 TKK 字符串。

    Returns:
        ``(tkk0, tkk1)``。

    Raises:
        ValueError: 格式非法。
    """
    parts = str(tkk).strip().split(".")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"invalid TKK string: {tkk!r}")
    return int(parts[0]), int(parts[1])


def _utf8_bytes(text: str) -> list[int]:
    """把字符串编码成 UTF-8 字节列表（PHP ``TL()`` 里的 ``$d`` 数组）。

    与 JS 参考实现 ``charCodeAt`` 分支一一对应：

    * ``< 128``：单字节；
    * ``< 2048``：双字节；
    * ``>= 0x10000``（astral，JS 侧表现为代理对）：四字节；
    * 其余（含 BMP 私有区与孤立代理）：三字节。

    Args:
        text: 任意 Unicode 字符串。

    Returns:
        UTF-8 字节值列表，每个元素 ``0..255``。
    """
    data: list[int] = []
    append = data.append
    for char in text:
        code = ord(char)
        if code < 128:
            append(code)
        elif code < 2048:
            append(code >> 6 | 192)
            append(code & 63 | 128)
        elif code >= 0x10000:
            # astral 平面：JS 用代理对合成为四字节，结果与标准 UTF-8 一致。
            append(code >> 18 | 240)
            append(code >> 12 & 63 | 128)
            append(code >> 6 & 63 | 128)
            append(code & 63 | 128)
        else:
            append(code >> 12 | 224)
            append(code >> 6 & 63 | 128)
            append(code & 63 | 128)
    return data


def build_tk(text: str, tkk: str = DEFAULT_TKK) -> str:
    """计算 Google 翻译接口所需的 ``tk`` 令牌（PHP ``TL()``）。

    Args:
        text: 参与计算的原文。批量翻译时是 **所有文本的拼接串**
            （对应 PHP ``implode("", $text)``）。
        tkk: TKK 字符串，默认使用内置常量 ``DEFAULT_TKK``。

    Returns:
        形如 ``"123456.789012"`` 的令牌字符串。

    Raises:
        ValueError: ``tkk`` 格式非法。
    """
    tkk0, tkk1 = dec_tkk(tkk)
    data = _utf8_bytes(text if isinstance(text, str) else str(text))

    a = tkk0
    for byte in data:
        a = (a + byte) & _MASK32
        a = _rl(a, "+-a^+6")
    a = _rl(a, "+-3^+b+-f")

    a = (a ^ tkk1) & _MASK32
    # PHP: if ($a < 0) $a = ($a & 2147483647) + 2147483648;
    # 在无符号空间里这条分支是恒等变换，保留以对齐原算法语义。
    if a >= 0x80000000:
        a = (a & 0x7FFFFFFF) + 0x80000000

    a = a % 1_000_000  # 等价于 PHP fmod(floatval($a), 1000000)
    return f"{a}.{a ^ tkk0}"


# --------------------------------------------------------------------------- #
# TKK 动态刷新
# --------------------------------------------------------------------------- #

#: 传输层签名，与 ``google_web`` 保持一致：
#: ``transport(method, url, params, headers, data, timeout) -> (status, text)``。
Transport = Callable[..., "tuple[int, str]"]


def fetch_tkk(
    transport: Transport,
    url: str = "https://translate.google.com",
    *,
    timeout: float = 8.0,
) -> str | None:
    """从 Google 首页抓取 ``tkk``，失败返回 ``None``（调用方回落内置常量）。

    Args:
        transport: HTTP 传输层，签名见 :data:`Transport`。
        url: 首页地址。
        timeout: 单次请求超时秒数。

    Returns:
        抓到的 TKK 字符串，或 ``None``。
    """
    try:
        status, body = transport(
            "GET",
            url,
            None,
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            None,
            timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 抓取失败必须降级而非中断
        logger.warning("fetch TKK failed (transport): %s", exc)
        return None

    if status != 200 or not body:
        logger.warning("fetch TKK failed (http status=%s)", status)
        return None

    match = TKK_PATTERN.search(body)
    if not match:
        # 结构性失效，不是网络抖动：2026-11-19 实测首页已无 tkk 变量。
        # 这类失败必然连续发生，故退避序列末档抬到 24h（见 FAILURE_BACKOFF_STEPS），
        # 避免每小时白下载数 MB 首页。文案必须点明"页面结构变了"，
        # 否则运维会误判成网络问题去查代理。
        logger.warning(
            "fetch TKK failed (pattern not found: Google 首页已不含 tkk 变量, "
            "html=%d bytes); falling back to DEFAULT_TKK",
            len(body),
        )
        return None

    tkk = f"{match.group(1)}.{match.group(2)}"
    logger.debug("fetched TKK: %s", tkk)
    return tkk


class TKKSnapshot:
    """带 TTL 内存缓存的 TKK 持有者（线程安全）。

    行为：

    * :meth:`get` 命中缓存直接返回；缓存过期（或从未抓取）时尝试重新抓取；
    * 抓取失败 **不抛异常**，继续使用上一个可用值（最终回落 :data:`DEFAULT_TKK`）；
    * :meth:`invalidate` 主动置为过期，下一次 :meth:`get` 会重新抓取。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        url: str = "https://translate.google.com",
        ttl: float = 21600.0,
        timeout: float = 8.0,
        initial: str = DEFAULT_TKK,
        allow_fetch: bool = True,
    ) -> None:
        """初始化。

        Args:
            transport: HTTP 传输层。
            url: 抓取 TKK 的首页地址。
            ttl: 缓存有效期（秒），默认 6 小时。
            timeout: 抓取超时秒数。
            initial: 初始值。
            allow_fetch: 是否允许联网抓取。**生产环境建议 ``False``**：
                2026-11 实测 Google 翻译首页（3.1MB）已完全不含 ``tkk`` 变量，
                每次抓取只会白下几 MB 并白等一次超时（connect 3s）。
                内置常量 :data:`DEFAULT_TKK` 仍然被服务端接受。
                保留开关是为了"万一 Google 恢复首页变量"时有翻盘余地。
        """
        self._transport = transport
        self._url = url
        self._ttl = max(0.0, float(ttl))
        self._timeout = timeout
        self._value = initial
        self._allow_fetch = allow_fetch
        self._fetched_at: float | None = None
        self._retry_at: float = 0.0
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    @property
    def value(self) -> str:
        """当前缓存值（不触发抓取）。"""
        with self._lock:
            return self._value

    @property
    def ttl(self) -> float:
        """缓存有效期（秒）。"""
        return self._ttl

    def _stale(self) -> bool:
        now = time.monotonic()
        if self._fetched_at is None:
            return now >= self._retry_at
        return (now - self._fetched_at) >= self._ttl

    def _failure_delay(self) -> float:
        """连续第 N 次抓取失败后的再试间隔（秒）。

        序列 60 / 300 / 900 / 3600，用尽后保持 3600。
        """
        index = min(max(self._consecutive_failures - 1, 0), len(FAILURE_BACKOFF_STEPS) - 1)
        return FAILURE_BACKOFF_STEPS[index]

    def get(self) -> str:
        """取当前 TKK；必要时尝试刷新。

        失败后不会每个请求都重试：连续失败第 N 次会退避
        :data:`FAILURE_BACKOFF_STEPS` 里对应的秒数（60/300/900/3600）。
        ``allow_fetch=False`` 时完全不发网络请求，直接返回缓存值。
        """
        if not self._allow_fetch:
            return self._value

        with self._lock:
            stale = self._stale()
        if not stale:
            return self._value

        # 抓取放在锁外，避免网络慢时阻塞其它线程读缓存。
        fetched = fetch_tkk(self._transport, self._url, timeout=self._timeout)
        with self._lock:
            if fetched:
                self._value = fetched
                self._fetched_at = time.monotonic()
                self._consecutive_failures = 0
            else:
                # 抓取失败：按连续失败次数递增退避，避免每个请求都白等一次超时。
                self._consecutive_failures += 1
                self._retry_at = time.monotonic() + self._failure_delay()
                if self._consecutive_failures in (1, 10) or self._consecutive_failures % 50 == 0:
                    # 连续失败 10 次以上说明 Google 前端结构或网络已变，必须人工介入。
                    logger.error(
                        "TKK fetch failed %d time(s) in a row; using %s, "
                        "next retry in %.0fs",
                        self._consecutive_failures,
                        self._value,
                        self._failure_delay(),
                    )
            return self._value

    def invalidate(self) -> None:
        """标记过期，下一次 :meth:`get` 强制重新抓取。"""
        with self._lock:
            self._fetched_at = None
            self._retry_at = 0.0
