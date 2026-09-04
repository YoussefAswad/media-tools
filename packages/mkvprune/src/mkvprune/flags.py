"""Default-track-flag resolution (applied independently to audio and subtitles)."""

from __future__ import annotations

from mkvprune.probe import Track


def is_english(language: str | None, language_ietf: str | None) -> bool:
    """Treat ``en`` / ``eng`` / ``en-US`` (any ``en-*``) as English."""
    if (language or "").lower() in {"en", "eng"}:
        return True
    code = (language_ietf or "").lower()
    return code == "en" or code.startswith("en-")


def source_default_id(tracks: list[Track]) -> int | None:
    """The mkvmerge id of the source's default track for a given type, if any."""
    for t in tracks:
        if t.default:
            return t.mkv_id
    return None


def resolve_default(kept: list[Track], src_default_id: int | None) -> int | None:
    """Pick the single output default among kept source tracks of one type.

    ``kept`` are the surviving *source* tracks in output order; added compatibility downmix
    tracks are never *candidates* here and must be excluded by the caller. The caller may
    still hand the resolved flag to a downmix afterwards — see ``mux._resolve_defaults``,
    where a downmix takes the flag on behalf of the source it was made from.

    Priority:
      1. keep the source default if it survived,
      2. otherwise the first kept English track,
      3. otherwise the first kept track,
      4. no kept tracks -> None (no-op).
    """
    if not kept:
        return None
    kept_ids = [t.mkv_id for t in kept]
    if src_default_id is not None and src_default_id in kept_ids:
        return src_default_id
    for t in kept:
        if is_english(t.language, t.language_ietf):
            return t.mkv_id
    return kept[0].mkv_id
