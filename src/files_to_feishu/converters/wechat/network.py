"""Bounded public HTTPS fetching without ambient credentials or DNS rebinding."""

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from ...models import UserError


def normalize_url(url: str) -> str:
    """Accept only public-account article routes and drop tracking parameters."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UserError("公众号文章链接格式不正确。") from exc
    if (
        parts.scheme != "https"
        or parts.hostname != "mp.weixin.qq.com"
        or parts.username
        or parts.password
        or port not in (None, 443)
        or not (parts.path == "/s" or parts.path.startswith("/s/"))
    ):
        raise UserError("请输入 https://mp.weixin.qq.com/s/ 开头的公众号文章链接。")
    if parts.path == "/s":
        query = dict(parse_qsl(parts.query))
        if not all(query.get(key) for key in ("__biz", "mid", "idx", "sn")):
            raise UserError("公众号文章链接缺少必要参数，请复制完整文章链接。")
        kept = [(key, query[key]) for key in ("__biz", "mid", "idx", "sn")]
        return urlunsplit(("https", "mp.weixin.qq.com", "/s", urlencode(kept), ""))
    token = parts.path.removeprefix("/s/")
    if not token or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for char in token
    ):
        raise UserError("公众号文章链接格式不正确。")
    return urlunsplit(("https", "mp.weixin.qq.com", parts.path, "", ""))


def public_addresses(host: str) -> list[str]:
    try:
        addresses = sorted(
            {str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        )
    except OSError as exc:
        raise UserError("文章或素材的域名解析失败，请稍后重试。") from exc
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise UserError("已阻止访问非公网地址。")
    return addresses


def validate_https(url: str, resolver: Callable[[str], list[str]] = public_addresses) -> list[str]:
    try:
        if len(url) > 8192 or any(ord(char) < 32 for char in url):
            raise ValueError("invalid URL characters or length")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise ValueError("not public HTTPS")
        host = parts.hostname.encode("idna").decode("ascii")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals are not allowed")
        addresses = resolver(host)
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError("non-public DNS")
        return addresses
    except (ValueError, UnicodeError) as exc:
        raise UserError("已阻止不安全的文章或素材地址。") from exc


@dataclass(frozen=True)
class Resource:
    body: bytes
    content_type: str
    url: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


class SafeFetcher:
    def __init__(
        self,
        timeout: float = 30,
        client: httpx.Client | None = None,
        resolver: Callable[[str], list[str]] = public_addresses,
    ) -> None:
        self.timeout = timeout
        self.client = client or httpx.Client(timeout=timeout, trust_env=False)
        self.resolver = resolver

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, max_bytes: int, *, article: bool = False) -> Resource:
        current = url
        deadline = time.monotonic() + self.timeout
        for _ in range(6):
            if article and urlsplit(current).hostname != "mp.weixin.qq.com":
                raise UserError("公众号文章重定向到了不支持的地址。")
            resource = self.request("GET", current, max_bytes, deadline=deadline)
            if resource.status in (301, 302, 303, 307, 308):
                location = resource.headers.get("location", "")
                if not location:
                    raise UserError("文章或素材返回了无效重定向。")
                current = urljoin(current, location)
                continue
            if resource.status >= 400:
                raise UserError(f"文章或素材获取失败（HTTP {resource.status}）。")
            return resource
        raise UserError("文章或素材重定向次数过多。")

    def request(
        self,
        method: str,
        url: str,
        max_bytes: int,
        *,
        headers: dict[str, str] | None = None,
        content: bytes = b"",
        deadline: float | None = None,
    ) -> Resource:
        """One bounded request, also used to proxy the ephemeral verification browser."""
        if method not in {"GET", "HEAD", "POST", "OPTIONS"} or len(content) > 1024 * 1024:
            raise UserError("已阻止不支持或过大的浏览器请求。")
        deadline = deadline or time.monotonic() + self.timeout
        addresses = validate_https(url, self.resolver)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UserError("文章或素材下载超时。")
        host = (urlsplit(url).hostname or "").encode("idna").decode("ascii")
        request_url = httpx.URL(url).copy_with(host=addresses[0])
        outgoing = {
            "host": host,
            "user-agent": "Mozilla/5.0 files-to-feishu/0.3",
            "accept": "text/html,image/*;q=0.9,*/*;q=0.8",
        }
        # Only the verification browser supplies its temporary cookies. Neither caller
        # authorization nor an ambient cookie jar is sent to a pinned address.
        allowed = {
            "cookie",
            "user-agent",
            "accept",
            "accept-language",
            "content-type",
            "origin",
            "referer",
        }
        for key, value in (headers or {}).items():
            if key.lower() in allowed:
                outgoing[key.lower()] = value
        self.client.cookies.clear()
        try:
            with self.client.stream(
                method,
                request_url,
                headers=outgoing,
                content=content,
                timeout=min(self.timeout, remaining),
                extensions={"sni_hostname": host},
            ) as response:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > max_bytes:
                    raise UserError("文章或素材超过下载大小限制。")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise UserError("文章或素材超过下载大小限制。")
                    if time.monotonic() > deadline:
                        raise UserError("文章或素材下载超时。")
                    chunks.append(chunk)
                response_headers = dict(response.headers)
                # iter_bytes decompresses; Chromium must not decompress the body again.
                for key in ("content-encoding", "content-length", "transfer-encoding"):
                    response_headers.pop(key, None)
                cookies = response.headers.get_list("set-cookie")
                if cookies:
                    response_headers["set-cookie"] = "\n".join(cookies)
                return Resource(
                    b"".join(chunks),
                    response.headers.get("content-type", "").split(";", 1)[0].lower(),
                    url,
                    response.status_code,
                    response_headers,
                )
        except httpx.HTTPError as exc:
            raise UserError("文章或素材网络请求失败，请稍后重试。") from exc
