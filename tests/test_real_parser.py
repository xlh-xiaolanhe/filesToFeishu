import os
from pathlib import Path

import pytest

from files_to_feishu.parser import DoclingParser
from tests.samples import make_sample


@pytest.mark.parser
@pytest.mark.skipif(
    os.environ.get("RUN_PARSER_TESTS") != "1", reason="设置 RUN_PARSER_TESTS=1 运行真实模型测试"
)
def test_representative_pdf_offline(tmp_path, monkeypatch):
    source = tmp_path / "sample.pdf"
    make_sample(source)

    def no_network(*args, **kwargs):
        raise AssertionError("PDF 解析阶段禁止访问网络")

    monkeypatch.setattr("socket.socket.connect", no_network)
    parsed = DoclingParser(Path(".models"), 20 * 1024 * 1024, 100)(
        source, tmp_path / "assets", print
    )
    assert parsed.pages == 2
    text = " ".join(e.text for e in parsed.elements if e.kind != "image")
    assert "This paragraph must remain editable and unchanged." in text
    assert "中文转换测试" in text
    assert any(e.kind == "heading" for e in parsed.elements)
    assert any(e.kind in {"bullet", "ordered"} for e in parsed.elements)
    assert any(
        e.rows == [["Item", "Quantity"], ["Apples", "12"], ["Pears", "24"]] for e in parsed.elements
    )
    assert any(e.kind == "image" and e.page == 1 for e in parsed.elements)
    assert any(n.page == 2 and "表格" in n.reason for n in parsed.notices)
    assert any(n.page == 2 and "公式" in n.reason for n in parsed.notices)
    for element in parsed.elements:
        if element.kind == "image":
            assert (tmp_path / "assets" / element.asset).stat().st_size > 0
