import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from files_to_feishu.converters.pdf.parser import from_layout, inspect_pdf, render_pages
from files_to_feishu.models import UserError


def test_headings_without_explicit_levels_keep_chapter_section_and_detail_hierarchy(tmp_path):
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    headings = [
        ("七、常用类型与语法", 24),
        ("9. 一个特殊情况", 18),
        ("代码段1（正常）", 14),
        ("代码段2（特殊）", 14),
        ("为什么会这样？", 14),
        ("10. 复习类相关知识", 18),
    ]
    items = [
        {
            "self_ref": f"#/texts/{i}",
            "label": "section_header",
            "text": text,
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {"l": 20, "t": 20 + i * 50, "r": 260, "b": 20 + i * 50 + size},
                }
            ],
        }
        for i, (text, size) in enumerate(headings)
    ]
    parsed = from_layout({"texts": items}, [" ".join(t for t, _ in headings)], tmp_path)
    assert [e.level for e in parsed.elements] == [1, 2, 3, 3, 3, 2]


def test_layout_does_not_publish_displaced_list_marker_or_page_number(tmp_path):
    # PDF reading order can put a left-hand list marker after the sentence.
    Image.new("RGB", (600, 800), "white").save(tmp_path / "page-1.png")
    items = [
        {
            "label": "list_item",
            "text": "JavaScript 代码量很少。 ●",
            "marker": "●",
            "bbox": {"l": 30, "t": 180, "r": 260, "b": 220},
        },
        {"label": "page_footer", "text": "1", "bbox": {"l": 270, "t": 386, "r": 280, "b": 392}},
    ]
    texts = [
        {
            "self_ref": f"#/texts/{i}",
            "label": item["label"],
            "text": item["text"],
            "marker": item.get("marker", ""),
            "prov": [{"page_no": 1, "bbox": item["bbox"]}],
        }
        for i, item in enumerate(items)
    ]
    layout = {"body": {"children": [{"$ref": t["self_ref"]} for t in texts]}, "texts": texts}
    parsed = from_layout(layout, ["● JavaScript 代码量很少。 1"], tmp_path)
    assert [(e.kind, e.text) for e in parsed.elements] == [("bullet", "JavaScript 代码量很少。")]
    assert not parsed.notices


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
