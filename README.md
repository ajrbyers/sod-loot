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

It also builds the raid comp (`/comp`) — see below.

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

## Comp builder (`/comp`)

Paste a **Raid-Helper event link** and it builds the 5-man groups, then lets the
raid lead drag anything around. Behind the shared password, like the other
officer pages.

**Why Raid-Helper and not softres.** Softres stores a plain retail spec id, and
under it a shockadin and a holy paladin healer are *the same number* (65). It has
no melee/ranged split and no SoD tank option at all. Raid-Helper records the role
somebody actually signed as, and the (spec, role) pair is what makes SoD legible:

| Signed as | Read as |
|---|---|
| `Holy1` + Melee | Shockadin |
| `Holy1` + Healer | Holy paladin healer |
| `Survival` + Melee | Melee hunter (Melee Specialist rune) |
| `Combat` + Tank | SoD rogue tank (Just a Flesh Wound) |
| `Demonology` + Tank | SoD warlock tank (Metamorphosis) |
| `Arcane` + Healer | SoD healer mage |

The event names its own softres sheet, so one link gets both. Signups and
reserves each carry the signer's **Discord id**, so joining on it turns
Raid-Helper's nicknames (`<OC>Kalu|Lipis|Idhunn`) into the real character names
the rest of the app looks up. Where there's no reserve, the nickname is picked
apart and matched against the GRM roster, preferring the toon whose class
matches what they signed as. `Absence`/`Bench` signups never take a seat.

**The rules.** Nearly every buff that matters for grouping is *party*-scoped and
**does not stack**, so each group is seeded with its aura carriers before any
seats are filled:

- **Melee groups** — tanks first (a paladin tank gets a Retribution paladin for
  the aura; rogue/druid/warrior tanks anchor a melee group). Then one ret
  paladin per group (Sanctity), then warriors spread for the shout.
- **Warlock tanks** — placed *after* the boomkins, not with the other tanks.
  They want a boomkin's group for the spell crit, else a warrior for shout HP;
  run the rule before the boomkins are seated and it can never see one.
- **Ferals** — in with a tank where there's a seat: Leader of the Pack is melee
  crit and the tanks want it. A bear tank already carries LotP himself, so the
  non-stacking check sends ferals to a tank who doesn't have it.
- **Caster groups** — a boomkin per group (Moonkin aura), then Atiesh holders
  spread by version, then a shadow priest per group.
- **Shadow priests** — Vampiric Touch returns party mana only, so they spread
  one per group. Once there are **two or more**, one is reserved for the healer
  group before the casters get a second: healers are the ones who actually run
  dry. A lone shadow priest stays with the casters.
- **Healer groups** — the same aura logic at lower priority; the priest Atiesh
  prefers it (it buffs healing), and a spare boomkin lands here rather than
  doubling up in a caster group.
- **Paladin healers** — sat with the **casters**. They never really run out of
  mana, so they free a healer-group seat for someone who does (and for the
  shadow priest's VT to land on). A `Holy1` who signed *melee* is still a
  shockadin and is unaffected.
- **Shockadins** — boomkin's group > ret paladin's group > any free seat.
- **Ranged hunters** — the filler pool; they gain nothing from the caster auras,
  so they take the leftover seats.
- **Stack tanks in group 1** — optional toggle. Off, tanks anchor a melee group
  each (and the warlock tank still prefers the boomkin's caster group); on, they
  all go to group 1 and everyone else re-flows around them. Saved with the comp.

Group count defaults to as few full parties as the signups fill, capped by the
raid size from the softres sheet; change it in the UI.

**Who gets the good seats.** Every pass that hands out a limited buffed seat —
aura carriers, Atiesh, ret paladins, and the final fill — goes **best parse
first**, so a crit or damage aura lands on whoever converts it. The metric is
the **whole-raid percentile** from the guild standings, not best-boss: in a
clearing guild nearly everyone has a single 99 somewhere, so `best_parse` ranks
no one. Percentiles still bunch at the top, so raid DPS breaks ties (only ever
compared within the same bucket). Archetype always outranks parse — a 99% mage
is never dragged into a melee group. Players absent from our logs are seated
after those with a standing, not below them.

Parses arrive after the page renders (the first sweep of the guild's reports is
slow, then cached), so the page re-arranges itself once they land — unless
you've already started dragging people, since those edits are the point.

**📖 Rules** opens a modal listing all of the above, generated from the same
tables the placement code reads, so it can't drift from what the builder does.
**🔍 Why this comp?** explains the comp in front of you from a trace recorded
*while the rules ran* — the group budget, then every placement with its reason.
Players you moved by hand say so rather than getting invented justifications.

**Atiesh** (item IDs `236398`–`236401`) is read from the Blizzard armory,
scanned in the background so the first build doesn't wait on ~30 lookups, and
remembered. Where Blizzard can't see a character, set it by hand — a manual
entry always beats the scan. The four auras are party-only and don't stack, so
no group is ever given two of the same version:

| Version | Party aura (30 yd) |
|---|---|
| Mage | +2% spell haste |
| Druid | +2% spell crit |
| Priest | +62 healing / +19 damage |
| Warlock | +33 spell damage & healing |

**Editing.** Drag between groups or onto the bench (tap-select then tap-group on
touch). Dragging pins someone, so **↻ Auto-arrange** keeps your decisions and
re-places everyone else. 📌 toggles a pin, *Edit selected* overrides role and
Atiesh, and **Save comp** persists the layout, overrides and pins against the
event id so re-opening the link resumes where you left off. *Copy for Discord*
gives a plain-text block.

**Warnings** only fire when you could act on them — six ret paladins across four
melee groups is forced, so it stays quiet, but doubling a boomkin while another
caster group has none does not. Duplicate Atiesh in one group is always flagged.

**Warcraft Logs cross-check.** `wcl.py` already derives SoD specs the signup
can't express (Hunter `Melee`, Rogue/Warlock `Tank`, and `Shockadin` from damage
beating healing). Where it disagrees with a signup it raises a ⚑ chip you click
to accept — it never silently overrules what somebody registered as.

## Deploy

Set `DJANGO_DEBUG=0`, a real `DJANGO_SECRET_KEY`, and `DJANGO_ALLOWED_HOSTS`, then
serve with gunicorn: `gunicorn sodloot.wsgi`. Keep `.env` out of version control.
