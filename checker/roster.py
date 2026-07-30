"""Guild roster from a Guild Roster Manager (GRM) CSV export.

Powers the character-name autosuggest and prefills a player's alts. Every
character row in the export carries the player's full alt cluster in the
"Player Alts" column (e.g. "Akaslam-WildGrowth,Shapíe-WildGrowth(main)"), so a
single row lookup yields the complete prefill for the toons box.
"""
import csv
import functools
import unicodedata
from pathlib import Path

from django.conf import settings

ALT_MAIN_MARKER = "(main)"


def fold(name):
    """Accent- and case-insensitive key, so "shapie" matches "Shapíe"."""
    decomposed = unicodedata.normalize("NFD", name or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _clean_alt(entry):
    """"Shapíe-WildGrowth(main)" -> "Shapíe" (realm suffix + marker stripped)."""
    entry = entry.strip()
    if entry.endswith(ALT_MAIN_MARKER):
        entry = entry[: -len(ALT_MAIN_MARKER)].strip()
    name, _, _realm = entry.partition("-")
    return name.strip()


@functools.lru_cache(maxsize=2)
def _load(path, mtime):
    """mtime participates in the cache key so a fresh GRM export dropped over
    the old file is picked up without a restart."""
    characters = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            alts = [
                _clean_alt(a)
                for a in (row.get("Player Alts") or "").split(",")
                if a.strip()
            ]
            characters.append(
                {
                    "name": name,
                    "level": (row.get("Level") or "").strip(),
                    "class": (row.get("Class") or "").strip(),
                    "main_or_alt": (row.get("Main/Alt") or "").strip(),
                    "alts": alts,
                }
            )
    return characters


def characters():
    path = Path(settings.ROSTER_FILE)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    return _load(str(path), mtime)


def search(query, limit=10):
    """Prefix matches first, then substring matches, capped at `limit`."""
    q = fold((query or "").strip())
    if not q:
        return []
    prefix, contains = [], []
    for character in characters():
        key = fold(character["name"])
        if key.startswith(q):
            prefix.append(character)
        elif q in key:
            contains.append(character)
    return (prefix + contains)[:limit]
