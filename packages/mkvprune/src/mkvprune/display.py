"""Render the pre-prompt track table."""

from __future__ import annotations

from mkvprune.classify import AudioAction
from mkvprune.probe import ProbeResult, Track

_COLUMNS = ["ID", "Type", "Codec", "Ch / Layout", "Rate", "Bitrate", "Lang", "Name", "Flags", "Action"]


def _flags_str(t: Track) -> str:
    flags: list[str] = []
    if t.default:
        flags.append("default")
    if t.forced:
        flags.append("forced")
    if t.hearing_impaired:
        flags.append("HI")
    if t.visual_impaired:
        flags.append("VI")
    if t.commentary:
        flags.append("commentary")
    return ",".join(flags)


def _ch_layout(t: Track) -> str:
    if t.kind != "audio":
        return ""
    if t.channels is None:
        return t.channel_layout or ""
    layout = t.channel_layout or f"{t.channels}ch"
    return f"{t.channels} / {layout}"


def _rate(t: Track) -> str:
    if t.sample_rate is None:
        return ""
    return f"{t.sample_rate / 1000:g}k"


def _bitrate(t: Track) -> str:
    if t.bit_rate is None:
        return ""
    return f"{round(t.bit_rate / 1000)}k"


def _lang(t: Track) -> str:
    if t.language_ietf and t.language_ietf not in {"und", t.language}:
        return f"{t.language} ({t.language_ietf})"
    return t.language


def build_rows(probe: ProbeResult, audio_actions: dict[int, AudioAction]) -> list[list[str]]:
    rows: list[list[str]] = []
    for t in probe.tracks:
        if t.kind == "video":
            action = "keep (untouched)"
        elif t.kind == "audio":
            act = audio_actions.get(t.mkv_id)
            action = act.action_label() if act else "keep"
        else:
            action = "keep"
        rows.append(
            [
                str(t.mkv_id),
                t.kind,
                t.codec_human or (t.codec_name or ""),
                _ch_layout(t),
                _rate(t),
                _bitrate(t),
                _lang(t),
                t.track_name,
                _flags_str(t),
                action,
            ]
        )
    return rows


def render_table(probe: ProbeResult, audio_actions: dict[int, AudioAction]) -> str:
    rows = build_rows(probe, audio_actions)
    widths = [len(h) for h in _COLUMNS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt(_COLUMNS), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)
