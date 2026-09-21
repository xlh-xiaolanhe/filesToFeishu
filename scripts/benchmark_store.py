"""Compare legacy full-history reads with indexed summaries using disposable databases."""

import argparse
import json
import sqlite3
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

from files_to_feishu.store import Store


def measure(read: Callable[[], list[dict]]) -> dict:
    tracemalloc.start()
    start = time.perf_counter()
    rows = read()
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"rows": len(rows), "milliseconds": round(elapsed * 1000, 3), "peak_bytes": peak}


def benchmark(count: int, steps: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="files-to-feishu-benchmark-") as directory:
        path = Path(directory) / "jobs.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            journal = {
                f"step-{index}": {"state": "done", "result": {"text": "x" * 1024}}
                for index in range(steps)
            }
            for index in range(count):
                job = {
                    "id": f"{index:032x}",
                    "filename": f"sample-{index}.pdf",
                    "digest": "digest",
                    "status": "succeeded",
                    "created_at": "2026-09-21T00:00:00+00:00",
                    "journal": journal,
                }
                if index % 2:
                    job["deleted_at"] = "2026-09-21T01:00:00+00:00"
                conn.execute("INSERT INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job)))

        def legacy_read() -> list[dict]:
            with sqlite3.connect(path) as legacy:
                rows = legacy.execute("SELECT data FROM jobs ORDER BY rowid DESC").fetchall()
            return [job for row in rows if not (job := json.loads(row[0])).get("deleted_at")]

        result = {"jobs": count, "journal_steps": steps, "legacy": measure(legacy_read)}
        result["legacy_database_bytes"] = path.stat().st_size
        store = Store(path)
        query = getattr(store, "list_summaries", None)
        if query is not None:
            result["summary_page"] = measure(lambda: query(limit=50))
            result["indexed_database_bytes"] = path.stat().st_size
        return result


def benchmark_effects(count: int) -> dict:
    """Measure actual changed effect rows; legacy bytes are serialized on every save."""
    with tempfile.TemporaryDirectory(prefix="files-to-feishu-effects-") as directory:
        store = Store(Path(directory) / "effects.sqlite3")
        job = store.create("benchmark.pdf", "hash")
        with store.connect() as conn:
            conn.execute("CREATE TABLE write_audit (bytes INTEGER)")
            for operation in ("INSERT", "UPDATE"):
                conn.execute(
                    f"CREATE TRIGGER audit_{operation} AFTER {operation} ON job_effects "
                    "BEGIN INSERT INTO write_audit VALUES (length(new.data)); END"
                )
        journal = {}
        legacy_bytes = 0
        start = time.perf_counter()
        for index in range(count):
            journal[f"element-{index}"] = {
                "state": "done",
                "result": {"block_id": f"block-{index}"},
            }
            legacy_bytes += len(json.dumps(journal).encode())
            store.update(job["id"], journal=journal, load_journal=False)
        elapsed = time.perf_counter() - start
        with store.connect() as conn:
            writes, payload_bytes = conn.execute(
                "SELECT COUNT(*), SUM(bytes) FROM write_audit"
            ).fetchone()
        return {
            "effects": count,
            "legacy_serialized_journal_bytes": legacy_bytes,
            "changed_effect_rows": writes,
            "changed_effect_payload_bytes": payload_bytes,
            "save_milliseconds": round(elapsed * 1000, 3),
            "database_bytes": store.path.stat().st_size,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[10, 100, 1000])
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--effect-counts", type=int, nargs="*", default=[10, 100, 1000])
    arguments = parser.parse_args()
    if min(arguments.counts) < 1 or arguments.steps < 0:
        parser.error("counts must be positive and steps must be nonnegative")
    for count in arguments.counts:
        print(json.dumps(benchmark(count, arguments.steps)))

    for count in arguments.effect_counts:
        print(json.dumps(benchmark_effects(count)))
