from copy import deepcopy

import pytest
from reportlab.pdfgen import canvas

from files_to_feishu.converters.pdf.code import extract_code_regions
from files_to_feishu.converters.pdf.continuation import continues_code, merge_cross_page_code
from files_to_feishu.converters.pdf.parser import from_layout, inspect_pdf, render_pages
from files_to_feishu.models import Element, ParsedDocument

SIZES = {1: (600, 800), 2: (600, 800), 3: (600, 800)}
FIRST = "function greet(name) {\n  const welcome = 'hello ' + name;"
SECOND = "  return welcome;\n}"


def pair():
    return [
        Element(
            kind="code",
            page=1,
            text=FIRST,
            bbox=[50, 680, 550, 760],
            asset="code-1.png",
            language="javascript",
            code_reviewed=True,
        ),
        Element(
            kind="code",
            page=2,
            text=SECOND,
            bbox=[50, 40, 550, 160],
            asset="code-2.png",
            code_reviewed=True,
        ),
    ]


def test_merge_keeps_one_editable_block_with_both_sources_and_review_gate():
    original = pair()
    parsed = merge_cross_page_code(ParsedDocument(pages=2, elements=deepcopy(original)), SIZES)
    assert len(parsed.elements) == 1
    merged = parsed.elements[0]
    assert merged.text == FIRST + "\n" + SECOND
    assert merged.language == "javascript"
    assert not merged.code_reviewed
    assert [(s.page, s.asset, s.bbox) for s in merged.code_sources] == [
        (e.page, e.asset, e.bbox) for e in original
    ]
    assert "1–2" in parsed.notices[0].reason
    assert ParsedDocument.model_validate_json(parsed.model_dump_json()) == parsed
    assert merge_cross_page_code(parsed, SIZES) == parsed  # No repeated joining on re-read.


@pytest.mark.parametrize(
    "changes",
    [
        {"page": 3},
        {"page": 1},
        {"bbox": [50, 350, 550, 430]},
        {"bbox": [250, 40, 590, 160]},
        {"bbox": []},
        {"text": "function other() {\n  return 1;\n}"},
        {"text": "  return welcome;\n]"},
        {"language": "typescript"},
        {"text": "x" * 20000 + "\n}"},
    ],
)
def test_independent_or_uncertain_regions_are_not_merged(changes):
    elements = pair()
    elements[1] = elements[1].model_copy(update=changes)
    result = merge_cross_page_code(ParsedDocument(pages=3, elements=elements), SIZES)
    assert len(result.elements) == 2
    assert not result.notices


@pytest.mark.parametrize("kind", ["heading", "text", "image", "table"])
def test_intervening_body_content_prevents_merge(kind):
    first, second = pair()
    intervening = Element(kind=kind, page=2, text="Another example", bbox=[50, 10, 550, 30])
    parsed = ParsedDocument(pages=2, elements=[first, intervening, second])
    assert len(merge_cross_page_code(parsed, SIZES).elements) == 3


def test_page_furniture_is_preserved_outside_merged_code():
    first, second = pair()
    footer = Element(kind="text", page=1, text="1", bbox=[290, 778, 305, 790])
    header = Element(kind="text", page=2, text="Course title", bbox=[50, 10, 300, 20])
    parsed = ParsedDocument(pages=2, elements=[first, footer, header, second])
    result = merge_cross_page_code(parsed, SIZES, {id(header)})
    assert [e.text for e in result.elements] == [FIRST + "\n" + SECOND, "1", "Course title"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("const x = '{'", "}", False),
        ("// function f() {", "}", False),
        ("const x = 1; /* { */", "}", False),
        ("function f() {\n  const x = '}';", "  return x;\n}", True),
        ("function f() { // ignore }", "  return 1;\n}", True),
        ("const regex = /{/;", "}", False),
        ("const x = `hello ${", "name}`", False),
        ("function f() {\n  /* split", "comment */\n}", False),
        ("function f() {\n  const x = 'split", "string';\n}", False),
        ("const done = 1", "const next = 2", False),
    ],
)
def test_syntax_continuity_does_not_count_strings_comments_or_guess(first, second, expected):
    assert continues_code(first, second) is expected


def test_three_page_chain_preserves_exact_text_and_order():
    first, middle = pair()
    first.text = "function outer() {\n  if (ready) {"
    middle.text = "    work();\n  }\n  if (next) {"
    middle.bbox[3] = 760
    last = Element(kind="code", page=3, text="    finish();\n  }\n}", bbox=[50, 40, 550, 160])
    expected = "\n".join(e.text for e in [first, middle, last])
    parsed = merge_cross_page_code(ParsedDocument(pages=3, elements=[first, middle, last]), SIZES)
    assert len(parsed.elements) == 1
    assert parsed.elements[0].text == expected
    assert [s.page for s in parsed.elements[0].code_sources] == [1, 2, 3]


def make_split_code_pdf(source):
    pdf = canvas.Canvas(str(source), pagesize=(600, 800))
    for page, text, panel_y, text_y in [(1, FIRST, 40, 100), (2, SECOND, 640, 740)]:
        pdf.setFillColorRGB(0.15, 0.17, 0.2)
        pdf.rect(50, panel_y, 500, 80 if page == 1 else 120, fill=1)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont("Courier", 12)
        for line, value in enumerate(text.split("\n")):
            pdf.drawString(65, text_y - line * 20, value)
        pdf.showPage()
    pdf.save()


def test_real_pdf_tail_without_keyword_is_recovered_and_merged(tmp_path):
    source = tmp_path / "split.pdf"
    make_split_code_pdf(source)
    texts = inspect_pdf(source, 20_000_000, 100)
    render_pages(source, tmp_path)
    regions, _ = extract_code_regions(source, tmp_path, {}, lambda _: None)
    assert [e.text for e in regions] == [FIRST, SECOND]
    parsed = from_layout({}, texts, tmp_path, regions)
    assert len(parsed.elements) == 1
    assert parsed.elements[0].text == FIRST + "\n" + SECOND
    assert len(parsed.elements[0].code_sources) == 2
    assert all((tmp_path / s.asset).is_file() for s in parsed.elements[0].code_sources)
    assert not any("覆盖不足" in n.reason for n in parsed.notices)


def test_docling_multipage_code_provenance_does_not_duplicate_text(tmp_path):
    source = tmp_path / "split.pdf"
    make_split_code_pdf(source)
    render_pages(source, tmp_path)
    layout = {
        "texts": [
            {
                "self_ref": "#/texts/0",
                "label": "code",
                "text": FIRST + "\n" + SECOND,
                "prov": [
                    {"page_no": 1, "bbox": {"l": 50, "t": 680, "r": 550, "b": 760}},
                    {"page_no": 2, "bbox": {"l": 50, "t": 40, "r": 550, "b": 160}},
                ],
            }
        ]
    }
    regions, _ = extract_code_regions(source, tmp_path, layout, lambda _: None)
    parsed = from_layout(layout, [FIRST, SECOND], tmp_path, regions)
    assert [e.text for e in parsed.elements] == [FIRST + "\n" + SECOND]
    assert not any("整页图片" in n.reason for n in parsed.notices)
