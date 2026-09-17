import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader

from .models import Element, Notice, ParsedDocument, UserError


def inspect_pdf(source: Path, max_bytes: int, max_pages: int) -> list[str]:
    if source.stat().st_size > max_bytes:
        raise UserError("文件超过 20 MB 限制。")
    with source.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise UserError("文件不是有效 PDF。")
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise UserError("首版不支持加密 PDF，请先提供未加密文件。")
        if not 0 < len(reader.pages) <= max_pages:
            raise UserError("PDF 必须包含 1 至 100 页。")
        texts = [page.extract_text() or "" for page in reader.pages]
    except UserError:
        raise
    except Exception as exc:
        raise UserError("PDF 无法读取，文件可能已损坏。") from exc
    if not any(text.strip() for text in texts):
        raise UserError("未检测到文字层；首版不支持扫描型 PDF 或空白文件。")
    return texts


def render_pages(source: Path, output: Path) -> list[str]:
    output.mkdir(parents=True, exist_ok=True)
    names = []
    with pdfium.PdfDocument(source) as pdf:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                w, h = page.get_size()
                if w * h * 4 > 30_000_000:
                    raise UserError(f"第 {i + 1} 页尺寸过大，无法安全生成预览。")
                bitmap = page.render(scale=2)
                try:
                    name = f"page-{i + 1}.png"
                    bitmap.to_pil().save(output / name)
                    names.append(name)
                finally:
                    bitmap.close()
            finally:
                page.close()
    return names


