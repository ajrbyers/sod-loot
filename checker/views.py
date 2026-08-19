"""Views for the SoD loot-eligibility checker."""
import functools
import hmac
import json

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import apicache, blizzard, comp, gear, items, raidhelper, roster, softres, wcl
from .models import AtieshHolder, RaidComp, SoftresAudit, ToonLink


# ---------------------------------------------------------------------------
# Simple shared-password gate for the standings pages (no accounts/sessions —
# a signed cookie, like the rest of this tool's lightweight approach).
# ---------------------------------------------------------------------------
PW_COOKIE = "sodloot_access"
_PW_SALT = "sodloot.page-password"
_PW_MAX_AGE = 30 * 24 * 3600  # re-prompt after a month


def has_page_access(request):
    try:
        token = request.COOKIES.get(PW_COOKIE, "")
        return signing.loads(token, salt=_PW_SALT, max_age=_PW_MAX_AGE) == "ok"
    except signing.BadSignature:
        return False


def password_protected(view):
    """Gate a page behind settings.PAGE_PASSWORD.

    A correct submission sets the signed cookie and redirects back to the
    page; anything else gets the password form."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if has_page_access(request):
            return view(request, *args, **kwargs)
        error = None
        if request.method == "POST":
            supplied = request.POST.get("password", "")
            if hmac.compare_digest(supplied, settings.PAGE_PASSWORD):
                response = redirect(request.path)
                response.set_cookie(
                    PW_COOKIE,
                    signing.dumps("ok", salt=_PW_SALT),
                    max_age=_PW_MAX_AGE,
                    httponly=True,
                    samesite="Lax",
                    secure=request.is_secure(),
                )
                return response
            error = "Wrong password."
        response = render(
            request,
            "checker/password.html",
            {"error": error},
            status=403 if error else 200,
        )
        response["Cache-Control"] = "no-store, must-revalidate"
        return response

    return wrapper


def require_page_access(view):
    """JSON-flavoured guard for the gated pages' API endpoints."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not has_page_access(request):
            return JsonResponse({"error": "Password required."}, status=403)
        return view(request, *args, **kwargs)

    return wrapper


def index(request):
    response = render(
        request,
        "checker/index.html",
        {
            "guild_id": settings.GUILD_ID,
            "parse_zone_name": settings.PARSE_ZONE_NAME,
            "parse_threshold": settings.PARSE_THRESHOLD,
            "weeks_required": settings.WEEKS_REQUIRED,
            "weeks_window": settings.WEEKS_WINDOW,
            "rare_items": items.all_items(),
        },
    )
    # Prevent the browser caching a stale page/JS during iteration.
    response["Cache-Control"] = "no-store, must-revalidate"
    return response


