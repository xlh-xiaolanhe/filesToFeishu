"""Bounded reads and isolated checkpoint writes, without timing-sensitive assertions."""

import json

from files_to_feishu.store import Store


def test_summary_pagination_filters_deleted_history_without_loading_journals(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    ids = []
    for index in range(12):
        task = store.create(
            f"sample-{index}.pdf", "hash", batch_id="batch" if index < 8 else "other"
        )
        store.update(task["id"], status="succeeded", journal={"large": "x" * 100_000})
        ids.append(task["id"])
    store.delete(ids[0])
    store.delete(ids[6])
    # Poisoning the unneeded body proves summary reads do not accidentally deserialize it.
    with store.connect() as conn:
        conn.execute("UPDATE job_effects SET data='not json'")
    page = store.list_summaries(limit=3, offset=1, batch_id="batch")
    assert [job["id"] for job in page] == [ids[5], ids[4], ids[3]]
    assert all("journal" not in job and "parsed_json" not in job for job in page)
    assert all(job["has_journal"] for job in page)
    assert store.count() == 10
    assert store.count(batch_id="batch") == 6
    assert store.count(include_deleted=True) == 12
    assert len(json.dumps(page)) < 10_000


def test_progress_and_notification_updates_do_not_rewrite_checkpoint_evidence(tmp_path):
    store = Store(tmp_path / "updates.sqlite3")
    job = store.create("large.pdf", "digest")
    journal = {"document": {"state": "done", "result": "large" * 10_000}}
    store.update(job["id"], journal=journal, notifications={"failed": {"status": "pending"}})
    with store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER forbid_journal_write BEFORE UPDATE ON job_effects "
            "BEGIN SELECT RAISE(ABORT, 'journal rewritten'); END"
        )
    store.update(job["id"], progress="next block")
    store.update_notification(job["id"], "failed", {"pending"}, status="sent")
    store.update(job["id"], journal=journal)  # An unchanged checkpoint is also not rewritten.
    assert store.get(job["id"])["journal"] == journal


def test_indexed_queue_and_identity_queries_only_read_relevant_summaries(tmp_path):
    store = Store(tmp_path / "query.sqlite3")
    old = store.create("old.pdf", "same")
    store.update(
        old["id"],
        status="succeeded",
        target={"space_id": "space", "node_token": "node"},
        app_id="app",
        requested_title="title",
        intent_digest="intent",
        journal={"verified": {"state": "done"}},
    )
    store.delete(old["id"])
    first = store.create("first.pdf", "new", batch_id="a")
    second = store.create("second.pdf", "new", batch_id="b")
    with store.connect() as conn:
        conn.execute("UPDATE job_effects SET data='not json'")
    assert store.next_queued()["id"] == first["id"]
    assert store.count(statuses=["queued"]) == 2
    found = store.find_candidates(
        source_kind="pdf", digest="same", app_id="app", space_id="space", parent_token="node"
    )
    assert [job["id"] for job in found] == [old["id"]]
    assert store.find_candidates(intent_digest="intent")[0]["id"] == old["id"]
    assert store.find_candidates(intent_digest="intent", include_deleted=False) == []
    assert store.list_summaries(batch_id="b")[0]["id"] == second["id"]
    with store.connect() as conn:
        plan = str(
            conn.execute(
                "EXPLAIN QUERY PLAN SELECT data FROM jobs WHERE "
                "source_kind=? AND digest=? AND app_id=? AND space_id=? "
                "AND parent_token=?",
                ("pdf", "same", "app", "space", "node"),
            ).fetchall()
        )
    assert "jobs_identity" in plan


def test_effect_persistence_writes_only_added_or_changed_checkpoints(tmp_path):
    store = Store(tmp_path / "effects.sqlite3")
    job = store.create("large.pdf", "hash")
    with store.connect() as conn:
        conn.execute("CREATE TABLE effect_write_audit (bytes INTEGER)")
        for operation in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER count_effect_{operation} AFTER {operation} ON job_effects "
                "BEGIN INSERT INTO effect_write_audit VALUES (length(new.data)); END"
            )
    journal = {}
    for index in range(40):
        journal[f"step-{index}"] = {"state": "done", "result": "x" * 1000}
        store.update(job["id"], journal=journal, load_journal=False)
    with store.connect() as conn:
        writes, byte_count = conn.execute(
            "SELECT COUNT(*), SUM(bytes) FROM effect_write_audit"
        ).fetchone()
    assert writes == 40  # The unchanged 0 + 1 + ... + 39 earlier steps were not rewritten.
    assert byte_count == sum(len(json.dumps(value)) for value in journal.values())
    assert store.get(job["id"])["journal"] == journal


def test_summary_only_progress_never_reads_effect_results(tmp_path):
    store = Store(tmp_path / "progress.sqlite3")
    job = store.create("sample.pdf", "hash")
    store.update(job["id"], journal={"step": {"state": "pending"}})
    with store.connect() as conn:
        conn.execute("UPDATE job_effects SET data='not json'")
    result = store.update(job["id"], progress="safe summary", load_journal=False)
    assert result["progress"] == "safe summary"
    assert "journal" not in result
