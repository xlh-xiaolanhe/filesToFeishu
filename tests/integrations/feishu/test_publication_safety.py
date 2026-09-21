"""Failure injection at the boundary between local planning and remote effects."""

import copy
import json
import time

import pytest

from files_to_feishu.models import Element, UncertainWrite, UserError
from tests.integrations.feishu.test_publisher import publishing as publishing


def test_irregular_table_is_rejected_before_any_remote_effect(publishing):
    client, journal, publisher, args = publishing
    args[0].elements = [Element(kind="text", text="safe"), Element(kind="table", rows=[["a"], []])]
    with pytest.raises(UserError, match="表格"):
        publisher().publish(*args)
    assert client.created == 0
    assert not journal


def test_checkpoint_size_scales_linearly_with_appends(publishing):
    client, journal, publisher, _ = publishing
    client.data["doc"] = {"block_id": "doc", "block_type": 1, "children": []}
    instance = publisher()
    sizes = []
    for index in range(400):
        instance.children(
            f"element-{index}",
            "doc",
            "doc",
            [{"block_type": 2, "text": {"elements": [{"text_run": {"content": "x"}}]}}],
        )
        if index + 1 in (100, 200, 400):
            sizes.append(len(json.dumps(journal)))
    assert sizes[1] < sizes[0] * 2.3
    assert sizes[2] < sizes[1] * 2.3


@pytest.mark.parametrize(
    "element",
    [
        Element(kind="heading", text="invalid heading", level=0),
        Element(kind="heading", text="invalid heading", level=10),
        Element(kind="text", text="not a list", list_start=2),
        Element(kind="table", rows=[]),
        Element(kind="table", rows=[[]]),
    ],
)
def test_invalid_local_structure_never_creates_a_document(publishing, element):
    client, journal, publisher, args = publishing
    args[0].elements = [Element(kind="text", text="first"), element]
    with pytest.raises(UserError):
        publisher().publish(*args)
    assert client.created == 0
    assert not journal


def test_original_digest_mismatch_is_rejected_before_remote_creation(publishing):
    client, journal, publisher, args = publishing
    with pytest.raises(UserError, match="原附件摘要"):
        publisher().publish(*args[:5], "wrong-digest", args[6])
    assert client.created == 0
    assert not journal


def test_future_journal_and_plan_versions_are_rejected(publishing):
    client, journal, publisher, args = publishing
    journal["document"] = {"schema_version": 99, "state": "pending"}
    with pytest.raises(UserError, match="版本"):
        publisher()
    journal.clear()
    publisher().prepare_plan(*args)
    journal["plan"]["result"]["version"] = 99
    with pytest.raises(UserError, match="计划版本"):
        publisher()
    assert client.created == 0


def test_changed_plan_cannot_reinterpret_existing_checkpoints(publishing):
    client, journal, publisher, args = publishing
    client.fail_upload = True
    with pytest.raises(UserError, match="素材上传失败"):
        publisher().publish(*args)
    before = copy.deepcopy(client.data)
    args[0].elements[0].text = "edited after publishing"
    with pytest.raises(UserError, match="发布计划"):
        publisher().publish(*args)
    assert client.data == before
    assert client.created == 1


def recovery_identity(client):
    """Expose the current app's identity through the same read-only client boundary."""
    client.document_metadata = lambda doc: {
        "doc_token": doc,
        "doc_type": "docx",
        "title": "Test",
        "owner_id": "current-app-bot",
        "create_time": time.time(),
    }
    client.bot_open_id = lambda: "current-app-bot"


def unknown_creation(publishing):
    client, journal, publisher, args = publishing
    client.lost_response = True
    with pytest.raises(UncertainWrite):
        publisher().publish(*args, app_id="app")
    client.lost_response = False
    recovery_identity(client)
    instance = publisher()
    instance.prepare_plan(*args, app_id="app")
    return client, journal, instance, args


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_create_candidate_is_verified_then_normal_publish_resumes(publishing, legacy):
    client, journal, instance, args = unknown_creation(publishing)
    if legacy:
        journal.pop("plan")
        journal["document"] = {"state": "pending"}
        instance = publishing[2]()
        instance.prepare_plan(*args, app_id="app")
    instance.recover_candidate("document", "doc1", app_id="app")
    assert journal["document"]["state"] == "done"
    assert journal["document"]["recovery"]["method"] == "manual_readback"
    assert "verified" not in journal
    assert "move" not in journal
    assert client.created == 1
    assert instance.publish(*args, app_id="app")["wiki_token"] == "child"
    assert client.created == 1


