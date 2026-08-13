from __future__ import annotations

from reference_transfer.storage import Storage


def test_token_bucket_reports_wait_when_capacity_is_exhausted(tmp_path) -> None:
    store = Storage(tmp_path / "queue.sqlite3")
    assert store.consume_token("workflowy", 1) == 0
    assert store.consume_token("workflowy", 1) > 59


def test_claims_oldest_jobs_first(tmp_path) -> None:
    store = Storage(tmp_path / "queue.sqlite3")
    store.enqueue("first", {"task": {"id": "first"}}, "a")
    store.enqueue("second", {"task": {"id": "second"}}, "b")
    assert [job.task_id for job in store.claim(10, 60)] == ["first", "second"]


def test_recover_releases_processing_jobs(tmp_path) -> None:
    store = Storage(tmp_path / "queue.sqlite3")
    store.enqueue("one", {"task": {"id": "one"}}, "a")
    store.claim(1, 60)
    assert store.release_processing() == 1
    assert [job.task_id for job in store.claim(1, 60)] == ["one"]
