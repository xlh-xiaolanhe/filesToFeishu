"""Remove PDF layout artifacts using labels and positioned source evidence."""

import re
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from ...models import Element
from .code import layout_bounds

BULLETS = "●•◦▪▫‣⁃"


def normalize_number_markers(elements: list[Element]) -> list[Element]:
    """Replace decorative numeric prefixes and join detached same-line labels.

    Explicit plain numbering preserves the source number even when another
    ordered list precedes it. Never alter symbols inside code or prose.
    """
    prefix = re.compile(r"^\s*(?:([0-9])\ufe0f?\u20e3|([①-⑳]))\s*")
    replacements: dict[int, Element] = {}
    consumed: set[int] = set()
    for index, element in enumerate(elements):
        if index in consumed or element.kind not in {"text", "heading", "bullet", "ordered"}:
            continue
        match = prefix.match(element.text)
        if not match:
            continue
        number = match[1] if match[1] is not None else str(ord(match[2]) - ord("①") + 1)
        text = element.text[match.end() :]
        target = element
        if not text and len(element.bbox) == 4:
            _, top, right, bottom = element.bbox
            height = bottom - top
            candidates = []
            for other_index, other in enumerate(elements):
                if (
                    other_index == index
                    or other_index in consumed
                    or other.page != element.page
                    or len(other.bbox) != 4
                    or other.kind not in {"text", "bullet", "ordered"}
                    or not other.text.strip()
                    or prefix.match(other.text)
                ):
                    continue
                other_left, other_top, _, other_bottom = other.bbox
                overlap = min(bottom, other_bottom) - max(top, other_top)
                if (
                    -1 <= other_left - right <= height * 1.5
                    and overlap > min(height, other_bottom - other_top) * 0.5
                ):
                    candidates.append((other_index, other))
            if len(candidates) == 1:
                other_index, target = candidates[0]
                consumed.add(other_index)
                text = target.text
        # Mutate the original target so heading/furniture identity stays valid.
        target.text = f"{number}. {text}".rstrip()
        if target.kind in {"bullet", "ordered"}:
            target.kind = "text"
        if target is not element:
            target.bbox = [
                min(element.bbox[0], target.bbox[0]),
                min(element.bbox[1], target.bbox[1]),
                max(element.bbox[2], target.bbox[2]),
                max(element.bbox[3], target.bbox[3]),
            ]
        replacements[index] = target
    return [replacements.get(i, element) for i, element in enumerate(elements) if i not in consumed]


def source_list_markers(source: Path, layout: dict) -> dict[str, str]:
    """Identify a marker visually at the start, even if extracted at the end.

    Do not strip a literal bullet mentioned at the end of a sentence. Require a
    unique matching glyph at the left edge of the first source line instead.
    """
    result: dict[str, str] = {}
    candidates = [
        item
        for item in layout.get("texts", [])
        if item.get("label") == "list_item"
        and not item.get("enumerated")
        and re.search(rf"\s[{BULLETS}]\s*$", item.get("text", ""))
        and len(item.get("prov", [])) == 1
    ]
    if not candidates:
        return result
    with pdfium.PdfDocument(source) as pdf:
        for number in sorted({item["prov"][0]["page_no"] for item in candidates}):
            page = pdf[number - 1]
            try:
                height = page.get_height()
                textpage = page.get_textpage()
                try:
                    chars = []
                    for index in range(textpage.count_chars()):
                        char = textpage.get_text_range(index, 1)
                        if not char or char.isspace() or char in {"\x00", "\ufeff"}:
                            continue
                        left, bottom, right, top = textpage.get_charbox(index)
                        chars.append((char, left, height - (top + bottom) / 2, right, top - bottom))
                    for item in candidates:
                        if item["prov"][0]["page_no"] != number:
                            continue
                        left, top, right, bottom = layout_bounds(item, height)
                        inside = [
                            c
                            for c in chars
                            if left - 1 <= (c[1] + c[3]) / 2 <= right + 1
                            and top - 1 <= c[2] <= bottom + 1
                        ]
                        marker = item["text"].rstrip()[-1]
                        glyphs = [c for c in inside if c[0] == marker]
                        if len(glyphs) != 1 or len(inside) < 2:
                            continue
                        glyph = glyphs[0]
                        first_y = min(c[2] for c in inside)
                        tolerance = max(3, max(c[4] for c in inside) * 0.6)
                        first_line = [c for c in inside if c[2] <= first_y + tolerance]
                        if glyph in first_line and all(
                            c == glyph or c[1] > glyph[3] for c in first_line
                        ):
                            result[item["self_ref"]] = marker
                finally:
                    textpage.close()
            finally:
                page.close()
    return result


def list_text(item: dict, markers: dict[str, str]) -> str:
    text = item.get("text", "")
    if item.get("enumerated"):
        return text
    marker = markers.get(item.get("self_ref", "")) or item.get("marker", "")
    if len(marker) != 1 or marker not in BULLETS:
        return text
    # Explicit Docling marker metadata or positioned PDF evidence is required.
    return re.sub(rf"\s+{re.escape(marker)}\s*$", "", text)


def is_page_number(text: str, label: str, bounds: list[float], height: float, page: int) -> bool:
    if label not in {"text", "page_header", "page_footer"} or len(bounds) != 4 or height <= 0:
        return False
    if not (bounds[3] <= height * 0.08 or bounds[1] >= height * 0.92):
        return False
    value = text.strip()
    match = re.fullmatch(
        r"(?:[-–—]\s*)?(\d{1,3})(?:\s*[-–—])?"
        r"|第\s*(\d{1,3})\s*页(?:\s*[/／]?\s*共\s*\d{1,3}\s*页)?"
        r"|(?:Page\s+)?(\d{1,3})\s*(?:[/／]|of)\s*\d{1,3}"
        r"|Page\s+(\d{1,3})",
        value,
        re.IGNORECASE,
    )
    if not match:
        return False
    number = int(next(group for group in match.groups() if group is not None))
    return number > 0 and (label in {"page_header", "page_footer"} or number == page)


def page_without_numbers(
    output: Path, page: int, boxes: list[list[float]], size: tuple[float, float]
) -> str:
    """Keep the original preview intact; hide only known folios in the fallback asset."""
    name = f"page-{page}.png"
    if not boxes:
        return name
    with Image.open(output / name) as original:
        image = original.convert("RGB")
        sx, sy = image.width / size[0], image.height / size[1]
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in boxes:
            box = (
                max(0, int((left - 1) * sx)),
                max(0, int((top - 1) * sy)),
                min(image.width - 1, int((right + 1) * sx)),
                min(image.height - 1, int((bottom + 1) * sy)),
            )
            draw.rectangle(box, fill=image.getpixel((box[0], box[1])))
        name = f"content-page-{page}.png"
        image.save(output / name)
    return name
