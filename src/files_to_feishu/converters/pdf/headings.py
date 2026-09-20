"""Recover heading levels when a PDF layout engine returns a flat outline."""

import math
import re
from pathlib import Path
from statistics import median

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from ...models import Element, ParsedDocument


def heading_sizes(headings: list[Element], source: Path | None) -> dict[int, float]:
    sizes = {id(e): e.bbox[3] - e.bbox[1] for e in headings if len(e.bbox) == 4}
    if source is None:
        return sizes
    with pdfium.PdfDocument(source) as pdf:
        for number in sorted({e.page for e in headings}):
            page = pdf[number - 1]
            try:
                height = page.get_height()
                textpage = page.get_textpage()
                try:
                    chars = []
                    for index in range(textpage.count_chars()):
                        char = textpage.get_text_range(index, 1)
                        if not char.strip() or char in {"\x00", "\ufeff"}:
                            continue
                        left, bottom, right, top = textpage.get_charbox(index)
                        size = pdfium_raw.FPDFText_GetFontSize(textpage.raw, index)
                        if math.isfinite(size) and size > 0:
                            chars.append(((left + right) / 2, height - (top + bottom) / 2, size))
                    for heading in headings:
                        if heading.page != number or len(heading.bbox) != 4:
                            continue
                        left, top, right, bottom = heading.bbox
                        fonts = [
                            s
                            for x, y, s in chars
                            if left - 1 <= x <= right + 1 and top - 1 <= y <= bottom + 1
                        ]
                        if fonts:
                            sizes[id(heading)] = median(fonts)
                finally:
                    textpage.close()
            finally:
                page.close()
    return sizes


def chapter(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:[一二三四五六七八九十百]+[、．.]|第[一二三四五六七八九十百\d]+[章节篇部])",
            text.strip(),
        )
    )


def numbering(text: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[.、)]|\s)\s*", text.strip())
    return tuple(int(n) for n in match[1].split(".")) if match else ()


def assign_heading_levels(
    parsed: ParsedDocument, labels: dict[int, str], source: Path | None = None
) -> ParsedDocument:
    headings = [e for e in parsed.elements if e.kind == "heading"]
    if len(headings) < 2 or {e.level for e in headings} != {1}:
        # Keep genuine structured levels supplied by the parser; old all-1 output
        # carries no hierarchy information, so it requires source-based recovery.
        return parsed
    sizes = heading_sizes(headings, source)
    first = headings[0]
    others = [sizes[id(e)] for e in headings[1:] if id(e) in sizes]
    title = (
        first.page == 1
        and parsed.elements[0] is first
        and not chapter(first.text)
        and not numbering(first.text)
        and (
            labels.get(id(first)) == "title"
            or (others and sizes.get(id(first), 0) > max(others) * 1.08)
        )
    )
    base = 1 if title else 0
    # The stack spans PDF pages: a page break never starts a new section.
    stack: list[Element] = []
    for heading in headings:
        if title and heading is first:
            heading.level = 1
            continue
        number = numbering(heading.text)
        if chapter(heading.text):
            stack.clear()
        elif len(number) > 1 and any(numbering(e.text) == number[:-1] for e in stack):
            parent = next(
                i for i in range(len(stack) - 1, -1, -1) if numbering(stack[i].text) == number[:-1]
            )
            stack = stack[: parent + 1]
        else:
            size = sizes.get(id(heading), 0)
            if size:
                # Font sizes are stable across pages; tolerate small PDF variations.
                while stack and size >= sizes.get(id(stack[-1]), size) * 0.94:
                    stack.pop()
            else:
                # With no geometry, numbering is the only reliable parent signal.
                while stack and not chapter(stack[-1].text):
                    prior_number = numbering(stack[-1].text)
                    if not number and prior_number:
                        break
                    stack.pop()
        heading.level = min(6, (stack[-1].level if stack else base) + 1)
        stack.append(heading)
    return parsed
