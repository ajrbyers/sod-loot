"""Thin Warcraft Logs v2 (GraphQL) client using only the standard library.

Handles OAuth2 client-credentials token caching and the two queries this tool
needs: character best-parse lookup and guild attendance.
"""
import json
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from . import apicache


def _norm(s):
    """Normalise a character name for matching (NFC + casefold), so accented
    names like 'Zkittlèz' compare regardless of Unicode form / case."""
    return unicodedata.normalize("NFC", (s or "").strip()).casefold()

# OAuth tokens are minted on www and are valid for every partition endpoint.
TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"

# In-process caches. Fine for a small single-worker tool; swap for a real cache
# (Redis / Django cache framework) if you scale out to multiple workers.
_token_cache = {"access_token": None, "expires_at": 0}
_guild_server_cache = {"slug": None, "region": None, "name": None, "fetched": False}
# name(lower) -> {"id": int, "name": str}; refreshed periodically.
_member_cache = {"map": None, "expires_at": 0}
_MEMBER_TTL = 900  # seconds


class WCLError(Exception):
    """Raised when the Warcraft Logs API can't be reached or returns errors."""


def _http_json(url, data=None, headers=None, method=None):
    headers = headers or {}
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise WCLError(f"Warcraft Logs returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WCLError(f"Couldn't reach Warcraft Logs: {exc.reason}") from exc


def _get_token():
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 60 > now:
        return _token_cache["access_token"]

    client_id = settings.WCL_CLIENT_ID
    client_secret = settings.WCL_CLIENT_SECRET
    if not client_id or not client_secret:
        raise WCLError(
            "WCL_CLIENT_ID / WCL_CLIENT_SECRET are not configured. "
            "Add them to your .env file."
        )

    # HTTP Basic auth with the client id/secret is the documented flow.
    import base64

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = _http_json(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    token = payload.get("access_token")
    if not token:
        raise WCLError(f"No access_token in token response: {payload}")
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    return token


def graphql(query, variables=None):
    token = _get_token()
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    payload = _http_json(
        settings.WCL_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise WCLError(f"GraphQL error: {msgs}")
    return payload.get("data", {})


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
_GUILD_SERVER_QUERY = """
query GuildServer($id: Int!) {
  guildData {
    guild(id: $id) {
      name
      server { slug region { slug } }
    }
  }
}
"""

# SoD raids run as both 40-man and 20-man; each is ranked separately. We fetch
# both and take the character's higher best-performance-average. metric/spec are
# variables so callers can request e.g. a Holy paladin's Shockadin (dps) parse.
_RANKINGS_FRAGMENT = """
      s40: zoneRankings(zoneID: $zone, metric: $metric, size: 40, specName: $spec)
      s20: zoneRankings(zoneID: $zone, metric: $metric, size: 20, specName: $spec)
"""

_CHARACTER_BY_NAME_QUERY = """
query CharByName($name: String!, $server: String!, $region: String!, $zone: Int!, $metric: CharacterPageRankingMetricType!, $spec: String) {
  characterData {
    character(name: $name, serverSlug: $server, serverRegion: $region) {
      id
      name
%s
    }
  }
}
""" % _RANKINGS_FRAGMENT

_CHARACTER_BY_ID_QUERY = """
query CharById($id: Int!, $zone: Int!, $metric: CharacterPageRankingMetricType!, $spec: String) {
  characterData {
    character(id: $id) {
      id
      name
%s
    }
  }
}
""" % _RANKINGS_FRAGMENT

# The guild roster gives us numeric WCL ids up front, saving a name+realm
# resolution round-trip for the common (guildie) case.
_MEMBERS_QUERY = """
query Members($id: Int!, $page: Int!) {
  guildData {
    guild(id: $id) {
      members(limit: 100, page: $page) {
        has_more_pages
        data { id name }
      }
    }
  }
}
"""

# Resolves name -> WCL character id for non-roster raiders (trials/pugs) via a
# report they appeared in.
_RANKED_CHARS_QUERY = """
query Ranked($code: String!) {
  reportData {
    report(code: $code) {
      rankedCharacters { id name }
    }
  }
}
"""

_ATTENDANCE_QUERY = """
query Attendance($id: Int!, $page: Int!) {
  guildData {
    guild(id: $id) {
      attendance(limit: 25, page: $page) {
        has_more_pages
        data {
          code
          startTime
          zone { name }
          players { name presence }
        }
      }
    }
  }
}
"""


def get_guild_server():
    """Return (realm_slug, region_slug, guild_name), auto-detected & cached.

    An explicit WCL_REALM_SLUG / WCL_REGION in settings overrides detection.
    """
    if settings.WCL_REALM_SLUG and settings.WCL_REGION:
        return settings.WCL_REALM_SLUG, settings.WCL_REGION, None
    if _guild_server_cache["fetched"]:
        return (
            _guild_server_cache["slug"],
            _guild_server_cache["region"],
            _guild_server_cache["name"],
        )
    data = graphql(_GUILD_SERVER_QUERY, {"id": settings.GUILD_ID})
    guild = (data.get("guildData") or {}).get("guild")
    if not guild:
        raise WCLError(f"Guild {settings.GUILD_ID} not found on Warcraft Logs.")
    server = guild.get("server") or {}
    _guild_server_cache.update(
        slug=server.get("slug"),
        region=(server.get("region") or {}).get("slug"),
        name=guild.get("name"),
        fetched=True,
    )
    return (
        _guild_server_cache["slug"],
        _guild_server_cache["region"],
        _guild_server_cache["name"],
    )


def get_member_map(force=False):
    """Return {lowercase_name: {"id": int, "name": str}} for the guild roster."""
    now = time.time()
    if not force and _member_cache["map"] is not None and _member_cache["expires_at"] > now:
        return _member_cache["map"]

    mapping = {}
    page = 1
    while page <= 50:  # 100/page * 50 = 5000 members, plenty
        data = graphql(_MEMBERS_QUERY, {"id": settings.GUILD_ID, "page": page})
        members = ((data.get("guildData") or {}).get("guild") or {}).get("members") or {}
        for m in members.get("data") or []:
            mapping[_norm(m["name"])] = {"id": m["id"], "name": m["name"]}
        if not members.get("has_more_pages"):
            break
        page += 1

    _member_cache["map"] = mapping
    _member_cache["expires_at"] = now + _MEMBER_TTL
    return mapping


def _summarise_size(zr):
    """Pull best-average, top single parse, and per-boss list from one size's
    zoneRankings block."""
    zr = zr or {}
    best = zr.get("bestPerformanceAverage")
    rankings = []
    top = None
    for r in zr.get("rankings", []) or []:
        enc = r.get("encounter") or {}
        pct = r.get("rankPercent")
        rankings.append({"encounter": enc.get("name"), "best_percent": pct})
        if pct is not None and (top is None or pct > top["percent"]):
            top = {"encounter": enc.get("name"), "percent": pct}
    return best, top, rankings


def _parse_from_character(character):
    """Evaluate both 40-man and 20-man rankings and keep the higher best-average."""
    candidates = []
    per_size = {}  # {40: best_avg_or_None, 20: best_avg_or_None}
    for size, key in ((40, "s40"), (20, "s20")):
        best, top, rankings = _summarise_size(character.get(key))
        per_size[size] = best
        if best is not None:
            candidates.append((best, size, top, rankings))

    if not candidates:
        return {
            "found": True,
            "name": character.get("name"),
            "best_average": None,
            "size": None,
            "sizes": per_size,
            "top_parse": None,
            "rankings": [],
        }

    # Highest best-average wins the headline (ties -> 40-man, listed first).
    best, size, top, rankings = max(candidates, key=lambda c: c[0])
    return {
        "found": True,
        "name": character.get("name"),
        "best_average": best,  # exact WCL value, unrounded
        "size": size,  # which raid size (40/20) produced the headline
        "sizes": per_size,  # both sizes' best-averages, for transparency
        "top_parse": top,  # highest single-boss parse in the winning size
        "rankings": rankings,
    }


_NOT_FOUND = {"found": False, "name": None, "best_average": None, "zone_name": None, "rankings": []}


def get_best_parse(name, realm_slug=None, region=None, metric="dps", spec=None, force=False):
    """Cached best-parse lookup. Returns (result, cache_meta).

    metric: 'dps' or 'hps'. spec: optional WCL spec name (e.g. 'Holy' for a
    Shockadin paladin's damage parse); None uses the character's default spec.
    """
    key = f"parse:{settings.PARSE_ZONE_ID}:{metric}:{(spec or '').lower()}:{_norm(name)}"
    return apicache.get_or_set(
        key, lambda: _get_best_parse(name, realm_slug, region, metric, spec), force=force
    )


def _resolve_character_id(name):
    """Resolve a name to a WCL character id via a recent report they were in.

    Covers non-roster raiders (trials/pugs) without a name+realm round-trip.
    """
    target = _norm(name)
    for raid in _fetch_all_raids():
        if not any(_norm(n) == target for n in raid["present"]):
            continue
        data = graphql(_RANKED_CHARS_QUERY, {"code": raid["code"]})
        ranked = ((data.get("reportData") or {}).get("report") or {}).get("rankedCharacters") or []
        for c in ranked:
            if _norm(c["name"]) == target:
                return c["id"]
    return None


def _get_best_parse(name, realm_slug=None, region=None, metric="dps", spec=None):
    """Return the character's best-performance-average and per-boss rankings.

    Resolves the name to a WCL character id via the guild roster first (no
    extra round-trip), then via guild reports, then falls back to a name+realm
    lookup — which requires the partition-scoped WCL_API_URL endpoint to be the
    one hosting the character (see settings).
    """
    zone = settings.PARSE_ZONE_ID
    variables = {"zone": zone, "metric": metric, "spec": spec or None}

    # 1) Guild roster gives us the character id directly (most common case).
    member = get_member_map().get(_norm(name))
    char_id = member["id"] if member else None

    # 2) Non-roster raiders (trials/pugs): resolve id via a report they were in.
    if char_id is None:
        char_id = _resolve_character_id(name)

    if char_id is not None:
        data = graphql(_CHARACTER_BY_ID_QUERY, {"id": char_id, **variables})
        character = (data.get("characterData") or {}).get("character")
        if character:
            return _parse_from_character(character)

    # 3) Last resort: direct name+realm lookup (non-guildies who've never
    #    raided with us, e.g. SR-ing outsiders).
    if realm_slug is None or region is None:
        realm_slug, region, _ = get_guild_server()
    data = graphql(
        _CHARACTER_BY_NAME_QUERY,
        {"name": name, "server": realm_slug, "region": region, **variables},
    )
    character = (data.get("characterData") or {}).get("character")
    if character:
        return _parse_from_character(character)
    return dict(_NOT_FOUND)


def _reset_week_start(when):
    """Return the datetime of the SoD reset (Wednesday RESET_HOUR_UTC) that opened
    the lockout `when` belongs to."""
    import datetime as dt

    days_since = (when.weekday() - settings.RESET_WEEKDAY) % 7
    start = (when - dt.timedelta(days=days_since)).replace(
        hour=settings.RESET_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if start > when:
        # `when` is on the reset weekday but before the reset hour -> previous lockout.
        start -= dt.timedelta(days=7)
    return start


def _fetch_all_raids():
    """Page through guild attendance back to a generous cutoff (window + buffer).

    Returns a list of {"start", "present"(set), "zone", "code"}, newest-first.
    Cached guild-wide so it serves any set of names being checked.
    """
    import datetime as dt

    lookback = settings.WEEKS_WINDOW + 4
    cutoff_ms = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=lookback)
    ).timestamp() * 1000

    raids = []
    page = 1
    while page <= 20:  # safety bound
        data = graphql(_ATTENDANCE_QUERY, {"id": settings.GUILD_ID, "page": page})
        attendance = ((data.get("guildData") or {}).get("guild") or {}).get(
            "attendance"
        ) or {}
        rows = attendance.get("data") or []
        stop = False
        for raid in rows:
            start = raid.get("startTime") or 0
            if start < cutoff_ms:
                stop = True  # newest-first, so we're past the window
                continue
            raids.append(
                {
                    "start": start,
                    "present": {
                        _norm(p.get("name", ""))
                        for p in (raid.get("players") or [])
                        if p.get("presence") == 1
                    },
                    "zone": (raid.get("zone") or {}).get("name"),
                    "code": raid.get("code"),
                }
            )
        if stop or not attendance.get("has_more_pages"):
            break
        page += 1
    return raids


def get_attendance(names, weeks_window, force=False):
    """Count distinct SoD reset weeks (Wed->Wed) any of `names` raided with us.

    Returns (result, cache_meta). The window is the most recent `weeks_window`
    resets (current lockout plus the previous weeks_window-1).
    """
    import datetime as dt

    wanted = {_norm(n) for n in names if n.strip()}

    all_raids, meta = apicache.get_or_set(
        f"raids:{settings.GUILD_ID}", _fetch_all_raids, force=force
    )

    now = dt.datetime.now(dt.timezone.utc)
    current_reset = _reset_week_start(now)
    window_start = current_reset - dt.timedelta(weeks=weeks_window - 1)
    cutoff_ms = window_start.timestamp() * 1000

    resets = {}  # "YYYY-MM-DD" (reset Wednesday) -> list of raid descriptions
    for raid in all_raids:
        if raid["start"] < cutoff_ms:
            continue
        hit = wanted & raid["present"]
        if not hit:
            continue
        when = dt.datetime.fromtimestamp(raid["start"] / 1000, dt.timezone.utc)
        key = _reset_week_start(when).strftime("%Y-%m-%d")
        resets.setdefault(key, []).append(
            {
                "date": when.strftime("%Y-%m-%d"),
                "zone": raid["zone"],
                "as": sorted(hit),
                "code": raid["code"],
            }
        )

    return {
        "distinct_weeks": len(resets),
        "window_start": window_start.strftime("%Y-%m-%d"),
        "weeks": {k: resets[k] for k in sorted(resets)},
    }, meta
