"""远程图片下载：SSRF 防护 + 体积/格式校验。

这个模块的存在理由是：接口接受**用户给的 URL**，等于把服务器当代理用。
没有防护就是一个现成的 SSRF 跳板（读云元数据 ``169.254.169.254``、
扫内网、打 ``file://`` 等）。

防护策略（逐条对应下面的实现）：

1. 只允许 ``http`` / ``https``（拒绝 ``file`` / ``ftp`` / ``gopher`` 等）；
2. **DNS 解析后再校验 IP**，而不是只校验字面量——否则
   ``http://127.0.0.1.nip.io/`` 这类域名会绕过；
3. 拒绝回环 / 私网 / 链路本地 / 保留 / 组播 / 未指定地址（含 IPv6 映射）；
4. **手动跟随重定向**，每一跳都重新走 2、3——否则公网 URL 302 到
   ``http://169.254.169.254`` 就绕过了首跳校验；
5. 边下边计数，超限即断（不能等下完再判断大小，那是 OOM 的经典来源）；
6. 交给 PIL 真正解码一次，防止 ``Content-Type`` 撒谎。

依赖 ``httpx``；DNS 校验用标准库 ``ipaddress``。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

import httpx

from .types import ImageFetchError

logger = logging.getLogger(__name__)

#: 单张图上限。方案里对标的百度是 4MB；自建没有该约束，
#: 但 20MB 足够覆盖 4096px 级大图，再大应走异步任务而不是同步接口。
MAX_BYTES = 20 * 1024 * 1024

#: 最多跟随的重定向次数。每一跳都做内网校验。
MAX_REDIRECTS = 3

ALLOWED_SCHEMES = ("http", "https")

#: 允许的 MIME。SVG 被排除：它可内嵌脚本与外部引用，且光栅化行为不一致。
ALLOWED_MIME = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp")

_IPV4_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",  # 本网络
        "10.0.0.0/8",  # 私网
        "100.64.0.0/10",  # 运营商级 NAT
        "127.0.0.0/8",  # 回环
        "169.254.0.0/16",  # 链路本地（云元数据在此）
        "172.16.0.0/12",  # 私网
        "192.0.0.0/24",  # IETF 协议分配
        "192.0.2.0/24",  # 文档
        "192.168.0.0/16",  # 私网
        "198.18.0.0/15",  # 基准测试
        "198.51.100.0/24",  # 文档
        "203.0.113.0/24",  # 文档
        "224.0.0.0/4",  # 组播
        "240.0.0.0/4",  # 保留
    )
)

_IPV6_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/128",  # 未指定
        "::1/128",  # 回环
        "::ffff:0:0/96",  # IPv4 映射，需单独展开判断
        "64:ff9b::/96",  # NAT64
        "100::/64",  # 丢弃
        "fc00::/7",  # 唯一本地
        "fe80::/10",  # 链路本地
        "ff00::/8",  # 组播
    )
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断单个 IP 是否禁止访问。"""
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4 映射地址（::ffff:192.168.1.1）必须还原成 IPv4 再判断，
        # 否则会落进 ::ffff:0:0/96 之外的分支被误放行。
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return _is_blocked_ip(mapped)
        return any(ip in net for net in _IPV6_BLOCKED)
    return any(ip in net for net in _IPV4_BLOCKED)


