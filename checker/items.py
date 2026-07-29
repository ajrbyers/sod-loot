"""Loads the curated Naxx / Scarlet Enclave item catalogue and answers, for a
searched item, the FULL set of loot requirements (not just the parse)."""
import functools
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
ITEMS_FILE = DATA_DIR / "items.json"
TIERS_FILE = DATA_DIR / "tier_sets.json"


@functools.lru_cache(maxsize=1)
def _catalogue():
    return json.loads(ITEMS_FILE.read_text()).get("items", [])


@functools.lru_cache(maxsize=1)
def _token_prefixes():
    cfg = json.loads(TIERS_FILE.read_text())
    # Lowercased prefixes -> which raid's token.
    return {
        cfg["scarlet_enclave"]["token_prefix"].lower(): "se",
        cfg["naxxramas"]["token_prefix"].lower(): "naxx",
    }


def all_items():
    return _catalogue()


def _requirements(item_type):
    """What a toon needs to SR an item of this type.

    * token    -> attendance only (always SR-able).
    * standard -> attendance + tier set bonus (non-token).
    * rare     -> parse + attendance + fully enchanted + set bonus.
    """
    return {
        "attendance": True,
        "set_bonus": item_type in ("standard", "rare"),
        "parse": item_type == "rare",
        "enchants": item_type == "rare",
    }


def search(query):
    q = (query or "").strip().lower()
    if not q:
        return {"query": query, "item_type": None, "matches": []}

    matches = [it for it in _catalogue() if q in it["name"].lower()]

    # Classify: rare (on the list) > token (Consecrated/Desecrated) > standard.
    token_raid = None
    for prefix, raid in _token_prefixes().items():
        if q.startswith(prefix) or prefix in q:
            token_raid = raid
            break

    if matches:
        item_type = "rare"
    elif token_raid:
        item_type = "token"
    else:
        item_type = "standard"

    return {
        "query": query,
        "item_type": item_type,
        "token_raid": token_raid,
        "matches": matches,
        "requires": _requirements(item_type),
    }
