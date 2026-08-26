"""Selection-spec parsing and interactive prompt behavior."""

from __future__ import annotations

import pytest

from mkvprune.errors import SelectionAbort, SelectionSkip
from mkvprune.select import (
    SelectionError,
    parse_selection,
    prompt_selection,
)

VALID = [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1,3,4", [1, 3, 4]),
        ("1-3", [1, 2, 3]),
        ("1,3-5,0", [0, 1, 3, 4, 5]),
        ("all", [0, 1, 2, 3, 4, 5]),
        ("", [0, 1, 2, 3, 4, 5]),  # empty -> all
        ("none", []),
        ("2,2,2", [2]),  # de-dup
        (" 1 , 3 - 4 ", [1, 3, 4]),  # whitespace tolerant
    ],
)
def test_parse_selection_ok(spec: str, expected: list[int]) -> None:
    assert parse_selection(spec, VALID) == expected


@pytest.mark.parametrize("spec", ["9", "1-9", "1-", "-3", "3-1", "a", "1,x", "1--3"])
def test_parse_selection_bad(spec: str) -> None:
    with pytest.raises(SelectionError):
        parse_selection(spec, VALID)


def test_parse_selection_q_aborts() -> None:
    with pytest.raises(SelectionAbort):
        parse_selection("q", VALID)


def test_prompt_reprompts_then_accepts() -> None:
    answers = iter(["bogus", "9", "1,2"])
    result = prompt_selection("audio", [1, 2, 3], require_nonempty=True, input_fn=lambda _: next(answers))
    assert result == [1, 2]


def test_prompt_requires_nonempty_until_none() -> None:
    answers = iter(["", "none"])  # "" -> all here (nonempty), so returns [1,2]
    result = prompt_selection("audio", [1, 2], require_nonempty=True, input_fn=lambda _: next(answers))
    assert result == [1, 2]


def test_prompt_empty_valid_returns_empty() -> None:
    # No tracks of this type -> immediately empty, no prompt.
    assert prompt_selection("subtitle", [], require_nonempty=False, input_fn=lambda _: "boom") == []


def test_prompt_explicit_none_allowed() -> None:
    result = prompt_selection("audio", [1, 2], require_nonempty=True, input_fn=lambda _: "none")
    assert result == []


def test_prompt_eof_aborts() -> None:
    def raise_eof(_prompt: str) -> str:
        raise EOFError

    with pytest.raises(SelectionAbort):
        prompt_selection("audio", [1, 2], require_nonempty=True, input_fn=raise_eof)


def test_prompt_skip_raises_only_when_allowed() -> None:
    with pytest.raises(SelectionSkip):
        prompt_selection(
            "audio", [1, 2], require_nonempty=True, allow_skip=True,
            input_fn=lambda _: "skip",
        )


def test_prompt_skip_is_not_a_track_id_without_allow_skip() -> None:
    # Without allow_skip, "skip" is just a bad spec: re-prompt rather than abort.
    answers = iter(["skip", "1"])
    result = prompt_selection(
        "audio", [1, 2], require_nonempty=True, input_fn=lambda _: next(answers)
    )
    assert result == [1]


def test_prompt_mentions_skip_only_when_allowed() -> None:
    seen: list[str] = []

    def capture(prompt: str) -> str:
        seen.append(prompt)
        return "1"

    prompt_selection("audio", [1, 2], require_nonempty=True, input_fn=capture)
    prompt_selection("audio", [1, 2], require_nonempty=True, allow_skip=True, input_fn=capture)
    assert "skip this group" not in seen[0]
    assert "skip this group" in seen[1]
