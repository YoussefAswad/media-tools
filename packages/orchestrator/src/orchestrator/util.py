"""Small formatting/parsing helpers for the downloads ledger UI."""

from __future__ import annotations

import datetime as _dt
import re

#: Emoji per download type, for the ledger's label column / filters / dropdowns.
KIND_ICONS = {
    "movie": "🎬",
    "show": "📺",
    "episode": "📺",
    "game": "🎮",
    "software": "💿",
    "other": "📦",
}


def kind_icon(kind: str | None) -> str:
    return KIND_ICONS.get((kind or "").lower(), "📦")


# Both scales use the same 1024 math (matching qBittorrent); only the suffix differs. The
# IEC form (GiB/MiB) is the technically-correct label for 1024-based values; the plain form
# (GB/MB) is the familiar one. A UI toggle picks which suffix to render.
_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
_UNITS_IEC = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
_UNIT_FACTORS = {
    "B": 1,
    "KB": 1024,
    "K": 1024,
    "KIB": 1024,
    "MB": 1024**2,
    "M": 1024**2,
    "MIB": 1024**2,
    "GB": 1024**3,
    "G": 1024**3,
    "GIB": 1024**3,
    "TB": 1024**4,
    "T": 1024**4,
    "TIB": 1024**4,
    "PB": 1024**5,
    "PIB": 1024**5,
}


def human_bytes(n: int | float | None, *, iec: bool = False) -> str:
    """1234567 -> '1.18 MB' (or '1.18 MiB' when ``iec``). Binary (1024) math either way,
    matching qBittorrent's display; ``iec`` only switches the suffix to the KiB/MiB/GiB form."""
    units = _UNITS_IEC if iec else _UNITS
    if not n or n < 0:
        return "0 B"
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return (f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}")
        size /= 1024
    return f"{size:.2f} {units[-1]}"


_BYTES_RE = re.compile(r"^\s*([\d.,]+)\s*([A-Za-z]{0,3})\s*$")


def parse_bytes(text: str | None) -> int:
    """Parse '12.3 GB' / '500MB' / '1048576' into an int number of bytes. 0 on empty/invalid."""
    if text is None:
        return 0
    s = str(text).strip()
    if not s:
        return 0
    m = _BYTES_RE.match(s)
    if not m:
        return 0
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "B").upper()
    return int(num * _UNIT_FACTORS.get(unit, 1))


#: Selectable time ranges for the downloads tab.
RANGES = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "1y": 365,
    "all": None,
}


def parse_range(key: str | None) -> tuple[str | None, str]:
    """Return (start_iso | None, end_iso) for a range key. 'all' -> (None, now)."""
    now = _dt.datetime.now(_dt.UTC)
    end = now.isoformat()
    days = RANGES.get(key or "30d", 30)
    if days is None:
        return None, end
    start = (now - _dt.timedelta(days=days)).isoformat()
    return start, end


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def range_bounds(
    key: str | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Resolve time-frame bounds. An explicit start/end date (YYYY-MM-DD) wins over the preset
    range key; a lone start or end makes an open-ended range."""
    s = start if start and _DATE_RE.match(start) else None
    e = end if end and _DATE_RE.match(end) else None
    if s or e:
        return (f"{s}T00:00:00+00:00" if s else None), (f"{e}T23:59:59+00:00" if e else None)
    return parse_range(key)
