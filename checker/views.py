"""Views for the SoD loot-eligibility checker."""
import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import apicache, blizzard, gear, items, wcl


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


@require_GET
def item_search(request):
    return JsonResponse(items.search(request.GET.get("q", "")))
