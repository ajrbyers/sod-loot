"""Thin softres.it client for the soft-reserve audit page.

softres.it exposes an unauthenticated JSON endpoint per raid sheet. It only
stores item IDs, so names are resolved via Wowhead's tooltip endpoint
(dataEnv=4 selects the Classic-era/SoD item dataset) and cached long-term —
item names never change.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from django.core.cache import cache

from . import apicache

RAID_URL = "https://softres.it/api/raid/{raid_id}"
ITEM_URL = "https://nether.wowhead.com/tooltip/item/{item_id}?dataEnv=4&locale=0"

# Reserve sheets change right up to raid time; don't sit on them for hours.
RAID_TTL = 300
ITEM_TTL = 30 * 24 * 3600

# softres.it stores retail spec ids. Healing specs get their parses checked
# against HPS rather than DPS. Holy paladin (65) is deliberately absent:
# in SoD ours are shockadins, so their damage parse is the one that counts.
HEALER_SPECS = {105, 256, 257, 264}

# Arcane (62) is the healer-mage spec on softres — SoD mage healers sign up
# as Arcane; Fire (63) and Frost (64) are dps. An arcane mage reserving a
# heal-parse item (items.json mage_heal_parse) gets an HPS check alongside
# the usual DPS one.
MAGE_HEALER_SPECS = {62}

_ID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")
_URL_RE = re.compile(r"softres\.it/raid/([A-Za-z0-9]{4,20})", re.IGNORECASE)


class SoftresError(Exception):
    """Raised when softres.it can't be reached or rejects the raid ID."""


def parse_raid_id(text):
    """Accept a bare raid ID or any softres.it/raid/<id> URL; None if neither."""
    text = (text or "").strip()
    match = _URL_RE.search(text)
    if match:
        return match.group(1)
    if _ID_RE.match(text):
        return text
    return None


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sod-loot-checker"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SoftresError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SoftresError(f"unreachable: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise SoftresError(str(exc)) from exc


def get_raid(raid_id, force=False):
    """Cached softres raid sheet. Returns (data, cache_meta)."""

    def produce():
        url = RAID_URL.format(raid_id=urllib.parse.quote(raid_id, safe=""))
        try:
            return _http_json(url)
        except SoftresError as exc:
            if "HTTP 404" in str(exc):
                raise SoftresError(
                    f'No softres raid found for "{raid_id}" — check the URL/ID.'
                ) from exc
            raise SoftresError(f"Couldn't fetch the softres raid ({exc}).") from exc

    return apicache.get_or_set(f"softres:raid:{raid_id}", produce, force=force, ttl=RAID_TTL)


def reserve_snapshot(raid):
    """Slim a raw softres payload to the per-reserver item lists the contested
    sweep needs: [{"name": <reserver>, "items": [<item_id>, ...]}, ...]."""
    return [
        {"name": r.get("name") or "", "items": list(r.get("items") or [])}
        for r in raid.get("reserves") or []
    ]


def contested_summary(audits, force=False):
    """Aggregate contested items across the stored softres sheets.

    Each sheet contributes its *distinct* reservers per item — one person
    stacking an item ×3 never contests it, matching the single-sheet audit.
    Rows audited before snapshots existed are fetched live and backfilled;
    with force=True every sheet is re-fetched (falling back to its stored
    snapshot). Sheets softres.it no longer serves are skipped, not fatal.

    Returns (summary, cache_metas): summary is {"items": [...], "sheets_scanned",
    "sheets_missing"} with items ranked most-contested first.
    """
    agg = {}  # item_id -> aggregate row
    metas = []
    scanned = 0
    missing = []
    for audit in audits:
        snapshot = audit.reserves
        if force or not snapshot:
            try:
                raid, meta = get_raid(audit.raid_id, force=force)
            except SoftresError:
                if not snapshot:
                    missing.append(audit.raid_id)
                    continue
                # The sheet is gone from softres.it; the snapshot is history.
            else:
                metas.append(meta)
                snapshot = reserve_snapshot(raid)
                if snapshot != audit.reserves:
                    audit.reserves = snapshot
                    audit.save(update_fields=["reserves", "updated"])
        scanned += 1

        holders = {}  # item_id -> distinct reserver names on this sheet
        for r in snapshot:
            who = (r.get("name") or "").strip().casefold()
            for item_id in set(r.get("items") or []):
                holders.setdefault(item_id, set()).add(who)

        for item_id, names in holders.items():
            row = agg.setdefault(
                item_id,
                {
                    "id": item_id,
                    "instances": set(),
                    "sheets": 0,
                    "contested_sheets": 0,
                    "total_reservers": 0,
                    "max_reservers": 0,
                },
            )
            if audit.instance:
                row["instances"].add(audit.instance)
            row["sheets"] += 1
            row["total_reservers"] += len(names)
            row["max_reservers"] = max(row["max_reservers"], len(names))
            if len(names) > 1:
                row["contested_sheets"] += 1

    rows = sorted(
        agg.values(),
        key=lambda r: (-r["contested_sheets"], -r["total_reservers"], r["id"]),
    )
    for row in rows:
        row["name"] = item_name(row["id"])
        row["instances"] = sorted(row["instances"])
        row["contested"] = row["contested_sheets"] > 0

    return {
        "items": rows,
        "sheets_scanned": scanned,
        "sheets_missing": missing,
    }, metas


def item_name(item_id):
    """Resolve an item ID to its name via Wowhead; None if the lookup fails.

    Successes cache for a month; failures aren't cached, so a transient
    Wowhead blip doesn't pin a blank name."""
    key = f"softres:item:{item_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        name = _http_json(ITEM_URL.format(item_id=int(item_id))).get("name")
    except (SoftresError, ValueError, TypeError):
        return None
    if name:
        cache.set(key, name, ITEM_TTL)
    return name
