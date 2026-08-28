"""Event labels — what the ntfy notification and the job row are named.

A re-run (Library tab or /rerun) synthesizes a Download event from a path alone, with no arr
title. That used to become "Unknown series", which is what every re-run notification said.
"""

from __future__ import annotations

from mediacore.arr.events import parse_event


def rerun_sonarr(path: str) -> dict:
    """Exactly what orchestrator.app._rerun_payload builds."""
    return {"eventType": "Download", "episodeFile": {"path": path}, "series": {"title": ""}}


def rerun_radarr(path: str) -> dict:
    return {"eventType": "Download", "movieFile": {"path": path}}


def test_a_rerun_is_labelled_with_the_filename_not_unknown() -> None:
    ep = "/data/Shows/Beastars/Season 2/Beastars - S02E12 - The Taste of Rebellion.mkv"
    got = parse_event("sonarr", rerun_sonarr(ep))
    assert got.label == "Beastars - S02E12 - The Taste of Rebellion"
    assert "Unknown" not in got.label


def test_a_rerun_movie_is_labelled_with_the_filename() -> None:
    got = parse_event("radarr", rerun_radarr("/data/Movies/Coraline (2009)/Coraline (2009).mkv"))
    assert got.label == "Coraline (2009)"


def test_a_missing_title_leaves_title_empty_rather_than_placeholder() -> None:
    """`title` is the ARR's title and callers key per-title policy off it; a placeholder would
    read as a real title and shadow the real one."""
    got = parse_event("sonarr", rerun_sonarr("/data/Shows/X/Season 1/x.mkv"))
    assert got.title == ""


def test_a_real_webhook_label_is_unchanged() -> None:
    payload = {
        "eventType": "Download",
        "series": {"id": 7, "title": "Beastars"},
        "episodeFile": {"path": "/data/Shows/Beastars/Season 2/ep.mkv"},
        "episodes": [{"seasonNumber": 2, "episodeNumber": 12}],
    }
    assert parse_event("sonarr", payload).label == "Beastars - S02E12"

    movie = {
        "eventType": "Download",
        "movie": {"id": 1, "title": "Dune", "year": 2021},
        "movieFile": {"path": "/data/Movies/Dune (2021)/Dune.mkv"},
    }
    assert parse_event("radarr", movie).label == "Dune (2021)"


def test_a_multi_episode_label_still_lists_every_episode() -> None:
    payload = {
        "eventType": "Download",
        "series": {"title": "Show"},
        "episodeFile": {"path": "/data/Shows/Show/Season 1/e.mkv"},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 1},
                     {"seasonNumber": 1, "episodeNumber": 2}],
    }
    assert parse_event("sonarr", payload).label == "Show - S01E01E02"


def test_no_title_and_no_path_falls_back_to_a_readable_string() -> None:
    got = parse_event("sonarr", {"eventType": "Download", "series": {}})
    assert got.label == "unknown episode"
