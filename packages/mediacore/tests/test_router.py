from __future__ import annotations

import base64

import pytest
import requests
from mediacore import _aes
from mediacore.router import ACT_GET, ACT_GL, ACT_SET, RouterClient

#: A real ``STAT_ENTRY`` body, captured from an Archer VR600. Two devices plus the trailing
#: ``[error]0`` status marker, which is not a row and must not be parsed as one.
_STAT_ENTRY = (
    "[1,0,0,0,0,0]0\r\n"
    "hostType=wired\r\nhostName=Linux-G15\r\nipAddress=3232235827\r\n"
    "macAddress=60:18:95:71:E8:FC\r\n"
    "totalBytesRx=65120134\r\ntotalBytesTx=4712516\r\n"
    "currBytesRx=2188\r\ncurrBytesTx=3175\r\n"
    "currIcmp=0\r\ncurrUdp=0\r\ncurrSyn=0\r\n"
    "[2,0,0,0,0,0]0\r\n"
    "hostType=5g\r\nhostName=OnePlus-12\r\nipAddress=3232235841\r\n"
    "macAddress=5a:d4:4b:00:55:c7\r\n"
    "totalBytesRx=38198089\r\ntotalBytesTx=844872\r\n"
    "currBytesRx=33\r\ncurrBytesTx=46\r\n"
    "[error]0\r\n"
)

_STAT_CFG_ON = "[0,0,0,0,0,0]0\r\nenable=1\r\ninterval=60\r\naction=0\r\n[error]0\r\n"
_STAT_CFG_OFF = "[0,0,0,0,0,0]0\r\nenable=0\r\ninterval=60\r\naction=0\r\n[error]0\r\n"


def _client(monkeypatch, replies):
    """A client with a pre-primed session and a canned ``cgi`` transport.

    ``replies`` is a list consumed in order, so a test can assert exactly which calls happened.
    """
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    calls: list[tuple[int, str, list[str] | None]] = []

    def fake_cgi(act, oid, attrs=None):
        calls.append((act, oid, attrs))
        return replies.pop(0) if replies else None

    monkeypatch.setattr(c, "cgi", fake_cgi)
    return c, calls


def test_devices_parses_rows_and_drops_the_error_marker(monkeypatch) -> None:
    c, _ = _client(monkeypatch, [_STAT_ENTRY])
    devs = c.devices()
    assert devs is not None
    assert len(devs) == 2  # not 3 — [error]0 is a status line
    linux = devs[0]
    assert linux.host == "Linux-G15"
    assert linux.kind == "wired"
    assert linux.rx == 65_120_134 and linux.tx == 4_712_516
    assert linux.curr_rx == 2188 and linux.curr_tx == 3175
    assert linux.total == 65_120_134 + 4_712_516


def test_devices_normalizes_mac_case(monkeypatch) -> None:
    """The cursor is keyed by MAC, so casing must not create a phantom second device."""
    c, _ = _client(monkeypatch, [_STAT_ENTRY])
    devs = c.devices()
    assert devs is not None
    assert [d.mac for d in devs] == ["60:18:95:71:E8:FC", "5A:D4:4B:00:55:C7"]


def test_devices_unreachable_is_none_not_empty(monkeypatch) -> None:
    """None ("could not ask") and [] ("nothing seen") are different answers to the UI."""
    c, _ = _client(monkeypatch, [None])
    assert c.devices() is None


def test_devices_empty_table_is_an_empty_list(monkeypatch) -> None:
    c, _ = _client(monkeypatch, ["[error]0\r\n"])
    assert c.devices() == []


def test_row_without_a_mac_is_skipped(monkeypatch) -> None:
    c, _ = _client(monkeypatch, ["[1,0,0,0,0,0]0\r\nhostName=ghost\r\n[error]0\r\n"])
    assert c.devices() == []


def test_non_numeric_counter_does_not_raise(monkeypatch) -> None:
    body = "[1,0,0,0,0,0]0\r\nmacAddress=AA:BB\r\ntotalBytesRx=nonsense\r\n[error]0\r\n"
    c, _ = _client(monkeypatch, [body])
    devs = c.devices()
    assert devs is not None
    assert devs[0].rx == 0


def test_ensure_stats_enabled_does_not_write_when_already_on(monkeypatch) -> None:
    """Settings live in the router's flash — a write per startup would burn write cycles."""
    c, calls = _client(monkeypatch, [_STAT_CFG_ON])
    assert c.ensure_stats_enabled() is True
    assert calls == [(ACT_GET, "STAT_CFG", None)]  # read only, no ACT_SET


