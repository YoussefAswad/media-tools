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


def human_duration(ms: int | float | None) -> str:
    """Milliseconds -> a compact elapsed string: '820ms', '4.2s', '1m 12s', '2h 05m'. Empty
    for a missing value (old rows recorded before durations were tracked)."""
    if ms is None or ms < 0:
        return ""
    if ms < 1000:
        return f"{int(ms)}ms"
    secs = ms / 1000
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# Two genuinely different scales: the plain form is decimal SI (GB = 1000^3), the IEC form is
# binary (GiB = 1024^3, what qBittorrent counts in). A UI toggle picks which to render, so the
# same byte count shows a different magnitude in each. Suffixes are matched to their scale.
def human_uptime(seconds: float | None) -> str:
    """Seconds -> a compact uptime string: '10d 22h', '3h 12m', '8m'. Empty for missing."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def humanize_days(days: float) -> str:
    """A coarse human span for disk time-to-full: '~3 days', '~5 weeks', '~2 years'. Clamps a
    very long horizon to '>1 year' so a nearly-flat disk doesn't render an absurd number."""
    if days <= 0:
        return ""
    if days < 1:
        return "<1 day"
    if days < 14:
        return f"~{round(days)} days"
    if days < 90:
        return f"~{round(days / 7)} weeks"
    if days < 365:
        return f"~{round(days / 30)} months"
    if days < 365 * 3:
        return f"~{days / 365:.1f} years"
    return ">3 years"


_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]
_UNITS_IEC = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
_UNIT_FACTORS = {
    "B": 1,
    # decimal / SI
    "KB": 1000, "K": 1000,
    "MB": 1000**2, "M": 1000**2,
    "GB": 1000**3, "G": 1000**3,
    "TB": 1000**4, "T": 1000**4,
    "PB": 1000**5, "P": 1000**5,
    # binary / IEC
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
}


def human_bytes(n: int | float | None, *, iec: bool = False) -> str:
    """Format a byte count. Decimal by default (``1_500_000_000 -> '1.50 GB'``, base 1000); with
    ``iec`` it's binary (``1073741824 -> '1.00 GiB'``, base 1024). The two are different
    magnitudes for the same input, not a relabelled suffix."""
    units = _UNITS_IEC if iec else _UNITS
    base = 1024 if iec else 1000
    if not n or n < 0:
        return "0 B"
    size = float(n)
    for unit in units:
        if size < base or unit == units[-1]:
            return (f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}")
        size /= base
    return f"{size:.2f} {units[-1]}"


_BYTES_RE = re.compile(r"^\s*([\d.,]+)\s*([A-Za-z]{0,3})\s*$")


def parse_bytes(text: str | None) -> int:
    """Parse '12.3 GB' / '500MiB' / '1048576' into an int number of bytes. 0 on empty/invalid.
    Decimal suffixes (GB/MB/...) are 1000-based; IEC suffixes (GiB/MiB/...) are 1024-based, so
    a value round-trips with whichever unit :func:`human_bytes` rendered it in."""
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
