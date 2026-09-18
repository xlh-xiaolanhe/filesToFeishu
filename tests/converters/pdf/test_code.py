import os
import sys

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from files_to_feishu.converters.pdf.code import (
    dark_panels,
    extract_code_regions,
    native_code,
    recognize_image,
)
from files_to_feishu.converters.pdf.parser import from_layout, render_pages
from files_to_feishu.models import Element

CODE = "const obj = { width: 10, height: 15 };\nconst area = obj.width * obj.heigth;"


def code_image():
    image = Image.new("RGB", (850, 120), (37, 42, 52))
    draw = ImageDraw.Draw(image)
    draw.multiline_text((20, 20), CODE, fill="white", font=ImageFont.load_default(24), spacing=10)
    return image


def test_native_panel_keeps_newlines_indentation_and_intentional_errors(tmp_path):
    source = tmp_path / "code.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(600, 800))
    pdf.setFillColorRGB(0.15, 0.17, 0.2)
    pdf.rect(50, 580, 500, 150, fill=1)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Courier", 12)
    lines = ["const message = 'hello,world'", "", "if (message) {", "  message.toUperCase()", "}"]
    for i, line in enumerate(lines):
        pdf.drawString(65, 710 - i * 20, line)
    pdf.save()
    render_pages(source, tmp_path)
    with Image.open(tmp_path / "page-1.png") as image:
        panels = dark_panels(image)
    assert len(panels) == 1
    with pdfium.PdfDocument(source) as doc:
        page = doc[0]
        text = page.get_textpage()
        try:
            result = native_code(text, [v / 2 for v in panels[0]], 800)
        finally:
            text.close()
            page.close()
    assert result == "\n".join(lines)


def test_code_regions_replace_split_layout_text_without_duplicates(tmp_path):
    Image.new("RGB", (1200, 1600), "white").save(tmp_path / "page-1.png")
    layout = {"body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]}, "texts": []}
    for i, text in enumerate(["let welcome = 'hello'", "welcome() // intentional error"]):
        layout["texts"].append(
            {
                "self_ref": f"#/texts/{i}",
                "label": "code" if i == 0 else "text",
                "text": text,
                "prov": [
                    {"page_no": 1, "bbox": {"l": 50, "t": 50 + i * 20, "r": 350, "b": 65 + i * 20}}
                ],
            }
        )
    code = "let welcome = 'hello'\nwelcome() // intentional error"
    parsed = from_layout(
        layout,
        [code],
        tmp_path,
        [
            Element(
                kind="code",
                page=1,
                text=code,
                bbox=[45, 45, 400, 100],
                code_origin="pdf_text",
                code_reviewed=True,
            )
        ],
    )
    assert [e.kind for e in parsed.elements] == ["code"]
    assert parsed.elements[0].text == code
    assert not parsed.notices


def test_code_label_takes_precedence_over_formula_heuristic(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    item = {
        "self_ref": "#/texts/0",
        "label": "code",
        "text": "let n = 1",
        "prov": [{"page_no": 1, "bbox": {"l": 20, "t": 20, "r": 200, "b": 50}}],
    }
    result = from_layout({"texts": [item]}, [item["text"]], tmp_path)
    assert result.elements[0].kind == "code"
    assert not result.elements[0].code_reviewed


def test_recovered_snippets_follow_their_own_captions(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    items = []
    # Simulate a layout model grouping captions first, then their code.
    for i, (text, label, top) in enumerate(
        [
            ("Later marker", "text", 220),
            ("Example one", "list_item", 20),
            ("Example two", "list_item", 120),
            ("const a = 1", "code", 50),
            ("const b = 2", "code", 150),
            ("Watermark", "picture", 125),
        ]
    ):
        items.append(
            {
                "self_ref": f"#/texts/{i}",
                "text": text,
                "label": label,
                "prov": [{"page_no": 1, "bbox": {"l": 20, "t": top, "r": 200, "b": top + 15}}],
            }
        )
    regions = [
        Element(kind="code", page=1, text=text, bbox=[15, top - 5, 210, top + 20])
        for text, top in [("const a = 1", 50), ("const b = 2", 150)]
    ]
    result = from_layout({"texts": items}, ["".join(i["text"] for i in items)], tmp_path, regions)
    assert [e.text for e in result.elements if e.kind != "image"] == [
        "Later marker",
        "Example one",
        "const a = 1",
        "Example two",
        "const b = 2",
    ]


def test_true_raster_code_uses_ocr_and_requires_review(tmp_path, monkeypatch):
    source = tmp_path / "raster.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(600, 800))
    pdf.drawString(50, 750, "Raster code example")
    pdf.drawImage(ImageReader(code_image()), 50, 580, width=500, height=100)
    pdf.save()
    render_pages(source, tmp_path)
    monkeypatch.setattr("files_to_feishu.converters.pdf.code.recognize_image", lambda image: CODE)
    regions, notices = extract_code_regions(source, tmp_path, {}, lambda _: None)
    assert len(regions) == 1
    assert regions[0].text == CODE
    assert regions[0].code_origin == "ocr"
    assert regions[0].code_reviewed is False
    assert (tmp_path / regions[0].asset).is_file()
    assert any("OCR" in n.reason for n in notices)
    monkeypatch.setattr(
        "files_to_feishu.converters.pdf.code.recognize_image", lambda image: "Quarterly sales chart"
    )
    assert extract_code_regions(source, tmp_path, {}, lambda _: None)[0] == []


def test_invalid_layout_regions_do_not_prevent_valid_code_extraction(tmp_path):
    source = tmp_path / "bounds.pdf"
    pdf = canvas.Canvas(str(source), pagesize=(600, 800))
    pdf.setFont("Courier", 12)
    pdf.drawString(50, 700, "const n = 1")
    pdf.save()
    render_pages(source, tmp_path)
    layout = {"texts": []}
    for left, top, right, bottom in [
        (650, 20, 700, 40),
        (50, 20, 50, 40),
        (float("nan"), 20, 100, 40),
        (40, 85, 200, 110),
    ]:
        layout["texts"].append(
            {
                "label": "code",
                "prov": [{"page_no": 1, "bbox": {"l": left, "t": top, "r": right, "b": bottom}}],
            }
        )
    regions, _ = extract_code_regions(source, tmp_path, layout, lambda _: None)
    assert [region.text for region in regions] == ["const n = 1"]


@pytest.mark.parser
@pytest.mark.skipif(
    sys.platform != "darwin" or os.getenv("RUN_OCR_TESTS") != "1",
    reason="requires macOS Vision and RUN_OCR_TESTS=1",
)
def test_real_local_ocr_preserves_english_code_tokens():
    text = recognize_image(code_image())
    assert "const" in text
    assert "obj.heigth" in text
    assert "obj.height" not in text
    assert "\n" in text