@require_POST
def eligibility(request):
    """Check parse + attendance for a person and the toon they're enquiring about.

    Expects JSON: {"main": "<enquiring toon>", "toons": ["alt1", "alt2", ...]}
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    enquiring = (payload.get("main") or "").strip()
    toons = [t.strip() for t in (payload.get("toons") or []) if t.strip()]
    raid = (payload.get("raid") or "se").strip().lower()
    if raid not in ("se", "naxx"):
        raid = "se"
    metric = (payload.get("metric") or "dps").strip().lower()
    if metric not in ("dps", "hps"):
        metric = "dps"
    spec = (payload.get("spec") or "").strip() or None
    force = bool(payload.get("force"))
    if not enquiring:
        return JsonResponse(
            {"error": "Tell us which toon you're enquiring about."}, status=400
        )

    # The person may raid across several characters — count attendance over all
    # of them (the enquiring toon is always included).
    all_toons = list(dict.fromkeys([enquiring, *toons]))

    threshold = settings.PARSE_THRESHOLD
    weeks_required = settings.WEEKS_REQUIRED
    weeks_window = settings.WEEKS_WINDOW

    try:
        realm, region, guild_name = wcl.get_guild_server()
        parse, parse_meta = wcl.get_best_parse(
            enquiring, realm, region, metric=metric, spec=spec, force=force
        )
        attendance, attend_meta = wcl.get_attendance(all_toons, weeks_window, force=force)
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    # Remember that these toons belong together so future checks (by any
    # officer, starting from any member of the cluster) prefill the rest.
    # Only when alts were provided: a bare main submitted without picking the
    # suggestion must not erase a learned link. The submitted cluster replaces
    # any cluster it overlaps with (latest wins, so corrections self-apply).
    # The SR audit passes remember=false — its clusters come from us in the
    # first place, so writing them back would only churn the table.
    if toons and payload.get("remember", True):
        keys = [roster.fold(t) for t in all_toons]
        stale = [
            link.pk
            for link in ToonLink.objects.all()
            if set(link.keys) & set(keys)
        ]
        ToonLink.objects.filter(pk__in=stale).delete()
        ToonLink.objects.create(members=all_toons, keys=keys)

    best = parse.get("best_average")
    parse_ok = best is not None and best >= threshold
    attendance_ok = attendance["distinct_weeks"] >= weeks_required

    # Gear (current, from Blizzard armory) — set-bonus + enchant WARNINGS. Never
    # blocks the response: if Blizzard isn't configured or the toon isn't on the
    # armory, we degrade gracefully to "couldn't check".
    gear_block = {"available": False, "raid": raid}
    gear_meta = None
    try:
        analysis, gear_meta = gear.analyse_gear(enquiring, realm, force=force)
        if analysis.get("found"):
            setv = gear.evaluate(raid, analysis)
            gear_block = {
                "available": True,
                "raid": raid,
                "se_pieces": analysis["se_pieces"],
                "naxx_pieces": analysis["naxx_pieces"],
                "tier_sets": analysis["tier_sets"],
                "set_bonus": setv,
                "fully_enchanted": analysis["fully_enchanted"],
                "missing_enchants": analysis["missing_enchants"],
                "items": analysis["items"],
            }
        else:
            gear_block["reason"] = (
                "Not found on the armory (Blizzard has no recent login for this "
                "character). Gear can't be verified."
            )
    except blizzard.BlizzardError as exc:
        gear_block["reason"] = str(exc)

    # Gear gates. Both hard-block when gear IS available; when it can't be verified
    # (armory not found) we don't block, but flag it as unverified.
    #   * enchants  -> required for RARE items.
    #   * set bonus -> required to SR any NON-TOKEN item (rare or standard).
    #                  Tokens ("Consecrated"/"Desecrated") are always SR-able.
    if gear_block.get("available"):
        enchant_ok = gear_block.get("fully_enchanted", False)
        set_ok = gear_block.get("set_bonus", {}).get("meets", False)
    else:
        enchant_ok = True
        set_ok = True

    result = {
        "raid": raid,
        "enquiring_toon": enquiring,
        "toons_checked": all_toons,
        "guild": {"id": settings.GUILD_ID, "name": guild_name, "realm": realm, "region": region},
        "parse": {
            "found": parse.get("found"),
            "best_average": best,
            "size": parse.get("size"),
            "sizes": parse.get("sizes", {}),
            "top_parse": parse.get("top_parse"),
            "threshold": threshold,
            "passed": parse_ok,
            "zone": settings.PARSE_ZONE_NAME,
            "metric": metric,
            "spec": spec,
            "rankings": parse.get("rankings", []),
        },
        "attendance": {
            "distinct_weeks": attendance["distinct_weeks"],
            "required": weeks_required,
            "window_weeks": weeks_window,
            "window_start": attendance.get("window_start"),
            "passed": attendance_ok,
            "weeks": attendance["weeks"],
        },
        "gear": gear_block,
        # Enchants are a HARD gate for rare items: any missing enchant on an
        # enchantable slot => ineligible for rare loot. Unknown (armory not
        # available) doesn't block, but is flagged as unverified.
        "enchants": {
            "verified": gear_block.get("available", False),
            "fully_enchanted": gear_block.get("fully_enchanted"),
            "missing": gear_block.get("missing_enchants", []),
        },
        "set_bonus_ok": set_ok,
        # Tokens (Consecrated/Desecrated) are SR-able by anyone, no checks.
        "token_eligible": True,
        # Standard NON-TOKEN items need the tier set bonus only.
        "standard_item_eligible": set_ok,
        # RARE (75%+) items need parse + attendance + fully enchanted + set bonus.
        "rare_item_eligible": parse_ok and attendance_ok and enchant_ok and set_ok,
        "cache": apicache.summarise([parse_meta, attend_meta, gear_meta]),
    }
    return JsonResponse(result)


@require_POST
def top_dps(request):
    """Highest raid-wide DPS for one toon, across every spec of their class and
    both raid sizes. The frontend fans a pasted list out as one request per name
    so results stream in and each toon caches independently.

    Expects JSON: {"name": "<toon>", "raid": "se"|"naxx", "force": bool}
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    name = (payload.get("name") or "").strip()
    force = bool(payload.get("force"))
    raid = (payload.get("raid") or "se").strip().lower()
    zone = settings.DPS_ZONES.get(raid) or settings.DPS_ZONES["se"]
    if not name:
        return JsonResponse({"error": "Provide a character name."}, status=400)

    try:
        result, meta = wcl.get_top_dps(name, zone_id=zone["id"], force=force)
        # Whole-raid ("complete raid") DPS comes from the guild's own logs and
        # is cached guild-wide, so it's composed here rather than per toon.
        overall, overall_meta = wcl.get_complete_raid_best(
            result.get("name") or name,
            zone_id=zone["id"],
            zone_name=zone["name"],
            force=force,
        )
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse(
        {
            "query": name,
            **result,
            "overall": overall,
            "zone": zone["name"],
            "cache": apicache.summarise([meta, overall_meta]),
        }
    )


