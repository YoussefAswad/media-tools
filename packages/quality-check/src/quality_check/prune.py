"""Compute the mkvprune invocation for a file, from policy — the pure decision layer.

Policy (confirmed): **keep every audio track** (mkvprune still re-encodes only the non-Dolby/DTS
multichannel ones to AC-3, per its own classifier), and **drop only non-eng/ara image-based
subtitles**. This module turns a probe + policy into the exact ``--audio``/``--subs`` selection,
and reports whether any work is needed at all (so clean files are never remuxed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track, classify_audio

from quality_check.rules import foreign_image_subs


@dataclass
class PruneDecision:
    keep_sub_ids: list[int]
    dropped_subs: list[Track] = field(default_factory=list)
    audio_encode_tracks: list[Track] = field(default_factory=list)

    @property
    def needs_work(self) -> bool:
        return bool(self.dropped_subs) or bool(self.audio_encode_tracks)

    @property
    def audio_arg(self) -> str:
        return "all"

    @property
    def subs_arg(self) -> str:
        if not self.dropped_subs:
            return "all"
        if not self.keep_sub_ids:
            return "none"
        return ",".join(str(i) for i in self.keep_sub_ids)

    def summary(self) -> str:
        parts: list[str] = []
        if self.dropped_subs:
            langs = ", ".join(sorted({t.language or "und" for t in self.dropped_subs}))
            parts.append(f"dropped {len(self.dropped_subs)} foreign image sub(s) [{langs}]")
        if self.audio_encode_tracks:
            codecs = ", ".join(sorted({t.audio_codec for t in self.audio_encode_tracks}))
            parts.append(f"re-encode {len(self.audio_encode_tracks)} audio track(s) [{codecs}] → AC-3")
        return "; ".join(parts) if parts else "nothing to do"


def decide(probe: ProbeResult, policy: PolicyConfig) -> PruneDecision:
    foreign = foreign_image_subs(probe, policy)
    if policy.keep_default_subtitle:
        foreign = [t for t in foreign if not t.default]

    drop_ids = {t.mkv_id for t in foreign}
    keep_ids = [t.mkv_id for t in probe.subtitles if t.mkv_id not in drop_ids]

    audio_encode = [t for t in probe.audio if classify_audio(t).is_encode]

    return PruneDecision(
        keep_sub_ids=keep_ids,
        dropped_subs=foreign,
        audio_encode_tracks=audio_encode,
    )


def build_mkvprune_argv(
    path: str, decision: PruneDecision, *, executable: str = "mkvprune", yes: bool = True
) -> list[str]:
    """The exact command the orchestrator runs (also handy for --dry-run / tests)."""
    argv = [executable]
    if yes:
        argv.append("-y")
    argv += ["--audio", decision.audio_arg, "--subs", decision.subs_arg, path]
    return argv
