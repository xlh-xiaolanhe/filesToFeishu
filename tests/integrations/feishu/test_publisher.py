import copy
import hashlib

import pytest

from files_to_feishu.integrations.feishu import Publisher
from files_to_feishu.models import (
    Asset,
    Element,
    ParsedDocument,
    Target,
    TextRun,
    UncertainWrite,
    UserError,
)


class MemoryFeishu:
    """Stateful external-service double; checks the publisher's actual remote result."""

    def __init__(self):
        self.data = {}
        self.files = {}
        self.created = 0
        self.sequence = 0
        self.target = None
        self.fail_upload = False
        self.lost_response = False
        self.parent = "parent"
        self.blank_cells = False

    def identifier(self):
        self.sequence += 1
        return f"b{self.sequence}"

    def request(self, method, path, **kwargs):
        body = kwargs.get("json", {})
        if method == "POST" and path == "/docx/v1/documents":
            self.created += 1
            doc = f"doc{self.created}"
            self.data[doc] = {"block_id": doc, "children": [], "block_type": 1}
            if self.lost_response:
                raise UncertainWrite("响应丢失")
            return {"document": {"document_id": doc}}
        if path.endswith("/children"):
            parent = path.split("/")[-2]
            children = []
            for item in body["children"]:
                block = copy.deepcopy(item)
                block.update(block_id=self.identifier(), parent_id=parent)
                if block["block_type"] == 31:
                    shape = block["table"]["property"]
                    cells = []
                    for _ in range(shape["row_size"] * shape["column_size"]):
                        cell = {
                            "block_id": self.identifier(),
                            "block_type": 32,
                            "parent_id": block["block_id"],
                            "children": [],
                        }
                        self.data[cell["block_id"]] = cell
                        if self.blank_cells:
                            blank = {
                                "block_id": self.identifier(),
                                "block_type": 2,
                                "parent_id": cell["block_id"],
                                "text": {"elements": [{"text_run": {"content": ""}}]},
                            }
                            self.data[blank["block_id"]] = blank
                            cell["children"].append(blank["block_id"])
                        cells.append(cell["block_id"])
                    block["table"]["cells"] = cells
                    block["children"] = cells
                if block["block_type"] == 23:
                    inner = copy.deepcopy(block)
                    inner["block_id"] = self.identifier()
                    inner["parent_id"] = block["block_id"]
                    self.data[inner["block_id"]] = inner
                    block = {
                        "block_id": block["block_id"],
                        "block_type": 33,
                        "children": [inner["block_id"]],
                        "parent_id": parent,
                    }
                self.data[block["block_id"]] = block
                self.data[parent].setdefault("children", []).append(block["block_id"])
                children.append(copy.deepcopy(block))
            return {"children": children}
        if method == "PATCH":
            block = self.data[path.split("/")[-1]]
            for field, value in body.items():
                if field == "update_text_elements":
                    block["text"]["elements"] = copy.deepcopy(value["elements"])
                else:
                    block[field.removeprefix("replace_")] = value
            return {"block": block}
        if path.endswith("move_docs_to_wiki"):
            self.target = body["obj_token"]
            return {"wiki_token": "child"}
        raise AssertionError((method, path, kwargs))

    def upload(self, block_id, source, kind, filename=""):
        if self.fail_upload:
            raise UserError("素材上传失败")
        assert self.data[block_id]["block_type"] == (27 if kind == "image" else 23)
        token = self.identifier()
        self.files[token] = source.read_bytes()
        return {"file_token": token}

    def download_digest(self, token):
        return hashlib.sha256(self.files[token]).hexdigest()

    def blocks(self, doc):
        return copy.deepcopy(list(self.data.values()))

    def block(self, doc, block_id):
        return copy.deepcopy(self.data[block_id])

    def node(self, token, obj_type="wiki"):
        return {
            "node_token": "child",
            "space_id": "space",
            "parent_node_token": self.parent,
            "obj_token": self.target,
        }

    def resolve(self, url):
        return Target(
            space_id="space", node_token="parent", title="Test parent", host="test.feishu.cn"
        )

    def close(self):
        pass