@password_protected
def leaderboard(request):
    response = render(
        request,
        "checker/leaderboard.html",
        {
            "guild_id": settings.GUILD_ID,
            "parse_zone_name": settings.PARSE_ZONE_NAME,
            "weeks_window": settings.WEEKS_WINDOW,
        },
    )
    response["Cache-Control"] = "no-store, must-revalidate"
    return response


@password_protected
def reports(request):
    response = render(
        request,
        "checker/reports.html",
        {
            "guild_id": settings.GUILD_ID,
            "report_zones": settings.REPORT_CARD_ZONES,
        },
    )
    response["Cache-Control"] = "no-store, must-revalidate"
    return response


@require_GET
@require_page_access
def api_leaderboard(request):
    """Guild standings, aggregated from the guild's own SE report rankings."""
    force = request.GET.get("force") == "1"
    try:
        result, meta = wcl.get_leaderboard(force=force)
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({**result, "cache": apicache.summarise([meta])})


@require_GET
@require_page_access
def api_report_card(request):
    """After-action card for one report (?code=...), defaulting to the newest.

    Also returns the SE/Naxx report list so one fetch fills the picker."""
    force = request.GET.get("force") == "1"
    code = (request.GET.get("code") or "").strip()
    try:
        raids, _ = wcl._cached_all_raids(force=force)
        listing = [
            {
                "code": r["code"],
                "zone": r["zone"],
                "date": wcl.raid_date(r),
            }
            for r in raids
            if r.get("zone") in settings.REPORT_CARD_ZONES
        ]
        if not listing:
            return JsonResponse({"error": "No reports found."}, status=404)
        if not code:
            code = listing[0]["code"]
        card, metas = wcl.get_report_card(code, force=force)
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    if card is None:
        return JsonResponse({"error": "Unknown report code."}, status=404)
    return JsonResponse(
        {
            "reports": listing,
            "card": card,
            "cache": apicache.summarise(metas or []),
        }
    )


