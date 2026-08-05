"""Views for the SoD loot-eligibility checker."""
import functools
import hmac
import json

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import apicache, blizzard, gear, items, roster, wcl
from .models import ToonLink


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
    if toons:
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
        # Tokens (Consecrated/Desecrated) only need attendance.
        "token_eligible": attendance_ok,
        # Standard NON-TOKEN items need attendance + the tier set bonus.
        "standard_item_eligible": attendance_ok and set_ok,
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

    Expects JSON: {"name": "<toon>", "force": bool}
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    name = (payload.get("name") or "").strip()
    force = bool(payload.get("force"))
    if not name:
        return JsonResponse({"error": "Provide a character name."}, status=400)

    try:
        result, meta = wcl.get_top_dps(name, force=force)
        # Whole-raid ("complete raid") DPS comes from the guild's own logs and
        # is cached guild-wide, so it's composed here rather than per toon.
        overall, overall_meta = wcl.get_complete_raid_best(
            result.get("name") or name, force=force
        )
    except wcl.WCLError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    return JsonResponse(
        {
            "query": name,
            **result,
            "overall": overall,
            "zone": settings.PARSE_ZONE_NAME,
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
