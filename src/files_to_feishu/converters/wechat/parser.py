"""Convert article HTML into safe, editable shared content without executing it."""

import re
from collections.abc import Callable, Iterable
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ...models import (
    Asset,
    CodeLanguage,
    Element,
    Notice,
    ParsedDocument,
    SourceMetadata,
    TextRun,
    UserError,
)

_BLOCKS = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
_IGNORED = {"script", "style", "noscript", "form", "input", "button", "svg", "canvas"}
_MEDIA = {
    "iframe",
    "video",
    "audio",
    "mpvideo",
    "mp-video",
    "mpvoice",
    "mp-common-mpaudio",
    "mp-miniprogram",
    "mp-common-videosnap",
    "mp-common-profile",
    "mp-common-product",
}


def safe_link(value: str, base: str) -> str:
    try:
        url = urljoin(base, value)
        parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username:
            return url
    except ValueError:
        pass
    return ""


def article_state(html: str) -> str:
    """Never treat an access notice as a successful article conversion."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    has_article = bool(
        body and (body.get_text(strip=True) or body.find(["img", "video", "iframe", *_MEDIA]))
    )
    paid = soup.select_one(".pay_read_area, #js_pay_content, .js_pay_article") is not None
    # Interstitial phrases quoted inside an actual article are ordinary content.
    if has_article and body is not None:
        body.extract()
    text = soup.get_text(" ", strip=True)
    if any(
        value in text
        for value in (
            "该内容已被发布者删除",
            "此内容因违规无法查看",
            "该内容已删除",
        )
    ):
        return "deleted"
    if paid or any(
        value in text
        for value in (
            "付费后可阅读全文",
            "付费阅读剩余",
            "购买后阅读全文",
        )
    ):
        return "paid"
    if has_article:
        return "article"
    if any(value in text for value in ("环境异常", "完成验证", "访问过于频繁", "去验证")):
        return "verification"
    return "unavailable"


def _text(soup: BeautifulSoup, selector: str) -> str:
    value = soup.select_one(selector)
    return value.get_text(" ", strip=True) if value else ""


def _styled(node: Tag, styles: TextRun, base: str) -> TextRun:
    inherited = styles.model_copy()
    style = str(node.get("style", "")).lower()
    if node.name in {"b", "strong"} or re.search(r"font-weight\s*:\s*(bold|[6-9]00)", style):
        inherited.bold = True
    if node.name in {"i", "em"} or "font-style:italic" in style.replace(" ", ""):
        inherited.italic = True
    if node.name in {"s", "del", "strike"} or "line-through" in style:
        inherited.strike = True
    if node.name == "code":
        inherited.inline_code = True
    if node.name == "a":
        inherited.link = safe_link(str(node.get("href", "")), base)
    return inherited


def _runs(nodes: Iterable[object], base: str, styles: TextRun | None = None) -> list[TextRun]:
    output: list[TextRun] = []
    styles = styles or TextRun(text="")
    for node in nodes:
        if isinstance(node, NavigableString):
            text = re.sub(r"[\t\r\n ]+", " ", str(node)).replace("\xa0", " ")
            if text:
                output.append(styles.model_copy(update={"text": text}))
        elif isinstance(node, Tag) and node.name not in _IGNORED:
            if node.name == "br":
                output.append(styles.model_copy(update={"text": "\n"}))
                continue
            if node.name in {"img", "ul", "ol", "table", "pre"} | _MEDIA:
                continue
            inherited = _styled(node, styles, base)
            if node.name in _BLOCKS and output and not output[-1].text.endswith("\n"):
                output.append(styles.model_copy(update={"text": "\n"}))
            output.extend(_runs(node.children, base, inherited))
            if node.name in _BLOCKS and output and not output[-1].text.endswith("\n"):
                output.append(styles.model_copy(update={"text": "\n"}))
    merged: list[TextRun] = []
    for run in output:
        if merged and run.model_dump(exclude={"text"}) == merged[-1].model_dump(exclude={"text"}):
            merged[-1].text += run.text
        else:
            merged.append(run)
    return [run for run in merged if run.text]


def _code_text(node: Tag) -> str:
    # WeChat uses either a literal pre/code text node or one code node per line.
    codes = node.find_all("code", recursive=False)
    target = node
    if len(codes) > 1:
        return "\n".join(code.get_text().replace("\xa0", " ") for code in codes)
    if codes:
        target = codes[0]
    pieces: list[str] = []
    for item in target.descendants:
        if isinstance(item, NavigableString):
            pieces.append(str(item))
        elif isinstance(item, Tag) and item.name == "br":
            pieces.append("\n")
    return "".join(pieces).replace("\r\n", "\n").replace("\xa0", " ")


def parse_article(
    html: str,
    url: str,
    asset_loader: Callable[[str], Asset],
) -> ParsedDocument:
    state = article_state(html)
    if state != "article":
        messages = {
            "deleted": "该公众号文章已删除或无法查看。",
            "paid": "该文章包含付费内容，无法获取完整正文，请勿将预览视为完整原文。",
            "verification": "文章需要在独立浏览器中完成验证。",
            "unavailable": "未识别到公众号文章正文，请检查链接是否可访问。",
        }
        raise UserError(messages[state])
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#js_content")
    assert body is not None
    title = _text(soup, "#activity-name")
    if not title:
        meta = soup.find("meta", property="og:title")
        title = str(meta.get("content", "")) if isinstance(meta, Tag) else ""
    document = ParsedDocument(
        source_kind="wechat",
        elements=[],
        metadata=SourceMetadata(
            url=url,
            title=title,
            account=_text(soup, "#js_name"),
            author=_text(soup, "#js_author_name"),
            published_at=_text(soup, "#publish_time"),
        ),
    )
    if not title:
        document.notices.append(Notice(reason="原网页未提供可识别的文章标题，请在发布前填写标题。"))

    def notice(reason: str) -> None:
        document.notices.append(Notice(reason=reason))

    def append_runs(
        runs: list[TextRun],
        kind: Literal["text", "heading", "quote", "bullet", "ordered"] = "text",
        *,
        level: int = 1,
        list_depth: int = 0,
        list_start: int | None = None,
    ) -> None:
        if runs:
            runs[0].text = runs[0].text.lstrip(" ")
            runs[-1].text = runs[-1].text.rstrip(" ")
            runs = [run for run in runs if run.text]
        text = "".join(run.text for run in runs)
        if text.strip():
            document.elements.append(
                Element(
                    kind=kind,
                    text=text,
                    runs=runs,
                    level=level,
                    list_depth=list_depth,
                    list_start=list_start,
                )
            )

    def image(node: Tag) -> None:
        source = str(node.get("data-src") or node.get("src") or "")
        source = safe_link(source, url) if source else ""
        caption = str(node.get("alt") or node.get("data-caption") or "")
        if not source:
            notice("一张图片缺少可获取的地址，已保留缺图提示。")
            append_runs([TextRun(text=caption or "[图片缺失：原网页未提供地址]")])
            return
        try:
            asset = asset_loader(source)
        except UserError as exc:
            notice(f"图片未完整获取：{exc}")
            append_runs([TextRun(text=caption or "[图片获取失败，查看原文]", link=url)])
            return
        if not any(existing.source_url == asset.source_url for existing in document.assets):
            document.assets.append(asset)
        document.elements.append(Element(kind="image", asset=asset.name))
        if caption:
            append_runs([TextRun(text=caption)])

    def media(node: Tag) -> None:
        name = str(node.get("title") or node.get("aria-label") or node.get("data-title") or "")
        kind = "音视频或小程序卡片"
        notice(f"{name or kind}未完整转换；保留可获得的封面、标题和原文入口。")
        poster = str(
            node.get("poster") or node.get("data-cover") or node.get("data-cover-url") or ""
        )
        if poster:
            poster_node = soup.new_tag("img", attrs={"src": poster, "alt": name})
            image(poster_node)
        for cover in node.find_all("img"):
            image(cover)
        append_runs([TextRun(text=f"{name or kind}（未完整转换，查看原文）", link=url)])

    def walk(
        node: object, depth: int = 0, quote: bool = False, styles: TextRun | None = None
    ) -> None:
        before = len(document.elements)
        walk_node(node, depth, quote, styles or TextRun(text=""))
        if depth:
            for element in document.elements[before:]:
                if element.kind not in {"bullet", "ordered"} and not element.list_depth:
                    element.list_depth = min(depth, 8)

    def walk_node(node: object, depth: int, quote: bool, styles: TextRun) -> None:
        if isinstance(node, NavigableString):
            append_runs(_runs([node], url, styles), "quote" if quote else "text")
            return
        if not isinstance(node, Tag):
            return
        styles = _styled(node, styles, url)
        if re.search(
            r"(?:background(?:-image)?|content)\s*:[^;]*url\(", str(node.get("style", "")), re.I
        ):
            notice("网页样式中含背景图片或生成内容，未完整转换，请通过原文核对。")
        if node.name in _IGNORED:
            if node.name in {"svg", "canvas"}:
                notice("矢量图或画布区域无法转为可编辑内容，请通过原文核对。")
                append_runs([TextRun(text="[复杂图形未完整转换，查看原文]", link=url)])
            return
        if node.name == "img":
            image(node)
        elif (
            node.name in _MEDIA or node.name.startswith("mp-") or node.get("data-miniprogram-appid")
        ):
            media(node)
        elif node.name == "pre":
            code = _code_text(node)
            classes = " ".join(node.get_attribute_list("class"))
            child = node.find("code")
            if child:
                classes += " " + " ".join(child.get_attribute_list("class"))
            language = str(node.get("data-language") or node.get("data-lang") or classes).lower()
            detected: CodeLanguage = "plaintext"
            if re.search(r"(?:^|[\s-])(typescript|ts)(?:$|\s)", language):
                detected = "typescript"
            elif re.search(r"(?:^|[\s-])(javascript|js)(?:$|\s)", language):
                detected = "javascript"
            if len(code) > 20000:
                notice("较长代码已按原顺序拆成多个可编辑代码块，内容和缩进均保留。")
            while code:
                boundary = min(len(code), 20000)
                if len(code) > 20000:
                    newline = code.rfind("\n", 0, 20000)
                    if newline >= 0:
                        boundary = newline + 1
                document.elements.append(
                    Element(
                        kind="code",
                        text=code[:boundary],
                        language=detected,
                        code_origin="html",
                        code_reviewed=True,
                    )
                )
                code = code[boundary:]
        elif node.name in {"ul", "ol"}:
            raw_start = str(node.get("start", "1"))
            start = int(raw_start) if raw_start.isdigit() and int(raw_start) > 0 else 1
            number = start
            for item in node.find_all("li", recursive=False):
                raw_value = str(item.get("value", ""))
                if raw_value.isdigit() and int(raw_value) > 0:
                    number = int(raw_value)
                before = len(document.elements)
                walk(item, depth + 1, quote, styles)
                emitted = document.elements[before:]
                if emitted and emitted[0].kind in {"text", "quote"}:
                    emitted[0].kind = "ordered" if node.name == "ol" else "bullet"
                    emitted[0].list_depth = min(depth, 8)
                    emitted[0].list_start = number if node.name == "ol" else None
                elif emitted:
                    notice("一个列表项以非文本内容开头，其列表标记未完整保留。")
                    for element in emitted:
                        element.list_depth = max(0, element.list_depth - 1)
                if depth > 8:
                    notice("列表嵌套超过 9 层，超出部分已合并到最深一层。")
                number += 1
        elif node.name == "table":
            rows: list[list[str]] = []
            table_runs: list[list[list[TextRun]]] = []
            complex_table = False
            for tr in node.find_all("tr"):
                if tr.find_parent("table") != node:
                    continue
                cells = tr.find_all(["th", "td"], recursive=False)
                rich = [_runs(cell.children, url, _styled(cell, styles, url)) for cell in cells]
                if rich:
                    rows.append(["".join(run.text for run in cell) for cell in rich])
                    table_runs.append(rich)
                complex_table |= any(
                    str(cell.get("rowspan", "1")) != "1"
                    or str(cell.get("colspan", "1")) != "1"
                    or cell.find(["img", "pre", "table", *_MEDIA]) is not None
                    for cell in cells
                )
            if rows:
                width = max(map(len, rows))
                for row, rich_row in zip(rows, table_runs, strict=True):
                    row.extend([""] * (width - len(row)))
                    rich_row.extend([[] for _ in range(width - len(rich_row))])
                document.elements.append(Element(kind="table", rows=rows, table_runs=table_runs))
            if complex_table:
                notice("复杂表格的合并单元格或内嵌媒体无法完整表达；已保留单元格文字及下方素材。")
                for child in node.find_all(["img", "pre", *_MEDIA]):
                    walk(child, depth, quote, styles)
        else:
            heading = re.fullmatch(r"h([1-6])", node.name or "")
            if heading:
                append_runs(_runs(node.children, url, styles), "heading", level=int(heading[1]))
                for child in node.find_all("img"):
                    image(child)
                return
            children = list(node.children)
            structural = _BLOCKS | {"pre", "ul", "ol", "table", "img"} | _MEDIA | _IGNORED
            grouped: list[object] = []
            is_quote = quote or node.name == "blockquote"
            for item_child in children:
                if isinstance(item_child, Tag) and (
                    item_child.name in structural or item_child.find(list(structural)) is not None
                ):
                    append_runs(_runs(grouped, url, styles), "quote" if is_quote else "text")
                    grouped = []
                    walk(item_child, depth, is_quote, styles)
                else:
                    grouped.append(item_child)
            append_runs(_runs(grouped, url, styles), "quote" if is_quote else "text")

    walk(body)
    if not document.elements:
        raise UserError("文章没有可转换的正文内容。")
    metadata = document.metadata
    labels = [
        value
        for value in (
            f"公众号：{metadata.account}" if metadata.account else "",
            f"作者：{metadata.author}" if metadata.author else "",
            f"发布时间：{metadata.published_at}" if metadata.published_at else "",
        )
        if value
    ]
    if labels:
        append_runs([TextRun(text=" · ".join(labels))])
    append_runs([TextRun(text="查看公众号原文", link=url)])
    return document
