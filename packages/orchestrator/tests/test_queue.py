from __future__ import annotations

from pathlib import Path

from mediacore.audit import open_db
from orchestrator.queue import DEAD, DONE, PROCESSING, QUEUED, JobQueue, _iso_in


def make_queue(
    tmp_path: Path, max_attempts: int = 3, backoff: tuple[int, ...] = (0,)
) -> JobQueue:
    # Default to zero backoff so retry/reclaim timing is deterministic in tests; backoff behaviour
    # is exercised explicitly in test_retry_backoff.
    return JobQueue(open_db(tmp_path), max_attempts=max_attempts, backoff_schedule=backoff)


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


def test_set_progress_and_step_clears(tmp_path):
    q = make_queue(tmp_path)
    jid = q.enqueue("k", {})
    q.claim_next()
    q.set_progress(jid, 42.0, "muxing · 42%")
    job = q.get(jid)
    assert job.progress == 42.0 and job.progress_note == "muxing · 42%"
    # a new step resets progress
    q.set_step(jid, "prune")
    job2 = q.get(jid)
    assert job2.progress is None and job2.current_step == "prune"


def _backdate(q: JobQueue, jid: str, seconds: int) -> None:
    """Push a job's updated_at into the past so cooldown/retention windows apply."""
    q.db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_iso_in(-seconds), jid))
    q.db.commit()


def test_retry_backoff_defers_reclaim(tmp_path):
    q = make_queue(tmp_path, max_attempts=3, backoff=(120,))
    jid = q.enqueue("k", {})
    q.claim_next()  # attempts -> 1
    assert q.mark_failed(jid, "boom") == QUEUED
    # not_before is ~120s out, so the job is not immediately re-claimable
    assert q.claim_next() is None
    assert q.get(jid).status == QUEUED
    # once not_before has passed, it can be claimed again
    q.db.execute("UPDATE jobs SET not_before=? WHERE id=?", (_iso_in(-1), jid))
    q.db.commit()
    job = q.claim_next()
    assert job is not None and job.attempts == 2


def test_defer_never_consumes_attempts(tmp_path):
    q = make_queue(tmp_path, max_attempts=2)
    jid = q.enqueue("k", {})
    q.claim_next()  # attempts -> 1
    for _ in range(5):  # deferring repeatedly must never dead-letter
        q.defer(jid, 60, note="low disk space")
        assert q.get(jid).status == QUEUED
    job = q.get(jid)
    assert job.status == QUEUED and job.attempts == 1  # attempts untouched
    assert q.claim_next() is None  # still deferred (not_before in the future)


def test_pause_blocks_claim(tmp_path):
    q = make_queue(tmp_path)
    q.enqueue("k", {})
    q.pause()
    assert q.is_paused() is True
    assert q.claim_next() is None
    q.resume()
    assert q.is_paused() is False
    assert q.claim_next() is not None


def test_revive_dead_bounded(tmp_path):
    q = make_queue(tmp_path, max_attempts=1)
    jid = q.enqueue("k", {})
    q.claim_next()
    q.mark_failed(jid, "x")  # -> dead
    assert q.get(jid).status == DEAD
    # still within cooldown -> not revived
    assert q.revive_dead(cooldown_s=3600, max_revives=1) == 0
    _backdate(q, jid, 7200)
    assert q.revive_dead(cooldown_s=3600, max_revives=1) == 1
    assert q.get(jid).status == QUEUED
    # send it back to dead; second auto-revive is capped
    q.claim_next()
    q.mark_failed(jid, "x")
    _backdate(q, jid, 7200)
    assert q.revive_dead(cooldown_s=3600, max_revives=1) == 0  # revives already == max


def test_retry_all_dead(tmp_path):
    q = make_queue(tmp_path, max_attempts=1)
    a, b = q.enqueue("k", {}), q.enqueue("k", {})
    for jid in (a, b):
        q.claim_next()
        q.mark_failed(jid, "x")
    assert q.counts().get(DEAD) == 2
    assert q.retry_all_dead() == 2
    assert q.get(a).status == QUEUED and q.get(b).status == QUEUED


def test_prune_old_removes_only_old_finished(tmp_path):
    q = make_queue(tmp_path)
    old = q.enqueue("k", {})
    q.claim_next()
    q.mark_done(old)
    _backdate(q, old, 100 * 86400)  # 100 days old
    recent = q.enqueue("k", {})
    q.claim_next()
    q.mark_done(recent)
    assert q.prune_old(90) == 1
    assert q.get(old) is None and q.get(recent) is not None
    assert q.prune_old(0) == 0  # disabled


def test_meta_roundtrip(tmp_path):
    q = make_queue(tmp_path)
    assert q.get_meta("k") is None
    q.set_meta("k", "v")
    assert q.get_meta("k") == "v"
    q.set_meta("k", "v2")  # upsert
    assert q.get_meta("k") == "v2"
    q.set_meta("k", None)  # delete
    assert q.get_meta("k") is None


def test_progress_columns_migrated_on_existing_table(tmp_path):
    from mediacore.audit import open_db
    # simulate an OLD db: jobs table without progress columns
    db = open_db(tmp_path)
    db.execute("DROP TABLE IF EXISTS jobs")
    db.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT, label TEXT, payload TEXT, "
        "status TEXT, attempts INTEGER, current_step TEXT, error TEXT, created_at TEXT, updated_at TEXT)"
    )
    db.commit()
    q = JobQueue(db)  # __init__ runs the migration
    cols = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
    assert "progress" in cols and "progress_note" in cols
    assert "not_before" in cols and "revives" in cols
    # the meta table is created too
    q.set_meta("probe", "1")
    assert q.get_meta("probe") == "1"
    # and it still works
    jid = q.enqueue("k", {})
    q.claim_next()
    q.set_progress(jid, 10.0, "x")
    assert q.get(jid).progress == 10.0
