"""The notification policy layer: what gets sent, at what priority, and how often.

These encode the three things measured on the real deployment that motivated the section — a
12-hour window carried 554 messages, of which 60% were findings the job rollup restated 8 s
later and 49% came from just 30 files retrying.
"""

from __future__ import annotations

from conftest import FakeNotifier, build_test_services, mk, probe_of, radarr_config
from mediacore.arr.events import parse_event
from orchestrator.config import OrchestratorConfig, load_orchestrator_config
from orchestrator.steps import JobContext
from orchestrator.steps.quality import QualityStep


def _services(tmp_path, cfg=None):
    services = build_test_services(tmp_path, cfg)
    services.notifier = FakeNotifier()
    return services


def _send(services, event="job-success", **kw):
    kw.setdefault("title", "T")
    kw.setdefault("body", "b")
    kw.setdefault("tags", [])
    kw.setdefault("priority", 3)
    return services.notify.send(event, **kw)


# --- the switch every emitter now has -------------------------------------------------
def test_a_disabled_rule_silences_only_its_own_event(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_success.enabled = False

    assert _send(services, "job-success") is False
    assert _send(services, "job-failed") is True
    assert services.notifier.titles == ["T"]


def test_the_master_switch_silences_everything(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.enabled = False
    assert _send(services, "job-failed") is False
    assert services.notifier.sent == []


def test_rule_overrides_priority_and_topic_but_leaves_them_alone_when_unset(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_failed.priority = 1
    services.config.notifications.job_failed.topic = "elsewhere"

    _send(services, "job-failed", priority=5, topic="asked-for")
    _send(services, "quota", priority=5, topic="asked-for")

    override, untouched = services.notifier.sent
    assert (override.priority, override.topic) == (1, "elsewhere")
    assert (untouched.priority, untouched.topic) == (5, "asked-for")


# --- de-duplication: the retry storm --------------------------------------------------
def test_once_per_job_collapses_a_retry_storm(tmp_path):
    """A retry re-runs the whole pipeline under the same job id. Seven attempts used to send
    seven copies of the same finding; 30 such files produced 210 messages in one day."""
    services = _services(tmp_path)
    services.config.notifications.job_failed.once_per = "job"

    results = [_send(services, "job-failed", job_id="job-1") for _ in range(7)]

    assert results == [True, False, False, False, False, False, False]
    assert len(services.notifier.sent) == 1


def test_once_per_job_still_lets_a_different_job_through(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_failed.once_per = "job"

    _send(services, "job-failed", job_id="job-1")
    _send(services, "job-failed", job_id="job-2")
    assert len(services.notifier.sent) == 2


def test_once_per_path_collapses_across_different_jobs(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_failed.once_per = "path"

    _send(services, "job-failed", job_id="job-1", path="/data/a.mkv")
    _send(services, "job-failed", job_id="job-2", path="/data/a.mkv")
    _send(services, "job-failed", job_id="job-3", path="/data/b.mkv")
    assert len(services.notifier.sent) == 2


def test_off_means_off_even_with_an_identity_to_dedupe_on(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_failed.once_per = "off"
    for _ in range(3):
        _send(services, "job-failed", job_id="job-1")
    assert len(services.notifier.sent) == 3


def test_a_failed_delivery_does_not_suppress_the_next_attempt(tmp_path):
    """Suppression records a message that *went out*. If ntfy was unreachable the caller has not
    been told anything, so silencing the retry would lose the notification entirely."""

    class DeadNotifier(FakeNotifier):
        def send(self, msg) -> bool:
            super().send(msg)
            return False

    services = build_test_services(tmp_path)
    services.notifier = DeadNotifier()
    services.config.notifications.job_failed.once_per = "job"

    assert _send(services, "job-failed", job_id="job-1") is False
    assert _send(services, "job-failed", job_id="job-1") is False
    assert len(services.notifier.sent) == 2  # both were attempted

    services.notifier = FakeNotifier()  # ntfy comes back
    assert _send(services, "job-failed", job_id="job-1") is True


def test_dedupe_records_are_pruned_by_retention(tmp_path):
    services = _services(tmp_path)
    services.config.notifications.job_failed.once_per = "job"
    _send(services, "job-failed", job_id="job-1")

    assert services.notify.prune_old(0) == 0  # 0 = keep forever
    assert services.notify.prune_old(3650) == 0  # nothing that old yet
    services.db.execute("UPDATE notify_sent SET ts='2000-01-01T00:00:00+00:00'")
    assert services.notify.prune_old(1) == 1


# --- the finding/rollup duplication ---------------------------------------------------
def _dirty_ctx(services, tmp_path, steps):
    path = tmp_path / "f.mkv"
    path.write_bytes(b"x")
    probe = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="ac3", language="eng", channels=6),
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # -> foreign sub
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),
        path=str(path),
    )
    event = parse_event(
        "radarr",
        {"eventType": "Download", "movieFile": {"path": str(path)}, "movie": {"title": "T"}},
    )
    return JobContext(
        services=services,
        job_id="job-1",
        event=event,
        local_path=str(path),
        probe_fn=lambda p: probe,
        prune_runner=lambda *a, **k: None,
        steps=steps,
    )


def test_findings_are_suppressed_when_prune_will_fix_them(tmp_path):
    """The rollup restates every finding a few seconds later, at a lower priority. Measured on
    a real day: 153 of 153 rollups repeated the finding text."""
    services = _services(tmp_path, radarr_config())
    ctx = _dirty_ctx(services, tmp_path, steps=("quality-check", "prune"))

    result = QualityStep().run(ctx)

    assert services.notifier.sent == []
    assert "findings:" in result.detail  # still recorded in the audit trail and the rollup


def test_findings_are_sent_when_prune_is_not_in_the_pipeline(tmp_path):
    """The rail that matters: with no prune configured the finding is the *only* thing that
    tells you, so suppressing it there would lose the information rather than de-duplicate it."""
    services = _services(tmp_path, radarr_config())
    ctx = _dirty_ctx(services, tmp_path, steps=("quality-check",))

    QualityStep().run(ctx)

    assert len(services.notifier.sent) == 1
    assert "Foreign subtitles" in services.notifier.sent[0].body


def test_suppression_can_be_switched_off(tmp_path):
    services = _services(tmp_path, radarr_config())
    services.config.notifications.suppress_findings_when_pruning = False
    ctx = _dirty_ctx(services, tmp_path, steps=("quality-check", "prune"))

    QualityStep().run(ctx)
    assert len(services.notifier.sent) == 1


# --- back-compat ----------------------------------------------------------------------
def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return load_orchestrator_config(p)


def test_legacy_status_granularity_off_silences_the_rollup(tmp_path):
    cfg = _write(tmp_path, 'ntfy:\n  topic: t\n  status_granularity: "off"\n')
    assert cfg.notifications.job_success.enabled is False
    assert cfg.notifications.job_step.enabled is False
    assert cfg.notifications.job_failed.enabled is True  # failures always still alert


def test_legacy_status_granularity_action_switches_to_per_step(tmp_path):
    cfg = _write(tmp_path, 'ntfy:\n  topic: t\n  status_granularity: "action"\n')
    assert cfg.notifications.job_step.enabled is True
    assert cfg.notifications.job_success.enabled is False


def test_legacy_status_granularity_job_keeps_the_defaults(tmp_path):
    cfg = _write(tmp_path, 'ntfy:\n  topic: t\n  status_granularity: "job"\n')
    assert cfg.notifications.job_success.enabled is True
    assert cfg.notifications.job_step.enabled is False


def test_an_explicit_section_wins_over_the_legacy_key(tmp_path):
    cfg = _write(
        tmp_path,
        'ntfy:\n  topic: t\n  status_granularity: "off"\n'
        "notifications:\n  job_success:\n    enabled: true\n",
    )
    assert cfg.notifications.job_success.enabled is True


def test_every_event_name_has_a_config_field():
    """A send() to an unknown name silently falls back to a default rule, which would make a
    typo look like working config. Keep EVENTS and the section in step."""
    from orchestrator.notifications import EVENTS

    cfg = OrchestratorConfig().notifications
    for event in EVENTS:
        assert hasattr(cfg, event.replace("-", "_")), f"{event} has no NotifyRule field"
