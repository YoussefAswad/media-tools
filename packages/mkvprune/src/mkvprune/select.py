"""Track-selection spec parsing and interactive prompts."""

from __future__ import annotations

import sys
from collections.abc import Iterable

from mkvprune.errors import SelectionAbort, SelectionSkip


class SelectionError(ValueError):
    """Raised for a malformed or out-of-range selection spec."""


def parse_selection(spec: str, valid_ids: Iterable[int]) -> list[int]:
    """Parse a selection spec into a sorted, de-duplicated list of track IDs.

    Accepted syntax (case-insensitive):

    * comma-separated IDs: ``1,3,4``
    * ranges: ``1-3``
    * mixed: ``1,3-5,8``
    * ``all`` / empty  -> every valid ID
    * ``none``         -> no IDs
    * ``q``            -> abort (raises SelectionAbort)

    Every referenced ID must exist in ``valid_ids`` or SelectionError is raised.
    """
    valid = list(dict.fromkeys(valid_ids))
    valid_set = set(valid)
    text = spec.strip().lower()

    if text == "q":
        raise SelectionAbort("aborted at selection prompt")
    if text in {"", "all"}:
        return sorted(valid_set)
    if text == "none":
        return []

    selected: set[int] = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, sep, hi_s = part.partition("-")
            lo_s, hi_s = lo_s.strip(), hi_s.strip()
            if not sep or not lo_s or not hi_s:
                raise SelectionError(f"malformed range: {part!r}")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise SelectionError(f"non-numeric range: {part!r}") from exc
            if lo > hi:
                raise SelectionError(f"reversed range: {part!r}")
            for value in range(lo, hi + 1):
                if value not in valid_set:
                    raise SelectionError(f"unknown track ID {value} in range {part!r}")
                selected.add(value)
        else:
            try:
                value = int(part)
            except ValueError as exc:
                raise SelectionError(f"not a track ID: {part!r}") from exc
            if value not in valid_set:
                raise SelectionError(f"unknown track ID {value}")
            selected.add(value)

    return sorted(selected)


def prompt_selection(
    label: str,
    valid_ids: list[int],
    *,
    require_nonempty: bool,
    allow_skip: bool = False,
    input_fn=input,
) -> list[int]:
    """Interactively prompt for a selection of ``label`` tracks, re-prompting on error.

    ``require_nonempty`` enforces "at least one" unless the user explicitly types ``none``.
    ``allow_skip`` offers ``skip`` (batch mode: leave this group's files untouched), which
    raises SelectionSkip. Returns the chosen IDs. Raises SelectionAbort on ``q`` or EOF.
    """
    if not valid_ids:
        return []

    id_list = ", ".join(str(i) for i in valid_ids)
    skip_hint = " / skip this group" if allow_skip else ""
    prompt = (
        f"Select {label} tracks to KEEP [{id_list}] "
        f"(e.g. 1,3-5 / all / none{skip_hint} / q to abort; empty = all): "
    )
    while True:
        try:
            raw = input_fn(prompt)
        except EOFError as exc:
            raise SelectionAbort(f"no input for {label} selection") from exc

        if allow_skip and raw.strip().lower() == "skip":
            raise SelectionSkip(f"skipped at {label} selection")

        explicit_none = raw.strip().lower() == "none"
        try:
            chosen = parse_selection(raw, valid_ids)
        except SelectionError as exc:
            print(f"  ! {exc}. Try again.", file=sys.stderr)
            continue

        if require_nonempty and not chosen and not explicit_none:
            print(
                f"  ! at least one {label} track is required "
                "(type 'none' to explicitly keep none).",
                file=sys.stderr,
            )
            continue
        if require_nonempty and not chosen and explicit_none:
            # Confirm the destructive "keep no audio" choice implicitly by honoring it.
            return []
        return chosen


def confirm(prompt: str, *, assume_yes: bool, input_fn=input) -> bool:
    """Ask a ``[y/N]`` question. Returns True only on an explicit yes."""
    if assume_yes:
        return True
    try:
        answer = input_fn(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}