@pytest.mark.parametrize("problem", ["owner", "title", "type", "id", "time", "content", "app"])
def test_unproven_document_candidate_is_not_associated(publishing, problem):
    client, journal, instance, _ = unknown_creation(publishing)
    original = client.document_metadata

    def metadata(doc):
        result = original(doc)
        field = {"owner": "owner_id", "title": "title", "type": "doc_type", "id": "doc_token"}
        if problem in field:
            result[field[problem]] = "different"
        if problem == "time":
            result["create_time"] = 1
        return result

    client.document_metadata = metadata
    if problem == "content":
        client.data["extra"] = {"block_id": "extra", "parent_id": "doc1", "block_type": 2}
        client.data["doc1"]["children"] = ["extra"]
    before = copy.deepcopy(client.data)
    with pytest.raises(UserError):
        instance.recover_candidate(
            "document", "doc1", app_id="other" if problem == "app" else "app"
        )
    assert journal["document"]["state"] == "pending"
    assert client.data == before
    assert client.created == 1


def unknown_upload(publishing):
    client, journal, publisher, args = publishing
    original = client.upload

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise UncertainWrite("lost upload response")

    client.upload = lost
    with pytest.raises(UncertainWrite):
        publisher().publish(*args, app_id="app")
    client.upload = original
    instance = publisher()
    instance.prepare_plan(*args, app_id="app")
    return client, journal, instance, args


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_upload_matches_digest_and_parent_then_resumes(publishing, legacy):
    client, journal, instance, args = unknown_upload(publishing)
    key = "element-3:upload"
    token = next(iter(client.files))
    if legacy:
        journal.pop("plan")
        journal[key] = {"state": "pending"}
        instance = publishing[2]()
        instance.prepare_plan(*args, app_id="app")
    before = copy.deepcopy(client.data)
    instance.recover_candidate(key, token, app_id="app")
    assert client.data == before  # The recovery endpoint reads; it does not bind or publish.
    assert "verified" not in journal
    assert len(client.files) == 1
    assert instance.publish(*args, app_id="app")["wiki_token"] == "child"
    assert len(client.files) == 2  # Reused image and one subsequently uploaded original.


@pytest.mark.parametrize("problem", ["digest", "parent", "binding", "context", "detached"])
def test_unproven_upload_candidate_keeps_unknown_state(publishing, problem):
    client, journal, instance, _ = unknown_upload(publishing)
    key = "element-3:upload"
    token = next(iter(client.files))
    node = next(block for block in client.data.values() if block["block_type"] == 27)
    if problem == "digest":
        client.files[token] = b"wrong image"
    elif problem == "parent":
        node["parent_id"] = "other-doc"
    elif problem == "binding":
        node["image"]["token"] = "already-bound-other-token"
    elif problem == "context":
        instance.journal[key]["input"]["block_id"] = "other-block"
    elif problem == "detached":
        client.data["doc1"]["children"].remove(node["block_id"])
    before = copy.deepcopy(client.data)
    with pytest.raises(UserError):
        instance.recover_candidate(key, token, app_id="app")
    assert journal[key]["state"] == "pending"
    assert client.data == before
    assert len(client.files) == 1


@pytest.mark.parametrize("step", ["document", "upload", "children"])
def test_missing_acknowledgement_remains_pending_for_readback(publishing, step):
    client, journal, publisher, args = publishing
    if step == "upload":
        original_upload = client.upload

        def empty_upload(*args, **kwargs):
            original_upload(*args, **kwargs)
            return {}

        client.upload = empty_upload
        key = "element-3:upload"
    else:
        original_request = client.request

        def empty_response(method, path, **kwargs):
            result = original_request(method, path, **kwargs)
            if (step == "document" and path == "/docx/v1/documents") or (
                step == "children" and path.endswith("/children")
            ):
                return {}
            return result

        client.request = empty_response
        key = "document" if step == "document" else "element-0:0"
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    assert journal[key]["state"] == "pending"
    assert client.created == 1


