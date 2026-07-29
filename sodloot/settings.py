"""Django settings for the SoD loot-eligibility checker."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """Tiny .env loader so we don't need python-dotenv as a dependency."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "checker",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sodloot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

WSGI_APPLICATION = "sodloot.wsgi.application"

# SQLite is used purely as a cache backend for the (slow, rate-limited) WCL and
# Blizzard API responses — no application models.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "api_cache",
    }
}

# How long API responses (parses, attendance, gear) are cached, in seconds.
API_CACHE_SECONDS = int(os.environ.get("API_CACHE_SECONDS", str(3 * 60 * 60)))

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

USE_TZ = True
TIME_ZONE = "UTC"

# ---------------------------------------------------------------------------
# Warcraft Logs / guild configuration
# ---------------------------------------------------------------------------
WCL_CLIENT_ID = os.environ.get("WCL_CLIENT_ID", "")
WCL_CLIENT_SECRET = os.environ.get("WCL_CLIENT_SECRET", "")

# Our guild on Warcraft Logs.
GUILD_ID = int(os.environ.get("GUILD_ID", "811296"))

# Warcraft Logs zone the parse is checked against. 2018 = Scarlet Enclave (SoD).
PARSE_ZONE_ID = int(os.environ.get("PARSE_ZONE_ID", "2018"))
PARSE_ZONE_NAME = os.environ.get("PARSE_ZONE_NAME", "Scarlet Enclave")

# Eligibility thresholds (see the loot rules doc).
PARSE_THRESHOLD = float(os.environ.get("PARSE_THRESHOLD", "75"))
WEEKS_REQUIRED = int(os.environ.get("WEEKS_REQUIRED", "4"))
WEEKS_WINDOW = int(os.environ.get("WEEKS_WINDOW", "8"))

# SoD raid lockout resets weekly on Wednesday. Attendance is bucketed into these
# Wednesday->Wednesday reset weeks. Monday=0 .. Wednesday=2 .. Sunday=6.
RESET_WEEKDAY = int(os.environ.get("RESET_WEEKDAY", "2"))
# Reset hour in UTC (EU realms reset ~07:00 CET = ~06:00 UTC). Evening raids fall
# well after this, so it only matters for the exact boundary.
RESET_HOUR_UTC = int(os.environ.get("RESET_HOUR_UTC", "6"))

# Optional realm override. If blank, the realm/region is auto-detected from
# the guild's server on Warcraft Logs (recommended for a single-realm guild).
WCL_REALM_SLUG = os.environ.get("WCL_REALM_SLUG", "")
WCL_REGION = os.environ.get("WCL_REGION", "")

# ---------------------------------------------------------------------------
# Blizzard API — used for CURRENT gear (SoD set-bonus warning). WCL exposes no
# gear for Season of Discovery, so this is the only live source.
# Create a client at https://develop.battle.net (grant: client_credentials).
# ---------------------------------------------------------------------------
BLIZZARD_CLIENT_ID = os.environ.get("BLIZZARD_CLIENT_ID", "")
BLIZZARD_CLIENT_SECRET = os.environ.get("BLIZZARD_CLIENT_SECRET", "")
# SoD / Classic Era realms use the classic1x namespaces.
BLIZZARD_REGION = os.environ.get("BLIZZARD_REGION", "eu")
BLIZZARD_NAMESPACE = os.environ.get("BLIZZARD_NAMESPACE", "profile-classic1x-eu")
BLIZZARD_LOCALE = os.environ.get("BLIZZARD_LOCALE", "en_GB")
# Realm slug for character lookups (Blizzard uses hyphenated lowercase).
BLIZZARD_REALM_SLUG = os.environ.get("BLIZZARD_REALM_SLUG", "wild-growth")
