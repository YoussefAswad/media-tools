"""Probe facade: a single import surface over mkvprune's reconciled track model.

mkvprune remains the owner of the mkvmerge↔ffprobe reconciliation and codec classification;
mediacore simply re-exports it so quality-check and the orchestrator share one track model
instead of re-implementing probing. Kept as a thin, side-effect-free re-export.
"""

from __future__ import annotations

from mkvprune.classify import AudioAction, classify_audio, classify_audio_plan
from mkvprune.flags import is_english
from mkvprune.probe import ProbeResult, Track, canonical_audio_codec, probe

# Language helpers ---------------------------------------------------------------------

_ARABIC = {"ar", "ara"}


def is_arabic(language: str | None, language_ietf: str | None) -> bool:
    """Treat ``ar`` / ``ara`` / ``ar-*`` as Arabic (mirror of mkvprune.is_english)."""
    if (language or "").lower() in _ARABIC:
        return True
    code = (language_ietf or "").lower()
    return code == "ar" or code.startswith("ar-")


def is_undetermined(language: str | None) -> bool:
    return (language or "").lower() in {"", "und", "unknown"}


def lang_in(track: Track, langs: set[str]) -> bool:
    """True if the track's ISO-639 or IETF language is in ``langs`` (case-insensitive)."""
    low = {code.lower() for code in langs}
    if (track.language or "").lower() in low:
        return True
    ietf = (track.language_ietf or "").lower()
    return bool(ietf) and (ietf in low or ietf.split("-", 1)[0] in low)


__all__ = [
    "AudioAction",
    "ProbeResult",
    "Track",
    "canonical_audio_codec",
    "classify_audio",
    "classify_audio_plan",
    "is_arabic",
    "is_english",
    "is_undetermined",
    "lang_in",
    "probe",
]
