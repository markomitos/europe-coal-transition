"""Country lookups and deterministic text keys shared by source adapters."""

from __future__ import annotations

import hashlib
import re
import unicodedata

EU_COUNTRIES = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia",
    "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia",
    "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia",
    "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta", "NL": "Netherlands",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SK": "Slovakia",
    "SI": "Slovenia", "ES": "Spain", "SE": "Sweden",
}
WB_COUNTRIES = {"RS": "Serbia", "BA": "Bosnia & Herzegovina", "ME": "Montenegro",
                "MK": "North Macedonia", "XK": "Kosovo", "AL": "Albania"}
OTHER_COUNTRIES = {"GB": "United Kingdom", "TR": "Türkiye",
                   "NO": "Norway", "CH": "Switzerland"}
COUNTRIES = {**EU_COUNTRIES, **WB_COUNTRIES, **OTHER_COUNTRIES}
CYRILLIC = str.maketrans(dict(zip(
    "абвгдђежзијклљмнњопрстћуфхцчџш",
    ["a", "b", "v", "g", "d", "dj", "e", "z", "z", "i", "j", "k", "l", "lj",
     "m", "n", "nj", "o", "p", "r", "s", "t", "c", "u", "f", "h", "c", "c", "dz", "s"],
)))


def normalize_text(value: object) -> str:
    """Normalize accents, Serbian Cyrillic, punctuation, and spacing for matching."""
    text = str(value).strip().lower().translate(CYRILLIC).replace("ł", "l").replace("đ", "dj")
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


COUNTRY_ALIASES = {normalize_text(name): iso for iso, name in COUNTRIES.items()}
COUNTRY_ALIASES.update({normalize_text(iso): iso for iso in COUNTRIES})
COUNTRY_ALIASES.update({
    "bosnia and herzegovina": "BA", "bosna i hercegovina": "BA", "bih": "BA",
    "srbija": "RS", "crna gora": "ME", "severna makedonija": "MK",
    "macedonia": "MK", "czech republic": "CZ", "kosovo un resolution 1244": "XK",
    "kosova": "XK", "turkey": "TR", "uk": "GB",
})


def stable_id(*parts: object) -> str:
    """Build an unambiguous deterministic identifier from normalized natural keys."""
    payload = "\x1f".join(normalize_text(part) for part in parts)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]

