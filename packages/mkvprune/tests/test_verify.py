"""Container-integrity verification: benign muxer chatter vs. real problems."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import make_track, requires_tools
from mkvprune.errors import VerificationError
from mkvprune.probe import ProbeResult
from mkvprune.verify import (
    _MAX_REPORTED_LINES,
    _check_container_integrity,
    _check_video,
    _fatal_ffmpeg_lines,
    _format_ffmpeg_lines,
    _frame_rates_match,
    _is_progress_line,
    _video_stream,
)

# What ffmpeg's null muxer emits for every TrueHD frame in a passthrough remux.
_TRUEHD_DTS = (
    "[null @ 0x55b4eb0e0540] Application provided invalid, non monotonically "
    "increasing dts to muxer in stream 1: {n} >= {n}"
)
_READ_ERROR = "[matroska,webm @ 0x5588e3ae15c0] Read error at pos. 12345 (of 67890)"
# The same chatter from a stream-copied PGS subtitle track (stream 6 of the Bleach files).
_PGS_DTS = (
    "[null @ 0x55813c0b9c00] Application provided invalid, non monotonically "
    "increasing dts to muxer in stream 6: {a} >= {b}"
)


def test_truehd_dts_chatter_is_not_fatal() -> None:
    stderr = "\n".join(_TRUEHD_DTS.format(n=n) for n in range(5, 105, 5))
    assert _fatal_ffmpeg_lines(stderr) == []


def test_blank_lines_are_ignored() -> None:
    assert _fatal_ffmpeg_lines("\n  \n\n") == []


def test_demuxer_error_is_fatal() -> None:
    assert _fatal_ffmpeg_lines(_READ_ERROR) == [_READ_ERROR]


def test_mixed_output_keeps_only_the_real_error() -> None:
    stderr = "\n".join([_TRUEHD_DTS.format(n=5), _READ_ERROR, _TRUEHD_DTS.format(n=10)])
    assert _fatal_ffmpeg_lines(stderr) == [_READ_ERROR]


def test_repeat_collapse_line_is_not_fatal() -> None:
    # Verbatim shape of a real capture: a PGS subtitle track copied into the null muxer emits
    # the same benign DTS chatter as TrueHD, and ffmpeg collapses consecutive duplicates into
    # its own "Last message repeated" line. Filtering the chatter but keeping its companions
    # failed 30 files ffmpeg itself exited 0 on.
    stderr = "\n".join([
        _PGS_DTS.format(a=14570, b=14568),
        _PGS_DTS.format(a=14570, b=14503),
        "    Last message repeated 1 times",
        _PGS_DTS.format(a=16906, b=16904),
        "    Last message repeated 3 times",
    ])
    assert _fatal_ffmpeg_lines(stderr) == []


def test_repeated_real_error_still_reports_its_first_occurrence() -> None:
    # The loosening is safe because a genuinely repeated error still prints once itself.
    stderr = "\n".join([_READ_ERROR, "    Last message repeated 9 times"])
    assert _fatal_ffmpeg_lines(stderr) == [_READ_ERROR]


def test_format_below_cap_is_verbatim() -> None:
    lines = ["a", "b", "c"]
    assert _format_ffmpeg_lines(lines) == "a\nb\nc"


def test_format_caps_and_counts_the_remainder() -> None:
    lines = [f"line {i}" for i in range(_MAX_REPORTED_LINES + 7)]
    out = _format_ffmpeg_lines(lines)
    assert out.count("\n") == _MAX_REPORTED_LINES  # capped lines + the summary line
    assert out.endswith("... (7 more)")
    assert f"line {_MAX_REPORTED_LINES}" not in out


@requires_tools
@pytest.mark.parametrize("deep", [False, True])
def test_intact_file_passes(sample_mkv: Path, deep: bool) -> None:
    # deep=True must survive the subtitle and attachment streams too: the null muxer has
    # no encoder for them, so they have to be stream-copied rather than re-encoded.
    _check_container_integrity(str(sample_mkv), deep=deep, verbose=False)


@requires_tools
def test_truncated_file_still_fails(sample_mkv: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mkv"
    shutil.copy(sample_mkv, broken)
    with broken.open("r+b") as fh:
        fh.truncate(int(broken.stat().st_size * 0.6))
    with pytest.raises(VerificationError, match="container integrity"):
        _check_container_integrity(str(broken), deep=False, verbose=False)


def test_progress_status_lines_are_not_fatal() -> None:
    # `ffmpeg -progress pipe:1` merges these key=value lines into the stream we scan for
    # errors; they must be ignored so a healthy file doesn't fail verification.
    noise = "\n".join(
        ["frame=123", "fps=24.0", "out_time_us=4500000", "stream_0_0_q=-1.0",
         "total_size=1048576", "speed=1.2x", "progress=continue", "progress=end"]
    )
    assert _fatal_ffmpeg_lines(noise) == []
    assert _is_progress_line("out_time_us=4500000")
    assert _is_progress_line("stream_0_0_q=-1.0")
    assert not _is_progress_line(_READ_ERROR)


@requires_tools
def test_integrity_reports_progress(sample_mkv: Path) -> None:
    fracs: list[float] = []
    _check_container_integrity(
        str(sample_mkv), deep=False, verbose=False,
        duration_s=1.0, on_progress=fracs.append,
    )
    # phase flips immediately (0.0) and always resolves to a final 1.0 at progress=end
    assert fracs and fracs[0] == 0.0
    assert fracs[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in fracs)


def test_video_stream_skips_leading_cover_art() -> None:
    # Artwork at ffprobe index 0, the real video at index 1: the reconciled track model
    # decides, not the stream order.
    pr = ProbeResult(
        path="x.mp4",
        tracks=[make_track(kind="video", mkv_id=0, ffprobe_index=1, codec_name="hevc")],
        duration_s=1.0,
        chapter_entries=0,
        attachment_count=1,
        ffprobe_json={
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "mjpeg"},
                {"index": 1, "codec_type": "video", "codec_name": "hevc"},
            ]
        },
    )
    assert _video_stream(pr)["codec_name"] == "hevc"


def test_video_stream_without_video_track() -> None:
    pr = ProbeResult(
        path="x.mka", tracks=[], duration_s=None, chapter_entries=0, attachment_count=0
    )
    assert _video_stream(pr) == {}


def _video_probe(**stream) -> ProbeResult:
    """A ProbeResult whose single video track carries the given ffprobe stream fields."""
    return ProbeResult(
        path="x.mkv",
        tracks=[make_track(kind="video", mkv_id=0, ffprobe_index=0, codec_name="h264")],
        duration_s=1.0,
        chapter_entries=0,
        attachment_count=0,
        ffprobe_json={
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", **stream}]
        },
    )


def test_frame_rate_survives_a_container_change() -> None:
    # Verbatim from the remux that dead-lettered: ffprobe reads The Deer Hunter's mp4 rate in
    # the movie timescale, mkvmerge writes Matroska's nearest well-known default_duration.
    # 23.97599 vs 23.97602 — the same rate spelled two ways, 1.6e-6 apart.
    assert _frame_rates_match("13978/583", "24000/1001")
    # The other shape measured on this library: an exact 29.97 against true NTSC, 1.0e-5.
    assert _frame_rates_match("2997/100", "30000/1001")


def test_ntsc_pulldown_pairs_stay_distinct() -> None:
    # The bound the tolerance is set against: 1/1001 apart is a real rate change, not a
    # re-derived rational, and must keep failing in both directions.
    assert not _frame_rates_match("24/1", "24000/1001")
    assert not _frame_rates_match("24000/1001", "24/1")
    assert not _frame_rates_match("30/1", "30000/1001")
    assert not _frame_rates_match("60000/1001", "60/1")
    # and anything grosser, e.g. a PAL speed-up or a halved rate from dropped frames
    assert not _frame_rates_match("25/1", "24/1")
    assert not _frame_rates_match("24000/1001", "12000/1001")


def test_unreadable_frame_rate_falls_back_to_an_exact_compare() -> None:
    # ffprobe's "no rate" spelling divides by zero; nothing is loosened for it.
    assert _frame_rates_match("0/0", "0/0")
    assert not _frame_rates_match("0/0", "24000/1001")
    assert not _frame_rates_match("24000/1001", "0/0")
    assert _frame_rates_match(None, None)
    assert not _frame_rates_match(None, "24000/1001")


def test_check_video_accepts_the_re_derived_rational() -> None:
    src = _video_probe(width=1904, height=816, r_frame_rate="13978/583")
    out = _video_probe(width=1904, height=816, r_frame_rate="24000/1001")
    _check_video(src, out)  # must not raise


def test_check_video_still_fails_a_real_rate_change() -> None:
    src = _video_probe(width=1904, height=816, r_frame_rate="24000/1001")
    out = _video_probe(width=1904, height=816, r_frame_rate="24/1")
    with pytest.raises(VerificationError, match="r_frame_rate"):
        _check_video(src, out)
