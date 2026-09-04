"""Compute the mkvprune invocation for a file, from policy — the pure decision layer.

Policy (confirmed): **keep every audio track** (mkvprune still re-encodes only the non-Dolby/DTS
multichannel ones to AC-3, per its own classifier), and **drop all non-eng/ara subtitles** (text
and image alike). This module turns a probe + policy into the exact ``--audio``/``--subs`` selection,
and reports whether any work is needed at all (so clean files are never remuxed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from mkvprune.classify import lang_key, needs_compat_downmix
from mkvprune.mux import is_compat_track

from mediacore.models import PolicyConfig
from mediacore.policy.rules import foreign_subs
from mediacore.probe import ProbeResult, Track, classify_audio_plan, is_undetermined, lang_in

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
    #: duplicate AC-3 compatibility downmixes left behind by a repeat prune. Also folded into
    #: ``dropped_audio``; kept separately so the summary can say what they actually are.
    surplus_compat: list[Track] = field(default_factory=list)
    #: the file carries a compatibility downmix but the default flag sits elsewhere. Nothing to
    #: drop — it needs a remux so mkvprune can move the flag.
    compat_default_fix: bool = False

    @property
    def needs_work(self) -> bool:
        return (
            self.remux
            or bool(self.dropped_subs)
            or bool(self.audio_encode_tracks)
            or bool(self.dropped_audio)
            or self.compat_default_fix
        )

    @property
    def dropped_by_language(self) -> list[Track]:
        """The audio drops the language filter is responsible for — ``dropped_audio`` minus the
        duplicate downmixes, which are cleanup and read wrong under a language heading."""
        surplus = {t.mkv_id for t in self.surplus_compat}
        return [t for t in self.dropped_audio if t.mkv_id not in surplus]

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
        by_lang = self.dropped_by_language
        if by_lang:
            langs = ", ".join(sorted({t.language or "und" for t in by_lang}))
            parts.append(f"dropped {len(by_lang)} audio track(s) [{langs}]")
        if self.surplus_compat:
            parts.append(f"dropped {len(self.surplus_compat)} duplicate AC-3 compat track(s)")
        if self.compat_default_fix:
            parts.append("make the AC-3 compat track default")
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


def surplus_compat_tracks(tracks: list[Track]) -> list[Track]:
    """Duplicate compatibility downmixes among ``tracks``, keeping the first of each kind.

    A repeat prune of a >6-channel source used to add a *second* identical downmix (and a third,
    and a fourth): the source it derives from is never modified, so nothing about that file said
    the work had already been done. Two tracks carrying mkvprune's own stamp with the same
    language, codec and channel count are that bug and nothing else.
    """
    seen: set[tuple[str, str, int | None]] = set()
    surplus: list[Track] = []
    for t in sorted((t for t in tracks if is_compat_track(t)), key=lambda t: t.mkv_id):
        key = (lang_key(t), t.audio_codec, t.channels)
        if key in seen:
            surplus.append(t)
        else:
            seen.add(key)
    return surplus


def compat_default_misplaced(kept: list[Track]) -> bool:
    """True when a kept downmix exists but the default flag sits on the source it stands in for.

    mkvprune gives the downmix the flag on the mux that creates it, but files pruned before that
    was true still point players at the >6-channel track they cannot decode. This mirrors
    ``mkvprune.mux._compat_stand_in`` exactly — flagging a file the remux would not actually
    change would leave it "needs work" forever.
    """
    compat = [t for t in kept if is_compat_track(t)]
    if not compat or any(t.default for t in compat):
        return False
    chosen = next((t for t in kept if t.default), None)
    if chosen is None or not needs_compat_downmix(chosen):
        return False
    return any(lang_key(t) == lang_key(chosen) for t in compat)


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
    kept_audio = [t for t in probe.audio if t.mkv_id in set(keep_audio_ids)]

    # Clean up after the repeat-prune bug: drop every downmix past the first of its kind. Guarded
    # by the same rail as the language filter — never empty a file's audio.
    surplus = surplus_compat_tracks(kept_audio)
    if surplus and len(surplus) < len(kept_audio):
        surplus_ids = {t.mkv_id for t in surplus}
        keep_audio_ids = [i for i in keep_audio_ids if i not in surplus_ids]
        kept_audio = [t for t in kept_audio if t.mkv_id not in surplus_ids]
        dropped_audio = [*dropped_audio, *surplus]
    else:
        surplus = []

    # Only tracks that survive the language filter are worth re-encoding — encoding a track that
    # is about to be dropped would burn hours for nothing. The plan classifier additionally sees
    # across tracks, so a source whose downmix is already present is left alone.
    actions = classify_audio_plan(probe.audio, kept_ids=keep_audio_ids)
    audio_encode = [t for t in kept_audio if actions[t.mkv_id].is_encode]

    remux = policy.remux_to_mkv and os.path.splitext(probe.path)[1].lower() in REMUX_EXTS

    return PruneDecision(
        keep_sub_ids=keep_ids,
        dropped_subs=foreign,
        audio_encode_tracks=audio_encode,
        remux=remux,
        keep_audio_ids=keep_audio_ids,
        dropped_audio=dropped_audio,
        audio_filter_abandoned=abandoned,
        surplus_compat=surplus,
        compat_default_fix=compat_default_misplaced(kept_audio),
    )


def build_mkvprune_argv(
    path: str,
    decision: PruneDecision,
    *,
    executable: str = "mkvprune",
    yes: bool = True,
    scratch_dir: str | None = None,
) -> list[str]:
    """The exact command the orchestrator runs (also handy for --dry-run / tests)."""
    argv = [executable]
    if yes:
        argv.append("-y")
    if scratch_dir:
        argv += ["--scratch-dir", scratch_dir]
    argv += ["--audio", decision.audio_arg, "--subs", decision.subs_arg, path]
    return argv
