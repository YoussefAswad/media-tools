"""Default-track-flag resolution."""

from __future__ import annotations

import pytest

from conftest import make_track
from mkvprune.flags import is_english, resolve_default, source_default_id


@pytest.mark.parametrize(
    ("lang", "ietf", "expected"),
    [
        ("eng", "en", True),
        ("en", None, True),
        ("eng", "en-US", True),
        (None, "en-GB", True),
        ("fre", "fr", False),
        ("und", "und", False),
    ],
)
def test_is_english(lang, ietf, expected) -> None:
    assert is_english(lang, ietf) is expected


def test_source_default_kept_is_preserved() -> None:
    a = make_track(mkv_id=1, language="fre", language_ietf="fr", default=True)
    b = make_track(mkv_id=2, language="eng", language_ietf="en")
    kept = [a, b]
    assert resolve_default(kept, source_default_id(kept)) == 1


def test_falls_back_to_first_english_when_default_dropped() -> None:
    # Source default was id 9 (not in kept). Kept has a French then an English track.
    kept = [
        make_track(mkv_id=1, language="fre", language_ietf="fr"),
        make_track(mkv_id=2, language="eng", language_ietf="en"),
    ]
    assert resolve_default(kept, src_default_id=9) == 2


def test_falls_back_to_first_kept_when_no_english() -> None:
    kept = [
        make_track(mkv_id=3, language="fre", language_ietf="fr"),
        make_track(mkv_id=4, language="ger", language_ietf="de"),
    ]
    assert resolve_default(kept, src_default_id=9) == 3


def test_no_kept_tracks_is_noop() -> None:
    assert resolve_default([], src_default_id=1) is None


def test_ietf_english_counts() -> None:
    kept = [make_track(mkv_id=1, language="und", language_ietf="und"),
            make_track(mkv_id=2, language="und", language_ietf="en-US")]
    assert resolve_default(kept, src_default_id=None) == 2
