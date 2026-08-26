"""Shared test fixtures. Tiny MKVs are generated at test time with ffmpeg/mkvmerge;
no binary media is committed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mkvprune.probe import Track

TOOLS = ("ffmpeg", "ffprobe", "mkvmerge")
_HAVE_TOOLS = all(shutil.which(t) for t in TOOLS)

requires_tools = pytest.mark.skipif(
    not _HAVE_TOOLS, reason="requires ffmpeg, ffprobe and mkvmerge on PATH"
)


def make_track(**overrides) -> Track:
    """Construct a Track with sensible defaults, overriding any field."""
    defaults = {
        "mkv_id": 0,
        "ffprobe_index": 0,
        "kind": "audio",
        "codec_id": "A_FLAC",
        "codec_name": "flac",
        "codec_human": "FLAC",
        "uid": 1,
        "language": "eng",
        "language_ietf": "en",
        "track_name": "",
        "default": False,
        "forced": False,
        "hearing_impaired": False,
        "visual_impaired": False,
        "commentary": False,
        "channels": 6,
        "channel_layout": "5.1",
        "sample_rate": 48000,
        "bit_rate": None,
        "profile": None,
        "delay_ms": 0,
    }
    defaults.update(overrides)
    return Track(**defaults)  # type: ignore[arg-type]


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="session")
def sample_mkv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A multi-track MKV: video + stereo AAC (default) + FLAC 5.1 + FLAC 7.1 + AC-3,
    two subtitle tracks, chapters, and a font attachment."""
    if not _HAVE_TOOLS:
        pytest.skip("requires ffmpeg/ffprobe/mkvmerge")
    d = tmp_path_factory.mktemp("sample")
    base = d / "base.mkv"
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
            "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=48000",
            "-f", "lavfi", "-i", "anullsrc=cl=5.1:r=48000",
            "-f", "lavfi", "-i", "anullsrc=cl=7.1:r=48000",
            "-f", "lavfi", "-i", "anullsrc=cl=5.1:r=48000",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a", "-map", "4:a",
            "-t", "1", "-c:v", "libx264",
            "-c:a:0", "aac", "-c:a:1", "flac", "-c:a:2", "flac", "-c:a:3", "ac3", "-b:a:3", "640k",
            str(base),
        ]
    )
    eng = d / "eng.srt"
    spa = d / "spa.srt"
    eng.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    spa.write_text("1\n00:00:00,000 --> 00:00:01,000\nhola\n")
    font = d / "font.ttf"
    font.write_bytes(b"not a real font")
    chapters = d / "chapters.txt"
    chapters.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Two\n"
    )
    out = d / "sample.mkv"
    _run(
        [
            "mkvmerge", "-q", "-o", str(out),
            "--language", "1:eng", "--track-name", "1:Stereo AAC", "--default-track-flag", "1:1",
            "--language", "2:eng", "--track-name", "2:FLAC 5.1",
            "--language", "3:eng", "--track-name", "3:FLAC 7.1",
            "--language", "4:eng", "--track-name", "4:AC3 5.1",
            str(base),
            "--language", "0:eng", "--track-name", "0:English", "--default-track-flag", "0:1",
            str(eng),
            "--language", "0:spa", "--track-name", "0:Spanish", str(spa),
            "--attachment-mime-type", "application/x-truetype-font", "--attach-file", str(font),
            "--chapters", str(chapters),
        ]
    )
    return out


@pytest.fixture(scope="session")
def sample_mp4_with_cover(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tiny MP4 with one video track, one AC-3 5.1 track, and embedded cover art.

    ffmpeg reports the artwork as a video stream flagged ``attached_pic`` while mkvmerge
    reports it as an attachment — the shape that used to break the ID reconciliation.
    """
    if not _HAVE_TOOLS:
        pytest.skip("requires ffmpeg/ffprobe/mkvmerge")
    d = tmp_path_factory.mktemp("sample_mp4")
    base = d / "base.mp4"
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
            "-f", "lavfi", "-i", "anullsrc=cl=5.1:r=48000",
            "-map", "0:v", "-map", "1:a", "-t", "1",
            "-c:v", "libx264", "-c:a", "ac3", "-b:a", "640k",
            str(base),
        ]
    )
    cover = d / "cover.jpg"
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(cover),
        ]
    )
    out = d / "sample.mp4"
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(base), "-i", str(cover),
            "-map", "0", "-map", "1", "-c", "copy", "-c:v:1", "mjpeg",
            "-disposition:v:1", "attached_pic",
            str(out),
        ]
    )
    return out
