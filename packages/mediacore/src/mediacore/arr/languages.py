"""Radarr/Sonarr language names → the ISO-639-2 codes that appear in Matroska track headers.

The arrs report a title's original language as a name from a fixed enum (``/api/v3/language``,
60 entries), while track headers carry ISO-639-2 codes — and often the bibliographic variant
(``fre``, ``ger``, ``dut``) rather than the terminological one (``fra``, ``deu``, ``nld``). Both
are mapped, so a match works whichever the muxer wrote.

The mapping is deliberately a static table rather than a library lookup: the arr vocabulary is
small, fixed, and includes entries that aren't languages at all (``Any``, ``Original``,
``Unknown``) which must resolve to "no opinion" rather than to a code.
"""

from __future__ import annotations

#: Names the arrs use for "we don't know" / "not a language". These must never contribute a code,
#: because an empty result is what tells the caller to keep every audio track.
NO_LANGUAGE = {"any", "original", "unknown", ""}

ARR_LANGUAGE_ISO: dict[str, set[str]] = {
    "Afrikaans": {"afr"},
    "Albanian": {"alb", "sqi"},
    "Arabic": {"ara"},
    "Bengali": {"ben"},
    "Bosnian": {"bos"},
    "Bulgarian": {"bul"},
    "Catalan": {"cat"},
    "Chinese": {"chi", "zho"},
    "Croatian": {"hrv"},
    "Czech": {"cze", "ces"},
    "Danish": {"dan"},
    "Dutch": {"dut", "nld"},
    "English": {"eng"},
    "Estonian": {"est"},
    "Finnish": {"fin"},
    "Flemish": {"dut", "nld"},  # arr lists it separately; Matroska tags it as Dutch
    "French": {"fre", "fra"},
    "Georgian": {"geo", "kat"},
    "German": {"ger", "deu"},
    "Greek": {"gre", "ell"},
    "Hebrew": {"heb", "iw"},
    "Hindi": {"hin"},
    "Hungarian": {"hun"},
    "Icelandic": {"ice", "isl"},
    "Indonesian": {"ind"},
    "Italian": {"ita"},
    "Japanese": {"jpn"},
    "Kannada": {"kan"},
    "Korean": {"kor"},
    "Latvian": {"lav"},
    "Lithuanian": {"lit"},
    "Macedonian": {"mac", "mkd"},
    "Malayalam": {"mal"},
    "Marathi": {"mar"},
    "Mongolian": {"mon"},
    "Norwegian": {"nor", "nob", "nno"},
    "Persian": {"per", "fas"},
    "Polish": {"pol"},
    "Portuguese": {"por"},
    "Portuguese (Brazil)": {"por"},
    "Romanian": {"rum", "ron"},
    "Romansh": {"roh"},
    "Russian": {"rus"},
    "Serbian": {"srp", "scc"},
    "Slovak": {"slo", "slk"},
    "Slovenian": {"slv"},
    "Spanish": {"spa"},
    "Spanish (Latino)": {"spa"},
    "Swedish": {"swe"},
    "Tagalog": {"tgl"},
    "Tamil": {"tam"},
    "Telugu": {"tel"},
    "Thai": {"tha"},
    "Turkish": {"tur"},
    "Ukrainian": {"ukr"},
    "Urdu": {"urd"},
    "Vietnamese": {"vie"},
}


def iso_for_arr_language(name: str | None) -> set[str]:
    """ISO codes for one arr language name. Empty when the name is unknown to us or is one of the
    arr's non-language placeholders — an empty set means "no opinion", never "drop everything"."""
    if not name or name.strip().lower() in NO_LANGUAGE:
        return set()
    return set(ARR_LANGUAGE_ISO.get(name.strip(), set()))
