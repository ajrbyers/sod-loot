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


@functools.lru_cache(maxsize=1)
def _token_names():
    """Exact item names ruled to be tokens despite lacking the prefix
    (e.g. Crusader's Chalice). Lowercased name -> which raid's token."""
    cfg = json.loads(TIERS_FILE.read_text())
    return {
        name.lower(): raid
        for key, raid in (("scarlet_enclave", "se"), ("naxxramas", "naxx"))
        for name in cfg[key].get("extra_tokens", [])
    }


def all_items():
    return _catalogue()


def _requirements(item_type):
    """What a toon needs to SR an item of this type.

    * token    -> nothing (SR-able by anyone).
    * standard -> tier set bonus (non-token).
    * rare     -> attendance + parse + fully enchanted + set bonus.
    """
    return {
        "attendance": item_type == "rare",
        "set_bonus": item_type in ("standard", "rare"),
        "parse": item_type == "rare",
        "enchants": item_type == "rare",
    }


def classify(name):
    """Classify an exact item name under the loot rules: 'rare' (curated
    list) > 'token' (Consecrated/Desecrated) > 'standard'. None for no name."""
    n = (name or "").strip().lower()
    if not n:
        return None
    if any(it["name"].lower() == n for it in _catalogue()):
        return "rare"
    if n in _token_names():
        return "token"
    if any(n.startswith(prefix) for prefix in _token_prefixes()):
        return "token"
    return "standard"


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
    if token_raid is None:
        for name, raid in _token_names().items():
            if q in name:
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