def test_legacy_full_prefix_baseline_still_recovers_append(publishing):
    client, journal, publisher, args = publishing
    original = client.request

    def lost(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if "Known editable text" in str(kwargs.get("json", {})):
            raise UncertainWrite("lost text response")
        return result

    client.request = lost
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    journal["element-1:0:before"]["result"] = {"children": client.data["doc1"]["children"][:-1]}
    client.request = original
    assert publisher().publish(*args)["wiki_token"] == "child"
    assert client.created == 1


def current_plan(args):
    from files_to_feishu.integrations.feishu.plan import build_plan

    return build_plan(*args, app_id="")


@pytest.mark.parametrize("blank_cells", [False, True])
def test_current_plan_verification_does_not_depend_on_historical_expectations(
    publishing, blank_cells
):
    from files_to_feishu.models import TextRun

    client, journal, publisher, args = publishing
    client.blank_cells = blank_cells
    args[0].elements = [
        Element(kind="heading", text="Section", level=2),
        Element(kind="ordered", text="Parent", list_start=3),
        Element(kind="bullet", text="Child", list_depth=1),
        Element(kind="text", text="Nested text", list_depth=2),
        Element(kind="quote", text="quoted", runs=[TextRun(text="quoted", bold=True)]),
        Element(kind="table", rows=[["left", "right"]]),
        Element(kind="code", text="  foo()\nbar()", language="javascript", code_reviewed=True),
        Element(kind="image", asset="figure-1.png"),
    ]
    publisher().publish(*args)
    journal.clear()  # A legacy document needs no trusted historical expectation record.
    before = copy.deepcopy(client.data)
    publisher().verify_plan("doc1", current_plan(args))
    assert client.data == before
    assert journal == {}
    assert client.created == 1


@pytest.mark.parametrize(
    "problem",
    [
        "text",
        "heading",
        "sequence",
        "nested",
        "cell",
        "code",
        "style",
        "link",
        "image",
        "original",
        "title",
        "extra",
    ],
)
def test_current_plan_rejects_remote_semantic_or_structure_changes(publishing, problem):
    from files_to_feishu.models import TextRun

    client, journal, publisher, args = publishing
    args[0].elements = [
        Element(kind="heading", text="heading", level=2),
        Element(kind="ordered", text="parent", list_start=3),
        Element(kind="bullet", text="child", list_depth=1),
        Element(kind="table", rows=[["cell"]]),
        Element(kind="code", text="  code()", language="javascript", code_reviewed=True),
        Element(
            kind="text",
            text="link",
            runs=[TextRun(text="link", bold=True, link="https://example.org")],
        ),
        Element(kind="image", asset="figure-1.png"),
    ]
    publisher().publish(*args)
    plan = current_plan(args)
    def get(kind):
        return next(block for block in client.data.values() if block["block_type"] == kind)
    if problem == "text":
        get(4)["heading2"]["elements"][0]["text_run"]["content"] = "wrong heading text"
    elif problem == "heading":
        get(4)["block_type"] = 3
    elif problem == "sequence":
        get(13)["ordered"]["style"]["sequence"] = "1"
    elif problem == "nested":
        get(12)["parent_id"] = "doc1"
    elif problem == "cell":
        get(32)["children"].append(get(32)["children"][0])
    elif problem == "code":
        get(14)["code"]["elements"][0]["text_run"]["content"] = "code()"
    elif problem in {"style", "link"}:
        block = next(
            block for block in client.data.values() if "link" in str(block.get("text", {}))
        )
        style = block["text"]["elements"][0]["text_run"]["text_element_style"]
        if problem == "style":
            style["bold"] = False
        else:
            style["link"]["url"] = "https://example.org/other"
    elif problem in {"image", "original"}:
        token = get(27 if problem == "image" else 23)["image" if problem == "image" else "file"][
            "token"
        ]
        client.files[token] = b"different bytes"
    elif problem == "title":
        client.data["doc1"]["title"] = "new title"
    elif problem == "extra":
        client.data["doc1"]["children"].append(client.data["doc1"]["children"][0])
    journal.clear()
    with pytest.raises(UserError, match="核对失败"):
        publisher().verify_plan("doc1", plan)
    assert client.created == 1


def test_candidate_matching_its_old_journal_is_rejected_by_current_plan(publishing):
    client, journal, publisher, args = publishing
    publisher().publish(*args)
    args[0].elements[1].text = "newly edited text"
    # Old expected readback succeeds. It must not substitute for the current plan.
    publisher().verify("doc1", **journal["verified"]["result"])
    with pytest.raises(UserError, match="本次确认"):
        publisher().verify_plan("doc1", current_plan(args))
    assert client.created == 1


def test_compact_append_baseline_still_rejects_changed_prefix(publishing):
    client, journal, publisher, args = publishing
    original = client.request

    def lost(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if "Known editable text" in str(kwargs.get("json", {})):
            raise UncertainWrite("lost text response")
        return result

    client.request = lost
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    assert "sha256" in journal["element-1:0:before"]["result"]
    client.data["doc1"]["children"].reverse()
    before = copy.deepcopy(client.data)
    client.request = original
    with pytest.raises(UncertainWrite):
        publisher().publish(*args)
    assert client.data == before
    assert client.created == 1