def known_alts(name):
    """A player's other toons, for attendance counted across the player.

    GRM roster rows win (guildies); remembered ToonLink clusters fill in
    non-guildies — the same precedence the autosuggest uses."""
    key = roster.fold(name)
    if not key:
        return []
    for character in roster.characters():
        if roster.fold(character["name"]) == key:
            return [a for a in character["alts"] if roster.fold(a) != key]
    for link in ToonLink.objects.all():
        if key in link.keys:
            return [m for i, m in enumerate(link.members) if link.keys[i] != key]
    return []


@password_protected
def softres_audit(request):
    response = render(
        request,
        "checker/softres.html",
        {
            "guild_id": settings.GUILD_ID,
            "parse_zone_name": settings.PARSE_ZONE_NAME,
            "parse_threshold": settings.PARSE_THRESHOLD,
            "weeks_required": settings.WEEKS_REQUIRED,
            "weeks_window": settings.WEEKS_WINDOW,
            "recent_audits": SoftresAudit.objects.all()[:10],
        },
    )
    response["Cache-Control"] = "no-store, must-revalidate"
    return response


@require_GET
@require_page_access
def api_softres(request):
    """Fetch a softres.it raid sheet and prepare it for auditing: resolved
    item names, rule classification per item, and each reserver's known alts.

    Per-toon verdicts come from the existing /api/eligibility endpoint — the
    page fans out one call per reserve so the rules live in exactly one place."""
    raid_ref = (request.GET.get("raid") or "").strip()
    force = request.GET.get("force") == "1"
    raid_id = softres.parse_raid_id(raid_ref)
    if not raid_id:
        return JsonResponse(
            {"error": "That doesn't look like a softres.it raid URL or ID."},
            status=400,
        )
    try:
        raid, meta = softres.get_raid(raid_id, force=force)
    except softres.SoftresError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    instances = raid.get("instances") or []
    slugs = " ".join(i.get("slug") or "" for i in instances)
    raid_type = "naxx" if "naxx" in slugs else "se"

    # Remember the sheet so the page can offer recent audits without the RL
    # hunting the link down again. Re-audits just refresh the entry.
    SoftresAudit.objects.update_or_create(
        raid_id=raid.get("id") or raid_id,
        defaults={
            "instance": (instances[0].get("name") if instances else "") or "",
            "raid_date": raid.get("raid_date"),
            "reserve_count": len(raid.get("reserves") or []),
        },
    )

    # Contested = the same item soft-reserved by 2+ different people. Stacking
    # an item ×3 yourself doesn't contest it. Informational only — it shows
    # the RL who's rolling against whom; the loot rules key off item type.
    holders = {}
    for r in raid.get("reserves") or []:
        for item_id in set(r.get("items") or []):
            holders[item_id] = holders.get(item_id, 0) + 1

    reserves = []
    for r in raid.get("reserves") or []:
        entries = []
        for item_id in r.get("items") or []:
            name = softres.item_name(item_id)
            entries.append(
                {
                    "id": item_id,
                    "name": name,
                    "type": items.classify(name),
                    "contested": holders.get(item_id, 0) > 1,
                }
            )
        spec = r.get("spec")
        reserves.append(
            {
                "name": r.get("name"),
                "spec": spec,
                "healer": spec in softres.HEALER_SPECS,
                "discord": (r.get("user") or {}).get("name"),
                "note": r.get("note"),
                "alts": known_alts(r.get("name") or ""),
                "items": entries,
            }
        )

    return JsonResponse(
        {
            "raid": {
                "id": raid.get("id"),
                "instance": instances[0].get("name") if instances else None,
                "raid_type": raid_type,
                "faction": raid.get("faction"),
                "date": raid.get("raid_date"),
                "locked": raid.get("locked"),
                "reserve_limit": raid.get("reserve_limit"),
                "creator": (raid.get("creator") or {}).get("name"),
            },
            "reserves": reserves,
            "cache": apicache.summarise([meta]),
        }
    )


