"""How much bigger or smaller a prune would leave a file.

The subtlest code in the index, and the part most likely to be wrong in an interesting way.
Two rules hold it together:

* **A partial answer stays partial.** A file where some tracks report a size and others do not
  yields a figure flagged ``known=False`` — a floor, never rounded down into a confident zero.
* **The pipeline usually makes files BIGGER.** Dropping foreign text subs reclaims ~200 KB;
  re-encoding a 300-400 kbps track to 640 kbps AC-3 adds 50-100 MB. That is correct behaviour —
  the re-encode buys player compatibility, not space — so the sign is always shown.

Changing anything here changes numbers already cached in the table, so it requires bumping
:data:`orchestrator.library.index.SCAN_VERSION`; otherwise the fixed formula would only ever
reach files that happened to change on disk.
"""

from __future__ import annotations

from mediacore.policy.prune import PruneDecision
from mediacore.probe import ProbeResult, classify_audio


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def track_sizes(pr: ProbeResult) -> dict[int, int]:
    """ffprobe stream index → that track's size in bytes, for the tracks that reveal it.

    Matroska has no per-track bitrate header, so ffprobe's own ``bit_rate`` is empty on the vast
    majority of an MKV library (measured on this one: 13% of audio tracks). What it *does* carry
    is mkvmerge's statistics tags — ``NUMBER_OF_BYTES`` (the exact track size) and ``BPS`` — on
    72% of audio and 86% of subtitle tracks. Reading them is the difference between a size
    estimate that means something and a column of question marks.

    Tried in order: the exact byte count, then a measured average bitrate over the runtime, then
    ffprobe's own bitrate. ``None`` when a track reveals none of them.
    """
    duration = pr.duration_s or 0.0
    sizes: dict[int, int] = {}
    for stream in (pr.ffprobe_json or {}).get("streams", []):
        index = _as_int(stream.get("index"))
        if index is None:
            continue
        tags = {k.upper(): v for k, v in (stream.get("tags") or {}).items()}
        exact = _as_int(tags.get("NUMBER_OF_BYTES"))
        if exact is not None:
            sizes[index] = exact
            continue
        if duration <= 0:
            continue
        bps = _as_int(tags.get("BPS")) or _as_int(stream.get("bit_rate"))
        if bps:
            sizes[index] = int(bps / 8 * duration)
    return sizes


def estimate_delta(pr: ProbeResult, decision: PruneDecision) -> tuple[int, bool]:
    """Estimated size change in bytes (negative = space reclaimed), and whether every part of it
    was actually computable.

    Dropping a subtitle or audio track reclaims exactly that track's bytes. Re-encoding audio
    source track for an AC-3 one at a known bitrate — except the >6-channel *compatibility* path,
    which keeps the original and **adds** a downmix, so its contribution is positive.

    A file where some tracks report a size and others don't yields a partial figure with
    ``known=False``: a floor, not a fiction. Never round an unknown down to a confident zero.
    """
    duration = pr.duration_s or 0.0
    sizes = track_sizes(pr)
    delta = 0
    known = True

    for track in [*decision.dropped_subs, *decision.dropped_audio]:
        size = sizes.get(track.ffprobe_index)
        if size is None:
            known = False
        else:
            delta -= size

    for track in decision.audio_encode_tracks:
        action = classify_audio(track)
        if not action.bitrate_bps or duration <= 0:
            known = False
            continue
        added = int(action.bitrate_bps / 8 * duration)
        if action.kind == "compat":
            delta += added  # the original stays; a 5.1 AC-3 downmix is added alongside it
            continue
        source = sizes.get(track.ffprobe_index)
        if source is None:
            known = False
            continue
        delta += added - source

    return delta, known

