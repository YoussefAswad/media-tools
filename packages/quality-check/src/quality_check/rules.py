"""The quality rules, ported 1:1 from arr-quality-check.sh (bash + jq) to pure Python.

A ``check`` over a reconciled probe returns zero or more :class:`Finding`s. Classification is
by *probed codec identity* (never the track title), matching mkvprune's policy:

* **Rule A** — non-Dolby/DTS multichannel audio: audio with ``channels > audio_min_channels``
  whose canonical codec is not in ``audio_allow`` (catches Opus/AAC 5.1).
* **Rule B** — foreign image-based subtitles: subtitle whose ffprobe codec is in
  ``subtitle_image_codecs`` and whose language is not in ``subtitle_keep_langs`` (untagged
  counts only when ``subtitle_flag_untagged``).
* **Rule C** — too many audio tracks: audio track count ``> audio_max_tracks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track, is_undetermined, lang_in


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


def foreign_image_subs(probe: ProbeResult, policy: PolicyConfig) -> list[Track]:
    """Subtitle tracks that are image-based AND not in the keep languages."""
    out: list[Track] = []
    for t in probe.subtitles:
        codec = (t.codec_name or "").lower()
        if codec not in policy.subtitle_image_codecs:
            continue
        if is_undetermined(t.language):
            if policy.subtitle_flag_untagged:
                out.append(t)
            continue
        if not lang_in(t, policy.subtitle_keep_langs):
            out.append(t)
    return out


def non_allowed_multichannel(probe: ProbeResult, policy: PolicyConfig) -> list[Track]:
    """Audio tracks with >min channels whose codec is not in the passthrough allowlist."""
    out: list[Track] = []
    for t in probe.audio:
        over_min = t.channels is not None and t.channels > policy.audio_min_channels
        if over_min and t.audio_codec not in policy.audio_allow:
            out.append(t)
    return out


def check(probe: ProbeResult, policy: PolicyConfig) -> list[Finding]:
    findings: list[Finding] = []

    bad_audio = non_allowed_multichannel(probe, policy)
    if bad_audio:
        findings.append(
            Finding("A", "Non-Dolby/DTS multichannel audio", [_audio_desc(t) for t in bad_audio])
        )

    foreign = foreign_image_subs(probe, policy)
    if foreign:
        findings.append(
            Finding("B", "Foreign image-based subtitles", [_sub_desc(t) for t in foreign])
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
