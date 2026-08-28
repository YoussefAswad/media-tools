from __future__ import annotations

from pathlib import Path

from conftest import build_test_services, radarr_config
from mediacore.arr.languages import iso_for_arr_language
from orchestrator.titles import TitleIndex


class FakeClient:
    def __init__(self, items, boom=False):
        self.items = items
        self.boom = boom
        self.calls = 0

    def titles(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("connection refused")
        return self.items


def _index(tmp_path, radarr=None, sonarr=None, **cfg_kw):
    services = build_test_services(tmp_path, radarr_config(**cfg_kw))
    clients = {"radarr": radarr, "sonarr": sonarr}
    services.arr_client = lambda app: clients.get(app)  # type: ignore[assignment]
    services.arr_instance = lambda app: None  # type: ignore[assignment]
    return services, TitleIndex(services)


# --- the mapping ---------------------------------------------------------------------------
def test_arr_placeholders_never_produce_a_language() -> None:
    """'Unknown'/'Any'/'Original' must read as no-opinion, or they'd narrow the keep set."""
    for name in ("Unknown", "Any", "Original", "", None, "Klingon"):
        assert iso_for_arr_language(name) == set()
    assert iso_for_arr_language("Japanese") == {"jpn"}
    assert iso_for_arr_language("German") == {"ger", "deu"}  # both ISO variants


# --- the index -----------------------------------------------------------------------------
def test_longest_prefix_wins(tmp_path) -> None:
    radarr = FakeClient([
        {"path": "/data/Movies", "title": "Wrong", "originalLanguage": {"name": "English"}},
        {"path": "/data/Movies/Amelie (2001)", "title": "Amélie",
         "originalLanguage": {"name": "French"}},
    ])
    _, idx = _index(tmp_path, radarr=radarr)
    info = idx.lookup("/data/Movies/Amelie (2001)/Amelie.mkv")
    assert info.title == "Amélie" and info.original_isos == frozenset({"fre", "fra"})


def test_a_path_no_arr_claims_is_unknown(tmp_path) -> None:
    """An orphaned folder (series deleted from Sonarr, files kept) must not guess."""
    radarr = FakeClient([{"path": "/data/Movies/A", "title": "A",
                          "originalLanguage": {"name": "English"}}])
    _, idx = _index(tmp_path, radarr=radarr)
    assert idx.lookup("/data/Shows/Orphan/e01.mkv") is None
    assert idx.original_isos("/data/Shows/Orphan/e01.mkv") is None


def test_an_unreachable_arr_yields_no_opinion(tmp_path) -> None:
    _, idx = _index(tmp_path, radarr=FakeClient([], boom=True))
    assert idx.lookup("/data/Movies/A/a.mkv") is None


def test_a_transient_outage_keeps_the_previous_index(tmp_path) -> None:
    client = FakeClient([{"path": "/data/Movies/A", "title": "A",
                          "originalLanguage": {"name": "Danish"}}])
    _, idx = _index(tmp_path, radarr=client)
    assert idx.original_isos("/data/Movies/A/a.mkv") == {"dan"}
    client.boom = True
    idx.refresh()
    assert idx.original_isos("/data/Movies/A/a.mkv") == {"dan"}  # still served


def test_the_index_is_cached_until_the_ttl(tmp_path) -> None:
    client = FakeClient([{"path": "/data/Movies/A", "title": "A",
                          "originalLanguage": {"name": "English"}}])
    _, idx = _index(tmp_path, radarr=client)
    for _ in range(5):
        idx.lookup("/data/Movies/A/a.mkv")
    assert client.calls == 1


def test_a_per_title_override_beats_the_arr(tmp_path) -> None:
    """Star Wars: Visions — TVDb says English (a Lucasfilm production), but the Japanese track is
    the one worth keeping."""
    sonarr = FakeClient([{"path": "/data/Shows/Visions", "title": "Star Wars: Visions",
                          "originalLanguage": {"name": "English"}}])
    services, idx = _index(tmp_path, sonarr=sonarr)
    services.config.policy.audio_original_overrides = {"star wars: visions": ["jpn"]}
    assert idx.original_isos("/data/Shows/Visions/S01E01.mkv") == {"jpn"}


def test_a_placeholder_original_language_reads_as_unknown(tmp_path) -> None:
    radarr = FakeClient([{"path": "/data/Movies/A", "title": "A",
                          "originalLanguage": {"name": "Unknown"}}])
    _, idx = _index(tmp_path, radarr=radarr)
    assert idx.lookup("/data/Movies/A/a.mkv").original_language is None
    assert idx.original_isos("/data/Movies/A/a.mkv") is None


def test_paths_are_translated_to_the_local_mount(tmp_path) -> None:
    from mediacore.models import ArrInstance

    services = build_test_services(tmp_path, radarr_config())
    services.config.arr.radarr = ArrInstance(
        base_url="http://radarr", api_key="k",
        media_root=Path("/data"), local_media_root=Path("/media"),
    )
    services.arr_client = lambda app: (  # type: ignore[assignment]
        FakeClient([{"path": "/data/Movies/A", "title": "A",
                     "originalLanguage": {"name": "Japanese"}}]) if app == "radarr" else None
    )
    idx = TitleIndex(services)
    assert idx.original_isos("/media/Movies/A/a.mkv") == {"jpn"}


def test_the_index_title_wins_over_a_synthesized_one_for_overrides(tmp_path) -> None:
    """The regression this guards: a re-run event carries no arr title, and whatever placeholder
    it did carry shadowed the index's real title — silently disabling the per-title override on
    every Library-tab re-run, which is precisely where it is needed."""
    sonarr = FakeClient([{"path": "/data/Shows/Visions", "title": "Star Wars: Visions",
                          "originalLanguage": {"name": "English"}}])
    services, idx = _index(tmp_path, sonarr=sonarr)
    services.config.policy.audio_original_overrides = {"Star Wars: Visions": ["jpn"]}
    path = "/data/Shows/Visions/Season 1/S01E01.mkv"

    assert idx.original_isos(path) == {"jpn"}                      # no title supplied
    assert idx.original_isos(path, "Unknown series") == {"jpn"}    # placeholder must not shadow
    assert idx.original_isos(path, "Star Wars: Visions") == {"jpn"}


def test_a_supplied_title_still_helps_when_no_arr_claims_the_path(tmp_path) -> None:
    services, idx = _index(tmp_path, sonarr=FakeClient([]))
    services.config.policy.audio_original_overrides = {"Orphaned Show": ["jpn"]}
    assert idx.original_isos("/data/Shows/Orphan/e.mkv", "Orphaned Show") == {"jpn"}
