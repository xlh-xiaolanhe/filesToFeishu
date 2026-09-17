import copy
import hashlib

import pytest

from files_to_feishu.integrations.feishu import Publisher
from files_to_feishu.models import Element, ParsedDocument, Target, UncertainWrite, UserError


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
