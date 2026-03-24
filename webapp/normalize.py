"""
webapp/normalize.py — Shared text normalisation utilities.

Adapted from patterns in missing_popular_albums.py so that webapp modules
can normalise artist/album names without importing the top-level scripts.
"""
import re
import unicodedata

_PAREN_RE = re.compile(r"\([^)]*\)")
_EDITION_KEYWORDS = (
    "deluxe edition", "deluxe", "expanded edition", "expanded",
    "remaster", "remastered", "special edition", "limited edition",
    "bonus track version", "anniversary edition",
    "20th anniversary", "30th anniversary", "40th anniversary",
)


def normalize_text(value: str) -> str:
    """Lowercase, strip diacritics, remove punctuation, collapse spaces."""
    if not value:
        return ""
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
    )
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if value.startswith("the "):
        value = value[4:]
    return re.sub(r"\s+", " ", value).strip()


def normalize_album_title(value: str) -> str:
    """normalize_text plus removal of edition/remaster parentheticals."""
    if not value:
        return ""
    value = _PAREN_RE.sub(" ", value)
    value = normalize_text(value)
    for kw in _EDITION_KEYWORDS:
        value = value.replace(kw, " ")
    return re.sub(r"\s+", " ", value).strip()
