import pytest

from files_to_feishu.store import Store


def test_job_survives_restart_and_uncertain_work_never_becomes_success(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    job = store.create("report.pdf", "abc123")
    store.update(job["id"], status="publishing", document_id="doc123")
    reopened = Store(tmp_path / "jobs.sqlite")
    reopened.recover()
    recovered = reopened.get(job["id"])
    assert recovered["status"] == "needs_review"
    assert recovered["document_id"] == "doc123"
    assert "中断" in recovered["error"]


@pytest.mark.parametrize("status", ["queued", "fetching", "waiting_verification", "cancelling"])
def test_article_restart_discards_session_and_requires_new_fetch(tmp_path, status):
    store = Store(tmp_path / "jobs.sqlite")
    job = store.create("article.zip", "", source_kind="wechat")
    store.update(job["id"], status=status)
    store.recover()
    restored = store.get(job["id"])
    assert restored["status"] == "failed"
    assert "重新获取" in restored["error"]
    assert not restored["journal"]


def test_legacy_migration_preserves_unknown_fields_and_exact_checkpoints(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "old.sqlite3"
    legacy = {
        "id": "old",
        "filename": "old.pdf",
        "digest": "hash",
        "status": "succeeded",
        "created_at": "2026-01-01",
        "future_metadata": {"keep": True},
        "journal": {"original-pdf:bind": {"state": "done", "result": {"token": "file"}}},
    }
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("INSERT INTO jobs VALUES (?, ?)", ("old", json.dumps(legacy)))
    store = Store(path)
    restored = store.get("old")
    assert restored["schema_version"] == 1
    assert restored["source_kind"] == "pdf"
    assert restored["content_revision"] == 0
    assert restored["future_metadata"] == {"keep": True}
    assert restored["journal"] == legacy["journal"]
    assert store.get_content("old") is None
    assert "journal" not in store.list_summaries()[0]
    assert Store(path).get("old") == restored


def test_unsupported_job_version_rolls_back_entire_legacy_migration(tmp_path):
    import json
    import sqlite3

    from files_to_feishu.models import UserError

    path = tmp_path / "future.sqlite3"
    records = [
        {
            "id": str(index),
            "filename": "sample.pdf",
            "digest": "hash",
            "status": "ready",
            "created_at": "2026-01-01",
            "schema_version": version,
            "journal": {"untouched": [1, 2, 3]},
        }
        for index, version in enumerate((1, 99))
    ]
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO jobs VALUES (?, ?)",
            [(record["id"], json.dumps(record)) for record in records],
        )
    with pytest.raises(UserError, match="版本不受支持"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert [row[1] for row in conn.execute("PRAGMA table_info(jobs)")] == ["id", "data"]
        assert [json.loads(row[0]) for row in conn.execute("SELECT data FROM jobs")] == records


def test_content_and_digest_are_atomic_even_if_summary_write_fails(tmp_path):
    import sqlite3

    from files_to_feishu.models import Element, ParsedDocument

    store = Store(tmp_path / "atomic.sqlite3")
    job = store.create("article.zip", "source", source_kind="wechat")
    old = ParsedDocument(source_kind="wechat", elements=[Element(kind="code", text="old")])
    saved = store.commit_content(job["id"], old.model_dump_json(), "old-hash", 0, status="ready")
    with store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON jobs "
            "BEGIN SELECT RAISE(ABORT, 'injected crash'); END"
        )
    new = old.model_copy(deep=True)
    new.elements[0].text = "new"
    with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
        store.commit_content(job["id"], new.model_dump_json(), "new-hash", 1)
    reopened = Store(store.path)
    content = reopened.get_content(job["id"])
    assert content == {
        "parsed_json": old.model_dump_json(),
        "content_digest": "old-hash",
        "content_revision": 1,
    }
    assert reopened.get(job["id"]) == saved


def test_content_compare_and_set_prevents_stale_concurrent_edits(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from files_to_feishu.job_models import RevisionConflict
    from files_to_feishu.models import Element, ParsedDocument

    store = Store(tmp_path / "versions.sqlite3")
    job = store.create("sample.pdf", "source")
    parsed = ParsedDocument(pages=1, elements=[Element(kind="code", text="old")])
    store.commit_content(job["id"], parsed.model_dump_json(), "old", 0, status="ready")
    with pytest.raises(RevisionConflict):
        store.commit_content(job["id"], parsed.model_dump_json(), "missing-baseline")
    barrier = Barrier(2)

    def edit(text):
        new = ParsedDocument(pages=1, elements=[Element(kind="code", text=text)])
        barrier.wait(timeout=5)
        try:
            store.commit_content(job["id"], new.model_dump_json(), text, 1)
            return text
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ("first", "second")))
    assert results.count("conflict") == 1
    winner = next(text for text in results if text != "conflict")
    content = store.get_content(job["id"])
    assert content["content_revision"] == 2
    assert content["content_digest"] == winner
    assert ParsedDocument.model_validate_json(content["parsed_json"]).elements[0].text == winner


