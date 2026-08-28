"""Compute the mkvprune invocation for a file, from policy — the pure decision layer.

Policy (confirmed): **keep every audio track** (mkvprune still re-encodes only the non-Dolby/DTS
multichannel ones to AC-3, per its own classifier), and **drop all non-eng/ara subtitles** (text
and image alike). This module turns a probe + policy into the exact ``--audio``/``--subs`` selection,
and reports whether any work is needed at all (so clean files are never remuxed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track, classify_audio, is_undetermined, lang_in

from quality_check.rules import foreign_subs

#: mp4-family containers mkvprune can remux to MKV (its accepted inputs minus .mkv).
REMUX_EXTS = {".mp4", ".m4v", ".mov"}


@dataclass
class PruneDecision:
    keep_sub_ids: list[int]
    dropped_subs: list[Track] = field(default_factory=list)
    audio_encode_tracks: list[Track] = field(default_factory=list)
    #: container-only conversion to MKV is required (a non-mkv source under ``remux_to_mkv``),
    #: even if no subs/audio work is needed.
    remux: bool = False
    #: audio tracks the language filter would drop, and the ids it keeps.
    keep_audio_ids: list[int] = field(default_factory=list)
    dropped_audio: list[Track] = field(default_factory=list)
    #: the language filter matched nothing and was abandoned, so every track is kept. Surfaced in
    #: :meth:`summary` because it means the policy disagrees with the file and a human should look.
    audio_filter_abandoned: bool = False

    @property
    def needs_work(self) -> bool:
        return (
            self.remux
            or bool(self.dropped_subs)
            or bool(self.audio_encode_tracks)
            or bool(self.dropped_audio)
        )

    @property
    def audio_arg(self) -> str:
        """The ``--audio`` selection. Never ``"none"``: :func:`decide` refuses to empty a file's
        audio, so an empty keep list can only mean "no filtering happened"."""
        if not self.dropped_audio or not self.keep_audio_ids:
            return "all"
        return ",".join(str(i) for i in self.keep_audio_ids)

    @property
    def subs_arg(self) -> str:
        if not self.dropped_subs:
            return "all"
        if not self.keep_sub_ids:
            return "none"
        return ",".join(str(i) for i in self.keep_sub_ids)

    def summary(self) -> str:
        parts: list[str] = []
        if self.remux:
            parts.append("remux → mkv")
        if self.dropped_audio:
            langs = ", ".join(sorted({t.language or "und" for t in self.dropped_audio}))
            parts.append(f"dropped {len(self.dropped_audio)} audio track(s) [{langs}]")
        if self.audio_filter_abandoned:
            parts.append("kept all audio (language filter would leave none)")
        if self.dropped_subs:
            langs = ", ".join(sorted({t.language or "und" for t in self.dropped_subs}))
            parts.append(f"dropped {len(self.dropped_subs)} foreign sub(s) [{langs}]")
        if self.audio_encode_tracks:
            codecs = ", ".join(sorted({t.audio_codec for t in self.audio_encode_tracks}))
            parts.append(f"re-encode {len(self.audio_encode_tracks)} audio track(s) [{codecs}] → AC-3")
        return "; ".join(parts) if parts else "nothing to do"


def select_audio(
    probe: ProbeResult, policy: PolicyConfig, original_langs: set[str] | None
) -> tuple[list[int], list[Track], bool]:
    """Apply the audio language filter: (kept ids, dropped tracks, filter-abandoned).

    Three things stop this from ever silencing a file, in order:

    1. It only runs when explicitly enabled *and* it knows which languages to keep. An unresolved
       original language contributes nothing, so "keep the original" with no original known
       degrades to "keep everything" rather than to "keep only English".
    2. Untagged tracks (``und``/``mis``/``zxx``) are kept under ``audio_keep_untagged``. An
       unknown language is not an unwanted one — on a real library this was the single case that
       would otherwise have left a file silent.
    3. A hard rail, not configurable: if the filter would drop *every* audio track, it is
       abandoned wholesale and the file keeps all of them. A file that matches nothing means the
       policy is wrong about that file, and a silent video is never the better answer.
    """
    if not policy.audio_language_filter or not probe.audio:
        return [t.mkv_id for t in probe.audio], [], False

    keep_langs = {code.lower() for code in policy.audio_keep_langs}
    if policy.audio_keep_original:
        # Promised to keep the original but don't know what it is (no arr record, an unreachable
        # arr, a placeholder value): filtering now would drop the original track. Keep everything.
        if not original_langs:
            return [t.mkv_id for t in probe.audio], [], False
        keep_langs |= {code.lower() for code in original_langs}
    if not keep_langs:
        return [t.mkv_id for t in probe.audio], [], False

    def wanted(track: Track) -> bool:
        if policy.audio_keep_untagged and is_undetermined(track.language):
            return True
        return lang_in(track, keep_langs)

    kept = [t for t in probe.audio if wanted(t)]
    dropped = [t for t in probe.audio if not wanted(t)]
    if not kept:  # rail: never leave a file with no audio
        return [t.mkv_id for t in probe.audio], [], True
    return [t.mkv_id for t in kept], dropped, False


def decide(
    probe: ProbeResult,
    policy: PolicyConfig,
    *,
    original_langs: set[str] | None = None,
) -> PruneDecision:
    """What mkvprune should do to this file.

    ``original_langs`` are the ISO codes of the title's original language, resolved by the caller
    from the arr (the webhook payload carries ``originalLanguage`` directly; the library scan
    looks it up by path). ``None`` means "unknown", which the audio filter treats as "keep
    everything" rather than as "no original".
    """
    foreign = foreign_subs(probe, policy)
    if policy.keep_default_subtitle:
        foreign = [t for t in foreign if not t.default]

    drop_ids = {t.mkv_id for t in foreign}
    keep_ids = [t.mkv_id for t in probe.subtitles if t.mkv_id not in drop_ids]

    keep_audio_ids, dropped_audio, abandoned = select_audio(probe, policy, original_langs)
    # Only tracks that survive the language filter are worth re-encoding — encoding a track that
    # is about to be dropped would burn hours for nothing.
    kept_audio = [t for t in probe.audio if t.mkv_id in set(keep_audio_ids)]
    audio_encode = [t for t in kept_audio if classify_audio(t).is_encode]

    remux = policy.remux_to_mkv and os.path.splitext(probe.path)[1].lower() in REMUX_EXTS

    return PruneDecision(
        keep_sub_ids=keep_ids,
        dropped_subs=foreign,
        audio_encode_tracks=audio_encode,
        remux=remux,
        keep_audio_ids=keep_audio_ids,
        dropped_audio=dropped_audio,
        audio_filter_abandoned=abandoned,
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