# ---------------------------------------------------------------------------
# Comp builder
# ---------------------------------------------------------------------------
@password_protected
def comp_builder(request):
    """The comp builder. ?event=<link or id> deep-links straight to a comp."""
    response = render(
        request,
        "checker/comp.html",
        {
            "guild_id": settings.GUILD_ID,
            "recent_comps": RaidComp.objects.all()[:8],
            # Loaded on open, so a saved comp can just be linked to.
            "event_ref": raidhelper.parse_event_id(request.GET.get("event", "")) or "",
        },
    )
    response["Cache-Control"] = "no-store, must-revalidate"
    return response


def _stored_atiesh():
    """{folded name: version} for everyone we've ever checked or been told."""
    return {h.key: h.version for h in AtieshHolder.objects.all()}


def _apply_overrides(players, overrides):
    """Re-apply the raid lead's manual bucket/Atiesh decisions after a refetch."""
    for player in players:
        override = (overrides or {}).get(player["name"]) or {}
        if override.get("bucket"):
            comp.set_bucket(player, override["bucket"])
        if "atiesh" in override:
            player["atiesh"] = override["atiesh"] or None
    return players


@require_GET
@require_page_access
def api_comp(request):
    """Build (or restore) a comp for a Raid-Helper event.

    Raid-Helper supplies who's coming and the role they signed as; its linked
    softres sheet supplies real character names (joined on Discord id) and the
    raid size. Everything else — Atiesh, Warcraft Logs suggestions — is layered
    on by the client so this stays a fast single request.
    """
    ref = (request.GET.get("event") or "").strip()
    force = request.GET.get("force") == "1"
    event_id = raidhelper.parse_event_id(ref)
    if not event_id:
        return JsonResponse(
            {"error": "That doesn't look like a Raid-Helper event link or ID."},
            status=400,
        )
    try:
        event, meta = raidhelper.get_event(event_id, force=force)
    except raidhelper.RaidHelperError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    # The event names its softres sheet, so the raid lead only pastes one link.
    reserves, raid_slots, sr_id = [], 40, (event.get("softres") or "").strip()
    metas = [meta]
    if sr_id:
        try:
            sheet, sr_meta = softres.get_raid(sr_id, force=force)
            reserves = sheet.get("reserves") or []
            instances = sheet.get("instances") or []
            if instances:
                raid_slots = instances[0].get("slots") or 40
            metas.append(sr_meta)
        except softres.SoftresError:
            sr_id = ""  # the sheet is optional; names just stay as nicknames

    players, excluded = comp.roster_from_event(event, reserves, roster.characters())

    stored = RaidComp.objects.filter(raid_id=event_id).first()
    overrides = stored.overrides if stored else {}
    pins = stored.pins if stored else {}
    _apply_overrides(players, overrides)

    # Atiesh we already know about (armory scan or a manual correction).
    known = _stored_atiesh()
    for player in players:
        if player["atiesh"] is None:
            player["atiesh"] = known.get(roster.fold(player["name"])) or None

    group_count = comp.default_group_count(len(players), raid_slots)
    if stored and stored.groups:
        group_count = max(group_count, len(stored.groups))
    stack_tanks = bool(stored.stack_tanks) if stored else False
    # A saved comp is restored as arranged. Signups shift after a save, so
    # anyone new is placed by the rules around the people already seated.
    layout = comp.layout_positions(stored.groups) if stored else {}
    result = comp.build_comp(
        players, group_count, pins=pins, stack_tanks=stack_tanks, layout=layout
    )

    return JsonResponse(
        {
            "event": {
                "id": event_id,
                "title": event.get("title") or event.get("displayTitle"),
                "date": event.get("unixtime"),
                "leader": event.get("leadername"),
                "softres": sr_id or None,
                "raid_slots": raid_slots,
            },
            "group_count": group_count,
            "groups": result["groups"],
            "bench": result["bench"],
            "warnings": result["warnings"],
            "excluded": excluded,
            "explain": result.get("explain"),
            # Handed back so re-opening a saved comp restores the raid lead's
            # decisions in the UI, not just in this one build.
            "pins": pins,
            "overrides": overrides,
            "stack_tanks": stack_tanks,
            "saved": bool(stored),
            "cache": apicache.summarise(metas),
        }
    )


