"""The config layout, and the shim that keeps the old one working.

Fifteen scalars used to sit loose at the YAML root, mixing three different jobs — connection
settings, media policy, operational tuning — next to `arr:` and `ntfy:`. They now live under
`worker:` and `pipeline:`. Nothing rewrites a user's file, so the old keys have to keep working.
"""

from __future__ import annotations

import pytest
from mediacore.config import parse_config
from orchestrator.config import OrchestratorConfig
from orchestrator.steps import build_pipeline
from pydantic import ValidationError


def _cfg(text: str) -> OrchestratorConfig:
    return parse_config(OrchestratorConfig, text)


def test_the_default_pipeline_is_the_documented_order() -> None:
    cfg = OrchestratorConfig()
    assert cfg.pipeline.steps == [
        "ledger", "quality-check", "prune", "seasonal-shift", "jellyfin"
    ]


def test_legacy_root_scalars_are_lifted_into_their_sections() -> None:
    cfg = _cfg(
        """
        max_attempts: 7
        retry_backoff_s: [1, 2]
        retention_days: 5
        prune_scratch_dir: /scratch
        prune_min_free_multiplier: 2.0
        prune_low_space_backoff_s: 60
        mkvprune_executable: /usr/local/bin/mkvprune
        """
    )
    assert cfg.worker.max_attempts == 7
    assert cfg.worker.retry_backoff_s == [1, 2]
    assert cfg.worker.retention_days == 5
    assert cfg.pipeline.scratch_dir == "/scratch"
    assert cfg.pipeline.min_free_multiplier == 2.0
    assert cfg.pipeline.low_space_backoff_s == 60
    assert cfg.pipeline.mkvprune_executable == "/usr/local/bin/mkvprune"


def test_a_new_style_section_wins_over_a_legacy_key() -> None:
    """Someone mid-migration will have both. The one they wrote deliberately should win."""
    cfg = _cfg(
        """
        max_attempts: 7
        worker: {max_attempts: 2}
        """
    )
    assert cfg.worker.max_attempts == 2


def test_a_legacy_enable_flag_removes_its_step_from_the_pipeline() -> None:
    """`enable_prune: false` used to be the switch; membership of pipeline.steps is now."""
    cfg = _cfg("enable_prune: false\nenable_ledger: false\n")
    assert cfg.pipeline.steps == ["quality-check", "seasonal-shift", "jellyfin"]
    assert [s.name for s in build_pipeline(cfg.pipeline.steps)] == cfg.pipeline.steps


def test_legacy_enable_true_changes_nothing() -> None:
    cfg = _cfg("enable_prune: true\nenable_seasonal_shift: true\n")
    assert cfg.pipeline.steps == OrchestratorConfig().pipeline.steps


def test_an_explicit_step_list_beats_the_legacy_flags() -> None:
    cfg = _cfg(
        """
        enable_prune: false
        pipeline: {steps: [prune]}
        """
    )
    assert cfg.pipeline.steps == ["prune"]


def test_the_pipeline_can_be_reordered_and_trimmed() -> None:
    cfg = _cfg("pipeline: {steps: [prune, ledger]}")
    assert [s.name for s in build_pipeline(cfg.pipeline.steps)] == ["prune", "ledger"]


def test_an_unknown_step_name_is_a_config_error_not_a_silent_drop() -> None:
    """A typo in the step list must fail loudly at startup. Silently running four of five steps
    is the kind of thing you find out about months later, from the files it didn't touch."""
    with pytest.raises(ValidationError, match="unknown pipeline step"):
        _cfg("pipeline: {steps: [prune, prnue]}")


def test_the_error_names_the_steps_that_do_exist() -> None:
    with pytest.raises(ValidationError, match="seasonal-shift"):
        _cfg("pipeline: {steps: [nope]}")


def test_legacy_keys_also_work_as_constructor_kwargs() -> None:
    """Not just YAML: the before-validator sees kwargs too, which is what keeps a pile of
    existing tests passing unchanged."""
    cfg = OrchestratorConfig(enable_prune=False, max_attempts=9)
    assert "prune" not in cfg.pipeline.steps
    assert cfg.worker.max_attempts == 9


# --- the generated settings reference -------------------------------------------------------
def test_the_schema_reference_covers_every_section() -> None:
    """It is derived from the models, so it cannot drift from what the config accepts."""
    from orchestrator.config_schema import describe

    sections = {s.name for s in describe(OrchestratorConfig)}
    for expected in ("worker", "pipeline", "policy", "library", "monitoring",
                     "monitoring.docker", "arr.radarr", "ntfy", "audit"):
        assert expected in sections, f"{expected} missing from the reference"


def test_the_reference_lifts_help_from_the_source_comments() -> None:
    """Pydantic ignores `#:` comments (they are a Sphinx convention, not Field(description=)),
    so they are read from the source. Adding one is all it takes to document a field."""
    from orchestrator.config_schema import describe

    worker = next(s for s in describe(OrchestratorConfig) if s.name == "worker")
    by_name = {f.name: f for f in worker.fields}
    assert "dead-lettered" in by_name["max_attempts"].help
    assert by_name["retry_backoff_s"].type == "list[int]"
    assert by_name["retry_backoff_s"].default == "[30, 300, 1800]"


def test_the_reference_does_not_label_undocumented_models_with_pydantics_docstring() -> None:
    """inspect.getdoc walks up to BaseModel; a section with no docstring must show nothing
    rather than pydantic's own class documentation."""
    from orchestrator.config_schema import describe

    for s in describe(OrchestratorConfig):
        assert "Usage Documentation" not in s.summary


def test_the_reference_never_exposes_a_resolved_secret(monkeypatch) -> None:
    """The reference shows DEFAULTS from the model, never values from the loaded config — which
    has had ${VAR} expanded. This is the reason it is read-only."""
    from orchestrator.config_schema import describe

    monkeypatch.setenv("RADARR_API_KEY", "super-secret-key")
    cfg = parse_config(OrchestratorConfig, "arr: {radarr: {base_url: 'x', api_key: '${RADARR_API_KEY}'}}")
    assert cfg.arr.radarr.api_key == "super-secret-key"  # the loaded model does hold it

    rendered = " ".join(
        f"{f.name} {f.default} {f.help}" for s in describe(OrchestratorConfig) for f in s.fields
    )
    assert "super-secret-key" not in rendered