@pytest.fixture
def publishing(tmp_path, pdf_bytes):
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes)
    (tmp_path / "figure-1.png").write_bytes(b"fixture-image")
    parsed = ParsedDocument(
        pages=1,
        elements=[
            Element(kind="heading", page=1, text="Known heading"),
            Element(kind="text", page=1, text="Known editable text"),
            Element(kind="table", page=1, rows=[["Name", "Value"], ["A", "42"]]),
            Element(kind="image", page=1, asset="figure-1.png"),
        ],
    )
    args = (
        parsed,
        source,
        tmp_path,
        "Test",
        MemoryFeishu().resolve(""),
        hashlib.sha256(pdf_bytes).hexdigest(),
        "source.pdf",
    )
    client = MemoryFeishu()
    journal = {}

    def save(value):
        journal.clear()
        journal.update(copy.deepcopy(value))

    def publisher():
        return Publisher(client, journal, save, lambda *a, **kw: None)

    return client, journal, publisher, args


def test_full_publish_preserves_editable_content_media_attachment_and_parent(publishing):
    client, journal, publisher, args = publishing
    result = publisher().publish(*args)
    assert result["url"] == "https://test.feishu.cn/wiki/child"
    assert client.created == 1
    assert journal["verified"]["state"] == "done"
    assert any(b.get("table", {}).get("cells") for b in client.data.values())
    assert args[1].read_bytes() in client.files.values()
    assert publisher().publish(*args) == result
    assert client.created == 1


def test_code_is_one_native_block_with_exact_text_and_verified_language(publishing):
    from files_to_feishu.integrations.feishu.publisher import block_text

    client, journal, publisher, args = publishing
    text = "const obj = { height: 15 };\n  obj.heigth;\n" * 70
    args[0].elements = [
        Element(kind="code", page=1, text=text, language="javascript", code_reviewed=True)
    ]
    publisher().publish(*args)
    blocks = [b for b in client.data.values() if b.get("block_type") == 14]
    assert len(blocks) == 1
    block = blocks[0]
    assert block_text(block) == text
    assert block["code"]["style"]["language"] == 30
    assert not any(b.get("block_type") == 27 for b in client.data.values())
    block["block_type"] = 2
    with pytest.raises(UserError, match="代码块类型"):
        publisher().verify("doc1", **journal["verified"]["result"])
    block["block_type"] = 14
    block["code"]["style"]["language"] = 63
    with pytest.raises(UserError, match="代码块类型或语言"):
        publisher().verify("doc1", **journal["verified"]["result"])
    block["code"]["style"]["language"] = 30
    block["code"]["elements"][0]["text_run"]["content"] = "lost indentation"
    with pytest.raises(UserError, match="远端文字"):
        publisher().verify("doc1", **journal["verified"]["result"])


def test_unreviewed_code_is_rejected_before_remote_creation(publishing):
    client, journal, publisher, args = publishing
    args[0].elements = [Element(kind="code", page=1, text="let n = 1", code_origin="ocr")]
    with pytest.raises(UserError, match="校对"):
        publisher().publish(*args)
    assert client.created == 0
    assert journal == {}


def test_cross_page_code_publishes_once_without_reference_images_or_page_breaks(publishing):
    from files_to_feishu.converters.pdf.continuation import merge_cross_page_code
    from files_to_feishu.integrations.feishu.publisher import block_text
    from tests.converters.pdf.test_continuation import FIRST, SECOND, SIZES, pair

    client, _, publisher, args = publishing
    parsed = merge_cross_page_code(ParsedDocument(pages=2, elements=pair()), SIZES)
    args[0].elements = parsed.elements
    with pytest.raises(UserError, match="校对"):
        publisher().publish(*args)
    assert client.created == 0
    parsed.elements[0].code_reviewed = True
    publisher().publish(*args)
    blocks = [client.data[key] for key in client.data["doc1"]["children"]]
    assert [block["block_type"] for block in blocks] == [14, 33]
    assert block_text(blocks[0]) == FIRST + "\n" + SECOND
    assert len(client.files) == 1  # Original PDF only; neither source crop is uploaded.