def test_ensure_stats_enabled_writes_once_when_off(monkeypatch) -> None:
    c, calls = _client(monkeypatch, [_STAT_CFG_OFF, "[error]0\r\n"])
    assert c.ensure_stats_enabled() is True
    assert [(a, o) for a, o, _ in calls] == [(ACT_GET, "STAT_CFG"), (ACT_SET, "STAT_CFG")]
    assert calls[1][2] == ["enable=1"]


def test_ensure_stats_enabled_gives_up_when_unreadable(monkeypatch) -> None:
    c, calls = _client(monkeypatch, [None])
    assert c.ensure_stats_enabled() is False
    assert len(calls) == 1  # never blind-writes to an unreachable router


# --- transport ----------------------------------------------------------------------------


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")


def _encrypted(body: str, key=b"0123456789abcdef", iv=b"abcdef9876543210") -> str:
    return base64.b64encode(_aes.encrypt(key, iv, body.encode())).decode()


def test_cgi_round_trip_decrypts_the_body(monkeypatch) -> None:
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    c._nn, c._ee, c._seq, c._hash = "c7", "010001", 1, "h"
    monkeypatch.setattr(c.session, "post", lambda *_a, **_k: _Resp(_encrypted(_STAT_CFG_ON)))
    body = c.cgi(ACT_GL, "STAT_ENTRY")
    assert body is not None and "enable=1" in body


def test_cgi_reauthenticates_once_on_500(monkeypatch) -> None:
    """A 500 is how this single-session firmware reports that another login evicted us."""
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    c._nn, c._ee, c._seq, c._hash = "c7", "010001", 1, "h"
    responses = [_Resp("", 500), _Resp(_encrypted(_STAT_CFG_ON))]
    monkeypatch.setattr(c.session, "post", lambda *_a, **_k: responses.pop(0))
    logins = []

    def fake_login():
        logins.append(1)
        c._authed = True
        return True

    monkeypatch.setattr(c, "login", fake_login)
    body = c.cgi(ACT_GET, "STAT_CFG")
    assert body is not None and "enable=1" in body
    assert len(logins) == 1  # exactly one re-auth, not a retry storm


def test_cgi_gives_up_after_a_second_500(monkeypatch) -> None:
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    c._nn, c._ee, c._seq, c._hash = "c7", "010001", 1, "h"
    monkeypatch.setattr(c.session, "post", lambda *_a, **_k: _Resp("", 500))
    monkeypatch.setattr(c, "login", lambda: True)
    assert c.cgi(ACT_GET, "STAT_CFG") is None


@pytest.mark.parametrize(
    "boom",
    [requests.ConnectionError("refused"), requests.Timeout("slow")],
)
def test_network_errors_return_none_never_raise(monkeypatch, boom) -> None:
    """Best-effort contract: a router hiccup must never reach the worker loop."""
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    c._nn, c._ee, c._seq, c._hash = "c7", "010001", 1, "h"

    def raise_it(*_a, **_k):
        raise boom

    monkeypatch.setattr(c.session, "post", raise_it)
    assert c.cgi(ACT_GET, "STAT_CFG") is None


def test_garbage_body_returns_none(monkeypatch) -> None:
    c = RouterClient("http://router", "admin", "pw")
    c._authed = True
    c._key, c._iv = b"0123456789abcdef", b"abcdef9876543210"
    c._nn, c._ee, c._seq, c._hash = "c7", "010001", 1, "h"
    monkeypatch.setattr(c.session, "post", lambda *_a, **_k: _Resp("!!!not base64!!!"))
    assert c.cgi(ACT_GET, "STAT_CFG") is None


def test_login_failure_is_reported_not_raised(monkeypatch) -> None:
    c = RouterClient("http://router", "admin", "pw")
    monkeypatch.setattr(c, "_get_parm", lambda: True)
    c._nn, c._ee, c._seq = "c7", "010001", 1
    monkeypatch.setattr(c.session, "post", lambda *_a, **_k: _Resp("$.ret=1;"))
    assert c.login() is False


def test_referer_header_is_always_present() -> None:
    """httpRefererCheckEnabled=1 on this firmware — without it every request is a 403."""
    c = RouterClient("http://192.168.1.1", "admin", "pw")
    assert c.session.headers["Referer"] == "http://192.168.1.1/"


def test_rsa_pads_blocks_with_zeros_to_the_key_size() -> None:
    """The firmware uses raw zero-padded RSA, not PKCS#1; a short string still fills 64 bytes."""
    c = RouterClient("http://router", "admin", "pw")
    c._nn, c._ee = "c7", "010001"  # tiny modulus, only the block/format shape matters here
    out = c._rsa("h=abc&s=1")
    assert len(out) == 128  # one 512-bit block, hex, zero-left-padded
    assert all(ch in "0123456789abcdef" for ch in out)
