from __future__ import annotations

from mediacore import jellyfin as jf_mod
from mediacore.jellyfin import JellyfinClient


class _Resp:
    def __init__(self, status=204):
        self.status_code = status

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


def test_refresh_posts_with_token(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        return _Resp(204)

    monkeypatch.setattr(jf_mod.requests, "post", fake_post)
    c = JellyfinClient("http://jf:8096", "KEY123")
    assert c.refresh_library() is True
    assert seen["url"] == "http://jf:8096/Library/Refresh"
    assert seen["headers"]["X-Emby-Token"] == "KEY123"


def test_refresh_skips_without_key(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(jf_mod.requests, "post", lambda *a, **k: called.__setitem__("n", 1))
    c = JellyfinClient("http://jf:8096", "")
    assert c.refresh_library() is False
    assert called["n"] == 0  # never even attempts the request


def test_refresh_swallows_errors(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr(jf_mod.requests, "post", boom)
    assert JellyfinClient("http://jf:8096", "K").refresh_library() is False
