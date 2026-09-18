"""Local code-region extraction. Recognition never executes or corrects source code."""

import io
import math
import re
import sys
from collections.abc import Callable
from statistics import median
from typing import Any, Literal, cast

import pypdfium2 as pdfium
from PIL import Image

from ...models import CodeLanguage, CodeSource, Element, Notice
from .continuation import adjacent_edges, continues_code, sources_of


def looks_like_code(text: str) -> bool:
    return bool(
        re.search(
            r"(?m)^\s*(?:(?:const|let|var|function|class|interface|type|import|export|def)\s+"
            r"[\w{$*]|(?:if|for|while|switch)\s*\(|(?:console\.log|alert|print)\s*\()",
            text,
        )
    )


def language_of(text: str) -> CodeLanguage:
    if re.search(r"\b(interface\s+\w+|type\s+\w+\s*=|\w+\s*:\s*(string|number|boolean)\b)", text):
        return "typescript"
    if re.search(r"\b(const|let|var|function)\s|\b(?:console\.|Date\.|alert\()", text):
        return "javascript"
    return "plaintext"


def contains(outer: list[float], inner: list[float], margin: float = 2) -> bool:
    return (
        outer[0] - margin <= inner[0]
        and outer[1] - margin <= inner[1]
        and outer[2] + margin >= inner[2]
        and outer[3] + margin >= inner[3]
    )


def layout_bounds(item: dict, height: float) -> list[float]:
    b = item["prov"][0]["bbox"]
    top, bottom = b["t"], b["b"]
    if b.get("coord_origin") == "BOTTOMLEFT":
        top, bottom = height - top, height - bottom
    return [b["l"], min(top, bottom), b["r"], max(top, bottom)]


def dark_panels(image: Image.Image) -> list[list[float]]:
    """Locate wide, mostly dark rectangular panels; syntax is checked separately."""
    small = image.convert("RGB")
    small.thumbnail((700, 1000))
    sx, sy = image.width / small.width, image.height / small.height
    pixels = cast(Any, small.load())
    panels: list[list[float]] = []
    current: list[float] | None = None
    for y in range(small.height):
        xs = [
            x
            for x in range(small.width)
            if max(pixels[x, y]) < 110 and max(pixels[x, y]) - min(pixels[x, y]) < 60
        ]
        valid = (
            xs
            and xs[-1] - xs[0] > max(80, small.width * 0.2)
            and len(xs) / (xs[-1] - xs[0] + 1) > 0.7
        )
        if valid:
            if current and abs(xs[0] - current[0]) < 8 and abs(xs[-1] + 1 - current[2]) < 8:
                current[3] = y + 1
            else:
                if current and current[3] - current[1] >= 16:
                    panels.append(current)
                current = [xs[0], y, xs[-1] + 1, y + 1]
        elif current:
            if current[3] - current[1] >= 16:
                panels.append(current)
            current = None
    if current and current[3] - current[1] >= 16:
        panels.append(current)
    return [[left * sx, t * sy, r * sx, b * sy] for left, t, r, b in panels]


def native_code(textpage, bounds: list[float], page_height: float) -> str:
    """Rebuild lines and leading indentation from positioned PDF characters."""
    raw = (
        textpage.get_text_bounded(
            bounds[0], page_height - bounds[3], bounds[2], page_height - bounds[1]
        )
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = [line for line in raw.split("\n") if line.strip()]
    if not lines:
        return ""
    chars = []
    for i in range(textpage.count_chars()):
        char = textpage.get_text_range(i, 1)
        if not char or char.isspace() or char == "\x00":
            continue
        left, b, r, t = textpage.get_charbox(i)
        y = page_height - (t + b) / 2
        if bounds[0] - 1 <= (left + r) / 2 <= bounds[2] + 1 and bounds[1] - 1 <= y <= bounds[3] + 1:
            chars.append((y, left, char, t - b))
    if not chars:
        return raw
    tolerance = max(3, median(c[3] for c in chars) * 0.85)
    rows: list[list[tuple]] = []
    for char in sorted(chars):
        if not rows or abs(char[0] - median(c[0] for c in rows[-1])) > tolerance:
            rows.append([char])
        else:
            rows[-1].append(char)
    if len(rows) != len(lines):
        return raw  # Never reconstruct characters by guessing if line geometry is ambiguous.
    rows = [sorted(row, key=lambda c: c[1]) for row in rows]
    base = min(row[0][1] for row in rows)
    advances = [
        b[1] - a[1]
        for row in rows
        for a, b in zip(row, row[1:], strict=False)
        if a[2].isascii() and b[2].isascii() and 2 < b[1] - a[1] < 20
    ]
    pitch = median(advances) if advances else 6
    ys = [median(c[0] for c in row) for row in rows]
    gaps = [b - a for a, b in zip(ys, ys[1:], strict=False) if b - a > 2]
    line_height = median(gaps) if gaps else 12
    result = []
    for index, (line, row) in enumerate(zip(lines, rows, strict=True)):
        if index and ys[index] - ys[index - 1] > 1.6 * line_height:
            result.extend([""] * min(4, round((ys[index] - ys[index - 1]) / line_height) - 1))
        indent = min(80, max(0, round((row[0][1] - base) / pitch)))
        result.append(" " * indent + line.lstrip(" \t"))
    return "\n".join(result)


def recognize_image(image: Image.Image) -> str:
    """Apple Vision OCR, offline and with language correction explicitly disabled."""
    if sys.platform != "darwin":
        raise RuntimeError("代码图片自动识别目前需要 macOS；可在预览中手动录入代码")
    import objc
    import Vision
    from Foundation import NSData

    stream = io.BytesIO()
    image.save(stream, format="PNG")
    data = stream.getvalue()
    with objc.autorelease_pool():
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(0)
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(["en-US", "zh-Hans"])
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            NSData.dataWithBytes_length_(data, len(data)), None
        )
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError("本地代码 OCR 未完成")
        rows = []
        for observation in request.results() or []:
            candidate = observation.topCandidates_(1)
            if not candidate:
                continue
            box = observation.boundingBox()
            rows.append(
                (
                    1 - box.origin.y - box.size.height / 2,
                    box.origin.x,
                    str(candidate[0].string()),
                    box.size.width,
                )
            )
    if not rows:
        return ""
    rows.sort()
    base = min(r[1] for r in rows)
    pitch = median(r[3] / max(len(r[2]), 1) for r in rows)
    return "\n".join(
        " " * min(80, max(0, round((x - base) / max(pitch, 0.001)))) + text
        for _, x, text, _ in rows
    )


