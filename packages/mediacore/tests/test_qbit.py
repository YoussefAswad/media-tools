from __future__ import annotations

from mediacore.qbit import QbitClient


class _Resp:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code = status
        self.text = text
        self.ok = 200 <= status < 300
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _client(monkeypatch, *, login_ok=True, info=None, info_status=200):
    c = QbitClient("http://qb:45666", "u", "p")

    def fake_post(url, data=None, timeout=None):
        return _Resp(200 if login_ok else 403, "Ok." if login_ok else "Fails.")

    def fake_get(url, params=None, timeout=None):
        return _Resp(info_status, json_data=info)

    monkeypatch.setattr(c.session, "post", fake_post)
    monkeypatch.setattr(c.session, "get", fake_get)
    return c


def test_torrent_by_hash_parses(monkeypatch):
    info = [{
        "hash": "abcdef", "name": "Some.Release", "downloaded": 1000, "uploaded": 200,
        "total_size": 900, "category": "Shows/Sonarr", "completion_on": 1700000000,
        "added_on": 1699990000, "state": "stoppedUP",
    }]
    c = _client(monkeypatch, info=info)
    # arr sends uppercase hash; qbit stores lowercase — must still match
    t = c.torrent_by_hash("ABCDEF")
    assert t is not None
    assert t["downloaded"] == 1000 and t["uploaded"] == 200
    assert t["name"] == "Some.Release" and t["hash"] == "abcdef"
    assert t["completion_on"] == 1700000000


def test_torrent_missing_returns_none(monkeypatch):
    c = _client(monkeypatch, info=[])
    assert c.torrent_by_hash("deadbeef") is None


def test_login_failure_returns_none(monkeypatch):
    c = _client(monkeypatch, login_ok=False)
    assert c.torrent_by_hash("abc") is None


def test_login_accepts_204_no_content(monkeypatch):
    # Some qBittorrent builds return 204 (empty body) on a successful login, not 200 "Ok.".
    c = QbitClient("http://qb:45666", "u", "p")
    info = [{"hash": "aa", "name": "n", "downloaded": 5, "uploaded": 1, "total_size": 4,
             "category": "", "completion_on": 0, "added_on": 0, "state": "up"}]
    monkeypatch.setattr(c.session, "post", lambda url, data=None, timeout=None: _Resp(204, ""))
    monkeypatch.setattr(c.session, "get", lambda url, params=None, timeout=None: _Resp(200, json_data=info))
    assert c.login() is True
    assert c.torrent_by_hash("AA")["downloaded"] == 5


def test_login_fails_text_is_rejected(monkeypatch):
    c = QbitClient("http://qb:45666", "u", "p")
    monkeypatch.setattr(c.session, "post", lambda url, data=None, timeout=None: _Resp(200, "Fails."))
    assert c.login() is False


def test_empty_hash(monkeypatch):
    c = _client(monkeypatch, info=[{"hash": "x"}])
    assert c.torrent_by_hash("") is None


def test_transport_error_is_swallowed(monkeypatch):
    import requests
    c = QbitClient("http://qb:45666", "u", "p")

    def boom(*a, **k):
        raise requests.RequestException("down")

    monkeypatch.setattr(c.session, "post", boom)
    assert c.torrent_by_hash("abc") is None
