"""Regression for the user-supplied split content/digest crash reproducer.

The original test failed at 6e11654: B was previewed while A's doc1 was reused.
Content now lives in SQLite; inject after writing content but before committing its
summary, and additionally kill a subprocess while that transaction is open.
"""

import json
import subprocess
import sys

import pytest

from files_to_feishu.content import article_digest
from files_to_feishu.store import Store
from tests.helpers import reviewed_payload
from tests.test_wechat_workflow import PAYLOAD, URL
from tests.test_wechat_workflow import article_app as article_app
from tests.test_workflow import wait


def ready_article(client):
    response = client.post("/api/jobs/wechat", json={"url": URL})
    assert response.status_code == 202, response.text
    result = wait(client, response.json()["id"], {"ready", "failed"})
    assert result["status"] == "ready", result
    return result


def publish_article(client, job_id):
    response = client.post(
        f"/api/jobs/{job_id}/publish", json=reviewed_payload(client, job_id, PAYLOAD)
    )
    assert response.status_code == 202, response.text
    result = wait(client, job_id, {"succeeded", "failed", "needs_review"})
    assert result["status"] == "succeeded", result
    return result


def test_interrupted_code_save_never_reuses_stale_article(article_app, monkeypatch):
    service, client = article_app
    old = ready_article(client)
    old_result = publish_article(client, old["id"])
    ready = ready_article(client)
    job_id = ready["id"]
    before = service.store.get_content(job_id)
    index = next(i for i, e in enumerate(ready["preview"]["elements"]) if e["kind"] == "code")
    text = 'console.log("reviewed revision B");'
    original_write = service.store._write

    def fail_summary_commit(conn, job, **kwargs):
        if job["id"] == job_id and job["content_revision"] == ready["content_revision"] + 1:
            # The new canonical JSON has already been written inside this transaction.
            assert text in conn.execute(
                "SELECT parsed_json FROM job_contents WHERE job_id=?", (job_id,)
            ).fetchone()[0].replace('\\"', '"')
            raise OSError("injected metadata commit failure")
        return original_write(conn, job, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "_write", fail_summary_commit)
        with pytest.raises(OSError, match="injected metadata commit failure"):
            service.save_code(job_id, index, text, "javascript", ready["content_revision"])
    service.store.recover()
    assert service.store.get_content(job_id) == before
    assert article_digest(service.parsed(job_id)) == before["content_digest"]
    # Saving B successfully must create and verify B, never report A as B.
    service.save_code(job_id, index, text, "javascript", ready["content_revision"])
    result = publish_article(client, job_id)
    assert result["document_id"] != old_result["document_id"]
    expected = service.store.get(job_id)["journal"]["verified"]["result"]["expected"]
    assert any(item.get("kind") == "code" and item.get("text") == text for item in expected)


def test_process_exit_during_content_transaction_rolls_back(article_app):
    service, client = article_app
    ready = ready_article(client)
    job_id = ready["id"]
    before = service.store.get_content(job_id)
    script = """
import os, sys
from pathlib import Path
from files_to_feishu.content import article_digest
from files_to_feishu.models import ParsedDocument
from files_to_feishu.store import Store
store=Store(Path(sys.argv[1]))
content=store.get_content(sys.argv[2])
parsed=ParsedDocument.model_validate_json(content['parsed_json'])
parsed.elements[-1].text='revision after interrupted transaction'
original=store._write
def interrupted(conn, job, **kwargs):
    original(conn, job, **kwargs)
    os._exit(73)
store._write=interrupted
store.commit_content(sys.argv[2], parsed.model_dump_json(), article_digest(parsed),
                     expected_revision=content['content_revision'])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(service.settings.data_dir / "jobs.sqlite3"), job_id],
        check=False,
        timeout=15,
    )
    assert result.returncode == 73
    reopened = Store(service.settings.data_dir / "jobs.sqlite3")
    reopened.recover()
    assert reopened.get_content(job_id) == before


def test_legacy_split_file_digest_is_not_used_for_reuse(article_app):
    service, client = article_app
    old = ready_article(client)
    first = publish_article(client, old["id"])
    new = ready_article(client)
    job_id = new["id"]
    parsed = service.parsed(job_id)
    parsed.elements[-1].text = "console.log('legacy revision B')"
    (service.folder(job_id) / "parsed.json").write_text(parsed.model_dump_json(), encoding="utf-8")
    # Reconstruct the exact pre-upgrade legacy persisted state: B file, A summary.
    with service.store.connect() as conn:
        row = json.loads(conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        row.pop("content_revision", None)
        row.pop("content_digest", None)
        conn.execute("DELETE FROM job_contents WHERE job_id=?", (job_id,))
        conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(row), job_id))
    service.store.recover()
    current = client.get(f"/api/jobs/{job_id}").json()
    assert current["preview"]["elements"][-1]["text"] == parsed.elements[-1].text
    result = publish_article(client, job_id)
    assert result["document_id"] != first["document_id"]
