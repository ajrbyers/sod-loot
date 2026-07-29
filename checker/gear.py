"""Current-gear analysis for the SoD set-bonus + enchant warnings.

Reads live equipped gear from Blizzard (checker/blizzard.py) and works out:
  * how many Scarlet Enclave / Naxxramas tier pieces are worn, and
  * which enchantable slots are missing a permanent enchant.

Both feed *warnings* (not hard blocks): Chalice tokens can change gear between
raids, so this reflects the character's last-saved gear on the armory.
"""
import functools
import json
from pathlib import Path

from . import apicache, blizzard

DATA_FILE = Path(__file__).resolve().parent / "data" / "tier_sets.json"


@functools.lru_cache(maxsize=1)
def _cfg():
    return json.loads(DATA_FILE.read_text())


def _classify_set(set_id, sample_item_id, total_pieces, cfg):
    """Return 'se', 'naxx', or None for a set the character is wearing."""
    for key in ("scarlet_enclave", "naxxramas"):
        tier = cfg[key]
        code = "se" if key == "scarlet_enclave" else "naxx"
        if set_id in tier["set_ids"]:
            return code
        lo, hi = tier["item_id_band"]
        if total_pieces >= tier["min_total_pieces"] and lo <= (sample_item_id or 0) <= hi:
            return code
    return None


def analyse_gear(name, realm_slug=None, force=False):
    """Cached gear analysis. Returns (result, cache_meta)."""
    key = f"gear:{(realm_slug or '').lower()}:{name.strip().lower()}"
    return apicache.get_or_set(key, lambda: _analyse_gear(name, realm_slug), force=force)


def _analyse_gear(name, realm_slug=None):
    """Return a structured gear report for a character (or {'found': False})."""
    eq = blizzard.get_equipment(name, realm_slug)
    if not eq:
        return {"found": False}

    cfg = _cfg()
    required = set(cfg["enchant_required_slots"])
    exempt = set(cfg["enchant_exempt_slots"])

    tier_sets = {}  # set_id -> {"name", "equipped", "tier"}
    items = []
    for it in eq.get("equipped_items", []):
        slot = (it.get("slot") or {}).get("type")
        item_id = (it.get("item") or {}).get("id")
        quality = (it.get("quality") or {}).get("type")  # e.g. EPIC, RARE, POOR
        perms = [
            e for e in (it.get("enchantments") or [])
            if (e.get("enchantment_slot") or {}).get("type") == "PERMANENT"
        ]
        enchant = perms[0].get("display_string") if perms else None

        enchantable = slot in required
        items.append(
            {
                "slot": slot,
                "name": it.get("name"),
                "item_id": item_id,
                "quality": quality,
                "enchant": enchant,
                "enchantable": enchantable,
                "missing_enchant": enchantable and not perms,
            }
        )

        s = it.get("set")
        if s:
            iset = s.get("item_set") or {}
            sid = iset.get("id")
            if sid not in tier_sets:
                equipped = sum(1 for x in (s.get("items") or []) if x.get("is_equipped"))
                tier = _classify_set(sid, item_id, len(s.get("items") or []), cfg)
                if tier:
                    tier_sets[sid] = {"name": iset.get("name"), "equipped": equipped, "tier": tier}

    se_count = sum(t["equipped"] for t in tier_sets.values() if t["tier"] == "se")
    naxx_count = sum(t["equipped"] for t in tier_sets.values() if t["tier"] == "naxx")
    missing = [i["slot"] for i in items if i["missing_enchant"]]

    return {
        "found": True,
        "name": eq.get("character", {}).get("name") or name,
        "se_pieces": se_count,
        "naxx_pieces": naxx_count,
        "tier_sets": sorted(tier_sets.values(), key=lambda t: -t["equipped"]),
        "items": items,
        "missing_enchants": missing,
        "fully_enchanted": not missing,
    }


def evaluate(raid, analysis):
    """Given a raid ('se' or 'naxx') and a gear analysis, return set-bonus verdict.

    SE raid  -> need 6 SE tier pieces.
    Naxx raid -> need 6 tier pieces counting SE and Naxx together.
    """
    cfg = _cfg()
    if not analysis.get("found"):
        return {"applicable": True, "meets": None, "count": 0, "need": 6, "raid": raid}

    se = analysis["se_pieces"]
    naxx = analysis["naxx_pieces"]
    if raid == "naxx":
        need = cfg["naxxramas"]["min_pieces"]
        count = se + naxx
        detail = f"{count} tier pieces ({se} SE + {naxx} Naxx)"
    else:
        need = cfg["scarlet_enclave"]["min_pieces"]
        count = se
        detail = f"{se}/{need} Scarlet Enclave tier pieces"

    return {
        "applicable": True,
        "raid": raid,
        "count": count,
        "need": need,
        "meets": count >= need,
        "detail": detail,
    }


def token_prefixes():
    cfg = _cfg()
    return {
        "se": cfg["scarlet_enclave"]["token_prefix"],
        "naxx": cfg["naxxramas"]["token_prefix"],
    }