def assert_public_host(host: str) -> None:
    """校验主机名的所有解析结果都是公网地址。

    只校验字面量 IP 是不够的：攻击者用 ``127.0.0.1.nip.io`` 这类
    通配 DNS 就能把内网地址包装成域名。所以必须真的解析。

    注意：这里存在一个固有窗口（解析与连接之间 DNS 可能被重绑定），
    彻底解决需要把解析结果固定下来传给定制的 socket 层。对图片翻译
    这类场景，校验 + 短超时已足够，如需更强可从 transport 层锁定 IP。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ImageFetchError(f"域名解析失败: {host!r}") from exc

    if not infos:
        raise ImageFetchError(f"域名无解析结果: {host!r}")

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise ImageFetchError(f"无法解析的地址: {raw!r}") from None
        if _is_blocked_ip(ip):
            raise ImageFetchError(
                f"目标地址属于内网/保留段，已拒绝: {host} -> {ip}"
            )


@dataclass(slots=True)
class FetchedImage:
    content: bytes
    mime: str
    #: 最终 URL（跟随重定向后），用于日志与缓存键
    final_url: str


def _guess_mime(content: bytes, header_value: str | None) -> str:
    """以实际字节嗅探为准，头部只作参考。"""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith(b"BM"):
        return "image/bmp"
    # 嗅探不出来时退回头部声明，交给 PIL 做最终裁决
    if header_value:
        declared = header_value.split(";")[0].strip().lower()
        if declared in ALLOWED_MIME:
            return declared
    raise ImageFetchError("无法识别的图片格式（仅支持 jpg/png/webp/bmp）")


def _validate_decodable(content: bytes) -> None:
    """用 PIL 真正解码一次，挡住"头是图片、体是垃圾"的情况。"""
    try:
        from PIL import Image

        with Image.open(BytesIO(content)) as im:
            im.verify()
    except ImageFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 的异常类型不稳定
        raise ImageFetchError(f"图片解码失败: {exc}") from exc


def fetch_image(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = MAX_BYTES,
    client: httpx.Client | None = None,
    proxy: str | None = None,
    user_agent: str = "imgtrans/0.1 (+image translate service)",
) -> FetchedImage:
    """下载图片并完成全部安全与完整性校验。

    Args:
        url: 用户提供的图片地址。
        timeout: 单次请求超时（秒）。
        max_bytes: 体积上限，超过立即断流。
        client: 复用外部 HTTP 客户端（便于注入测试 transport）。
        proxy: 代理地址，仅在自建 client 时生效。
        user_agent: 请求 UA。

    Raises:
        ImageFetchError: 任何一项校验不通过。
    """
    current = url
    own_client = client is None
    if own_client:
        client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            proxy=proxy,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )

    try:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ALLOWED_SCHEMES:
                raise ImageFetchError(
                    f"仅支持 http/https，收到: {parsed.scheme or '(空)'}"
                )
            if not parsed.hostname:
                raise ImageFetchError(f"URL 缺少主机名: {current!r}")

            # 关键：连接前校验解析结果，每一跳都校验
            assert_public_host(parsed.hostname)

            with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise ImageFetchError(
                            f"HTTP {resp.status_code} 缺少 Location 头"
                        )
                    current = str(httpx.URL(current).join(location))
                    logger.info("image redirect -> %s", current)
                    continue

                if resp.status_code != 200:
                    raise ImageFetchError(f"图片下载失败: HTTP {resp.status_code}")

                declared_len = resp.headers.get("content-length")
                if declared_len and declared_len.isdigit() and int(declared_len) > max_bytes:
                    raise ImageFetchError(
                        f"图片体积 {int(declared_len)} 字节超过上限 {max_bytes}"
                    )

                buf = bytearray()
                for chunk in resp.iter_bytes(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ImageFetchError(f"图片体积超过上限 {max_bytes} 字节")

                content = bytes(buf)
                if not content:
                    raise ImageFetchError("图片内容为空")

                mime = _guess_mime(content, resp.headers.get("content-type"))
                _validate_decodable(content)

                from PIL import Image

                with Image.open(BytesIO(content)) as im:
                    # 提前读一次尺寸，便于上层做超大图判断
                    w, h = im.size
                logger.info(
                    "image fetched: %dx%d %.1fKB mime=%s", w, h, len(content) / 1024, mime
                )
                return FetchedImage(content=content, mime=mime, final_url=current)

        raise ImageFetchError(f"重定向次数超过 {MAX_REDIRECTS} 次")
    finally:
        if own_client and client is not None:
            client.close()
