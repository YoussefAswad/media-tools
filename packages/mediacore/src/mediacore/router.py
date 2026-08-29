"""TP-Link router web-UI client (read-mostly, best-effort).

Reads the router's own per-device WAN byte counters, which is the only source in this stack that
knows how much *internet* each device in the house used. The alternatives were measured and
rejected: the host NIC counter is 99.96% LAN traffic on this network, and the router's UPnP
``WANCommonInterfaceConfig`` counters are fabricated (a 50 MB download moved them 1.59 MB).
``STAT_ENTRY``, by contrast, tracked a 60 MB download to within 0.5% and did not count 5 MB of
LAN-only traffic at all.

Like :class:`mediacore.qbit.QbitClient` and :class:`mediacore.notify.Notifier`, every public call
is best-effort: failures log and return ``None``/``False`` rather than raising, so a router
hiccup can never break the worker loop.

Protocol (TP-Link "GDPR encrypt" firmware, e.g. Archer VR600 1.5.0):

* ``POST /cgi/getParm`` returns a 512-bit RSA modulus ``nn``/exponent ``ee`` and a session ``seq``.
* The password hash is ``MD5(username + password)``.
* Bodies are AES-128-CBC under a per-session key/IV of 16 ASCII characters each, base64'd.
* Each request carries ``sign``, an RSA encryption of ``h=<hash>&s=<seq + len(data)>``
  (login adds ``key=``/``iv=`` so the router learns the AES key). RSA here is raw
  textbook exponentiation over **zero-padded** 64-byte blocks — not PKCS#1 — because that is what
  the firmware's own JavaScript does; it is a transport obfuscation, not real security.
* ``POST /cgi_gdpr`` then carries ``sign=…\\r\\ndata=…\\r\\n`` and returns an AES-encrypted body.

Two traps worth keeping in mind:

* **Every request needs a ``Referer``** — the firmware sets ``httpRefererCheckEnabled=1`` and
  answers 403 without one. Same shape of quirk as qBittorrent's ``HostHeaderValidation``.
* **The router allows exactly one admin session.** Logging in silently invalidates whatever
  session existed before, and the evicted session starts returning HTTP 500. There is no
  multi-admin option (``USER_CFG`` is unsupported on this firmware), so a human opening the
  router UI *will* evict this client and vice versa — which is why the caller owns a pause
  switch and why :meth:`cgi` re-authenticates once on a 500.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from mediacore import _aes

log = logging.getLogger(__name__)

#: $.act() action codes from the firmware's proxy.js.
ACT_GET = 1
ACT_SET = 2
ACT_GL = 5

_RSA_BITS = 512
_RSA_BLOCK = _RSA_BITS // 8  # 64 bytes per zero-padded block

_TOKEN_RE = re.compile(r'var token="([^"]+)"')
_PARM_RE = {
    "ee": re.compile(r'ee="([^"]+)"'),
    "nn": re.compile(r'nn="([^"]+)"'),
    "seq": re.compile(r'seq="(\d+)"'),
}
#: one ``[n,0,0,0,0,0]0`` block header per returned row.
_BLOCK_RE = re.compile(r"\[\d+(?:,\d+)*\]\d+")
_ATTR_RE = re.compile(r"^(\w+)=(.*)$")


@dataclass(frozen=True)
class DeviceStats:
    """One row of ``STAT_ENTRY`` — a device's cumulative WAN bytes since the router last reset.

    ``rx``/``tx`` are counters, not deltas, and they reset when the router reboots or the
    statistics feature is toggled. ``curr_rx``/``curr_tx`` are bytes within the router's current
    sampling interval (60 s by default), which makes them a free live rate reading.
    """

    mac: str
    host: str
    kind: str  # wired | 2.4g | 5g
    rx: int
    tx: int
    curr_rx: int
    curr_tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


class RouterClient:
    """Best-effort reader for the router's per-device WAN counters."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        # httpRefererCheckEnabled=1 — without this every request is a 403.
        self.session.headers.update({"Referer": self.base_url + "/"})
        self._nn = ""
        self._ee = ""
        self._seq = 0
        self._hash = ""
        self._key = b""
        self._iv = b""
        self._token = "0"
        self._authed = False

    # --- crypto -----------------------------------------------------------------------

    def _rsa(self, text: str) -> str:
        """Textbook RSA over zero-padded blocks, hex-encoded — what the firmware's JS does."""
        n = int(self._nn, 16)
        e = int(self._ee, 16)
        raw = text.encode()
        out = []
        for i in range(0, len(raw), _RSA_BLOCK):
            block = raw[i : i + _RSA_BLOCK].ljust(_RSA_BLOCK, b"\x00")
            c = pow(int.from_bytes(block, "big"), e, n)
            out.append(format(c, "x").rjust(_RSA_BITS // 4, "0"))
        return "".join(out)

    def _sign(self, data_len: int, *, with_key: bool = False) -> str:
        """The ``sign`` parameter. ``seq`` is offset by the encrypted body's length."""
        prefix = (
            f"key={self._key.decode()}&iv={self._iv.decode()}&" if with_key else ""
        )
        return self._rsa(f"{prefix}h={self._hash}&s={self._seq + data_len}")

    # --- session ----------------------------------------------------------------------

    def _get_parm(self) -> bool:
        try:
            r = self.session.post(
                f"{self.base_url}/cgi/getParm?_={int(time.time() * 1000)}",
                timeout=self.timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("router getParm failed: %s", exc)
            return False
        found = {}
        for name, pattern in _PARM_RE.items():
            m = pattern.search(r.text)
            if not m:
                log.warning("router getParm missing %s", name)
                return False
            found[name] = m.group(1)
        self._ee, self._nn, self._seq = found["ee"], found["nn"], int(found["seq"])
        return True

    def login(self) -> bool:
        """Authenticate and pick up the CSRF token. Evicts any other admin session."""
        self._authed = False
        if not self._get_parm():
            return False
        # MD5 because that is the scheme the firmware implements; not a security choice of ours.
        self._hash = hashlib.md5(f"{self.username}{self.password}".encode()).hexdigest()
        stamp = str(int(time.time() * 1000))
        # The firmware derives these from a timestamp too; any 16 ASCII bytes work.
        self._key = (stamp + "0123456789abcdef")[:16].encode()
        self._iv = (stamp + "fedcba9876543210")[:16].encode()
        payload = base64.b64encode(
            _aes.encrypt(self._key, self._iv, f"{self.username}\n{self.password}".encode())
        ).decode()
        sign = self._sign(len(payload), with_key=True)
        try:
            r = self.session.post(
                f"{self.base_url}/cgi/login?data={quote(payload, safe='')}&sign={sign}"
                f"&Action=1&LoginStatus=0&isMobile=0",
                timeout=self.timeout,
            )
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("router login failed: %s", exc)
            return False
        if "$.ret=0" not in r.text:
            log.warning("router login rejected: %s", r.text.strip()[:80])
            return False
        self._authed = self._fetch_token()
        return self._authed

    def _fetch_token(self) -> bool:
        """Scrape the CSRF token the authenticated index page embeds."""
        try:
            r = self.session.get(self.base_url + "/", timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.warning("router token fetch failed: %s", exc)
            return False
        m = _TOKEN_RE.search(r.text)
        if not m:
            log.warning("router token not found — login may not have taken")
            return False
        self._token = m.group(1)
        return True

    # --- requests ---------------------------------------------------------------------

    def cgi(
        self, act: int, oid: str, attrs: list[str] | None = None
    ) -> str | None:
        """One ``$.act()`` + ``$.exe()`` round trip. Returns the decrypted body, or None.

        Re-authenticates once on a 500, which is how an evicted session presents.
        """
        for attempt in (1, 2):
            if not self._authed and not self.login():
                return None
            rows = attrs or []
            body = "".join(f"{a}\r\n" for a in rows)
            plain = f"{act}\r\n[{oid}#0,0,0,0,0,0#0,0,0,0,0,0]0,{len(rows)}\r\n{body}"
            payload = base64.b64encode(
                _aes.encrypt(self._key, self._iv, plain.encode())
            ).decode()
            sign = self._sign(len(payload))
            try:
                r = self.session.post(
                    f"{self.base_url}/cgi_gdpr?",
                    data=f"sign={sign}\r\ndata={payload}\r\n".encode(),
                    headers={"TokenID": self._token, "Content-Type": "text/plain"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                log.warning("router %s %s error: %s", act, oid, exc)
                return None
            if r.status_code == 500 and attempt == 1:
                # Single-session firmware: somebody else logged in and evicted us.
                log.info("router session evicted (500); re-authenticating")
                self._authed = False
                continue
            if not r.ok:
                log.warning("router %s %s -> %s", act, oid, r.status_code)
                return None
            try:
                return _aes.decrypt(
                    self._key, self._iv, base64.b64decode(r.text)
                ).decode("utf-8", "replace")
            except (ValueError, TypeError) as exc:
                log.warning("router %s %s undecodable: %s", act, oid, exc)
                return None
        return None

    # --- parsing ----------------------------------------------------------------------

    @staticmethod
    def _rows(text: str) -> list[dict[str, str]]:
        """Split a CGI body into one attribute dict per ``[n,…]`` block.

        The trailing ``[error]0`` marker is a status line, not a row, and is dropped.
        """
        rows: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for line in text.replace("\r", "").split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("["):
                if _BLOCK_RE.fullmatch(line):
                    current = {}
                    rows.append(current)
                else:  # [error]N and friends
                    current = None
                continue
            m = _ATTR_RE.match(line)
            if m and current is not None:
                current[m.group(1)] = m.group(2)
        return [r for r in rows if r]

    # --- the API this client exists for -----------------------------------------------

    def devices(self) -> list[DeviceStats] | None:
        """Per-device WAN counters, or None when the router is unreachable.

        An empty list is a real answer (statistics enabled, nothing seen yet) and is distinct
        from None (could not ask), which is what lets the UI tell "no traffic" from "no router".
        """
        text = self.cgi(ACT_GL, "STAT_ENTRY")
        if text is None:
            return None
        out = []
        for row in self._rows(text):
            mac = (row.get("macAddress") or "").upper()
            if not mac:
                continue
            out.append(
                DeviceStats(
                    mac=mac,
                    host=row.get("hostName") or mac,
                    kind=row.get("hostType") or "",
                    rx=_int(row.get("totalBytesRx")),
                    tx=_int(row.get("totalBytesTx")),
                    curr_rx=_int(row.get("currBytesRx")),
                    curr_tx=_int(row.get("currBytesTx")),
                )
            )
        return out

    def stats_enabled(self) -> bool | None:
        """Whether ``STAT_CFG.enable`` is on, or None if it could not be read."""
        text = self.cgi(ACT_GET, "STAT_CFG")
        if text is None:
            return None
        rows = self._rows(text)
        return bool(rows) and rows[0].get("enable") == "1"

    def ensure_stats_enabled(self) -> bool:
        """Turn per-device statistics on **only if it is currently off**.

        Read-then-write on purpose: these settings live in the router's flash, which has finite
        write cycles, so this must never write unconditionally or on a schedule. Called once at
        startup so the feature survives a firmware update or factory reset resetting the flag —
        otherwise the source would silently record zeros forever.
        """
        state = self.stats_enabled()
        if state is None:
            return False
        if state:
            return True  # already on — no flash write
        log.info("router per-device statistics were off; enabling")
        return self.cgi(ACT_SET, "STAT_CFG", ["enable=1"]) is not None


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0