def _players_from_payload(payload):
    """Rebuild player records from what the page posts back.

    Everything is re-derived from (spec, role) server-side so the rules live in
    one place; only the raid lead's explicit decisions are taken on trust.
    """
    players = []
    for raw in payload.get("players") or []:
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        player = comp.make_player(
            name,
            spec=raw.get("raw_spec"),
            role=raw.get("role"),
            signup_name=raw.get("signup_name") or name,
            discord_id=raw.get("discord_id"),
            status=raw.get("status") or "primary",
        )
        if raw.get("bucket"):
            comp.set_bucket(player, raw["bucket"])
        player["atiesh"] = raw.get("atiesh") or None
        parse = raw.get("parse")
        player["parse"] = float(parse) if isinstance(parse, (int, float)) else None
        dps = raw.get("dps")
        player["dps"] = float(dps) if isinstance(dps, (int, float)) else None
        players.append(player)
    return players


@require_POST
@require_page_access
def api_comp_build(request):
    """Re-run the placement rules over an edited roster."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    players = _players_from_payload(payload)
    if not players:
        return JsonResponse({"error": "No players to place."}, status=400)

    try:
        group_count = int(payload.get("group_count") or 0)
    except (TypeError, ValueError):
        group_count = 0
    if not 1 <= group_count <= 8:
        group_count = comp.default_group_count(len(players))

    pins = {
        str(k): int(v)
        for k, v in (payload.get("pins") or {}).items()
        if str(v).lstrip("-").isdigit()
    }
    stack_tanks = bool(payload.get("stack_tanks"))
    result = comp.build_comp(
        players, group_count, pins=pins, stack_tanks=stack_tanks
    )
    return JsonResponse(
        {
            "group_count": group_count,
            "stack_tanks": stack_tanks,
            "groups": result["groups"],
            "bench": result["bench"],
            "warnings": result["warnings"],
            "explain": result.get("explain"),
        }
    )


@require_POST
@require_page_access
def api_comp_warnings(request):
    """Re-check a hand-arranged layout against the same rules an auto-build uses."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    return JsonResponse(
        {
            "warnings": comp.evaluate_layout(
                payload.get("groups") or [],
                payload.get("bench") or [],
                stack_tanks=bool(payload.get("stack_tanks")),
            )
        }
    )


