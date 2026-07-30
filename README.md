# SoD Loot Eligibility Checker

A small Django web app for the guild **Carnage** (Warcraft Logs guild ID `811296`,
Wild Growth · EU) that checks loot eligibility against the guild's SoD loot rules,
using the [Warcraft Logs v2 API](https://www.warcraftlogs.com/api/docs).

It answers two questions:

1. **Am I eligible?** — Enter the toon you're enquiring about, any other characters
   you raid on, and the raid (Scarlet Enclave / Naxxramas). The app checks:
   - **75% parse** — does the *enquiring* toon have a Best Perf. Average ≥ 75% in
     Scarlet Enclave? (Checks **both 40- and 20-man** and takes the higher.)
   - **Attendance** — raided in **4+ distinct SoD resets (Wed→Wed) of the last 8**,
     counted across *all* the toons entered.
   - **Enchants** *(rare items)* — every enchantable slot enchanted (shoulders
     excepted). Any missing enchant => **ineligible for rare loot**.
   - **Set bonus** *(warning)* — 6/6 SE tier for a SE raid, or 6 tier pieces
     (Naxx and/or SE) for a Naxx raid. Soft warning only: Chalice tokens can change
     gear between raids, so verify manually.
2. **Does this item need a parse?** — Search a Naxxramas / Scarlet Enclave drop.
   Only the 8 rare items require the parse; everything else is standard
   SR > MS > OS loot.

**Data sources:** Warcraft Logs (parses, attendance) + Blizzard `classic1x` API
(current gear — WCL exposes no gear for SoD). API responses are cached in SQLite
for a few hours (`API_CACHE_SECONDS`, default 3h); the result shows the cache age
and a **↻ Refresh** button to force a live re-fetch.

**Character autosuggest:** the toon input suggests guild characters from a
Guild Roster Manager export at `checker/data/grm.csv` (`ROSTER_FILE` to
override) and prefills the alts box from the export's "Player Alts" column.
Drop a fresh GRM export over the file to refresh — no restart needed.

Full loot rules: <https://docs.google.com/document/d/1JuMnO4QfMjLDNtPWO0-iHN9khzqxWRxpuhktsD8UdXo/edit>

## The rare (parse-gated) items

Abandoned Experiment · Sir Dornel's Didgeridoo · Queensfall · Mirage ·
Infusion of Souls · Putress' Diary · Mason's Fraternity Ring ·
Might of the Scourge · Power of the Scourge (Sapphiron shoulder enchants).

Edit `checker/data/items.json` to maintain the catalogue.

## Run it

```bash
cd sod-loot-checker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # fill in WCL_* and BLIZZARD_* credentials
python manage.py createcachetable   # one-time: creates the SQLite cache table
python manage.py runserver
```

Open <http://127.0.0.1:8000>. SQLite is used only as an API cache (no app models,
no migrations).

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` | — | WCL API client (create at warcraftlogs.com/api/clients) |
| `GUILD_ID` | `811296` | Guild on Warcraft Logs |
| `PARSE_THRESHOLD` | `75` | Minimum best-average % for rare items |
| `WEEKS_REQUIRED` | `4` | Distinct raid weeks required |
| `WEEKS_WINDOW` | `8` | Window (weeks) attendance is counted over |
| `WCL_REALM_SLUG` / `WCL_REGION` | auto | Leave blank to auto-detect from the guild |
| `BLIZZARD_CLIENT_ID` / `BLIZZARD_CLIENT_SECRET` | — | Blizzard API client (develop.battle.net) for current gear |
| `BLIZZARD_NAMESPACE` | `profile-classic1x-eu` | SoD/Era profile namespace |
| `BLIZZARD_REALM_SLUG` | `wild-growth` | Realm slug for armory lookups |
| `API_CACHE_SECONDS` | `10800` | How long API responses are cached (3h) |

## How it works (notes)

- **Auth**: OAuth2 client-credentials for both WCL and Blizzard; tokens cached until expiry.
- **Parses**: checked against **Scarlet Enclave** (WCL zone `2018`, configurable
  via `PARSE_ZONE_ID`). SoD raids run as both **40-man and 20-man**, ranked
  separately — the app queries both sizes and keeps the **higher Best Perf. Average**.
  Supports a WCL **spec** filter + **dps/hps** metric (e.g. Holy + dps for a
  Shockadin paladin). Shows per-boss parses in WCL colours, to 2 dp.
- SoD characters on connected realms aren't resolvable by name+realm, so the app
  resolves a toon name → WCL character ID via the guild roster and queries by ID.
- **Attendance**: distinct **SoD resets (Wed→Wed)** in which any entered toon was
  present, within the last `WEEKS_WINDOW` resets.
- **Gear** (Blizzard armory — current, since WCL has no SoD gear):
  - **Set bonus** (hard gate for non-token loot): 6/6 SE tier for a SE raid, or 6
    tier pieces (Naxx and/or SE) for a Naxx raid. Tier sets identified by set ID
    (see `checker/data/tier_sets.json`) with an item-ID-band fallback. Tokens
    (Consecrated/Desecrated) are always SR-able.
  - **Enchants** (hard gate for rare items): every enchantable slot must have a
    `PERMANENT` enchant (shoulders excepted). SoD runes are `TEMPORARY` and ignored.
- **Caching**: WCL + Blizzard responses cached in SQLite (`db.sqlite3`) for
  `API_CACHE_SECONDS`. Results show the cache age; **↻ Refresh** forces a live fetch.
- Loot council may still apply case by case — this tool checks the hard gates only.

## Deploy

Set `DJANGO_DEBUG=0`, a real `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS`, then
serve with gunicorn: `gunicorn sodloot.wsgi`. Keep `.env` out of version control.
