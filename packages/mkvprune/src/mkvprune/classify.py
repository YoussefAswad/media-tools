"""Audio codec policy: passthrough vs. convert, target codec, and bitrate ladder.

Classification is done strictly on the probe's codec identity (mkvmerge ``codec_id`` /
ffprobe ``codec_name``) — never on the human-readable track title, which can lie
(a track named "Dolby Digital 5.1" may actually be FLAC).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
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

# Codecs whose presence already satisfies the goal the 5.1 downmix exists for: something a
# limited player can decode. Only ever consulted about a *sibling* track — never to classify
# the track in hand.
COMPAT_SATISFYING_CODECS: frozenset[str] = frozenset({"ac3", "eac3"})


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

    This sees one track, so it cannot tell a file that needs a downmix from one that already
    has the downmix it added last time. Callers deciding what to actually *do* to a file want
    :func:`classify_audio_plan`, which has the whole track list.
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


def lang_key(track: Track) -> str:
    """The track's language as a comparable token.

    ``language`` is ISO-639-2 and is what mkvmerge normalizes onto every track it writes, so it
    is the reliable field; the IETF primary subtag is only a fallback for a track carrying no
    legacy code at all.
    """
    code = (track.language or "").lower()
    if code and code not in {"und", "unknown"}:
        return code
    ietf = (track.language_ietf or "").lower()
    return ietf.split("-", 1)[0] if ietf else "und"


def needs_compat_downmix(track: Track) -> bool:
    """True for a track the compatibility path exists for.

    That is: more channels than the AC-3/E-AC-3 encoders can take, in a codec a limited player
    may not decode. Such a track is kept as-is and a 5.1 downmix is added beside it — which is
    also why the *downmix*, not this track, should carry the default flag.
    """
    return (
        track.audio_codec not in PASSTHROUGH_CODECS
        and track.channels is not None
        and track.channels > MAX_ENCODER_CHANNELS
    )


def _satisfies_compat(track: Track) -> bool:
    """True if ``track`` already gives a limited player something it can decode."""
    return (
        track.audio_codec in COMPAT_SATISFYING_CODECS
        and track.channels is not None
        and track.channels <= MAX_ENCODER_CHANNELS
    )


def classify_audio_plan(
    audio: Sequence[Track],
    *,
    kept_ids: Collection[int] | None = None,
    ac3_bitrate_override: int | None = None,
) -> dict[int, AudioAction]:
    """Classify every audio track, with the cross-track view :func:`classify_audio` lacks.

    ``compat`` is the one *additive* branch: it keeps the >6-channel source and adds a 5.1 AC-3
    downmix beside it. That also makes it the one branch that is not idempotent — the source is
    left untouched, so a second run adds a second downmix and a third a third, and nothing about
    a single track in isolation can tell you that already happened.

    So a ``compat`` action is demoted to ``passthrough`` when some other **kept** track already
    satisfies the compatibility goal: an AC-3/E-AC-3 track of at most :data:`MAX_ENCODER_CHANNELS`
    channels in the same language. That is deliberately broader than "a downmix we added
    earlier" — a genuinely authored AC-3 5.1 serves the very same purpose — and deciding it on
    codec keeps this consistent with the module's rule of never reading a track's human-readable
    name.

    ``kept_ids`` scopes the sibling search to the tracks that survive selection: dropping the
    existing AC-3 via ``--audio`` must re-enable the encode, or the output would carry nothing a
    limited player could play.
    """
    actions = {
        t.mkv_id: classify_audio(t, ac3_bitrate_override=ac3_bitrate_override) for t in audio
    }
    kept = [t for t in audio if kept_ids is None or t.mkv_id in kept_ids]
    for track in kept:
        if actions[track.mkv_id].kind != "compat":
            continue
        sibling = next(
            (
                t
                for t in kept
                if t.mkv_id != track.mkv_id
                and _satisfies_compat(t)
                and lang_key(t) == lang_key(track)
            ),
            None,
        )
        if sibling is None:
            continue
        actions[track.mkv_id] = AudioAction(
            kind="passthrough",
            warnings=[
                f"track {track.mkv_id}: keeping as-is; track {sibling.mkv_id} "
                f"({sibling.codec_human}, {sibling.channels}ch) already provides a compatible "
                "downmix in the same language"
            ],
        )
    return actions
