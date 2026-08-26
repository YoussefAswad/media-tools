"""Audio codec policy: passthrough vs. convert, target codec, and bitrate ladder.

Classification is done strictly on the probe's codec identity (mkvmerge ``codec_id`` /
ffprobe ``codec_name``) — never on the human-readable track title, which can lie
(a track named "Dolby Digital 5.1" may actually be FLAC).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mkvprune.probe import Track

# Codecs that must never be re-encoded.
PASSTHROUGH_CODECS: frozenset[str] = frozenset(
    {"ac3", "eac3", "truehd", "dts", "mlp"}
)

# AC-3 / E-AC-3 only support these sample rates.
SUPPORTED_SAMPLE_RATES: frozenset[int] = frozenset({32000, 44100, 48000})

# FFmpeg's native ac3/eac3 encoders top out at 6 channels.
MAX_ENCODER_CHANNELS = 6

# Only *multi-channel* (> 2) convertible tracks are re-encoded. Mono/stereo tracks in any
# codec are kept as-is: transcoding stereo AAC/FLAC/Opus to AC-3 wastes bits and quality
# for no compatibility gain.
STEREO_MAX_CHANNELS = 2

AC3_MAX_BITRATE_BPS = 640_000


def parse_bitrate(text: str) -> int:
    """Parse a bitrate like ``640k`` / ``1536000`` into bits per second."""
    s = text.strip().lower()
    try:
        if s.endswith("k"):
            return round(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return round(float(s[:-1]) * 1_000_000)
        return int(s)
    except ValueError as exc:  # pragma: no cover - guarded by argparse usage
        raise ValueError(f"invalid bitrate: {text!r}") from exc


def format_bitrate(bps: int) -> str:
    """Render bits per second as an ffmpeg-friendly ``NNNk`` string."""
    if bps % 1000 == 0:
        return f"{bps // 1000}k"
    return str(bps)


def ac3_ladder_bitrate(channels: int | None) -> int:
    """Channel-based AC-3 ladder: 1.0->224k, 2.0->448k, else->640k."""
    if channels == 1:
        return 224_000
    if channels == 2:
        return 448_000
    return AC3_MAX_BITRATE_BPS


@dataclass
class AudioAction:
    """The plan for one kept audio track."""

    kind: str  # "passthrough" | "convert" | "compat"
    target_codec: str | None = None  # "ac3" | "eac3"
    bitrate_bps: int | None = None
    target_channels: int | None = None  # channels of the encoded output
    resample_to: int | None = None  # 48000 when source rate is unsupported
    warnings: list[str] = field(default_factory=list)

    @property
    def is_encode(self) -> bool:
        return self.kind in {"convert", "compat"}

    @property
    def file_ext(self) -> str:
        return "ec3" if self.target_codec == "eac3" else "ac3"

    def action_label(self) -> str:
        if self.kind == "passthrough":
            return "keep"
        rate = format_bitrate(self.bitrate_bps) if self.bitrate_bps else "?"
        codec = "E-AC-3" if self.target_codec == "eac3" else "AC-3"
        if self.kind == "compat":
            return f"keep + add 5.1 {codec} {rate}"
        return f"→ {codec} {rate}"


def _resample_target(track: Track, warnings: list[str]) -> int | None:
    rate = track.sample_rate
    if rate is None:
        return None
    if rate in SUPPORTED_SAMPLE_RATES:
        return None
    warnings.append(
        f"track {track.mkv_id}: sample rate {rate} Hz is unsupported by AC-3/E-AC-3; "
        "resampling to 48000 Hz"
    )
    return 48000


def classify_audio(
    track: Track,
    *,
    ac3_bitrate_override: int | None = None,
) -> AudioAction:
    """Decide what to do with one audio track.

    * Passthrough codecs are kept untouched.
    * Convertible codecs with <=6 channels are re-encoded to AC-3 (ladder bitrate).
    * Convertible codecs with >6 channels take the compatibility path: the original is
      kept untouched AND a downmixed 5.1 AC-3 track is added (encoders cap at 6ch and
      ffmpeg would otherwise silently downmix a mislabeled track).
    """
    codec = track.audio_codec
    if codec in PASSTHROUGH_CODECS:
        return AudioAction(kind="passthrough")

    warnings: list[str] = []

    # Unknown channel count -> don't guess; keep the track untouched.
    if track.channels is None:
        warnings.append(
            f"track {track.mkv_id}: unknown channel count for {codec.upper()}; "
            "keeping it as-is (not re-encoding)"
        )
        return AudioAction(kind="passthrough", warnings=warnings)

    channels = track.channels

    # Mono/stereo convertible tracks are kept as-is (only multichannel is re-encoded).
    if channels <= STEREO_MAX_CHANNELS:
        return AudioAction(kind="passthrough")

    if channels > MAX_ENCODER_CHANNELS:
        # Compatibility path: keep original + add a 5.1 AC-3 downmix.
        bitrate = ac3_bitrate_override or AC3_MAX_BITRATE_BPS
        bitrate = min(bitrate, AC3_MAX_BITRATE_BPS)
        warnings.append(
            f"track {track.mkv_id}: {channels}-channel {codec.upper()} exceeds the "
            "6-channel AC-3/E-AC-3 encoder limit. Keeping the original untouched and "
            "adding a downmixed 5.1 AC-3 compatibility track."
        )
        return AudioAction(
            kind="compat",
            target_codec="ac3",
            bitrate_bps=bitrate,
            target_channels=6,
            resample_to=_resample_target(track, warnings),
            warnings=warnings,
        )

    # <=6 channels -> AC-3, preserving channel count.
    if ac3_bitrate_override is not None:
        bitrate = min(ac3_bitrate_override, AC3_MAX_BITRATE_BPS)
        if ac3_bitrate_override > AC3_MAX_BITRATE_BPS:
            warnings.append(
                f"track {track.mkv_id}: requested AC-3 bitrate exceeds the 640k maximum; "
                "capping at 640k"
            )
    else:
        bitrate = ac3_ladder_bitrate(track.channels)

    return AudioAction(
        kind="convert",
        target_codec="ac3",
        bitrate_bps=bitrate,
        target_channels=track.channels,
        resample_to=_resample_target(track, warnings),
        warnings=warnings,
    )
