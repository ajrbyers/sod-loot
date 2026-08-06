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
