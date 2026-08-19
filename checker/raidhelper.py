"""Raid-Helper client — the roster source for the comp builder.

Raid-Helper is where people actually sign up, and unlike softres it records the
role somebody signed as. Its JSON is oddly named: the `class` field holds the
*role* column (Tank / Melee / Ranged / Healer) and `spec` holds the spec, with a
trailing "1" disambiguating specs that share a name across classes ("Holy" is
the priest, "Holy1" the paladin). That pair is the only thing in any of our data
sources that can tell a shockadin (Holy1 + Melee) from a holy paladin healer
(Holy1 + Healer).

Events also carry the id of their linked softres sheet, and signups carry the
signer's Discord id — the same id softres stores against a reserve. Joining on
it turns Raid-Helper's Discord nicknames ("<OC>Kalu|Lipis|Idhunn") into the real
character names the rest of this app looks up on Warcraft Logs and the armory.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import apicache

EVENT_URL = "https://raid-helper.dev/api/event/{event_id}"

# Signups change right up to raid time; don't sit on them.
EVENT_TTL = 300

# Raid-Helper event ids are Discord snowflakes.
_ID_RE = re.compile(r"^\d{15,25}$")
_URL_RE = re.compile(r"raid-helper\.(?:dev|xyz)/event/(\d{15,25})", re.IGNORECASE)

# Signup statuses that mean "not in the raid". Late/Tentative are kept but
# flagged, so the raid lead can see them without them taking a seat.
ABSENT = {"Absence", "Bench"}
UNSURE = {"Late", "Tentative"}


class RaidHelperError(Exception):
    """Raised when Raid-Helper can't be reached or rejects the event id."""


def parse_event_id(text):
    """Accept a bare event id or any raid-helper.dev/xyz event URL."""
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
        raise RaidHelperError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RaidHelperError(f"unreachable: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise RaidHelperError(str(exc)) from exc


def get_event(event_id, force=False):
    """Cached Raid-Helper event. Returns (data, cache_meta)."""

    def produce():
        url = EVENT_URL.format(event_id=urllib.parse.quote(event_id, safe=""))
        try:
            data = _http_json(url)
        except RaidHelperError as exc:
            if "HTTP 404" in str(exc):
                raise RaidHelperError(
                    f'No Raid-Helper event found for "{event_id}" — check the link.'
                ) from exc
            raise RaidHelperError(
                f"Couldn't fetch the Raid-Helper event ({exc})."
            ) from exc
        # Raid-Helper answers 200 for an event it can't serve, saying so in the
        # body instead: {"status": "failed", "reason": "unknown event"}. Past
        # events get cleaned up, so this is the common failure, not an edge case.
        if not isinstance(data, dict):
            raise RaidHelperError(
                f'No Raid-Helper event found for "{event_id}" — check the link.'
            )
        if data.get("status") == "failed" or "signups" not in data:
            reason = (data.get("reason") or "").strip()
            raise RaidHelperError(
                f'Raid-Helper couldn\'t serve event "{event_id}"'
                + (f" ({reason})" if reason else "")
                + " — check the link, or the event may have been deleted."
            )
        return data

    return apicache.get_or_set(
        f"rh:event:{event_id}", produce, force=force, ttl=EVENT_TTL
    )


# ---------------------------------------------------------------------------
# Discord nicknames -> character names
# ---------------------------------------------------------------------------
# People list their toons in their Discord nickname in every format going:
# "<OC>Kalu|Lipis|Idhunn", "Netti/Prata/Diakrath", "Striké(ManyAlts)". Guild
# tags and parentheticals are noise; the rest are candidate character names.
_TAG_RE = re.compile(r"[<\[(][^>\])]*[>\])]")
_SPLIT_RE = re.compile(r"[/|,]+")
_CLEAN_RE = re.compile(r"[^\wÀ-ɏͰ-῿]+", re.UNICODE)


def candidate_names(nickname):
    """Every plausible character name in a Discord nickname, best guess first."""
    text = _TAG_RE.sub(" ", nickname or "")
    out = []
    for part in _SPLIT_RE.split(text):
        name = _CLEAN_RE.sub("", part).strip()
        # Real toon names; skip the "ManyAlts"-style noise and bare initials.
        if 2 <= len(name) <= 12:
            out.append(name)
    return out
