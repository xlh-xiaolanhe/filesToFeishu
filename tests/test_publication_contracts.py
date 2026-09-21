"""User-visible confirmation, concurrent editing and cross-task publication safety."""

import pytest

from tests.helpers import reviewed_payload
from tests.test_content_identity_crash import publish_article, ready_article
from tests.test_wechat_workflow import PAYLOAD
from tests.test_wechat_workflow import article_app as article_app
from tests.test_workflow import wait


@pytest.mark.parametrize("missing", ["confirmed", "review_token", "confirmation_token"])
def test_publish_requires_complete_confirmation(article_app, missing):
    service, client = article_app
    job = ready_article(client)
    payload = reviewed_payload(client, job["id"], PAYLOAD)
    payload.pop(missing)
    response = client.post(f"/api/jobs/{job['id']}/publish", json=payload)
    assert response.status_code in {400, 409, 422}
    assert service.client.created == 0


def test_title_target_and_application_changes_expire_confirmation(article_app, monkeypatch):
    service, client = article_app
    original_resolve = service.client.resolve
    monkeypatch.setattr(
        service.client,
        "resolve",
        lambda url: original_resolve(url).model_copy(update={"node_token": url.rsplit("/", 1)[-1]}),
    )
    job = ready_article(client)
    payload = reviewed_payload(client, job["id"], {**PAYLOAD, "title": "Reviewed title"})
    for change in [{"title": "Different title"}, {"url": "https://test.feishu.cn/wiki/other"}]:
        response = client.post(f"/api/jobs/{job['id']}/publish", json={**payload, **change})
        assert response.status_code == 409, response.text
    service.settings.feishu_app_id = "different-app"
    response = client.post(f"/api/jobs/{job['id']}/publish", json=payload)
    assert response.status_code == 409
    assert service.client.created == 0


def test_stale_edit_conflicts_without_overwrite_or_losing_newer_content(article_app):
    service, client = article_app
    job = ready_article(client)
    job_id = job["id"]
    payload = reviewed_payload(client, job_id, PAYLOAD)
    index = len(job["preview"]["elements"]) - 1
    edit = {
        "text": "newer code",
        "language": "plaintext",
        "expected_revision": job["content_revision"],
    }
    assert client.post(f"/api/jobs/{job_id}/code/{index}", json=edit).status_code == 200
    response = client.post(f"/api/jobs/{job_id}/code/{index}", json={**edit, "text": "stale code"})
    assert response.status_code == 409
    assert service.parsed(job_id).elements[index].text == "newer code"
    assert client.post(f"/api/jobs/{job_id}/publish", json=payload).status_code == 409
    assert service.client.created == 0
    del edit["expected_revision"]
    assert client.post(f"/api/jobs/{job_id}/code/{index}", json=edit).status_code == 422


def test_unresolved_creation_blocks_same_intent_from_another_task(article_app, monkeypatch):
    service, client = article_app
    first = ready_article(client)
    service.client.lost_response = True
    response = client.post(
        f"/api/jobs/{first['id']}/publish", json=reviewed_payload(client, first["id"], PAYLOAD)
    )
    assert response.status_code == 202
    assert wait(client, first["id"], {"needs_review", "failed"})["status"] == "needs_review"
    second = ready_article(client)
    rejected = client.post(
        f"/api/jobs/{second['id']}/publish", json=reviewed_payload(client, second["id"], PAYLOAD)
    )
    assert rejected.status_code == 400
    assert first["id"] in rejected.text
    assert service.client.created == 1


def test_different_requested_title_is_a_new_publication_intent(article_app):
    service, client = article_app
    first = ready_article(client)
    publish_article(client, first["id"])
    second = ready_article(client)
    response = client.post(
        f"/api/jobs/{second['id']}/publish",
        json=reviewed_payload(client, second["id"], {**PAYLOAD, "title": "Another title"}),
    )
    assert response.status_code == 202
    assert wait(client, second["id"], {"succeeded", "failed"})["status"] == "succeeded"
    assert service.client.created == 2


def downgrade_to_legacy(service, job_id):
    """Remove fields unknown to v0.5.1 without changing its actual remote result."""
    service.store.update(job_id, intent_digest="", publish_snapshot=None)
    job = service.store.get(job_id)
    journal = {key: value for key, value in job["journal"].items() if key != "plan"}
    service.store.update(job_id, journal=journal)


def test_legacy_unknown_blocks_new_job_and_can_be_reviewed_for_recovery(article_app):
    service, client = article_app
    first = ready_article(client)
    service.client.lost_response = True
    client.post(
        f"/api/jobs/{first['id']}/publish", json=reviewed_payload(client, first["id"], PAYLOAD)
    )
    wait(client, first["id"], {"needs_review"})
    downgrade_to_legacy(service, first["id"])
    second = ready_article(client)
    response = client.post(
        f"/api/jobs/{second['id']}/publish", json=reviewed_payload(client, second["id"], PAYLOAD)
    )
    assert response.status_code == 400
    assert first["id"] in response.text
    assert service.client.created == 1
    # Reviewing the legacy job freezes its existing source before any candidate is accepted.
    reviewed_payload(client, first["id"], PAYLOAD)
    legacy = service.store.get(first["id"])
    assert legacy["publish_snapshot"]["content_revision"] == first["content_revision"]
    assert legacy["journal"]["document"]["state"] == "pending"
    assert service.client.created == 1