def _plain(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


def from_layout(layout: dict[str, Any], texts: list[str], output: Path) -> ParsedDocument:
    """Map Docling's public JSON document format to our preview/publish representation."""
    elements: list[Element] = []
    notices: list[Notice] = []
    seen: set[str] = set()
    fallback_pages: dict[int, str] = {}
    assets = 0
    covered: dict[int, str] = {}

    def resolve(ref):
        node: Any = layout
        for key in ref.removeprefix("#/").split("/"):
            node = node[int(key)] if isinstance(node, list) else node[key]
        return node

    def walk(item):
        nonlocal assets
        ref = item.get("self_ref", "")
        if ref and ref in seen:
            return
        seen.add(ref)
        label = item.get("label", "")
        if "prov" not in item:
            for child in item.get("children", []):
                walk(resolve(child["$ref"]))
            return
        provenance = item.get("prov", [])
        if not provenance:
            raise UserError("解析结果包含无法定位来源的内容，已停止转换。")
        page = provenance[0]["page_no"]
        if page < 1 or page > len(texts):
            raise UserError("解析结果的页码无效。")
        box = provenance[0].get("bbox", {})
        image_path = output / f"page-{page}.png"
        with Image.open(image_path) as image:
            size = layout.get("pages", {}).get(str(page), {}).get("size", {})
            width = size.get("width", image.width / 2)
            height = size.get("height", image.height / 2)
            top, bottom = box.get("t", 0), box.get("b", height)
            if box.get("coord_origin", "TOPLEFT") == "BOTTOMLEFT":
                top, bottom = height - top, height - bottom
            bounds = [box.get("l", 0), min(top, bottom), box.get("r", width), max(top, bottom)]
            bounds = [
                max(0, bounds[0]),
                max(0, bounds[1]),
                min(width, bounds[2]),
                min(height, bounds[3]),
            ]
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                fallback_pages[page] = "区域坐标无效，已保留整页图片"
                return
            element = Element(kind="text", page=page, text=item.get("text", ""), bbox=bounds)
            reason = ""
            if len({p["page_no"] for p in provenance}) > 1:
                for p in provenance:
                    fallback_pages[p["page_no"]] = "跨页复杂区域，已保留整页图片"
                return
            if label in {"title", "section_header"}:
                element.kind = "heading"
                element.level = min(max(int(item.get("level", 1)), 1), 6)
            elif label == "list_item":
                element.kind = "ordered" if item.get("enumerated") else "bullet"
            elif label == "table":
                data = item.get("data", {})
                cells = data.get("table_cells", [])
                rows, cols = data.get("num_rows", 0), data.get("num_cols", 0)
                merged = any(c.get("row_span", 1) > 1 or c.get("col_span", 1) > 1 for c in cells)
                if not rows or not cols or rows > 50 or cols > 9 or merged:
                    reason = "复杂表格或超出首版可编辑表格范围，已保留区域图片"
                else:
                    grid = [[""] * cols for _ in range(rows)]
                    try:
                        for cell in cells:
                            grid[cell["start_row_offset_idx"]][cell["start_col_offset_idx"]] = cell[
                                "text"
                            ]
                    except (KeyError, IndexError):
                        reason = "表格结构不完整，已保留区域图片"
                    if not reason:
                        element.kind = "table"
                        element.text = ""
                        element.rows = grid
            elif label == "picture":
                element.kind = "image"
            elif label == "formula":
                reason = "公式以图片保留"
            elif not element.text.strip():
                reason = "无法可靠转换的区域，已保留图片"

            if reason or element.kind == "image":
                element.kind = "image"
                assets += 1
                name = f"figure-{assets}.png"
                scale_x, scale_y = image.width / width, image.height / height
                crop = (
                    bounds[0] * scale_x,
                    bounds[1] * scale_y,
                    bounds[2] * scale_x,
                    bounds[3] * scale_y,
                )
                image.crop(crop).save(output / name)
                element.asset = name
                element.text = reason or "原文图片"
                if reason:
                    notices.append(Notice(page=page, reason=reason))
            covered[page] = covered.get(page, "") + item.get("text", "")
            if label == "table":
                covered[page] += "".join(
                    c.get("text", "") for c in item.get("data", {}).get("table_cells", [])
                )
            elements.append(element)
        # Table/picture children are represented by the enclosing grid or crop.
        if label not in {"table", "picture"}:
            for child in item.get("children", []):
                walk(resolve(child["$ref"]))

    walk(layout.get("body", {}))
    walk(layout.get("furniture", {}))
    # Keep headers/footers and any other items outside the body tree.
    for group in ("texts", "tables", "pictures", "key_value_items", "form_items"):
        for item in layout.get(group, []):
            if item.get("self_ref") not in seen:
                walk(item)

    result: list[Element] = []
    for page, native in enumerate(texts, 1):
        page_elements = [e for e in elements if e.page == page]
        # Compare native characters, including text inside cropped regions. This is
        # a loss detector, not a claim of semantic or layout equivalence.
        extracted = covered.get(page, "")
        expected = Counter(_plain(native))
        missing = sum((expected - Counter(_plain(extracted))).values())
        if not native.strip():
            fallback_pages[page] = "该页无可提取文字，已保留整页图片"
        elif not page_elements or missing > max(2, len(_plain(native)) * 0.02):
            fallback_pages[page] = "检测到文字覆盖不足，已保留整页图片，请核对"
        if page in fallback_pages:
            reason = fallback_pages[page]
            notices.append(Notice(page=page, reason=reason))
            result.append(Element(kind="image", page=page, asset=f"page-{page}.png", text=reason))
        else:
            result.extend(page_elements)
    return ParsedDocument(
        pages=len(texts),
        elements=result,
        notices=notices,
        page_images=[f"page-{p}.png" for p in range(1, len(texts) + 1)],
    )


class DoclingParser:
    def __init__(self, artifacts: Path, max_bytes: int, max_pages: int):
        self.artifacts = artifacts
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.converter: Any = None

    def __call__(self, source: Path, output: Path, progress: Callable[[str], None]):
        texts = inspect_pdf(source, self.max_bytes, self.max_pages)
        progress("正在生成原文预览")
        render_pages(source, output)
        if not (self.artifacts / ".ready").is_file():
            raise UserError(
                "本地模型未准备。请运行 uv run --extra parser python scripts/download_models.py"
            )
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise UserError("请先运行 uv sync --extra parser 安装本地解析引擎。") from exc
        progress("正在识别文档结构、文字和表格（本地运行）")
        if self.converter is None:
            options = PdfPipelineOptions(
                artifacts_path=self.artifacts.resolve(),
                do_ocr=False,
                do_table_structure=True,
                enable_remote_services=False,
                do_picture_description=False,
                do_picture_classification=False,
                do_code_enrichment=False,
                do_formula_enrichment=False,
            )
            self.converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
        converted = self.converter.convert(source)
        if str(converted.status.value) != "success":
            raise UserError("解析引擎未完整完成转换，请检查文件后重试。")
        progress("正在核对内容并整理预览")
        return from_layout(converted.document.export_to_dict(), texts, output)
