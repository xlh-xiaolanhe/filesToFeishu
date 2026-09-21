"""Fetch a public-account article and retain a deterministic offline source archive."""

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment
from PIL import Image, UnidentifiedImageError

from ...models import Asset, ParsedDocument, UserError
from .browser import BrowserFactory, VerificationBrowser
from .network import SafeFetcher, normalize_url
from .parser import article_state, parse_article, safe_link

_SAFE_TAGS = {
    "div",
    "section",
    "article",
    "p",
    "span",
    "br",
    "hr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "strong",
    "b",
    "em",
    "i",
    "s",
    "del",
    "strike",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "a",
    "img",
    "figure",
    "figcaption",
}
_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
}


def offline_html(html: str, document: ParsedDocument) -> str:
    """Keep article markup but remove executable attributes and remote subresources."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    assert body is not None
    assets = {asset.source_url: asset.name for asset in document.assets}
    for comment in body.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for node in list(body.find_all(True)):
        if node.parent is None:
            continue
        if node.name in {
            "script",
            "style",
            "noscript",
            "form",
            "input",
            "button",
            "object",
            "embed",
        }:
            node.decompose()
            continue
        if node.name in {
            "video",
            "audio",
            "iframe",
            "mpvideo",
            "mpvoice",
            "mp-common-mpaudio",
            "mp-miniprogram",
            "svg",
            "canvas",
        }:
            replacement = soup.new_tag("p")
            replacement.string = "[此媒体或复杂区域未完整转换，请查看原文和缺项说明]"
            node.replace_with(replacement)
            continue
        if node.name not in _SAFE_TAGS:
            node.unwrap()
            continue
        attrs: dict[str, str] = {}
        if node.name == "img":
            source = urljoin(
                document.metadata.url, str(node.get("data-src") or node.get("src") or "")
            )
            if source not in assets:
                replacement = soup.new_tag("p")
                replacement.string = str(node.get("alt") or "[图片未完整获取]")
                node.replace_with(replacement)
                continue
            attrs["src"] = "assets/" + assets[source]
            attrs["alt"] = str(node.get("alt", ""))
        elif node.name == "a":
            link = safe_link(str(node.get("href", "")), document.metadata.url)
            if link:
                attrs["href"] = link
                attrs["rel"] = "noopener noreferrer"
        for attr in {"start", "value", "rowspan", "colspan"}:
            value = str(node.get(attr, ""))
            if value.isdigit() and 0 < int(value) <= 10000:
                attrs[attr] = value
        node.attrs = {}
        for key, value in attrs.items():
            node[key] = value
    body.attrs = {}
    output = BeautifulSoup(
        "<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>",
        "html.parser",
    )
    assert output.head is not None and output.body is not None
    csp = output.new_tag(
        "meta",
        attrs={
            "http-equiv": "Content-Security-Policy",
            "content": (
                "default-src 'none'; img-src 'self' data:; style-src 'none'; "
                "base-uri 'none'; form-action 'none'"
            ),
        },
    )
    output.head.append(csp)
    heading = output.new_tag("h1")
    heading.string = document.metadata.title or "公众号文章"
    output.body.append(heading)
    output.body.append(body)
    source_link = output.new_tag("a", href=document.metadata.url)
    source_link.string = "查看原文"
    output.body.append(source_link)
    for notice in document.notices:
        paragraph = output.new_tag("p")
        paragraph.string = "未完整转换：" + notice.reason
        output.body.append(paragraph)
    return str(output)


def write_archive(folder: Path, html: str, document: ParsedDocument) -> None:
    manifest = {
        "source": document.metadata.model_dump(),
        "assets": [asset.model_dump() for asset in document.assets],
        "notices": [notice.reason for notice in document.notices],
    }
    entries = {
        "article.html": offline_html(html, document).encode("utf-8"),
        "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        ),
    }
    for asset in document.assets:
        entries["assets/" + asset.name] = (folder / "assets" / asset.name).read_bytes()
    with zipfile.ZipFile(folder / "source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, data)


class WechatConverter:
    def __init__(
        self,
        max_html_bytes: int = 5 * 1024 * 1024,
        max_asset_bytes: int = 20 * 1024 * 1024,
        max_total_bytes: int = 100 * 1024 * 1024,
        timeout: float = 30,
        *,
        fetcher: SafeFetcher | None = None,
        browser_factory: BrowserFactory = VerificationBrowser,
    ) -> None:
        self.max_html_bytes = max_html_bytes
        self.max_asset_bytes = max_asset_bytes
        self.max_total_bytes = max_total_bytes
        self.timeout = timeout
        self.fetcher = fetcher or SafeFetcher(timeout)
        self.browser_factory = browser_factory
        self.browser: VerificationBrowser | None = None
        self._url = ""
        self._folder: Path | None = None

    def start(
        self, url: str, folder: Path, progress: Callable[[str], None]
    ) -> ParsedDocument | None:
        self.cancel()
        self._url = normalize_url(url)
        self._folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        progress("正在获取公众号文章")
        resource = self.fetcher.get(self._url, self.max_html_bytes, article=True)
        html = resource.body.decode("utf-8", errors="replace")
        if article_state(html) == "verification":
            self.browser = self.browser_factory(self.max_html_bytes, self.timeout)
            self.browser.open(self._url)
            progress("请在独立浏览器完成验证，再点击继续获取；也可以取消")
            return None
        return self._convert(html, folder, progress)

    def resume(self, folder: Path, progress: Callable[[str], None]) -> ParsedDocument | None:
        if self.browser is None or self._folder != folder:
            raise UserError("验证会话已结束或程序已重启，请重新获取文章。")
        html = self.browser.content()
        if article_state(html) == "verification":
            progress("尚未完成验证，请在独立浏览器完成验证后继续")
            return None
        try:
            if (
                article_state(html) == "article"
                and normalize_url(self.browser.current_url()) != self._url
            ):
                raise UserError("验证窗口已跳转到另一篇文章，请重新获取原文章，避免来源不一致。")
            return self._convert(html, folder, progress)
        finally:
            self.cancel()

    def pump(self) -> None:
        if self.browser is not None:
            self.browser.pump()

    def cancel(self) -> None:
        if self.browser is not None:
            self.browser.close()
        self.browser = None
        self._folder = None

    def close(self) -> None:
        self.cancel()
        self.fetcher.close()

    def _convert(self, html: str, folder: Path, progress: Callable[[str], None]) -> ParsedDocument:
        progress("正在解析正文并下载图片")
        asset_folder = folder / "assets"
        asset_folder.mkdir(parents=True, exist_ok=True)
        total = 0
        cached: dict[str, Asset] = {}

        def load_asset(url: str) -> Asset:
            nonlocal total
            if url in cached:
                return cached[url]
            remaining = self.max_total_bytes - total
            if remaining <= 0:
                raise UserError("文章素材总大小超过限制。")
            allowance = min(self.max_asset_bytes, remaining)
            # Failed/oversized downloads consume their reserved budget as well.
            total += allowance
            resource = self.fetcher.get(url, allowance)
            total -= allowance - len(resource.body)
            try:
                with Image.open(io.BytesIO(resource.body)) as image:
                    image_format = image.format or ""
                    if image_format not in _IMAGE_FORMATS:
                        raise UserError("不支持此图片格式，已保留原文入口。")
                    image.verify()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise UserError("素材不是有效图片，未将其当作图片保存。") from exc
            extension, media_type = _IMAGE_FORMATS[image_format]
            digest = hashlib.sha256(resource.body).hexdigest()
            name = f"image-{digest}.{extension}"
            (asset_folder / name).write_bytes(resource.body)
            asset = Asset(name=name, media_type=media_type, digest=digest, source_url=url)
            cached[url] = asset
            return asset

        document = parse_article(html, self._url, load_asset)
        write_archive(folder, html, document)
        progress("公众号文章已获取，请核对内容和未完整转换提示")
        return document
