from files_to_feishu.models import Element, ParsedDocument, SourceMetadata, TextRun


def test_article_round_trip_preserves_structure_without_pdf_pages():
    article = ParsedDocument(
        source_kind="wechat",
        metadata=SourceMetadata(url="https://mp.weixin.qq.com/s/example", title="Original"),
        elements=[
            Element(kind="ordered", text="Parent", list_start=3),
            Element(
                kind="bullet",
                text="Child link",
                list_depth=1,
                runs=[
                    TextRun(text="Child ", bold=True),
                    TextRun(text="link", link="https://example.com", italic=True),
                ],
            ),
            Element(kind="code", text="if (ok) {\n  act();\n}", code_origin="html"),
        ],
    )
    restored = ParsedDocument.model_validate_json(article.model_dump_json())
    assert restored == article
    assert restored.pages is None
    assert all(e.page is None for e in restored.elements)
    assert restored.page_images == []
    assert restored.elements[1].runs[1].link == "https://example.com"


def test_new_mutable_model_fields_are_independent():
    first = ParsedDocument(elements=[Element(kind="text")])
    second = ParsedDocument(elements=[Element(kind="text")])
    first.metadata.title = "Changed"
    first.elements[0].runs.append(TextRun(text="New"))
    assert second.metadata.title == ""
    assert second.elements[0].runs == []
