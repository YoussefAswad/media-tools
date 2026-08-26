"""Batch mode: group a directory of similarly-laid-out files so one selection covers many.

mkvmerge track IDs are **not** stable across files. Two episodes of the same show routinely
differ by an extra subtitle track, which shifts every ID after it — replaying one file's ID
selection against another would silently keep the wrong tracks. So files are grouped by a
*layout signature* built from track identity (kind, codec, channels, language, flags, and
normally the track name), and the user is prompted once per group.

Track names drift cosmetically between episodes ("for Audio commentary…" vs "Audio
commentary…"), which fragments groups for no real reason. ``--group-by structure`` drops the
name term; :func:`name_only_collisions` detects exactly when that would help so the CLI can
say so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from mkvprune.classify import AudioAction
from mkvprune.errors import MkvPruneError
from mkvprune.probe import ProbeResult, Track

# Grouping strictness.
GROUP_BY_CHOICES = ("name", "structure")

# Never inputs: our own in-flight temp mux, and a previous ``--keep-original`` output.
_EXCLUDED_SUFFIXES = (".tmp.mkv", ".pruned.mkv")


@dataclass
class FileGroup:
    """A set of files that share a track layout, and the selection made for them."""

    signature: tuple[Any, ...]
    probes: list[ProbeResult] = field(default_factory=list)
    audio_actions: dict[int, AudioAction] = field(default_factory=dict)
    kept_audio: list[int] = field(default_factory=list)
    kept_subtitles: list[int] = field(default_factory=list)
    skipped: bool = False

    @property
    def representative(self) -> ProbeResult:
        """The probe whose table is shown and whose IDs the selection refers to."""
        return self.probes[0]

    @property
    def paths(self) -> list[str]:
        return [pr.path for pr in self.probes]

    def __len__(self) -> int:
        return len(self.probes)


# --- discovery ----------------------------------------------------------------------


def _is_candidate(name: str, exts: set[str]) -> bool:
    if any(name.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES):
        return False
    return os.path.splitext(name)[1].lower() in exts


def discover_inputs(root: str, *, exts: set[str], recursive: bool = True) -> list[str]:
    """Return the sorted input files under ``root``.

    Recurses by default — pointing at a show root whose files live in ``Season N/``
    subdirectories is the common case. Raises MkvPruneError when nothing matches.
    """
    found: list[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in filenames:
                if _is_candidate(name, exts):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isfile(path) and _is_candidate(name, exts):
                found.append(path)

    if not found:
        scope = "" if recursive else " (top level only; drop --no-recursive to recurse)"
        raise MkvPruneError(
            f"no supported input files found in {root!r}{scope}; "
            f"expected one of {', '.join(sorted(exts))}"
        )
    return sorted(found)


# --- layout signatures --------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Collapse whitespace and case so cosmetic title drift does not split a group."""
    return " ".join(name.split()).casefold()


def _codec_token(t: Track) -> str:
    """A container-independent codec token.

    Audio goes through :attr:`Track.audio_codec`, which already canonicalizes across
    mkvmerge/ffprobe naming. Video and subtitles prefer the ffprobe name, because mkvmerge
    reports no ``codec_id`` at all for non-Matroska sources.
    """
    if t.kind == "audio":
        return t.audio_codec
    return (t.codec_name or t.codec_id or "").lower()


def _track_signature(t: Track, *, by: str) -> tuple[Any, ...]:
    core = (
        t.kind,
        _codec_token(t),
        t.channels,
        t.language,
        t.language_ietf,
        t.default,
        t.forced,
        t.hearing_impaired,
        t.visual_impaired,
        t.commentary,
    )
    if by == "structure":
        return core
    return (*core, _normalize_name(t.track_name))


def layout_signature(pr: ProbeResult, *, by: str = "name") -> tuple[Any, ...]:
    """The ordered track-identity signature of one file.

    Two files with equal signatures have the same tracks in the same positions, so one
    ID-based selection is valid for both.
    """
    if by not in GROUP_BY_CHOICES:
        raise ValueError(f"unknown grouping mode: {by!r}")
    return tuple(_track_signature(t, by=by) for t in pr.tracks)


def group_files(probes: list[ProbeResult], *, by: str = "name") -> list[FileGroup]:
    """Group probes by layout signature, preserving first-seen order."""
    groups: list[FileGroup] = []
    index: dict[tuple[Any, ...], FileGroup] = {}
    for pr in probes:
        sig = layout_signature(pr, by=by)
        group = index.get(sig)
        if group is None:
            group = FileGroup(signature=sig)
            index[sig] = group
            groups.append(group)
        group.probes.append(pr)
    return groups


def name_only_collisions(groups: list[FileGroup]) -> list[tuple[int, int]]:
    """Pairs of group indices (1-based) that differ *only* by track names.

    Non-empty means ``--group-by structure`` would merge those groups, which is worth
    telling the user before they answer the same prompt several times.
    """
    structural = [layout_signature(g.representative, by="structure") for g in groups]
    pairs: list[tuple[int, int]] = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if structural[i] == structural[j]:
                pairs.append((i + 1, j + 1))
    return pairs