def test_claim_locks_content_updates_and_history_deletion_preserves_content(tmp_path):
    from files_to_feishu.models import Element, ParsedDocument, UserError
    from files_to_feishu.store import JobNotFound

    store = Store(tmp_path / "locked.sqlite3")
    job = store.create("sample.pdf", "source")
    parsed = ParsedDocument(pages=1, elements=[Element(kind="text", text="body")])
    store.commit_content(job["id"], parsed.model_dump_json(), "content", 0, status="ready")
    store.claim(job["id"])
    with pytest.raises(UserError, match="不能修改内容"):
        store.commit_content(job["id"], parsed.model_dump_json(), "changed", 1)
    store.update(job["id"], status="succeeded")
    store.delete(job["id"])
    with pytest.raises(JobNotFound):
        store.get_content(job["id"])
    assert store.get_content(job["id"], include_deleted=True)["content_digest"] == "content"


def test_task_schema_and_operation_policy_reject_invalid_records_and_actions(tmp_path):
    from files_to_feishu.job_models import allowed_actions
    from files_to_feishu.models import UserError

    store = Store(tmp_path / "policy.sqlite3")
    job = store.create("sample.pdf", "source")
    assert allowed_actions(job) == ["cancel"]
    with pytest.raises(UserError, match="状态无效"):
        store.update(job["id"], status="silently_done")
    with pytest.raises(UserError, match="版本不受支持"):
        store.update(job["id"], schema_version=2)
    with pytest.raises(UserError, match="状态无效"):
        store.update(job["id"], notifications={"failed": {"status": "forgotten"}})
    with pytest.raises(UserError, match="正在处理中"):
        store.claim(job["id"])
    ready = store.update(job["id"], status="ready")
    assert allowed_actions(ready) == ["edit", "publish", "delete"]
    locked = store.update(job["id"], journal={"document": {"state": "pending"}})
    assert allowed_actions(locked) == ["publish"]
    with pytest.raises(UserError, match="原子内容提交"):
        store.update(job["id"], content_digest="unpaired")


def test_content_reads_reject_a_digest_or_revision_mismatch(tmp_path):
    from files_to_feishu.models import ParsedDocument, UserError

    store = Store(tmp_path / "mismatch.sqlite3")
    task = store.create("sample.pdf", "hash")
    store.commit_content(task["id"], ParsedDocument(elements=[]).model_dump_json(), "hash", 0)
    with store.connect() as conn:
        conn.execute("UPDATE job_contents SET content_digest='another-version'")
    with pytest.raises(UserError, match="摘要版本不一致"):
        store.get_content(task["id"])


def test_get_content_uses_one_database_snapshot_during_concurrent_edit(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from files_to_feishu.models import ParsedDocument

    store = Store(tmp_path / "read-snapshot.sqlite3")
    task = store.create("sample.pdf", "hash")
    parsed = ParsedDocument(elements=[]).model_dump_json()
    store.commit_content(task["id"], parsed, "old", 0, status="ready")
    read = store._read

    with ThreadPoolExecutor(max_workers=1) as executor:

        def interleaved_read(conn, job_id, **kwargs):
            result = read(conn, job_id, **kwargs)
            if threading.current_thread() is threading.main_thread():
                executor.submit(store.commit_content, job_id, parsed, "new", 1).result(timeout=5)
            return result

        monkeypatch.setattr(store, "_read", interleaved_read)
        snapshot = store.get_content(task["id"])
    assert snapshot["content_revision"] == 1
    assert snapshot["content_digest"] == "old"
    monkeypatch.setattr(store, "_read", read)
    assert store.get_content(task["id"])["content_digest"] == "new"
