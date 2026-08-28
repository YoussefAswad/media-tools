"""Path -> item resolution, the part that decides whether a refresh can be targeted at all."""

from __future__ import annotations

from mediacore.jellyfin import JellyfinClient
from mediacore.models import JellyfinConfig


def client(items, **kw) -> JellyfinClient:
    c = JellyfinClient("http://jf", "key", **kw)
    c._admin_id = "admin"
    c._get = lambda path, **params: (  # type: ignore[assignment]
        {"Items": [{"Path": p, "Id": i} for p, i in items]} if path == "/Items" else []
    )
    return c


SHOWS = [
    ("/data/tvshows/Beastars/Season 2/Beastars - S02E12 - The Taste.mkv", "beast12"),
    ("/data/movies/Coraline (2009)/Coraline (2009).mkv", "cora"),
]


def test_exact_path_match_via_the_configured_map() -> None:
    """Jellyfin mounts the same library elsewhere: /data/Shows here is /data/tvshows there."""
    c = client(SHOWS, path_map={"/data/Shows": "/data/tvshows"})
    got = c.find_item("/data/Shows/Beastars/Season 2/Beastars - S02E12 - The Taste.mkv")
    assert got == "beast12"


def test_longest_prefix_wins_in_the_map() -> None:
    c = client([("/x/deep/a/b/c.mkv", "deep")],
               path_map={"/data": "/x", "/data/shows": "/x/deep"})
    assert c.find_item("/data/shows/a/b/c.mkv") == "deep"


def test_suffix_match_needs_no_configuration() -> None:
    """Without a map, the trailing components still identify the file — this is what makes the
    fix work out of the box on a differently-mounted Jellyfin."""
    c = client(SHOWS)
    assert c.find_item("/data/Shows/Beastars/Season 2/Beastars - S02E12 - The Taste.mkv") == "beast12"


def test_an_ambiguous_suffix_is_refused() -> None:
    """The same show under two libraries (e.g. /data/tvshows and /data/shared/Shows) must not be
    refreshed by guess — return None and let the caller fall back to a scan."""
    c = client([
        ("/data/tvshows/S/Season 1/ep.mkv", "one"),
        ("/data/shared/Shows/S/Season 1/ep.mkv", "two"),
    ])
    assert c.find_item("/data/Shows/S/Season 1/ep.mkv") is None


def test_an_unknown_path_resolves_to_nothing() -> None:
    assert client(SHOWS).find_item("/data/Shows/Nope/Season 1/nope.mkv") is None


def test_no_api_key_means_no_lookup_and_no_refresh() -> None:
    c = JellyfinClient("http://jf", "")
    assert c.find_item("/data/Shows/a/b/c.mkv") is None
    assert c.refresh_item("x") is False
    assert c.refresh_library() is False


def test_a_lookup_failure_is_swallowed() -> None:
    c = JellyfinClient("http://jf", "key")
    c._admin_id = "admin"

    def boom(path, **params):
        raise RuntimeError("jellyfin down")

    c._get = boom  # type: ignore[assignment]
    assert c.find_item("/data/Shows/a/b/c.mkv") is None


def test_the_admin_user_is_preferred() -> None:
    """A non-admin only sees shared libraries — on a real instance that was 10 of 2231 episodes."""
    c = JellyfinClient("http://jf", "key")
    c._get = lambda path, **params: [  # type: ignore[assignment]
        {"Id": "nagla", "Policy": {"IsAdministrator": False}},
        {"Id": "boss", "Policy": {"IsAdministrator": True}},
    ]
    assert c._admin_user_id() == "boss"


def test_config_carries_the_map_through() -> None:
    cfg = JellyfinConfig(base_url="http://jf", api_key="k",
                         path_map={"/data/Shows": "/data/tvshows"})
    assert JellyfinClient.from_config(cfg).path_map == {"/data/Shows": "/data/tvshows"}


# --- the "Jellyfin hasn't caught up yet" cases ----------------------------------------------
def test_a_file_added_since_the_index_was_built_is_found_after_one_rebuild() -> None:
    """The import race: the pipeline finishes before our cached index knows the new file."""
    calls = {"n": 0}
    c = JellyfinClient("http://jf", "key")
    c._admin_id = "admin"

    def get(path, **params):
        calls["n"] += 1
        items = [{"Path": "/data/tvshows/A/Season 1/a.mkv", "Id": "old"}]
        if calls["n"] > 1:  # Jellyfin learned about it between our two queries
            items.append({"Path": "/data/tvshows/B/Season 1/b.mkv", "Id": "new"})
        return {"Items": items}

    c._get = get  # type: ignore[assignment]
    assert c.find_item("/data/Shows/B/Season 1/b.mkv") == "new"
    assert calls["n"] == 2  # one cached build, one forced rebuild


def test_a_file_jellyfin_does_not_have_refetches_at_most_once_per_interval() -> None:
    """A burst of new imports must not refetch the whole library listing per job."""
    calls = {"n": 0}
    c = JellyfinClient("http://jf", "key", refetch_min_interval_s=3600)
    c._admin_id = "admin"

    def get(path, **params):
        calls["n"] += 1
        return {"Items": [{"Path": "/data/tvshows/A/Season 1/a.mkv", "Id": "one"}]}

    c._get = get  # type: ignore[assignment]
    for _ in range(5):
        assert c.find_item("/data/Shows/Nope/Season 1/nope.mkv") is None
    assert calls["n"] == 2  # initial build + a single forced rebuild
