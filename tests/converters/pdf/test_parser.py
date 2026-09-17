import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from files_to_feishu.converters.pdf.parser import from_layout, inspect_pdf, render_pages
from files_to_feishu.models import UserError


def test_missing_text_is_reported_even_when_page_contains_an_image(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    layout = {
        "body": {"children": [{"$ref": "#/pictures/0"}]},
        "pictures": [
            {
                "self_ref": "#/pictures/0",
                "label": "picture",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 0, "t": 0, "r": 100, "b": 100, "coord_origin": "TOPLEFT"},
                    }
                ],
            }
        ],
    }
    result = from_layout(layout, ["This entire paragraph must not disappear."], tmp_path)
    assert result.elements[0].asset == "page-1.png"
    assert any("覆盖不足" in notice.reason for notice in result.notices)


def test_pdf_validation_and_preview(tmp_path, pdf_bytes):
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes)
    assert "Editable PDF content." in inspect_pdf(source, 1024 * 1024, 100)[0]
    images = render_pages(source, tmp_path / "assets")
    with Image.open(tmp_path / "assets" / images[0]) as image:
        assert image.width > 1000
    with pytest.raises(UserError, match="20 MB"):
        inspect_pdf(source, 1, 100)


def test_rejects_encrypted_scanned_and_corrupt_files(tmp_path, pdf_bytes):
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes)
    writer = PdfWriter()
    writer.append(PdfReader(source))
    writer.encrypt("secret")
    writer.write(source)
    with pytest.raises(UserError, match="加密"):
        inspect_pdf(source, 1024 * 1024, 100)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.write(source)
    with pytest.raises(UserError, match="文字层"):
        inspect_pdf(source, 1024 * 1024, 100)
    source.write_bytes(b"not a PDF")
    with pytest.raises(UserError, match="有效 PDF"):
        inspect_pdf(source, 1024 * 1024, 100)


def test_table_children_are_not_duplicated_as_paragraphs(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    prov = [{"page_no": 1, "bbox": {"l": 0, "t": 0, "r": 100, "b": 100}}]
    layout = {
        "body": {"children": [{"$ref": "#/tables/0"}]},
        "tables": [
            {
                "self_ref": "#/tables/0",
                "label": "table",
                "prov": prov,
                "children": [{"$ref": "#/texts/0"}],
                "data": {
                    "num_rows": 1,
                    "num_cols": 1,
                    "table_cells": [
                        {"start_row_offset_idx": 0, "start_col_offset_idx": 0, "text": "Cell"}
                    ],
                },
            }
        ],
        "texts": [{"self_ref": "#/texts/0", "label": "text", "text": "Cell", "prov": prov}],
    }
    parsed = from_layout(layout, ["Cell"], tmp_path)
    assert len(parsed.elements) == 1
    assert parsed.elements[0].rows == [["Cell"]]