def test_known_upload_failure_resumes_without_duplicate_blocks(publishing):
    client, journal, publisher, args = publishing
    client.fail_upload = True
    with pytest.raises(UserError, match="素材上传失败"):
        publisher().publish(*args)
    assert "move" not in journal
    blocks = len(client.data)
    client.fail_upload = False
    publisher().publish(*args)
    assert client.created == 1
    assert len(client.data) == blocks + 2  # Original PDF wrapper and file.


@pytest.mark.parametrize("blank_cells", [False, True])
def test_lost_table_cell_response_resumes_without_duplicate_text(publishing, blank_cells):
    client, journal, publisher, args = publishing
    client.blank_cells = blank_cells
    args[0].elements = [
        Element(
            kind="table",
            page=1,
            rows=[[f"cell-{row * 3 + column}" for column in range(3)] for row in range(3)],
        )
    ]
    original = client.request
    dropped = False

    def request(method, path, **kwargs):
        nonlocal dropped
        result = original(method, path, **kwargs)
        if not dropped and "cell-8" in str(kwargs.get("json", {})):
            dropped = True
            raise UncertainWrite("飞书写入响应丢失，操作可能已生效，已停止自动重试。")
        return result

    client.request = request
    with pytest.raises(UncertainWrite, match="响应丢失"):
        publisher().publish(*args)
    assert journal["element-0:cell-8"]["state"] == "pending"
    client.request = original
    assert publisher().publish(*args)["wiki_token"] == "child"
    written = [block for block in client.data.values() if "cell-8" in str(block.get("text", {}))]
    assert len(written) == 1
    assert journal["element-0:cell-8"]["state"] == "done"


