from __future__ import annotations

from pathlib import Path

from mediacore.audit import Audit
from mediacore.db import open_db
from mediacore.models import ArrInstance, CoreConfig
from mediacore.notify import Notifier
from mediacore.probe import ProbeResult, Track
from orchestrator.alerts import AlertEngine
from orchestrator.config import OrchestratorConfig
from orchestrator.ledger import Cutoffs, Ledger
from orchestrator.library import LibraryIndex
from orchestrator.metrics import Metrics
from orchestrator.queue import JobQueue
from orchestrator.services import Services
from orchestrator.wan_usage import WanUsage


def mk(
    mkv_id: int,
    kind: str,
    *,
    codec_name: str | None = None,
    language: str = "und",
    default: bool = False,
    channels: int | None = None,
) -> Track:
    return Track(
        mkv_id=mkv_id,
        ffprobe_index=mkv_id,
        kind=kind,
        codec_id="",
        codec_name=codec_name,
        codec_human=codec_name or "",
        uid=None,
        language=language,
        language_ietf=None,
        track_name="",
        default=default,
        forced=False,
        hearing_impaired=False,
        visual_impaired=False,
        commentary=False,
        channels=channels,
        channel_layout=None,
        sample_rate=48000,
        bit_rate=None,
        profile=None,
        delay_ms=0,
    )


def probe_of(*tracks: Track, path: str = "/x/f.mkv") -> ProbeResult:
    return ProbeResult(
        path=path,
        tracks=list(tracks),
        duration_s=100.0,
        chapter_entries=0,
        attachment_count=0,
    )


class FakeNotifier:
    """Collects messages instead of posting them. Services reads ``services.notifier`` at send
    time, so assigning one of these after construction is enough to capture everything."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> bool:
        self.sent.append(msg)
        return True

    @property
    def titles(self) -> list[str]:
        return [m.title for m in self.sent]


def build_test_services(tmp_path: Path, config: OrchestratorConfig | None = None) -> Services:
    cfg = config or OrchestratorConfig()
    cfg.audit.state_dir = tmp_path / "state"
    db = open_db(cfg.audit.state_dir)
    audit = Audit(db, cfg.audit.state_dir)
    notifier = Notifier(cfg.ntfy)
    queue = JobQueue(db, max_attempts=cfg.worker.max_attempts)
    return Services(
        config=cfg,
        config_path=tmp_path / "config.yaml",
        db=db,
        audit=audit,
        notifier=notifier,
        queue=queue,
        ledger=Ledger(db),
        cutoffs=Cutoffs(db),
        library=LibraryIndex(db),
        metrics=Metrics(db),
        alerts=AlertEngine(db),
        wan=WanUsage(db),
    )


def radarr_config(**kw) -> OrchestratorConfig:
    cfg = OrchestratorConfig(**kw)
    cfg.arr = CoreConfig().arr
    cfg.arr.radarr = ArrInstance(base_url="http://radarr", api_key="k")
    return cfg