@require_POST
@require_page_access
def api_comp_save(request):
    """Persist the comp as the raid lead arranged it."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    event_id = raidhelper.parse_event_id(payload.get("event_id") or "")
    if not event_id:
        return JsonResponse({"error": "Unknown event."}, status=400)

    RaidComp.objects.update_or_create(
        raid_id=event_id,
        defaults={
            "instance": (payload.get("title") or "")[:64],
            "groups": payload.get("groups") or [],
            "bench": payload.get("bench") or [],
            "overrides": payload.get("overrides") or {},
            "pins": payload.get("pins") or {},
            "stack_tanks": bool(payload.get("stack_tanks")),
        },
    )
    return JsonResponse({"saved": True})


@require_GET
@require_page_access
def api_atiesh(request):
    """Does this character have an Atiesh? Armory scan, cached and remembered.

    A manual entry always wins: the raid lead sets those precisely because the
    armory couldn't see the character.
    """
    name = (request.GET.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Provide a character name."}, status=400)

    key = roster.fold(name)
    existing = AtieshHolder.objects.filter(key=key).first()
    if existing and existing.source == AtieshHolder.MANUAL:
        return JsonResponse(
            {"name": name, "version": existing.version or None, "source": "manual"}
        )

    try:
        realm, _region, _guild = wcl.get_guild_server()
    except wcl.WCLError:
        realm = None
    try:
        analysis, _meta = gear.analyse_gear(name, realm)
    except blizzard.BlizzardError as exc:
        return JsonResponse({"name": name, "version": None, "error": str(exc)})

    version = comp.atiesh_from_gear(analysis)
    if version is None:
        # Armory couldn't see them; don't record a miss we aren't sure of.
        return JsonResponse({"name": name, "version": None, "source": "unknown"})

    # Store Blizzard's spelling, not whatever casing the lookup happened to use.
    canonical = analysis.get("name") or name
    AtieshHolder.objects.update_or_create(
        key=key,
        defaults={"name": canonical, "version": version, "source": AtieshHolder.ARMORY},
    )
    return JsonResponse(
        {"name": canonical, "version": version or None, "source": "armory"}
    )


@require_POST
@require_page_access
def api_atiesh_set(request):
    """Raid lead correcting the Atiesh scan by hand."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    name = (payload.get("name") or "").strip()
    version = (payload.get("version") or "").strip()
    if not name:
        return JsonResponse({"error": "Provide a character name."}, status=400)
    if version and version not in comp.ATIESH_ITEMS.values():
        return JsonResponse({"error": "Unknown Atiesh version."}, status=400)

    AtieshHolder.objects.update_or_create(
        key=roster.fold(name),
        defaults={"name": name, "version": version, "source": AtieshHolder.MANUAL},
    )
    return JsonResponse({"name": name, "version": version or None, "source": "manual"})


@require_GET
@require_page_access
def api_comp_rules(request):
    """The rules the builder applies, generated from the tables it reads."""
    return JsonResponse({"sections": comp.rules_summary()})


@require_GET
@require_page_access
def api_comp_suggestions(request):
    """What Warcraft Logs thinks people actually play, as advisory chips.

    Kept off the main build request because the first call sweeps every guild
    report; the page renders without it and fills the chips in when it lands.
    """
    names = [n.strip() for n in (request.GET.get("names") or "").split(",") if n.strip()]
    if not names:
        return JsonResponse({"suggestions": {}})
    try:
        board, meta = wcl.get_leaderboard()
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    rows = board.get("players") or []
    players = [comp.make_player(n) for n in names]
    comp.suggestions_from_logs(players, rows)
    return JsonResponse(
        {
            "suggestions": {
                p["name"]: p["suggestion"] for p in players if p["suggestion"]
            },
            # Parses ride along on the same cached sweep: they decide who gets
            # the best-buffed seats when the page rebuilds.
            "parses": comp.parses_from_logs(names, rows),
            "cache": apicache.summarise([meta]),
        }
    )


@require_GET
def item_search(request):
    return JsonResponse(items.search(request.GET.get("q", "")))


@require_GET
def character_search(request):
    """Autosuggest for guild characters; each match carries the player's alts
    so the frontend can prefill the toons box without a second request.

    GRM roster matches come first; remembered links (ToonLink) fill in
    non-guildies the roster doesn't know about. Any member of a remembered
    cluster matches, with the rest of the cluster offered as their alts."""
    q = (request.GET.get("q", "") or "").strip()
    matches = roster.search(q)
    if q:
        fq = roster.fold(q)
        seen = {roster.fold(m["name"]) for m in matches}
        prefix, contains = [], []
        for link in ToonLink.objects.all():
            for i, key in enumerate(link.keys):
                if key in seen or fq not in key:
                    continue
                entry = {
                    "name": link.members[i],
                    "level": "",
                    "class": "",
                    "main_or_alt": "",
                    "alts": [m for j, m in enumerate(link.members) if j != i],
                    "remembered": True,
                }
                (prefix if key.startswith(fq) else contains).append(entry)
                seen.add(key)
        matches.extend(prefix + contains)
    return JsonResponse({"matches": matches[:10]})