def test_legacy_success_is_verified_from_current_plan_before_reuse(article_app):
    service, client = article_app
    first = ready_article(client)
    published = publish_article(client, first["id"])
    downgrade_to_legacy(service, first["id"])
    duplicate = ready_article(client)
    reused = publish_article(client, duplicate["id"])
    assert reused["document_id"] == published["document_id"]
    # A second recheck follows the original publication evidence, also when legacy.
    assert publish_article(client, duplicate["id"])["document_id"] == published["document_id"]
    assert service.client.created == 1


def test_legacy_stale_success_evidence_cannot_certify_new_content(article_app):
    service, client = article_app
    first = ready_article(client)
    published = publish_article(client, first["id"])
    downgrade_to_legacy(service, first["id"])
    duplicate = ready_article(client)
    # Old journal and content remain intact, but actual remote text has changed.
    for block in service.client.data.values():
        if block.get("block_type") == 14:
            block["code"]["elements"][0]["text_run"]["content"] = "remote changed"
    response = client.post(
        f"/api/jobs/{duplicate['id']}/publish",
        json=reviewed_payload(client, duplicate["id"], PAYLOAD),
    )
    assert response.status_code == 202
    result = wait(client, duplicate["id"], {"failed", "succeeded"})
    assert result["status"] == "failed"
    assert service.client.created == 1
    assert published["document_id"] == "doc1"


def test_preview_and_revision_are_read_under_the_edit_lock(article_app, monkeypatch):
    service, client = article_app
    job = ready_article(client)
    original_parsed = service.parsed

    def locked_parsed(*args, **kwargs):
        assert service.lock.locked(), "HTTP preview cannot race content CAS metadata"
        return original_parsed(*args, **kwargs)

    monkeypatch.setattr(service, "parsed", locked_parsed)
    response = client.get(f"/api/jobs/{job['id']}")
    assert response.status_code == 200
    assert response.json()["content_revision"] == job["content_revision"]


def test_schema_migration_never_runs_before_checking_active_instance(tmp_path, monkeypatch):
    import fcntl

    from files_to_feishu.app import create_app
    from files_to_feishu.config import Settings

    def forbidden_store(*args):
        raise AssertionError("must not migrate an active instance's database")

    with (tmp_path / "server.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr("files_to_feishu.app.Store", forbidden_store)
        with pytest.raises(RuntimeError, match="先停止旧服务"):
            create_app(Settings(data_dir=tmp_path))


def test_parsed_document_rejects_future_versions():
    from pydantic import ValidationError

    from files_to_feishu.models import ParsedDocument

    assert ParsedDocument.model_validate({"elements": []}).schema_version == 1
    with pytest.raises(ValidationError):
        ParsedDocument.model_validate({"elements": [], "schema_version": 99})


def test_candidate_association_api_never_marks_success_or_recreates(article_app, monkeypatch):
    import time

    service, client = article_app
    job = ready_article(client)
    service.client.lost_response = True
    client.post(f"/api/jobs/{job['id']}/publish", json=reviewed_payload(client, job["id"], PAYLOAD))
    current = wait(client, job["id"], {"needs_review"})
    assert current["recovery_steps"] == [{"key": "document", "kind": "document"}]
    monkeypatch.setattr(
        service.client,
        "document_metadata",
        lambda token: {
            "doc_token": token,
            "doc_type": "docx",
            "title": "Original article",
            "owner_id": "bot-owner",
            "create_time": time.time(),
        },
        raising=False,
    )
    monkeypatch.setattr(service.client, "bot_open_id", lambda: "bot-owner", raising=False)
    body = {
        "key": "document",
        "candidate": "doc1",
        "expected_revision": current["content_revision"],
    }
    assert (
        client.post(
            f"/api/jobs/{job['id']}/recover", json={**body, "expected_revision": 99}
        ).status_code
        == 409
    )
    result = client.post(f"/api/jobs/{job['id']}/recover", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review"
    assert service.client.created == 1
    assert service.store.get(job["id"])["journal"]["document"]["state"] == "done"
    service.client.lost_response = False
    assert publish_article(client, job["id"])["status"] == "succeeded"
    assert service.client.created == 1


def test_pdf_also_requires_current_confirmation(article_app, pdf_bytes):
    from files_to_feishu.models import Element, ParsedDocument

    service, client = article_app
    service.parser = lambda *_: ParsedDocument(pages=1, elements=[Element(kind="text", text="PDF")])
    created = client.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)})
    job = wait(client, created.json()["id"], {"ready"})
    response = client.post(f"/api/jobs/{job['id']}/publish", json=PAYLOAD)
    assert response.status_code == 400
    assert service.client.created == 0
    assert publish_article(client, job["id"])["status"] == "succeeded"
