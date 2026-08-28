"""The step registry itself: that a step is genuinely a unit you can add, and that the
framework — not each step — owns timing, auditing, the rollup, and following a moved file.

test_dispatch.py covers what the five real steps do. These cover the contract they plug into.
"""

from __future__ import annotations

from conftest import build_test_services, radarr_config
from mediacore.audit import FAIL, OK, SKIP
from orchestrator.dispatch import Dispatcher
from orchestrator.steps import JobContext, StepResult, default_pipeline


class _Recorder:
    """A step that records that it ran, and reports whatever it was told to."""

    def __init__(self, name: str, *, silent: bool = False, applies: bool = True, **result):
        self.name = name
        self.silent = silent
        self._applies = applies
        self._result = result
        self.saw: list[str] = []

    def applies(self, ctx: JobContext) -> bool:
        return self._applies

    def run(self, ctx: JobContext) -> StepResult:
        self.saw.append(ctx.local_path)
        return StepResult(self.name, "do", self._result.get("result", OK), **{
            k: v for k, v in self._result.items() if k != "result"
        })


def _payload(path: str = "/data/Movies/x.mkv") -> dict:
    return {
        "app": "radarr",
        "payload": {
            "eventType": "Download",
            "movieFile": {"path": path},
            "movie": {"title": "X", "id": 1},
        },
    }


def _dispatch(tmp_path, steps, **cfg_kw):
    services = build_test_services(tmp_path, radarr_config(**cfg_kw))
    disp = Dispatcher(services, steps=steps)
    return services, disp.dispatch("job-1", _payload())


def test_the_default_pipeline_is_the_documented_order(tmp_path) -> None:
    """Order is load-bearing (ledger first so bandwidth survives a later failure; prune before
    renumber so the shift moves a finished file), so it is worth asserting rather than trusting
    to the reading order of a list."""
    assert [s.name for s in default_pipeline()] == [
        "ledger",
        "quality-check",
        "prune",
        "seasonal-shift",
        "jellyfin",
    ]


def test_a_step_that_does_not_apply_never_runs(tmp_path) -> None:
    skipped = _Recorder("nope", applies=False)
    ran = _Recorder("yep")
    _services, outcome = _dispatch(tmp_path, [skipped, ran])

    assert skipped.saw == []
    assert ran.saw == ["/data/Movies/x.mkv"]
    assert [s.tool for s in outcome.steps] == ["yep"]


def test_the_framework_times_every_step(tmp_path) -> None:
    """Timing used to be four copies of the same monotonic() pair inline in dispatch()."""
    _services, outcome = _dispatch(tmp_path, [_Recorder("a"), _Recorder("b")])
    assert all(s.duration_ms is not None and s.duration_ms >= 0 for s in outcome.steps)


def test_a_moved_file_is_followed_by_the_steps_after_it(tmp_path) -> None:
    """The remux rename used to be rediscovered by stat()-ing a guessed sibling path in the
    middle of dispatch(). A step now says where it put the file and the pipeline follows."""
    mover = _Recorder("mover", detail="remuxed", new_path="/data/Movies/x.mkv.moved")
    after = _Recorder("after")
    _services, outcome = _dispatch(tmp_path, [mover, after])

    assert mover.saw == ["/data/Movies/x.mkv"]
    assert after.saw == ["/data/Movies/x.mkv.moved"]
    assert outcome.final_local_path == "/data/Movies/x.mkv.moved"


def test_a_silent_step_is_audited_but_kept_out_of_the_rollup(tmp_path) -> None:
    """The ledger snapshot and the Jellyfin refresh are bookkeeping about the job, not work done
    to the file — they belong in the audit trail and not in the ntfy summary."""
    services, outcome = _dispatch(
        tmp_path, [_Recorder("quiet", silent=True, detail="noted"), _Recorder("loud")]
    )
    assert [s.tool for s in outcome.steps] == ["loud"]

    tools = [a["tool"] for a in services.audit.recent(limit=10)]
    assert "quiet" in tools and "loud" in tools


def test_a_later_step_can_depend_on_an_earlier_one_having_run(tmp_path) -> None:
    """How the Jellyfin refresh waits for prune to have had its turn, without a bool threaded
    through the dispatch body."""
    first = _Recorder("first")

    class NeedsFirst:
        name = "second"
        silent = False

        def applies(self, ctx: JobContext) -> bool:
            return ctx.ran("first")

        def run(self, ctx: JobContext) -> StepResult:
            return StepResult("second", "do", OK, "saw first")

    _services, outcome = _dispatch(tmp_path, [first, NeedsFirst()])
    assert [s.tool for s in outcome.steps] == ["first", "second"]

    # ...and without the first step, it stays out
    _services, outcome = _dispatch(tmp_path, [NeedsFirst()])
    assert outcome.steps == []


def test_the_probe_is_taken_once_and_shared(tmp_path) -> None:
    """Probing costs an mkvmerge plus several ffprobe calls, so quality-check's probe is reused
    by prune rather than repeated."""
    from conftest import mk, probe_of

    calls: list[str] = []

    def probe_fn(path: str):
        calls.append(path)
        return probe_of(mk(1, "video"), path=path)

    class Prober:
        silent = False

        def __init__(self, name):
            self.name = name

        def applies(self, ctx):
            return True

        def run(self, ctx):
            ctx.probed()
            return StepResult(self.name, "do", OK)

    services = build_test_services(tmp_path, radarr_config())
    Dispatcher(services, probe_fn=probe_fn, steps=[Prober("a"), Prober("b")]).dispatch(
        "job-1", _payload()
    )
    assert calls == ["/data/Movies/x.mkv"]


def test_a_failed_step_does_not_stop_the_ones_after_it(tmp_path) -> None:
    """Returning FAIL marks the step failed and continues; only raising fails the job. That is
    what lets a quality-check probe failure still leave prune a chance to try."""
    _services, outcome = _dispatch(
        tmp_path, [_Recorder("broken", result=FAIL, error="boom"), _Recorder("later")]
    )
    assert [(s.tool, s.result) for s in outcome.steps] == [("broken", FAIL), ("later", OK)]


def test_a_raising_step_fails_the_job(tmp_path) -> None:
    """The other half of the rule above: prune raises rather than returning FAIL, because a
    half-written file is not something the rest of the pipeline should build on."""
    import pytest

    class Explodes:
        name = "boom"
        silent = False

        def applies(self, ctx):
            return True

        def run(self, ctx):
            raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        _dispatch(tmp_path, [Explodes(), _Recorder("never")])


def test_set_step_is_called_for_visible_steps_only(tmp_path) -> None:
    """A silent step is over in milliseconds; flashing it as the current step would only make
    the job row flicker."""
    services = build_test_services(tmp_path, radarr_config())
    seen: list[str] = []
    Dispatcher(
        services, steps=[_Recorder("quiet", silent=True), _Recorder("loud")]
    ).dispatch("job-1", _payload(), set_step=seen.append)
    assert seen == ["loud"]


def test_skipped_steps_are_still_audited(tmp_path) -> None:
    """A SKIP is a real answer about a file ("nothing to prune"), so it belongs in the trail."""
    services, _outcome = _dispatch(
        tmp_path, [_Recorder("s", result=SKIP, detail="nothing to do")]
    )
    rows = services.audit.recent(limit=10)
    assert [(r["tool"], r["result"]) for r in rows] == [("s", SKIP)]
