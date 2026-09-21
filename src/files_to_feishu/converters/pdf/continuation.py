"""Conservative reconstruction of code broken across adjacent PDF pages."""

import re

from ...models import CodeSource, Element, Notice, ParsedDocument


def sources_of(element: Element) -> list[CodeSource]:
    if element.page is None:
        raise ValueError("PDF code must have a source page")
    return element.code_sources or [
        CodeSource(page=element.page, asset=element.asset, bbox=element.bbox)
    ]


def _brackets(text: str, initial: list[str]) -> tuple[list[str], bool] | None:
    """Track delimiters without treating quoted strings or comments as code.

    Ambiguous regexes, templates and split strings/comments deliberately opt out.
    A continuation must close a delimiter inherited from the previous page.
    """
    stack = [(char, True) for char in initial]
    inherited_close = False
    i = 0
    while i < len(text):
        char = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                return None
            i = end + 2
            continue
        if char in "`/":
            return None
        if char in "\"'":
            quote = char
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] == "\n":
                    return None
                i += 2 if text[i] == "\\" else 1
            if i >= len(text):
                return None
        elif char in "([{":
            stack.append((char, False))
        elif char in ")]}":
            if not stack or stack[-1][0] != {"}": "{", "]": "[", ")": "("}[char]:
                return None
            inherited_close |= stack.pop()[1]
        i += 1
    return [entry[0] for entry in stack], inherited_close


def continues_code(left: str, right: str) -> bool:
    before = _brackets(left, [])
    if not before or not before[0] or not right.strip():
        return False
    after = _brackets(right, before[0])
    return after is not None and after[1]


def adjacent_edges(
    left: CodeSource, right: CodeSource, sizes: dict[int, tuple[float, float]]
) -> bool:
    if right.page != left.page + 1 or len(left.bbox) != 4 or len(right.bbox) != 4:
        return False
    w1, h1 = sizes.get(left.page, (0, 0))
    w2, h2 = sizes.get(right.page, (0, 0))
    if min(w1, h1, w2, h2) <= 0:
        return False
    return (
        left.bbox[3] >= h1 * 0.82
        and right.bbox[1] <= h2 * 0.18
        and abs(left.bbox[0] / w1 - right.bbox[0] / w2) <= 0.04
        and abs(left.bbox[2] / w1 - right.bbox[2] / w2) <= 0.04
    )


def merge_cross_page_code(
    parsed: ParsedDocument,
    sizes: dict[int, tuple[float, float]],
    furniture_ids: set[int] | None = None,
) -> ParsedDocument:
    """Merge only consecutive body blocks; preserve all margin text outside code."""
    if parsed.source_kind != "pdf":
        return parsed
    furniture_ids = furniture_ids or set()

    def furniture(item: Element) -> bool:
        if id(item) in furniture_ids:
            return True
        if item.kind != "text" or len(item.bbox) != 4 or item.page is None:
            return False
        height = sizes.get(item.page, (0, 0))[1]
        at_margin = height > 0 and (item.bbox[3] <= height * 0.08 or item.bbox[1] >= height * 0.92)
        return at_margin and bool(
            re.fullmatch(rf"\s*(?:{item.page}|第\s*{item.page}\s*页)\s*", item.text)
        )

    result: list[Element] = []
    previous: Element | None = None
    for item in parsed.elements:
        if furniture(item):
            result.append(item)
            continue
        joined = (previous.text + "\n" + item.text) if previous else ""
        if (
            previous is not None
            and previous.kind == item.kind == "code"
            and adjacent_edges(sources_of(previous)[-1], sources_of(item)[0], sizes)
            and not (
                previous.language != item.language
                and "plaintext" not in {previous.language, item.language}
            )
            and len(joined) <= 20000
            and continues_code(previous.text, item.text)
        ):
            previous.code_sources = sources_of(previous) + sources_of(item)
            previous.text = joined
            previous.code_reviewed = False
            if previous.language == "plaintext":
                previous.language = item.language
            if item.code_origin == "ocr":
                previous.code_origin = "ocr"
            pages = [source.page for source in previous.code_sources]
            parsed.notices.append(
                Notice(
                    page=item.page,
                    reason=f"第 {pages[0]}–{pages[-1]} 页代码已合并，请核对衔接、换行与缩进并保存",
                )
            )
            continue
        result.append(item)
        previous = item
    parsed.elements = result
    return parsed