def extract_code_regions(
    source, output, layout, progress: Callable
) -> tuple[list[Element], list[Notice]]:
    regions: list[Element] = []
    notices: list[Notice] = []
    ocr_error_reported = False
    sizes: dict[int, tuple[float, float]] = {}
    with pdfium.PdfDocument(source) as pdf:
        for number in range(1, len(pdf) + 1):
            page = pdf[number - 1]
            try:
                width, height = page.get_size()
                sizes[number] = (width, height)
                with Image.open(output / f"page-{number}.png") as image:
                    sx, sy = image.width / width, image.height / height
                    candidates = [
                        ([left / sx, t / sy, r / sx, b / sy], False)
                        for left, t, r, b in dark_panels(image)
                    ]
                    for item in layout.get("texts", []) + layout.get("pictures", []):
                        prov = item.get("prov", [])
                        if item.get("label") in {"code", "picture"}:
                            for location in prov:
                                if location["page_no"] != number:
                                    continue
                                bounds = layout_bounds({"prov": [location]}, height)
                                if not any(contains(b, bounds) for b, _ in candidates):
                                    candidates.append((bounds, item["label"] == "code"))
                    candidates.sort(key=lambda candidate: candidate[0][1])
                    textpage = page.get_textpage()
                    try:
                        for bounds, labelled in candidates:
                            if not all(math.isfinite(value) for value in bounds):
                                continue
                            bounds = [
                                max(0, bounds[0]),
                                max(0, bounds[1]),
                                min(width, bounds[2]),
                                min(height, bounds[3]),
                            ]
                            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                                continue
                            if any(contains(r.bbox, bounds) for r in regions if r.page == number):
                                continue
                            text = native_code(textpage, bounds, height)
                            origin: Literal["pdf_text", "ocr"] = "pdf_text"
                            crop = image.crop(
                                (
                                    max(0, bounds[0] * sx),
                                    max(0, bounds[1] * sy),
                                    min(image.width, bounds[2] * sx),
                                    min(image.height, bounds[3] * sy),
                                )
                            )
                            if not text.strip():
                                if crop.width < 30 or crop.height < 15:
                                    continue
                                progress(f"正在本地识别第 {number} 页的代码图片")
                                try:
                                    text = recognize_image(crop)
                                    origin = "ocr"
                                except Exception:
                                    if not ocr_error_reported:
                                        notices.append(
                                            Notice(
                                                page=number,
                                                reason=(
                                                    "本地代码 OCR 不可用，图片暂保留；"
                                                    "可在预览中改为代码并手动校对"
                                                ),
                                            )
                                        )
                                        ocr_error_reported = True
                                    continue
                            continuation = any(
                                adjacent_edges(
                                    sources_of(previous)[-1],
                                    CodeSource(page=number, bbox=bounds),
                                    sizes,
                                )
                                and continues_code(previous.text, text)
                                for previous in regions
                                if previous.page == number - 1
                            )
                            if not text.strip() or not (
                                labelled or looks_like_code(text) or continuation
                            ):
                                continue
                            asset = f"code-{len(regions) + 1}.png"
                            crop.save(output / asset)
                            regions.append(
                                Element(
                                    kind="code",
                                    page=number,
                                    text=text,
                                    bbox=bounds,
                                    asset=asset,
                                    language=language_of(text),
                                    code_origin=origin,
                                    code_reviewed=origin == "pdf_text",
                                )
                            )
                            if origin == "ocr":
                                notices.append(
                                    Notice(
                                        page=number,
                                        reason=(
                                            "代码图片已本地 OCR，请逐字核对符号、缩进和拼写"
                                            "并保存校对后发布"
                                        ),
                                    )
                                )
                    finally:
                        textpage.close()
            finally:
                page.close()
    return regions, notices
