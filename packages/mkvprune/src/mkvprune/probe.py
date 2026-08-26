"""Probe the input file and build a reconciled track model.

Two probes are combined:

* ``mkvmerge -J`` is the source of truth for **mkvmerge track IDs** (the IDs that
  ``--audio-tracks`` / ``--subtitle-tracks`` accept) and for container-level facts
  (duration, chapters, attachments).
* ``ffprobe`` supplies audio detail (channels, layout, sample rate, bitrate, profile)
  and DV/HDR side-data that mkvmerge does not expose.

ffprobe stream indices and mkvmerge track IDs are *not* interchangeable — they diverge in
the presence of attachment streams, data streams, or embedded cover art. We map by ordinal
within the file's real (track-bearing) stream order and assert that the codec families
agree, failing loudly on any mismatch rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mkvprune.errors import ProbeError
from mkvprune.proc import run_json

# --- kind normalization -------------------------------------------------------------

# mkvmerge uses "subtitles"; ffprobe uses "subtitle".
_MKV_KIND = {"video": "video", "audio": "audio", "subtitles": "subtitle"}
_FFPROBE_REAL_KINDS = {"video", "audio", "subtitle"}


def _is_real_stream(stream: dict[str, Any]) -> bool:
    """True for ffprobe streams that correspond to an mkvmerge *track*.

    ffmpeg models embedded cover art (MP4/M4V/MOV/FLAC/MP3) as a single-frame video
    stream flagged ``attached_pic``; mkvmerge reports the same data as an attachment,
    not a track. Excluding it — along with attachment/data streams — keeps the ordinals
    aligned.
    """
    if stream.get("codec_type") not in _FFPROBE_REAL_KINDS:
        return False
    return not (stream.get("disposition") or {}).get("attached_pic")


# --- codec family canonicalization --------------------------------------------------

# mkvmerge codec_id prefix -> canonical family token
_MKV_CODEC_ID_FAMILY: dict[str, str] = {
    "A_AC3": "ac3",
    "A_EAC3": "eac3",
    "A_TRUEHD": "truehd",
    "A_MLP": "mlp",
    "A_DTS": "dts",
    "A_FLAC": "flac",
    "A_AAC": "aac",
    "A_OPUS": "opus",
    "A_VORBIS": "vorbis",
    "A_ALAC": "alac",
    "A_PCM": "pcm",
    "A_MPEG/L3": "mp3",
    "A_MPEG/L2": "mp2",
}

# ffprobe codec_name -> canonical family token
_FFPROBE_CODEC_FAMILY: dict[str, str] = {
    "ac3": "ac3",
    "eac3": "eac3",
    "truehd": "truehd",
    "mlp": "mlp",
    "dts": "dts",
    "flac": "flac",
    "aac": "aac",
    "aac_latm": "aac",
    "opus": "opus",
    "vorbis": "vorbis",
    "alac": "alac",
    "mp3": "mp3",
    "mp2": "mp2",
    "wmav2": "wma",
    "wmapro": "wma",
}


def canonical_audio_codec(codec_id: str, codec_name: str | None) -> str:
    """Return a canonical audio codec token, preferring the ffprobe name.

    Falls back to the mkvmerge codec_id when ffprobe is silent. Any ``pcm_*`` variant
    collapses to ``pcm``. Unknown codecs return the raw ffprobe name (or a lowercased
    codec_id) so classification can still treat them as "convert".
    """
    name = (codec_name or "").lower()
    if name.startswith("pcm_") or name in {"pcm_bluray", "pcm_dvd"}:
        return "pcm"
    if name in _FFPROBE_CODEC_FAMILY:
        return _FFPROBE_CODEC_FAMILY[name]

    cid = (codec_id or "").upper()
    for prefix, family in _MKV_CODEC_ID_FAMILY.items():
        if cid.startswith(prefix):
            return family

    return name or cid.lower() or "unknown"


def _mkv_family(codec_id: str) -> str | None:
    cid = (codec_id or "").upper()
    for prefix, family in _MKV_CODEC_ID_FAMILY.items():
        if cid.startswith(prefix):
            return family
    # video / subtitle families for the cross-check
    if cid.startswith("V_MPEGH"):
        return "hevc"
    if cid.startswith("V_MPEG4/ISO/AVC"):
        return "h264"
    if cid.startswith("V_AV1"):
        return "av1"
    if cid.startswith("V_VP9"):
        return "vp9"
    if cid.startswith("S_TEXT/UTF8"):
        return "subrip"
    if cid.startswith("S_TEXT/ASS") or cid.startswith("S_TEXT/SSA"):
        return "ass"
    if cid.startswith("S_HDMV/PGS"):
        return "pgs"
    return None


def _ffprobe_family(codec_name: str | None) -> str | None:
    name = (codec_name or "").lower()
    if not name:
        return None
    if name.startswith("pcm_"):
        return None  # pcm has no single mkv codec_id; skip cross-check
    mapping = {
        **_FFPROBE_CODEC_FAMILY,
        "hevc": "hevc",
        "h264": "h264",
        "av1": "av1",
        "vp9": "vp9",
        "subrip": "subrip",
        "ass": "ass",
        "ssa": "ass",
        "hdmv_pgs_subtitle": "pgs",
    }
    return mapping.get(name)


# --- data model ---------------------------------------------------------------------


@dataclass
class Track:
    """One reconciled Matroska track."""

    mkv_id: int
    ffprobe_index: int
    kind: str  # "video" | "audio" | "subtitle"
    codec_id: str  # mkvmerge codec_id, e.g. "A_AC3"
    codec_name: str | None  # ffprobe codec_name, e.g. "ac3"
    codec_human: str  # mkvmerge human codec string, e.g. "AC-3"
    uid: int | None
    language: str  # ISO-639-2, e.g. "eng"
    language_ietf: str | None
    track_name: str
    default: bool
    forced: bool
    hearing_impaired: bool
    visual_impaired: bool
    commentary: bool
    channels: int | None
    channel_layout: str | None
    sample_rate: int | None
    bit_rate: int | None
    profile: str | None
    delay_ms: int  # container timestamp offset to re-apply on a re-encoded replacement

    @property
    def audio_codec(self) -> str:
        return canonical_audio_codec(self.codec_id, self.codec_name)


@dataclass
class ProbeResult:
    path: str
    tracks: list[Track]
    duration_s: float | None
    chapter_entries: int
    attachment_count: int
    mkvmerge_json: dict[str, Any] = field(repr=False, default_factory=dict)
    ffprobe_json: dict[str, Any] = field(repr=False, default_factory=dict)

    def by_kind(self, kind: str) -> list[Track]:
        return [t for t in self.tracks if t.kind == kind]

    @property
    def video(self) -> list[Track]:
        return self.by_kind("video")

    @property
    def audio(self) -> list[Track]:
        return self.by_kind("audio")

    @property
    def subtitles(self) -> list[Track]:
        return self.by_kind("subtitle")


# --- probing ------------------------------------------------------------------------


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _describe_sides(mkv_tracks: list[dict[str, Any]], ff_real: list[dict[str, Any]]) -> str:
    """Render both sides of a failed reconciliation so the error is self-diagnosing."""
    lines = ["  mkvmerge tracks:"]
    lines += [
        f"    id {t.get('id')}: {t.get('type')} ({t.get('codec')})" for t in mkv_tracks
    ] or ["    (none)"]
    lines.append("  ffprobe streams (after filtering):")
    lines += [
        f"    index {s.get('index')}: {s.get('codec_type')} ({s.get('codec_name')})"
        for s in ff_real
    ] or ["    (none)"]
    return "\n".join(lines)


def probe(path: str, *, verbose: bool = False) -> ProbeResult:
    """Probe ``path`` and return a reconciled model, or raise ProbeError."""
    mkv = run_json(["mkvmerge", "-J", path], verbose=verbose)
    # Full stream fields (channels, layout, colour identification, etc.).
    ff = run_json(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        verbose=verbose,
    )
    # Side data (DOVI config, mastering display, content light) needs a dedicated query,
    # because -show_entries would otherwise strip the full stream fields above.
    side = run_json(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_entries",
            "stream=index:stream_side_data_list",
            path,
        ],
        verbose=verbose,
    )
    side_by_index = {
        _to_int(s.get("index")): s.get("side_data_list")
        for s in side.get("streams", [])
        if s.get("side_data_list") is not None
    }
    for s in ff.get("streams", []):
        idx = _to_int(s.get("index"))
        if idx in side_by_index:
            s["side_data_list"] = side_by_index[idx]

    mkv_tracks = mkv.get("tracks", [])
    if not mkv_tracks:
        raise ProbeError(f"mkvmerge found no tracks in {path!r}")

    # ffprobe streams, real tracks only (drop attachment/data/cover-art streams), in order.
    ff_streams_all = ff.get("streams", [])
    ff_real = [s for s in ff_streams_all if _is_real_stream(s)]

    if len(ff_real) != len(mkv_tracks):
        raise ProbeError(
            "Track/stream count mismatch between mkvmerge and ffprobe "
            f"({len(mkv_tracks)} mkvmerge tracks vs {len(ff_real)} real ffprobe streams). "
            "Refusing to guess the ID mapping.\n"
            f"{_describe_sides(mkv_tracks, ff_real)}"
        )

    tracks: list[Track] = []
    for mkv_t, ff_s in zip(mkv_tracks, ff_real, strict=True):
        props = mkv_t.get("properties", {})
        mkv_kind = _MKV_KIND.get(mkv_t.get("type", ""))
        ff_kind = ff_s.get("codec_type")
        if mkv_kind is None:
            raise ProbeError(f"Unknown mkvmerge track type {mkv_t.get('type')!r}")
        if mkv_kind != ff_kind:
            raise ProbeError(
                f"Kind mismatch at ordinal for mkvmerge id {mkv_t.get('id')}: "
                f"mkvmerge says {mkv_kind!r} but ffprobe says {ff_kind!r}. "
                "Refusing to guess the ID mapping."
            )

        # Codec-family cross-check when both sides map to a known family.
        codec_id = props.get("codec_id", "")
        fam_mkv = _mkv_family(codec_id)
        fam_ff = _ffprobe_family(ff_s.get("codec_name"))
        if fam_mkv is not None and fam_ff is not None and fam_mkv != fam_ff:
            raise ProbeError(
                f"Codec mismatch at mkvmerge id {mkv_t.get('id')}: "
                f"mkvmerge codec_id {codec_id!r} (~{fam_mkv}) vs "
                f"ffprobe codec_name {ff_s.get('codec_name')!r} (~{fam_ff}). "
                "Refusing to guess the ID mapping."
            )

        delay_ns = _to_int(props.get("minimum_timestamp")) or 0
        tracks.append(
            Track(
                mkv_id=int(mkv_t["id"]),
                ffprobe_index=_to_int(ff_s.get("index")) or 0,
                kind=mkv_kind,
                codec_id=codec_id,
                codec_name=ff_s.get("codec_name"),
                codec_human=mkv_t.get("codec", ""),
                uid=_to_int(props.get("uid")),
                language=(props.get("language") or "und"),
                language_ietf=props.get("language_ietf"),
                track_name=(props.get("track_name") or ""),
                default=bool(props.get("default_track", False)),
                forced=bool(props.get("forced_track", False)),
                hearing_impaired=bool(props.get("flag_hearing_impaired", False)),
                visual_impaired=bool(props.get("flag_visual_impaired", False)),
                commentary=bool(props.get("flag_commentary", False)),
                channels=_to_int(ff_s.get("channels")),
                channel_layout=ff_s.get("channel_layout"),
                sample_rate=_to_int(ff_s.get("sample_rate")),
                bit_rate=_to_int(ff_s.get("bit_rate")),
                profile=ff_s.get("profile"),
                delay_ms=round(delay_ns / 1_000_000),
            )
        )

    chapters = mkv.get("chapters", [])
    chapter_entries = sum(_to_int(c.get("num_entries")) or 0 for c in chapters)

    return ProbeResult(
        path=path,
        tracks=tracks,
        duration_s=_to_float(ff.get("format", {}).get("duration")),
        chapter_entries=chapter_entries,
        attachment_count=len(mkv.get("attachments", [])),
        mkvmerge_json=mkv,
        ffprobe_json=ff,
    )