@pytest.mark.parametrize("lose_repair_response", [False, True])
def test_pending_empty_cell_is_filled_in_place_and_repair_can_resume(
    publishing, lose_repair_response
):
    from files_to_feishu.integrations.feishu.publisher import block_text

    client, journal, publisher, args = publishing
    client.blank_cells = True
    args[0].elements = [Element(kind="table", page=1, rows=[["expected cell text"]])]
    original = client.request

    def fail_before_write(method, path, **kwargs):
        if "expected cell text" in str(kwargs.get("json", {})):
            raise UncertainWrite("connection lost before text arrived")
        return original(method, path, **kwargs)

    client.request = fail_before_write
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    table = journal["element-0"]["result"]["children"][0]
    cell_id = table["table"]["cells"][0]
    placeholder_id = client.data[cell_id]["children"][0]
    client.request = original
    if lose_repair_response:

        def fail_after_patch(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if method == "PATCH" and "update_text_elements" in kwargs.get("json", {}):
                raise UncertainWrite("response lost after filling placeholder")
            return result

        client.request = fail_after_patch
        with pytest.raises(UncertainWrite, match="after filling"):
            publisher().publish(*args)
        assert journal["element-0:cell-0"]["state"] == "pending"
        client.request = original
    assert publisher().publish(*args)["wiki_token"] == "child"
    assert client.data[cell_id]["children"] == [placeholder_id]
    assert block_text(client.data[placeholder_id]) == "expected cell text"
    assert journal["element-0:cell-0"]["result"]["children"][0]["block_id"] == placeholder_id


@pytest.mark.parametrize(
    "replacement",
    [
        [{"text_run": {"content": "User changed this"}}],
        [{"mention_user": {"user_id": "user-reference"}}],
    ],
)
def test_pending_cell_with_different_content_is_never_overwritten(publishing, replacement):
    client, journal, publisher, args = publishing
    args[0].elements = [Element(kind="table", page=1, rows=[["expected"]])]
    original = client.request

    def lost(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if "expected" in str(kwargs.get("json", {})):
            raise UncertainWrite("lost response")
        return result

    client.request = lost
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    table = journal["element-0"]["result"]["children"][0]
    cell = client.data[table["table"]["cells"][0]]
    block = client.data[cell["children"][0]]
    block["text"]["elements"] = replacement
    before = copy.deepcopy(client.data)
    client.request = original
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    assert client.data == before
    assert journal["element-0:cell-0"]["state"] == "pending"


def test_verify_detects_duplicate_cell_text_even_with_original_blocks_intact(publishing):
    client, journal, publisher, args = publishing
    publisher().publish(*args)
    cell = next(block for block in client.data.values() if block["block_type"] == 32)
    duplicate = copy.deepcopy(client.data[cell["children"][0]])
    duplicate["block_id"] = client.identifier()
    client.data[duplicate["block_id"]] = duplicate
    cell["children"].append(duplicate["block_id"])
    with pytest.raises(UserError, match="单元格"):
        publisher().verify("doc1", **journal["verified"]["result"])


def test_native_heading_levels_are_published_and_verified(publishing):
    client, journal, publisher, args = publishing
    args[0].elements = [
        Element(kind="heading", page=1, text="Chapter", level=1),
        Element(kind="heading", page=1, text="Section", level=2),
        Element(kind="heading", page=2, text="Code example", level=3),
    ]
    publisher().publish(*args)
    roots = journal["verified"]["result"]["roots"]
    assert [client.data[block]["block_type"] for block in roots[:3]] == [3, 4, 5]
    client.data[roots[2]]["block_type"] = 3
    with pytest.raises(UserError, match="标题等级"):
        publisher().verify("doc1", **journal["verified"]["result"])


def test_lost_write_response_is_never_repeated(publishing):
    client, journal, publisher, args = publishing
    client.lost_response = True
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    client.lost_response = False
    with pytest.raises(UncertainWrite, match="不确定"):
        publisher().publish(*args)
    assert client.created == 1
    assert journal["document"]["state"] == "pending"


def test_remote_content_change_and_wrong_parent_are_not_success(publishing, monkeypatch):
    client, journal, publisher, args = publishing
    publisher().publish(*args)
    client.parent = "wrong"
    monkeypatch.setattr("files_to_feishu.integrations.feishu.publisher.time.sleep", lambda _: None)
    with pytest.raises(UserError, match="归档尚未确认"):
        publisher().publish(*args)
    for block in client.data.values():
        if "text" in block:
            block["text"]["elements"][0]["text_run"]["content"] = "changed"
            break
    with pytest.raises(UserError, match="核对失败"):
        publisher().verify("doc1", **journal["verified"]["result"])


@pytest.mark.parametrize("lost_step", ["bind", "move"])
def test_known_object_is_reconciled_after_lost_write_response(publishing, lost_step):
    client, journal, publisher, args = publishing
    original = client.request
    lost = []

    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        matches = method == "PATCH" if lost_step == "bind" else path.endswith("move_docs_to_wiki")
        if matches and not lost:
            lost.append(path)
            raise UncertainWrite("response lost after write")
        return result

    client.request = request
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    client.request = original
    assert publisher().publish(*args)["wiki_token"] == "child"
    assert client.created == 1


def test_attachment_is_verified_again_after_archiving(publishing):
    client, journal, publisher, args = publishing
    original = client.download_digest

    def download(token):
        if client.target:
            raise UserError("归档后无读取权限")
        return original(token)

    client.download_digest = download
    with pytest.raises(UserError, match="归档后"):
        publisher().publish(*args)
    assert journal["move"]["state"] == "done"


def test_wechat_rich_text_quote_table_and_original_gif_zip_are_preserved(publishing, tmp_path):
    client, journal, publisher, args = publishing
    gif = b"GIF89a-original-animation-two-frames"
    (tmp_path / "figure.gif").write_bytes(gif)
    archive = tmp_path / "article.zip"
    archive.write_bytes(b"PK-offline-article-and-original-materials")
    runs = [
        TextRun(text="Bold", bold=True),
        TextRun(text=" emphasis", italic=True, strike=True),
        TextRun(text=" code", inline_code=True, link="https://example.org/reference?q=1#code"),
    ]
    text = "".join(run.text for run in runs)
    parsed = ParsedDocument(
        source_kind="wechat",
        assets=[
            Asset(name="figure.gif", media_type="image/gif", digest=hashlib.sha256(gif).hexdigest())
        ],
        elements=[
            Element(kind="heading", text="Article", level=2),
            Element(kind="text", text=text, runs=runs),
            Element(kind="quote", text="Quoted", runs=[TextRun(text="Quoted", italic=True)]),
            Element(kind="table", rows=[[text]], table_runs=[[runs]]),
            Element(kind="image", asset="figure.gif"),
        ],
    )
    result = publisher().publish(
        parsed,
        archive,
        tmp_path,
        "Article",
        args[4],
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        "article.zip",
    )
    assert result["wiki_token"] == "child"
    assert "original-wechat:upload" in journal
    assert not any(key.startswith("original-pdf") for key in journal)
    assert gif in client.files.values()
    assert archive.read_bytes() in client.files.values()
    roots = journal["verified"]["result"]["roots"]
    assert [client.data[node]["block_type"] for node in roots] == [4, 2, 15, 31, 27, 33]
    paragraph = client.data[roots[1]]["text"]["elements"]
    assert paragraph[0]["text_run"]["text_element_style"] == {"bold": True}
    assert paragraph[1]["text_run"]["text_element_style"] == {
        "italic": True,
        "strikethrough": True,
    }
    assert paragraph[2]["text_run"]["text_element_style"]["link"]["url"].endswith("?q=1#code")
    cell = next(block for block in client.data.values() if block["block_type"] == 32)
    assert client.data[cell["children"][0]]["text"]["elements"] == paragraph
    paragraph[0]["text_run"]["text_element_style"].update(
        italic=False, inline_code=False, strikethrough=False, link={}
    )
    publisher().verify("doc1", **journal["verified"]["result"])
    paragraph[2]["text_run"]["text_element_style"]["link"]["url"] = "https://example.org/changed"
    with pytest.raises(UserError, match="超链接"):
        publisher().verify("doc1", **journal["verified"]["result"])


def test_nested_lists_keep_start_number_and_verify_child_order(publishing):
    client, journal, publisher, args = publishing
    args[0].elements = [
        Element(kind="ordered", text="Step 7", list_start=7),
        Element(kind="bullet", text="Child A", list_depth=1),
        Element(kind="ordered", text="Grandchild", list_depth=2, list_start=3),
        Element(kind="bullet", text="Child B", list_depth=1),
        Element(kind="text", text="Child detail", list_depth=2),
        Element(kind="ordered", text="Step 8"),
    ]
    publisher().publish(*args)
    first, second, _attachment = client.data["doc1"]["children"]
    parent = client.data[first]
    assert parent["ordered"]["style"]["sequence"] == "7"
    assert client.data[second]["ordered"]["elements"][0]["text_run"]["content"] == "Step 8"
    a, b = parent["children"]
    grandchild = client.data[client.data[a]["children"][0]]
    assert grandchild["ordered"]["style"]["sequence"] == "3"
    assert client.data[b]["children"]
    grandchild["ordered"]["style"]["sequence"] = "1"
    with pytest.raises(UserError, match="起始序号"):
        publisher().verify("doc1", **journal["verified"]["result"])
    grandchild["ordered"]["style"]["sequence"] = "3"
    parent["children"].reverse()
    with pytest.raises(UserError, match="嵌套列表"):
        publisher().verify("doc1", **journal["verified"]["result"])
    parent["children"].reverse()
    grandchild["children"] = ["unexpected-child"]
    with pytest.raises(UserError, match="嵌套列表"):
        publisher().verify("doc1", **journal["verified"]["result"])


@pytest.mark.parametrize("tamper", [False, True])
def test_lost_nested_rich_append_is_reconciled_without_repeating(publishing, tamper):
    client, journal, publisher, args = publishing
    args[0].elements = [
        Element(kind="bullet", text="Parent"),
        Element(
            kind="ordered",
            text="Nested",
            list_depth=1,
            list_start=4,
            runs=[TextRun(text="Nested", bold=True)],
        ),
    ]
    original = client.request

    def lost(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if "Nested" in str(kwargs.get("json", {})):
            raise UncertainWrite("lost nested response")
        return result

    client.request = lost
    with pytest.raises(UncertainWrite, match="lost nested"):
        publisher().publish(*args)
    nested = next(block for block in client.data.values() if block["block_type"] == 13)
    if tamper:
        nested["ordered"]["elements"][0]["text_run"]["text_element_style"]["bold"] = False
    before = copy.deepcopy(client.data)
    client.request = original
    if tamper:
        with pytest.raises(UncertainWrite, match="不确定"):
            publisher().publish(*args)
        assert client.data == before
    else:
        assert publisher().publish(*args)["wiki_token"] == "child"
        assert sum(block["block_type"] == 13 for block in client.data.values()) == 1
    assert client.created == 1


@pytest.mark.parametrize("kind", ["image", "table", "file"])
def test_lost_media_or_table_placeholder_response_is_reconciled(publishing, kind):
    client, journal, publisher, args = publishing
    original = client.request
    block_type = {"image": 27, "table": 31, "file": 23}[kind]
    dropped = False

    def lost(method, path, **kwargs):
        nonlocal dropped
        result = original(method, path, **kwargs)
        children = kwargs.get("json", {}).get("children", [])
        if not dropped and any(child["block_type"] == block_type for child in children):
            dropped = True
            raise UncertainWrite("lost placeholder response")
        return result

    client.request = lost
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    client.request = original
    assert publisher().publish(*args)["wiki_token"] == "child"
    assert sum(block["block_type"] == block_type for block in client.data.values()) == 1


def test_legacy_pdf_journal_and_verified_snapshot_resume_unchanged(publishing):
    client, journal, publisher, args = publishing
    result = publisher().publish(*args)
    assert "original-pdf:create" in journal
    assert not any(key.startswith("original-wechat") for key in journal)
    for key in list(journal):
        if key.endswith(":before"):
            del journal[key]
    expected = journal["verified"]["result"]["expected"]
    for item in expected:
        item.pop("rich_text", None)
        item.pop("sequence", None)
        if item["kind"] not in {"text", "code"}:
            item.pop("parent", None)
    before = copy.deepcopy(client.data)
    publisher().verify("doc1", **journal["verified"]["result"])
    assert publisher().publish(*args) == result
    assert client.data == before


def test_rich_long_list_keeps_one_item_and_run_boundaries_do_not_affect_verification(publishing):
    client, journal, publisher, args = publishing
    text = "long list " * 500
    args[0].elements = [
        Element(kind="ordered", text=text, runs=[TextRun(text=text, bold=True)], list_start=9)
    ]
    publisher().publish(*args)
    block = next(block for block in client.data.values() if block["block_type"] == 13)
    assert len(block["ordered"]["elements"]) > 1
    block["ordered"]["elements"] = [
        {"text_run": {"content": text, "text_element_style": {"bold": True}}}
    ]
    publisher().verify("doc1", **journal["verified"]["result"])


def test_invalid_rich_or_nested_content_is_rejected_before_creation(publishing):
    client, journal, publisher, args = publishing
    args[0].elements = [Element(kind="bullet", text="Missing parent", list_depth=1)]
    with pytest.raises(UserError, match="缺少父项"):
        publisher().publish(*args)
    args[0].elements = [Element(kind="text", text="Mismatch", runs=[TextRun(text="Different")])]
    with pytest.raises(UserError, match="不一致"):
        publisher().publish(*args)
    assert not journal
    assert client.created == 0


@pytest.mark.parametrize("kind", ["original", "image"])
@pytest.mark.parametrize("problem", ["missing", "empty", "oversize"])
def test_missing_or_oversized_files_fail_before_remote_creation(publishing, kind, problem):
    client, journal, publisher, args = publishing
    if kind == "original":
        args[0].source_kind = "wechat"
        args[0].elements = []
        source = args[1].with_suffix(".zip")
        source.write_bytes(b"PK-archive")
        args = (args[0], source, *args[2:])
    else:
        source = args[2] / "figure-1.png"
    if problem == "missing":
        source.unlink()
    else:
        with source.open("wb") as handle:
            handle.truncate(0 if problem == "empty" else 20 * 1024 * 1024 + 1)
    with pytest.raises(UserError, match="缺失|20 MB"):
        publisher().publish(*args)
    assert client.created == 0
    assert not journal
    assert not client.data


@pytest.fixture
def wechat_media(publishing, tmp_path):
    from io import BytesIO
    from zipfile import ZipFile

    from PIL import Image

    client, journal, publisher, args = publishing
    output = BytesIO()
    first = Image.new("RGB", (2, 2), "red")
    second = Image.new("RGB", (2, 2), "blue")
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    payload = output.getvalue()
    image = tmp_path / "animation.gif"
    image.write_bytes(payload)
    source = tmp_path / "article.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("article.html", '<img src="assets/animation.gif">')
        archive.writestr("assets/animation.gif", payload)
    parsed = ParsedDocument(
        source_kind="wechat",
        elements=[Element(kind="image", asset=image.name)],
        assets=[
            Asset(
                name=image.name, media_type="image/gif", digest=hashlib.sha256(payload).hexdigest()
            )
        ],
    )
    return (
        client,
        journal,
        publisher,
        (
            parsed,
            source,
            tmp_path,
            "GIF article",
            args[4],
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "article.zip",
        ),
    )


def test_wechat_gif_bytes_are_verified_before_and_after_archiving(wechat_media):
    from io import BytesIO

    from PIL import Image

    client, journal, publisher, args = wechat_media
    downloads = []
    original = client.download_digest

    def download(token):
        downloads.append((token, bool(client.target)))
        return original(token)

    client.download_digest = download
    publisher().publish(*args)
    image = next(
        item for item in journal["verified"]["result"]["expected"] if item["kind"] == "image"
    )
    assert image["asset_digest"] == args[0].assets[0].digest
    assert downloads.count((image["token"], False)) == 1
    assert downloads.count((image["token"], True)) == 1
    with Image.open(BytesIO(client.files[image["token"]])) as remote:
        assert remote.n_frames == 2


@pytest.mark.parametrize("failure", ["first_frame_only", "download_error"])
def test_wechat_image_token_alone_is_not_enough_to_pass_verification(wechat_media, failure):
    from io import BytesIO

    from PIL import Image

    client, journal, publisher, args = wechat_media
    publisher().publish(*args)
    verified = journal["verified"]["result"]
    image = next(item for item in verified["expected"] if item["kind"] == "image")
    if failure == "first_frame_only":
        output = BytesIO()
        with Image.open(BytesIO(client.files[image["token"]])) as animated:
            animated.save(output, format="GIF")
        client.files[image["token"]] = output.getvalue()
    else:
        original = client.download_digest

        def download(token):
            if token == image["token"]:
                raise UserError("图片下载失败")
            return original(token)

        client.download_digest = download
    with pytest.raises(UserError, match="图片"):
        publisher().verify("doc1", **verified)


@pytest.mark.parametrize("failure", ["missing_manifest", "changed_local_image"])
def test_wechat_image_manifest_is_checked_before_any_remote_mutation(wechat_media, failure):
    client, journal, publisher, args = wechat_media
    if failure == "missing_manifest":
        args[0].assets = []
    else:
        (args[2] / "animation.gif").write_bytes(b"changed bytes")
    with pytest.raises(UserError, match="素材摘要|摘要不一致"):
        publisher().publish(*args)
    assert client.created == 0
    assert not journal


def test_image_digest_download_is_cached_only_within_one_verification(wechat_media):
    client, journal, publisher, args = wechat_media
    publisher().publish(*args)
    verified = copy.deepcopy(journal["verified"]["result"])
    image = next(item for item in verified["expected"] if item["kind"] == "image")
    verified["expected"].append(copy.deepcopy(image))
    downloads = []
    original = client.download_digest

    def download(token):
        downloads.append(token)
        return original(token)

    client.download_digest = download
    instance = publisher()
    instance.verify("doc1", **verified)
    assert downloads == [image["token"]]
    instance.verify("doc1", **verified)
    assert downloads == [image["token"], image["token"]]


def test_pdf_and_legacy_expected_media_do_not_require_image_downloads(publishing):
    client, journal, publisher, args = publishing
    original = client.download_digest

    def download(token):
        assert client.files[token] == args[1].read_bytes()
        return original(token)

    client.download_digest = download
    publisher().publish(*args)
    verified = journal["verified"]["result"]
    assert all("asset_digest" not in item for item in verified["expected"])
    publisher().verify("doc1", **verified)
