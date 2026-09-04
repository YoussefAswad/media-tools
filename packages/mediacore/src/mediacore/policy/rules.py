"""The quality rules, ported 1:1 from arr-quality-check.sh (bash + jq) to pure Python.

A ``check`` over a reconciled probe returns zero or more :class:`Finding`s. Classification is
by *probed codec identity* (never the track title), matching mkvprune's policy:

* **Rule A** — non-Dolby/DTS multichannel audio: audio with ``channels > audio_min_channels``
  whose canonical codec is not in ``audio_allow`` (catches Opus/AAC 5.1). A >6-channel track
  whose AC-3 compatibility downmix is already in the file is excluded — see below.
* **Rule B** — foreign subtitles: subtitle whose language is not in ``subtitle_keep_langs``,
  regardless of codec (text SRT/ASS and image PGS/VOBSUB alike — this matches the confirmed
  prune policy in ``mediacore.policy.prune``). Untagged/``und`` tracks count only when
  ``subtitle_flag_untagged`` is set.
* **Rule C** — too many audio tracks: audio track count ``> audio_max_tracks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mkvprune.classify import needs_compat_downmix

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track, classify_audio_plan, is_undetermined, lang_in


@dataclass
class Finding:
    rule: str  # "A" | "B" | "C"
    title: str
    details: list[str] = field(default_factory=list)


def _audio_desc(t: Track) -> str:
    parts = [t.audio_codec]
    if t.channels:
        parts.append(f"{t.channels}ch")
    if t.language:
        parts.append(t.language)
    if t.track_name:
        parts.append(f"“{t.track_name}”")
    return " ".join(parts) + f" (id {t.mkv_id})"


def _sub_desc(t: Track) -> str:
    lang = t.language or "und"
    return f"{t.codec_name or t.codec_id} {lang} (id {t.mkv_id})"


#: mkvmerge codec ids for bitmap subtitles, used when ffprobe reports no codec name. Kept
#: alongside the configurable ffprobe names so a track is never misread as text by omission.
_IMAGE_CODEC_IDS = {"S_HDMV/PGS", "S_VOBSUB", "S_DVBSUB", "S_IMAGE/BMP", "S_HDMV/TEXTST"}


def is_image_sub(track: Track, policy: PolicyConfig) -> bool:
    """True for a bitmap subtitle (PGS/VobSub/DVB), by ffprobe codec name or mkvmerge codec id."""
    if (track.codec_name or "").lower() in policy.subtitle_image_codecs:
        return True
    return (track.codec_id or "").upper() in _IMAGE_CODEC_IDS


def foreign_subs(probe: ProbeResult, policy: PolicyConfig) -> list[Track]:
    """Subtitle tracks whose language is not in the keep languages.

    ``policy.subtitle_drop_scope`` decides the codec scope: ``all`` covers text (SRT/ASS) and
    image (PGS/VobSub) alike, while ``image`` leaves foreign text subs alone. Untagged/``und``
    tracks count only when ``subtitle_flag_untagged`` is set.
    """
    image_only = policy.subtitle_drop_scope == "image"
    out: list[Track] = []
    for t in probe.subtitles:
        if image_only and not is_image_sub(t, policy):
            continue
        if is_undetermined(t.language):
            if policy.subtitle_flag_untagged:
                out.append(t)
            continue
        if not lang_in(t, policy.subtitle_keep_langs):
            out.append(t)
    return out


# Back-compat alias. The codec scope is now `policy.subtitle_drop_scope`, so this name is only
# accurate under `scope: image`. Kept so callers/tests importing the old name still resolve.
foreign_image_subs = foreign_subs


def non_allowed_multichannel(probe: ProbeResult, policy: PolicyConfig) -> list[Track]:
    """Audio tracks with >min channels whose codec is not in the passthrough allowlist.

    With one exception: a >6-channel track that already carries its AC-3 compatibility downmix
    in the same file is not reported. What the finding warns about — audio the player cannot
    handle — has been dealt with, and the source track stays non-Dolby by design (the downmix is
    added beside it, never in place of it), so reporting it again is a permanent false alarm.
    """
    actions = classify_audio_plan(probe.audio)
    out: list[Track] = []
    for t in probe.audio:
        over_min = t.channels is not None and t.channels > policy.audio_min_channels
        if not over_min or t.audio_codec in policy.audio_allow:
            continue
        if needs_compat_downmix(t) and not actions[t.mkv_id].is_encode:
            continue  # the downmix it needs is already present
        out.append(t)
    return out


def check(probe: ProbeResult, policy: PolicyConfig) -> list[Finding]:
    findings: list[Finding] = []

    bad_audio = non_allowed_multichannel(probe, policy)
    if bad_audio:
        findings.append(
            Finding("A", "Non-Dolby/DTS multichannel audio", [_audio_desc(t) for t in bad_audio])
        )

    foreign = foreign_subs(probe, policy)
    if foreign:
        findings.append(
            Finding("B", "Foreign subtitles", [_sub_desc(t) for t in foreign])
        )

    n_audio = len(probe.audio)
    if n_audio > policy.audio_max_tracks:
        findings.append(
            Finding(
                "C",
                f"Too many audio tracks ({n_audio} > {policy.audio_max_tracks})",
                [_audio_desc(t) for t in probe.audio],
            )
        )

    return findings
