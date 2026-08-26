"""External subtitle files added to the mux, with language derived from the filename.

MKVToolNix derives a track's language from language codes embedded in the file name
(e.g. ``Movie.en.srt`` -> English, ``Movie.spa.forced.srt`` -> Spanish + forced). We do the
same: split the name on separators and pick the last recognized ISO-639 code, also noting
``forced`` / ``sdh`` markers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Recognized subtitle container extensions (informational; mkvmerge auto-detects the format).
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt", ".pgs", ".idx"}

_FORCED_TOKENS = {"forced", "foreign"}
_SDH_TOKENS = {"sdh", "cc", "hi"}

# ISO 639-1 (2-letter) and common ISO 639-2/B & /T (3-letter) codes.
_LANG_CODES: frozenset[str] = frozenset(
    {
        # 2-letter
        "en", "es", "fr", "de", "it", "pt", "nl", "sv", "no", "da", "fi", "pl", "ru",
        "uk", "cs", "sk", "hu", "ro", "bg", "el", "tr", "he", "ar", "fa", "hi", "bn",
        "ta", "te", "th", "vi", "id", "ms", "ja", "ko", "zh", "hr", "sr", "sl", "et",
        "lv", "lt", "is", "ga", "cy", "eu", "ca", "gl", "af", "sw", "fil", "tl",
        # 3-letter (639-2/B and /T)
        "eng", "spa", "fre", "fra", "ger", "deu", "ita", "por", "dut", "nld", "swe",
        "nor", "dan", "fin", "pol", "rus", "ukr", "cze", "ces", "slo", "slk", "hun",
        "rum", "ron", "bul", "gre", "ell", "tur", "heb", "ara", "per", "fas", "hin",
        "ben", "tam", "tel", "tha", "vie", "ind", "may", "msa", "jpn", "kor", "chi",
        "zho", "hrv", "srp", "slv", "est", "lav", "lit", "ice", "isl", "gle", "wel",
        "cym", "baq", "eus", "cat", "glg", "afr", "swa", "und",
    }
)


@dataclass
class AddedSub:
    """An external subtitle file to append to the mux."""

    path: str
    language: str
    forced: bool
    hearing_impaired: bool
    make_default: bool
    file_index: int = 0  # mkvmerge input-file index, assigned by the planner


def derive_subtitle_metadata(path: str) -> tuple[str, bool, bool]:
    """Return (language, forced, hearing_impaired) inferred from the file name.

    Language is the last recognized ISO-639 token; ``und`` if none is found.
    """
    name = os.path.basename(path)
    stem = name[: name.rfind(".")] if "." in name else name
    tokens = [t for t in re.split(r"[.\-_ ]+", stem.lower()) if t]

    language = "und"
    forced = False
    hearing = False
    for tok in tokens:
        if tok in _FORCED_TOKENS:
            forced = True
        elif tok in _SDH_TOKENS:
            hearing = True
        elif tok in _LANG_CODES:
            language = tok
    return language, forced, hearing


def make_added_sub(path: str, *, make_default: bool) -> AddedSub:
    language, forced, hearing = derive_subtitle_metadata(path)
    return AddedSub(
        path=path,
        language=language,
        forced=forced,
        hearing_impaired=hearing,
        make_default=make_default,
    )
