from __future__ import annotations

from pathlib import Path

from mediacore.audit import open_db
from orchestrator.queue import DEAD, DONE, PROCESSING, QUEUED, JobQueue


def make_queue(tmp_path: Path, max_attempts: int = 3) -> JobQueue:
    return JobQueue(open_db(tmp_path), max_attempts=max_attempts)


def test_enqueue_and_claim(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    jid = q.enqueue("arr_event", {"a": 1}, label="X")
    job = q.claim_next()
    assert job is not None and job.id == jid
    assert job.status == PROCESSING and job.attempts == 1
    assert job.payload == {"a": 1}
    assert q.claim_next() is None  # nothing left queued


def test_done(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    jid = q.enqueue("k", {})
    q.claim_next()
    q.mark_done(jid)
    assert q.get(jid).status == DONE


def test_retry_then_deadletter(tmp_path: Path) -> None:
    q = make_queue(tmp_path, max_attempts=2)
    jid = q.enqueue("k", {})
    q.claim_next()  # attempts -> 1
    assert q.mark_failed(jid, "boom") == QUEUED  # 1 < 2, requeued
    q.claim_next()  # attempts -> 2
    assert q.mark_failed(jid, "boom again") == DEAD  # 2 >= 2
    assert q.get(jid).status == DEAD


def test_cancel_only_queued(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    jid = q.enqueue("k", {})
    assert q.cancel(jid) is True
    # already claimed job cannot be canceled
    jid2 = q.enqueue("k", {})
    q.claim_next()
    assert q.cancel(jid2) is False


def test_retry_resets_attempts(tmp_path: Path) -> None:
    q = make_queue(tmp_path, max_attempts=1)
    jid = q.enqueue("k", {})
    q.claim_next()
    q.mark_failed(jid, "x")  # -> dead
    assert q.retry(jid) is True
    job = q.get(jid)
    assert job.status == QUEUED and job.attempts == 0


def test_requeue_stuck(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    q.enqueue("k", {})
    q.claim_next()  # now processing
    assert q.requeue_stuck() == 1
    assert q.list(statuses=(QUEUED,))[0].status == QUEUED


def test_clear_dead_and_delete(tmp_path: Path) -> None:
    q = make_queue(tmp_path, max_attempts=1)
    d = q.enqueue("k", {})
    q.claim_next()
    q.mark_failed(d, "x")  # -> dead
    keep = q.enqueue("k", {})  # queued
    assert q.counts().get(DEAD) == 1
    assert q.clear_dead() == 1
    assert q.get(d) is None and q.get(keep) is not None
    # delete a queued job by id
    assert q.delete(keep) is True and q.get(keep) is None


def test_delete_refuses_in_flight(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    jid = q.enqueue("k", {})
    q.claim_next()  # processing
    assert q.delete(jid) is False and q.get(jid) is not None


def test_list_q_filter(tmp_path: Path) -> None:
    q = make_queue(tmp_path)
    q.enqueue("k", {"app": "radarr"}, label="The Matrix (1999)")
    q.enqueue("k", {"app": "sonarr"}, label="Some Show - S01E01")
    assert len(q.list(q="matrix")) == 1
    assert len(q.list(q="S01E01")) == 1
    assert len(q.list(q="nope")) == 0
