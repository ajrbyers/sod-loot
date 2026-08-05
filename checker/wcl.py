"""Thin Warcraft Logs v2 (GraphQL) client using only the standard library.

Handles OAuth2 client-credentials token caching and the two queries this tool
needs: character best-parse lookup and guild attendance.
"""
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from . import apicache, roster


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
# classID -> {"name": str, "specs": [str, ...]}; static game data, cached for
# the process lifetime.
_class_specs_cache = {"map": None}
# name(lower) -> {"id": int, "name": str}; refreshed periodically.
_member_cache = {"map": None, "expires_at": 0}
_MEMBER_TTL = 900  # seconds


class WCLError(Exception):
    """Raised when the Warcraft Logs API can't be reached or returns errors."""


def _http_json(url, data=None, headers=None, method=None, timeout=20):
    headers = headers or {}
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise WCLError(f"Warcraft Logs returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WCLError(f"Couldn't reach Warcraft Logs: {exc.reason}") from exc
    except OSError as exc:
        # Read timeouts surface as bare TimeoutError/OSError, not URLError —
        # WCL computes report rankings lazily and can exceed the timeout.
        raise WCLError(f"Warcraft Logs timed out: {exc}") from exc


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


def graphql(query, variables=None, timeout=20):
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
        timeout=timeout,
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

# Class -> spec lists come from WCL's own game data, so SoD-specific specs
# (e.g. Rogue/Shaman/Warlock "Tank", Hunter "Melee") stay correct without a
# hand-maintained map.
_CLASS_SPECS_QUERY = """
query ClassSpecs {
  gameData {
    classes { id name specs { name } }
  }
}
"""

# Minimal character lookups used by the top-DPS feature to learn the classID
# before fanning out one zoneRankings per (spec, size).
_CHAR_CLASS_BY_ID_QUERY = """
query CharClassById($id: Int!) {
  characterData {
    character(id: $id) { id name classID }
  }
}
"""

_CHAR_CLASS_BY_NAME_QUERY = """
query CharClassByName($name: String!, $server: String!, $region: String!) {
  characterData {
    character(name: $name, serverSlug: $server, serverRegion: $region) { id name classID }
  }
}
"""

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

# Per-report rankings: per-fight blocks (every boss plus the fightID-10000
# complete-raid pseudo-fight) with players grouped into tanks/healers/dps roles,
# each carrying name/class/spec/amount/rankPercent for the requested metric.
_REPORT_RANKINGS_QUERY = """
query ReportRankings($code: String!, $metric: ReportRankingMetricType) {
  reportData {
    report(code: $code) { rankings(playerMetric: $metric) }
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


# ---------------------------------------------------------------------------
# Top DPS across every spec (both raid sizes)
# ---------------------------------------------------------------------------
def get_class_specs():
    """Return {classID: {"name": str, "specs": [str, ...]}} from WCL game data."""
    if _class_specs_cache["map"] is None:
        data = graphql(_CLASS_SPECS_QUERY)
        classes = (data.get("gameData") or {}).get("classes") or []
        _class_specs_cache["map"] = {
            c["id"]: {
                "name": c.get("name"),
                "specs": [s["name"] for s in c.get("specs") or []],
            }
            for c in classes
        }
    return _class_specs_cache["map"]


def _resolve_character(name):
    """Resolve a name to {"id", "name", "classID"} using the same chain as
    parses: guild roster -> guild reports -> name+realm on the partition
    endpoint. Returns None when nothing matches."""
    member = get_member_map().get(_norm(name))
    char_id = member["id"] if member else None
    if char_id is None:
        char_id = _resolve_character_id(name)
    if char_id is not None:
        data = graphql(_CHAR_CLASS_BY_ID_QUERY, {"id": char_id})
    else:
        realm_slug, region, _ = get_guild_server()
        data = graphql(
            _CHAR_CLASS_BY_NAME_QUERY,
            {"name": name, "server": realm_slug, "region": region},
        )
    return (data.get("characterData") or {}).get("character")


def _build_spec_rankings_query(specs):
    """One query with an aliased zoneRankings per (spec, size). Spec names are
    embedded as literals — they come from WCL's own gameData, and zoneRankings
    accepts only one specName per field, so variables can't express the fan-out."""
    parts = []
    for i, spec in enumerate(specs):
        for size in (40, 20):
            # specName wants the unspaced form ("BeastMastery"); the spaced
            # display name gameData reports is silently IGNORED, returning
            # unfiltered rankings misattributed to whatever spec was asked for.
            parts.append(
                '      s%d_%d: zoneRankings(zoneID: $zone, metric: dps, size: %d, specName: "%s")'
                % (size, i, size, spec.replace(" ", ""))
            )
    return (
        "query TopDps($id: Int!, $zone: Int!) {\n"
        "  characterData {\n"
        "    character(id: $id) {\n"
        "      id\n"
        "      name\n"
        "%s\n"
        "    }\n"
        "  }\n"
        "}"
    ) % "\n".join(parts)


def _spec_display(api_name, fallback):
    """Rankings report specs unspaced ("BeastMastery"); show them spaced.

    The ranking's own spec field is authoritative — it names the spec the parse
    was actually done in — with the requested spec as fallback."""
    if not api_name:
        return fallback
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", api_name)


def _top_dps_candidates(character, specs):
    """Best-DPS-amount encounter for each (spec, size), sorted by DPS desc.

    Unplayed specs come back with bestAmount 0 / rankPercent null — skipped."""
    candidates = []
    for i, spec in enumerate(specs):
        for size in (40, 20):
            blob = character.get("s%d_%d" % (size, i)) or {}
            top = None
            for r in blob.get("rankings") or []:
                amount = r.get("bestAmount") or 0
                if amount <= 0:
                    continue
                if top is None or amount > top["dps"]:
                    top = {
                        "dps": amount,
                        "encounter": (r.get("encounter") or {}).get("name"),
                        "rank_percent": r.get("rankPercent"),
                        "spec": _spec_display(r.get("spec"), spec),
                    }
            if top is None:
                continue
            candidates.append(
                {
                    "size": size,
                    "best_average": blob.get("bestPerformanceAverage"),
                    **top,
                }
            )
    candidates.sort(key=lambda c: c["dps"], reverse=True)
    return candidates


def _best_per_boss(character, specs):
    """Highest DPS per encounter across every (spec, size), in zone order.

    Unkilled bosses are kept (dps None) so the frontend can render a full
    boss-by-boss matrix with consistent columns."""
    order = []
    best = {}
    for i, spec in enumerate(specs):
        for size in (40, 20):
            blob = character.get("s%d_%d" % (size, i)) or {}
            for r in blob.get("rankings") or []:
                name = (r.get("encounter") or {}).get("name")
                if not name:
                    continue
                if name not in best:
                    order.append(name)
                    best[name] = None
                amount = r.get("bestAmount") or 0
                if amount <= 0:
                    continue
                cur = best[name]
                if cur is None or amount > cur["dps"]:
                    best[name] = {
                        "encounter": name,
                        "dps": amount,
                        "spec": _spec_display(r.get("spec"), spec),
                        "size": size,
                        "rank_percent": r.get("rankPercent"),
                    }
    return [
        best[n]
        or {"encounter": n, "dps": None, "spec": None, "size": None, "rank_percent": None}
        for n in order
    ]


_TOP_DPS_NOT_FOUND = {
    "found": False, "name": None, "class": None, "best": None, "specs": [], "bosses": []
}


def get_top_dps(name, force=False):
    """Cached highest raid-wide DPS across every spec of the character's class,
    in both 20-man and 40-man. Returns (result, cache_meta)."""
    # v3: unspaced specName filters + parse-reported spec attribution; version
    # bumps sidestep stale cached entries from earlier shapes.
    key = f"topdps3:{settings.PARSE_ZONE_ID}:{_norm(name)}"
    return apicache.get_or_set(key, lambda: _get_top_dps(name), force=force)


def _get_top_dps(name):
    character = _resolve_character(name)
    if not character:
        return dict(_TOP_DPS_NOT_FOUND)

    class_info = get_class_specs().get(character.get("classID")) or {}
    specs = class_info.get("specs") or []
    result = {
        "found": True,
        "name": character.get("name"),
        "class": class_info.get("name"),
        "best": None,
        "specs": [],
        "bosses": [],
    }
    if not specs:
        return result

    data = graphql(
        _build_spec_rankings_query(specs),
        {"id": character["id"], "zone": settings.PARSE_ZONE_ID},
    )
    ranked = (data.get("characterData") or {}).get("character") or {}
    candidates = _top_dps_candidates(ranked, specs)
    result["specs"] = candidates
    result["best"] = candidates[0] if candidates else None
    result["bosses"] = _best_per_boss(ranked, specs)
    return result


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


def _cached_all_raids(force=False):
    """Guild attendance reports, cached once for any consumer. Returns
    (raids, cache_meta)."""
    return apicache.get_or_set(
        f"raids:{settings.GUILD_ID}", _fetch_all_raids, force=force
    )


_COMPLETE_RAID_FIGHT_ID = 10000


def get_report_rankings(code, metric="dps", force=False):
    """One report's rankings blob for a metric, cached per (code, metric).

    Shared by the complete-raid map, the leaderboard sweep, and the
    after-action report card, so each report is fetched once per metric.
    Returns (rankings_dict, cache_meta).
    """
    def fetch_once():
        # WCL computes a report's rankings lazily on first request, which can
        # take well over the default timeout — give these calls longer.
        data = graphql(
            _REPORT_RANKINGS_QUERY, {"code": code, "metric": metric}, timeout=90
        )
        return (
            ((data.get("reportData") or {}).get("report") or {}).get("rankings") or {}
        )

    def fetch():
        # First requests can also 500 while WCL's computation is mid-flight;
        # a short pause and one retry usually lands after it completes.
        try:
            return fetch_once()
        except WCLError:
            time.sleep(2)
            return fetch_once()

    return apicache.get_or_set(f"reportrankings:{code}:{metric}", fetch, force=force)


def _fetch_complete_raid_map():
    """name(norm) -> best complete-raid DPS entry across the guild's SE logs.

    WCL blocks character-side rankings for the complete-raids pseudo-zone
    ("Unsupported zone") and its leaderboard is uncapped-by-name and truncated,
    so this is assembled from the guild's own reports instead: every full-clear
    log ranks each player's whole-raid DPS under fightID 10000.
    """
    raids, _ = _cached_all_raids()
    best = {}
    for raid in raids:
        if raid.get("zone") != settings.PARSE_ZONE_NAME:
            continue
        try:
            rankings, _ = get_report_rankings(raid["code"])
        except WCLError:
            # One slow/broken report mustn't sink the sweep; the next rebuild
            # retries it.
            continue
        for fight in rankings.get("data") or []:
            if fight.get("fightID") != _COMPLETE_RAID_FIGHT_ID:
                continue
            # Mixed logs can carry another zone's complete-raid pseudo-fight.
            enc = (fight.get("encounter") or {}).get("name")
            if enc and enc != settings.PARSE_ZONE_NAME:
                continue
            raid_size = fight.get("size")
            for role in (fight.get("roles") or {}).values():
                for c in role.get("characters") or []:
                    name = c.get("name")
                    amount = c.get("amount") or 0
                    if not name or amount <= 0:
                        continue
                    key = _norm(name)
                    cur = best.get(key)
                    if cur is None or amount > cur["dps"]:
                        best[key] = {
                            "dps": amount,
                            "spec": _spec_display(c.get("spec"), None),
                            "rank_percent": c.get("rankPercent"),
                            # Actual participant count, and the 20/40 bracket
                            # WCL files the run under.
                            "raid_size": raid_size,
                            "size": 40 if (raid_size or 0) > 21 else 20,
                        }
    return best


def get_complete_raid_map(force=False):
    """Cached guild-wide complete-raid bests. Returns (mapping, cache_meta)."""
    key = f"completeraid:{settings.GUILD_ID}:{settings.PARSE_ZONE_ID}"
    return apicache.get_or_set(key, _fetch_complete_raid_map, force=force)


def get_complete_raid_best(name, force=False):
    """One toon's best whole-raid DPS from the guild's logs, or None."""
    mapping, meta = get_complete_raid_map(force=force)
    return mapping.get(_norm(name)), meta


# ---------------------------------------------------------------------------
# Roster leaderboard + after-action report card (both built purely from the
# guild's own report rankings — no per-character queries)
# ---------------------------------------------------------------------------
_ROLE_LABELS = {"tanks": "Tank", "healers": "Healer", "dps": "DPS"}


def raid_date(raid):
    """A raid's start as YYYY-MM-DD (UTC)."""
    import datetime as dt

    return dt.datetime.fromtimestamp(
        (raid.get("start") or 0) / 1000, dt.timezone.utc
    ).strftime("%Y-%m-%d")


def _aggregate_leaderboard():
    """Fold every SE report's dps + hps rankings into per-player standings."""
    import datetime as dt

    raids, _ = _cached_all_raids()
    members = get_member_map()
    players = {}

    def row_for(c):
        key = _norm(c.get("name") or "")
        if not key:
            return None
        row = players.get(key)
        if row is None:
            row = players[key] = {
                "name": c.get("name"),
                "class": c.get("class"),
                "guildie": key in members,
                "roles": {},
                "overall": None,
                "best_boss": None,
                "best_parse": None,
                "best_hps": None,
                "weeks": set(),
                "last_seen": None,
            }
        return row

    se_raids = [r for r in raids if r.get("zone") == settings.PARSE_ZONE_NAME]
    failed = 0
    for raid in se_raids:
        when = dt.datetime.fromtimestamp(raid["start"] / 1000, dt.timezone.utc)
        week = _reset_week_start(when).strftime("%Y-%m-%d")
        date = when.strftime("%Y-%m-%d")

        try:
            dps_rankings, _ = get_report_rankings(raid["code"], "dps")
            hps_rankings, _ = get_report_rankings(raid["code"], "hps")
        except WCLError:
            failed += 1  # skip this log; the next rebuild retries it
            continue
        for fight in dps_rankings.get("data") or []:
            is_overall = fight.get("fightID") == _COMPLETE_RAID_FIGHT_ID
            encounter = (fight.get("encounter") or {}).get("name")
            # Mixed logs bundle several zones into one report; every fight entry
            # carries its own zone id, so filter per fight, not per report. The
            # complete-raid pseudo-fight lives in a separate pseudo-zone, so it
            # is guarded by its encounter name instead.
            if is_overall:
                if encounter and encounter != settings.PARSE_ZONE_NAME:
                    continue
            elif fight.get("zone") != settings.PARSE_ZONE_ID:
                continue
            for role, block in (fight.get("roles") or {}).items():
                for c in block.get("characters") or []:
                    row = row_for(c)
                    if row is None:
                        continue
                    row["roles"][role] = row["roles"].get(role, 0) + 1
                    row["weeks"].add(week)
                    if row["last_seen"] is None or date > row["last_seen"]:
                        row["last_seen"] = date
                    amount = c.get("amount") or 0
                    if amount <= 0:
                        continue
                    pct = c.get("rankPercent")
                    spec = _spec_display(c.get("spec"), None)
                    if is_overall:
                        if row["overall"] is None or amount > row["overall"]["dps"]:
                            row["overall"] = {
                                "dps": amount, "spec": spec, "rank_percent": pct
                            }
                    else:
                        if row["best_boss"] is None or amount > row["best_boss"]["dps"]:
                            row["best_boss"] = {
                                "dps": amount,
                                "encounter": encounter,
                                "spec": spec,
                                "rank_percent": pct,
                            }
                        if pct is not None and (
                            row["best_parse"] is None or pct > row["best_parse"]
                        ):
                            row["best_parse"] = pct

        # Healing: best single-boss HPS, healers only (a dps player's incidental
        # healing is noise, not a standing).
        for fight in hps_rankings.get("data") or []:
            if fight.get("fightID") == _COMPLETE_RAID_FIGHT_ID:
                continue
            if fight.get("zone") != settings.PARSE_ZONE_ID:
                continue
            block = (fight.get("roles") or {}).get("healers") or {}
            for c in block.get("characters") or []:
                row = row_for(c)
                if row is None:
                    continue
                amount = c.get("amount") or 0
                if amount <= 0:
                    continue
                if row["best_hps"] is None or amount > row["best_hps"]["hps"]:
                    row["best_hps"] = {
                        "hps": amount,
                        "rank_percent": c.get("rankPercent"),
                        "spec": _spec_display(c.get("spec"), None),
                    }

    # GRM alt clusters: the roster export names every player's full cluster,
    # letting attendance be counted across all their toons. GRM keys are
    # accent-folded so "Shapíe" in a log matches the export.
    fold_index = {}
    for key, row in players.items():
        fold_index.setdefault(roster.fold(row["name"]), key)
    clusters = {}
    for character in roster.characters():
        cluster = {roster.fold(character["name"])}
        cluster.update(roster.fold(a) for a in character["alts"])
        clusters[roster.fold(character["name"])] = cluster

    out = []
    for row in players.values():
        role = max(row["roles"], key=row["roles"].get) if row["roles"] else None
        cluster_weeks = set(row["weeks"])
        cluster_toons = []
        for fkey in clusters.get(roster.fold(row["name"]), ()):
            pkey = fold_index.get(fkey)
            if pkey is None:
                continue
            other = players[pkey]
            cluster_weeks |= other["weeks"]
            if other["name"] != row["name"]:
                cluster_toons.append(other["name"])
        out.append(
            {
                "name": row["name"],
                "class": row["class"],
                "role": _ROLE_LABELS.get(role, role),
                "guildie": row["guildie"],
                "overall": row["overall"],
                "best_boss": row["best_boss"],
                "best_parse": row["best_parse"],
                "best_hps": row["best_hps"],
                "weeks": len(row["weeks"]),
                # Distinct weeks any toon in their GRM cluster attended, and
                # which of those toons appear in our logs.
                "cluster_weeks": len(cluster_weeks),
                "cluster_toons": sorted(cluster_toons),
                "last_seen": row["last_seen"],
            }
        )
    # Overall DPS desc, toons without one after (by best single-boss DPS).
    out.sort(
        key=lambda r: (
            -(r["overall"]["dps"] if r["overall"] else -1),
            -(r["best_boss"]["dps"] if r["best_boss"] else -1),
        )
    )
    return {
        "players": out,
        "raids_swept": len(se_raids) - failed,
        "raids_failed": failed,
        "zone": settings.PARSE_ZONE_NAME,
    }


def get_leaderboard(force=False):
    """Cached guild standings. Returns (result, cache_meta)."""
    key = f"leaderboard:{settings.GUILD_ID}:{settings.PARSE_ZONE_ID}"
    return apicache.get_or_set(key, _aggregate_leaderboard, force=force)


def get_report_card(code, force=False):
    """After-action card for one guild report: fights in order plus per-role
    player matrices — DPS (+parse) cells for everyone, HPS folded in for
    healers. Returns (card, [metas]); (None, None) for unknown codes so the
    endpoint can't be used to probe arbitrary reports."""
    import datetime as dt

    raids, _ = _cached_all_raids()
    raid = next((r for r in raids if r["code"] == code), None)
    if raid is None:
        return None, None

    dps_rankings, m1 = get_report_rankings(code, "dps", force=force)
    hps_rankings, m2 = get_report_rankings(code, "hps", force=force)

    bosses, overall_fight = [], None
    roles = {"tanks": {}, "healers": {}, "dps": {}}

    def player(bucket, c):
        key = _norm(c.get("name") or "")
        if not key:
            return None
        p = bucket.get(key)
        if p is None:
            p = bucket[key] = {
                "name": c.get("name"),
                "class": c.get("class"),
                "spec": _spec_display(c.get("spec"), None),
                "cells": {},
            }
        return p

    for fight in dps_rankings.get("data") or []:
        fid = fight.get("fightID")
        if fid == _COMPLETE_RAID_FIGHT_ID:
            entry = {"id": fid, "name": "Overall", "size": fight.get("size")}
            overall_fight = entry
        else:
            entry = {
                "id": fid,
                "name": (fight.get("encounter") or {}).get("name") or f"Fight {fid}",
                "size": fight.get("size"),
            }
            bosses.append(entry)
        for role, block in (fight.get("roles") or {}).items():
            bucket = roles.setdefault(role, {})
            for c in block.get("characters") or []:
                p = player(bucket, c)
                if p is None:
                    continue
                p["cells"][str(fid)] = {
                    "dps": c.get("amount"),
                    "dps_percent": c.get("rankPercent"),
                    "spec": _spec_display(c.get("spec"), None),
                }

    for fight in hps_rankings.get("data") or []:
        fid = fight.get("fightID")
        block = (fight.get("roles") or {}).get("healers") or {}
        bucket = roles.setdefault("healers", {})
        for c in block.get("characters") or []:
            p = player(bucket, c)
            if p is None:
                continue
            cell = p["cells"].setdefault(str(fid), {})
            cell["hps"] = c.get("amount")
            cell["hps_percent"] = c.get("rankPercent")

    fights = bosses + ([overall_fight] if overall_fight else [])
    overall_key = str(_COMPLETE_RAID_FIGHT_ID)

    def by_overall(metric):
        def key(p):
            cell = p["cells"].get(overall_key) or {}
            return -(cell.get(metric) or 0)
        return key

    card = {
        "code": code,
        "zone": raid["zone"],
        "date": dt.datetime.fromtimestamp(
            raid["start"] / 1000, dt.timezone.utc
        ).strftime("%Y-%m-%d"),
        "fights": fights,
        "tanks": sorted(roles.get("tanks", {}).values(), key=by_overall("dps")),
        "healers": sorted(roles.get("healers", {}).values(), key=by_overall("hps")),
        "dps": sorted(roles.get("dps", {}).values(), key=by_overall("dps")),
    }
    return card, [m1, m2]


def get_attendance(names, weeks_window, force=False):
    """Count distinct SoD reset weeks (Wed->Wed) any of `names` raided with us.

    Returns (result, cache_meta). The window is the most recent `weeks_window`
    resets (current lockout plus the previous weeks_window-1).
    """
    import datetime as dt

    wanted = {_norm(n) for n in names if n.strip()}

    all_raids, meta = _cached_all_raids(force=force)

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
