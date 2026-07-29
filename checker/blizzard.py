"""Blizzard (Battle.net) API client for CURRENT character equipment.

Warcraft Logs exposes no gear for Season of Discovery, so we read live equipped
gear from Blizzard's classic1x profile API to power the set-bonus warning.

Uses only the standard library (urllib), mirroring checker/wcl.py.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

_token_cache = {"access_token": None, "expires_at": 0}


class BlizzardError(Exception):
    pass


def _token_host():
    # Battle.net OAuth is served per-region; the unified host also works.
    return "https://oauth.battle.net/token"


def _api_host():
    return f"https://{settings.BLIZZARD_REGION}.api.blizzard.com"


def _get_token():
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 60 > now:
        return _token_cache["access_token"]

    cid = settings.BLIZZARD_CLIENT_ID
    secret = settings.BLIZZARD_CLIENT_SECRET
    if not cid or not secret:
        raise BlizzardError(
            "BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET are not configured. "
            "Create a client at https://develop.battle.net and add them to .env."
        )
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        _token_host(),
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise BlizzardError(
            f"Blizzard token error HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BlizzardError(f"Couldn't reach Blizzard OAuth: {exc.reason}") from exc

    token = payload.get("access_token")
    if not token:
        raise BlizzardError(f"No access_token in Blizzard response: {payload}")
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    return token


def _get(path, params=None):
    token = _get_token()
    query = dict(params or {})
    query.setdefault("namespace", settings.BLIZZARD_NAMESPACE)
    query.setdefault("locale", settings.BLIZZARD_LOCALE)
    url = f"{_api_host()}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # character not found / no profile
        raise BlizzardError(
            f"Blizzard API HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BlizzardError(f"Couldn't reach Blizzard API: {exc.reason}") from exc


def get_equipment(character_name, realm_slug=None):
    """Return the raw Blizzard equipment payload for a character (or None)."""
    realm = (realm_slug or settings.BLIZZARD_REALM_SLUG).lower()
    name = urllib.parse.quote(character_name.strip().lower())
    return _get(f"/profile/wow/character/{realm}/{name}/equipment")
