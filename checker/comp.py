"""Raid comp builder: softres signups -> 5-man groups that respect party auras.

Almost every buff that matters for grouping in SoD is *party*-scoped and does
not stack with itself: Sanctity Aura, Leader of the Pack, Moonkin Aura, the
shaman totems and all four Atiesh auras. So the builder seeds each group with
the aura carriers it wants *first*, then fills the leftover seats — putting two
boomkins in one party wastes one of them, and no amount of later shuffling
recovers it.

Roles come from Raid-Helper, which records the role somebody actually signed as
(Tank / Melee / Ranged / Healer) alongside their spec. That pair is what makes
the SoD-specific cases expressible at all — softres stores a plain retail spec
id, under which a shockadin and a holy paladin healer are the same number:

    Holy1 + Melee   -> shockadin        Combat     + Tank -> SoD rogue tank
    Holy1 + Healer  -> holy paladin     Demonology + Tank -> SoD warlock tank
    Survival + Melee -> melee hunter    Arcane     + Healer -> SoD healer mage

So the signup is taken at its word: what somebody registered as is what they
are. Warcraft Logs may disagree (checker/wcl.py derives "Shockadin" from damage
beating healing), but that only ever raises a suggestion for the raid lead to
accept — it never silently overrules what somebody signed as.
"""
import math

GROUP_SIZE = 5

# Buckets drive both the group budget and the placement order.
TANK = "tank"
MELEE = "melee"
CASTER = "caster"
HEALER = "healer"
RANGED = "ranged"
SHOCKADIN = "shockadin"

# Aura tags. Every one of these is party-only and non-stacking, which is why
# the builder spreads their carriers one per group before filling seats.
SANCTITY = "sanctity"      # Retribution paladin — +holy damage
LOTP = "lotp"              # Feral druid — +melee crit
MOONKIN = "moonkin"        # Balance druid — +spell crit
SHOUT = "shout"            # Warrior — party HP
TRUESHOT = "trueshot"      # Marksmanship hunter — +AP
SUSTAIN = "sustain"        # Shadow priest — Vampiric Touch, party mana return
HORN = "horn"              # Any paladin — Horn of Lordaeron, +6 Str/Agi to party
WINDFURY = "windfury"      # Enhancement shaman (Horde)
MANASPRING = "manaspring"  # Restoration shaman (Horde)

AURA_LABELS = {
    SANCTITY: "Sanctity Aura",
    LOTP: "Leader of the Pack",
    MOONKIN: "Moonkin Aura",
    SHOUT: "Battle/Commanding Shout",
    TRUESHOT: "Trueshot Aura",
    SUSTAIN: "Vampiric Touch",
    HORN: "Horn of Lordaeron",
    WINDFURY: "Windfury Totem",
    MANASPRING: "Mana Spring Totem",
}

# Raid-Helper `spec` -> WoW class. A spec name shared by two classes carries a
# trailing "1" on the second, which is the only thing distinguishing a priest's
# Holy from a paladin's.
SPEC_CLASS = {
    "Arms": "Warrior", "Fury": "Warrior", "Protection": "Warrior",
    "Retribution": "Paladin", "Holy1": "Paladin", "Protection1": "Paladin",
    "Feral": "Druid", "Guardian": "Druid", "Balance": "Druid",
    "Restoration": "Druid",
    "Assassination": "Rogue", "Combat": "Rogue", "Subtlety": "Rogue",
    "Affliction": "Warlock", "Demonology": "Warlock", "Destruction": "Warlock",
    "Beastmastery": "Hunter", "Marksmanship": "Hunter", "Survival": "Hunter",
    "Arcane": "Mage", "Fire": "Mage", "Frost": "Mage",
    "Discipline": "Priest", "Holy": "Priest", "Shadow": "Priest",
    "Smite": "Priest",
    "Elemental": "Shaman", "Enhancement": "Shaman", "Restoration1": "Shaman",
}

# Display names for the specs Raid-Helper disambiguates with a trailing "1".
SPEC_LABELS = {
    "Holy1": "Holy",
    "Protection1": "Protection",
    "Restoration1": "Restoration",
    "Beastmastery": "Beast Mastery",
}

# Raid-Helper's role column (their JSON calls it "class") -> our bucket. Ranged
# is refined below: a boomkin and a hunter both sign Ranged but want opposite
# things from a group.
ROLE_BUCKETS = {
    "Tank": TANK,
    "Melee": MELEE,
    "Ranged": CASTER,
    "Healer": HEALER,
}

# The party aura each spec carries. Auras belong to the spec, not the role.
SPEC_AURAS = {
    "Retribution": [SANCTITY, HORN],
    "Holy1": [HORN],
    "Protection1": [HORN],
    "Arms": [SHOUT],
    "Fury": [SHOUT],
    "Protection": [SHOUT],
    "Feral": [LOTP],
    "Guardian": [LOTP],
    "Balance": [MOONKIN],
    # Vampiric Touch returns mana to the priest's *party* only, so shadow
    # priests spread one per caster group rather than stacking.
    "Shadow": [SUSTAIN],
    "Marksmanship": [TRUESHOT],
    "Enhancement": [WINDFURY],
    "Restoration1": [MANASPRING],
}

# Which party auras actually do something for each kind of player. Used to rank
# how well-buffed a group is when deciding who gets the good seats.
BUFF_AURAS = {
    TANK: (SANCTITY, LOTP, SHOUT, WINDFURY, HORN),
    MELEE: (SANCTITY, LOTP, SHOUT, WINDFURY, HORN),
    SHOCKADIN: (MOONKIN, SANCTITY, HORN),
    CASTER: (MOONKIN, SUSTAIN, MANASPRING),
    HEALER: (MOONKIN, SUSTAIN, MANASPRING),
    RANGED: (TRUESHOT, SHOUT),
}

# Where a bucket goes when its own groups are full. Casters and healers are
# both spellcasters wanting the same auras, so they share before either is sent
# to the melee, where neither gains anything.
SPILL_COMPATIBLE = {
    TANK: (MELEE,),
    MELEE: (MELEE, RANGED),
    CASTER: (HEALER, CASTER, RANGED),
    HEALER: (CASTER, HEALER, RANGED),
    SHOCKADIN: (CASTER, HEALER, MELEE),
    RANGED: (RANGED, MELEE),
}

# Hunters who sign Ranged are the filler pool: they gain nothing from the
# Atiesh and boomkin auras the caster groups are built around, so they fill
# leftover seats instead of taking one in a caster group.
HUNTER_SPECS = {"Beastmastery", "Marksmanship", "Survival"}

# Combinations that are a SoD-specific reading of an otherwise ordinary spec.
# Surfaced to the raid lead as a note so an unexpected one is obvious.
AMBIGUOUS = {
    ("Holy1", MELEE): "Holy paladin signed as melee — read as a shockadin",
    ("Demonology", TANK): "SoD warlock tank (Metamorphosis rune)",
    ("Destruction", TANK): "SoD warlock tank (Metamorphosis rune)",
    ("Combat", TANK): "SoD rogue tank (Just a Flesh Wound rune)",
    ("Assassination", TANK): "SoD rogue tank (Just a Flesh Wound rune)",
    ("Subtlety", TANK): "SoD rogue tank (Just a Flesh Wound rune)",
    ("Arcane", HEALER): "SoD healer mage",
    ("Frost", HEALER): "SoD healer mage",
    ("Survival", MELEE): "melee hunter (Melee Specialist rune)",
    ("Enhancement", TANK): "SoD shaman tank",
}

# SoD Atiesh. Every aura is party-only and does not stack with itself, so the
# builder never puts two of the same version in one group.
ATIESH_ITEMS = {
    236398: "Warlock",
    236399: "Priest",
    236400: "Mage",
    236401: "Druid",
}
ATIESH_AURAS = {
    "Mage": "+2% spell haste",
    "Druid": "+2% spell crit",
    "Priest": "+62 healing / +19 damage",
    "Warlock": "+33 spell damage & healing",
}

def roster_from_event(event, reserves=None, roster_characters=None):
    """Turn a Raid-Helper event into the builder's player list.

    Raid-Helper knows the role but only has a Discord nickname; softres knows
    the real character name but not the role. Signups carry the signer's
    Discord id and so do softres reserves, so joining on it gets both. Failing
    that, the nickname is picked apart and matched against the GRM roster —
    preferring a character whose class matches what they signed as, which is
    what disambiguates "Healrond/Irv" when only one of them is a druid.
    """
    by_discord = {}
    for reserve in reserves or []:
        discord_id = (reserve.get("user") or {}).get("discord_id")
        if discord_id and reserve.get("name"):
            by_discord[str(discord_id)] = reserve["name"]

    known = {}
    for character in roster_characters or []:
        known[roster_fold(character["name"])] = character

    players, excluded = [], []
    for signup in event.get("signups") or []:
        status = signup.get("class") or ""
        if status in ("Absence", "Bench"):
            excluded.append({"name": signup.get("name"), "status": status})
            continue

        nickname = signup.get("name") or ""
        discord_id = str(signup.get("userid") or "")
        spec = signup.get("spec")
        role = signup.get("class")

        name, resolved = by_discord.get(discord_id), bool(by_discord.get(discord_id))
        if not name:
            name = _match_nickname(nickname, spec, known)
            resolved = name is not None
        player = make_player(
            name or nickname,
            spec=spec,
            role=role,
            signup_name=nickname,
            discord_id=discord_id or None,
            status="late" if status in ("Late", "Tentative") else "primary",
            name_resolved=resolved,
        )
        players.append(player)
    return players, excluded


def roster_fold(name):
    """Accent/case-insensitive key. Mirrors checker.roster.fold, kept local so
    this module stays importable without Django settings loaded."""
    import unicodedata

    decomposed = unicodedata.normalize("NFD", name or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _match_nickname(nickname, spec, known):
    """Best character name in a Discord nickname, using the GRM roster.

    A nickname often lists several toons; the one whose class matches the
    signed spec is the one they're bringing tonight.
    """
    from . import raidhelper

    candidates = raidhelper.candidate_names(nickname)
    want = SPEC_CLASS.get(spec)
    fallback = None
    for candidate in candidates:
        character = known.get(roster_fold(candidate))
        if character is None:
            continue
        if want and (character.get("class") or "") == want:
            return character["name"]
        if fallback is None:
            fallback = character["name"]
    return fallback or (candidates[0] if candidates else None)


def atiesh_from_gear(analysis):
    """Which Atiesh the character has equipped, from a checker.gear analysis.

    Returns the version ("Mage"/…), "" for "looked, hasn't got one", or None
    when the armory couldn't tell us (no recent login) — the three states the
    UI needs to distinguish a scanned miss from an unscanned character.
    """
    if not (analysis or {}).get("found"):
        return None
    for item in analysis.get("items") or []:
        version = ATIESH_ITEMS.get(item.get("item_id"))
        if version:
            return version
    return ""


BUCKET_LABELS = {
    TANK: "Tank",
    MELEE: "Melee",
    CASTER: "Caster",
    HEALER: "Healer",
    RANGED: "Ranged",
    SHOCKADIN: "Shockadin",
}

# Which bucket each group archetype is built around.
ARCHETYPE_BUCKETS = {
    MELEE: (TANK, MELEE),
    CASTER: (CASTER,),
    HEALER: (HEALER,),
    RANGED: (RANGED,),
}


def classify(spec, role):
    """A Raid-Helper (spec, role) pair -> class, bucket and party auras.

    `role` is Raid-Helper's Tank/Melee/Ranged/Healer. Two refinements sit on
    top of the plain role mapping: a Holy paladin who signed melee is a
    shockadin (their own placement rule), and hunters who signed Ranged are
    filler rather than casters.
    """
    bucket = ROLE_BUCKETS.get(role, RANGED)
    klass = SPEC_CLASS.get(spec)

    if bucket == CASTER and spec in HUNTER_SPECS:
        bucket = RANGED
    if bucket == MELEE and spec == "Holy1":
        bucket = SHOCKADIN

    auras = list(SPEC_AURAS.get(spec, []))
    # Sanctity Aura needs 21 points in Retribution and a paladin runs only one
    # aura at a time, so a tank or shockadin isn't the one supplying it. A SoD
    # rune-tank *could* spec deep enough for it, but ours run something else —
    # which is why a paladin tank still wants a ret in the group. Flip this and
    # the pairing rule has to flip with it: Sanctity doesn't stack, so a tank
    # who brings their own makes the ret beside them redundant.
    if bucket in (TANK, SHOCKADIN):
        auras = [a for a in auras if a != SANCTITY]

    return {
        "class": klass,
        "spec": SPEC_LABELS.get(spec, spec),
        "raw_spec": spec,
        "bucket": bucket,
        "auras": auras,
        "note": AMBIGUOUS.get((spec, bucket)),
    }


def make_player(name, spec=None, role=None, **overrides):
    """Build the player record the builder and the UI both work from."""
    info = classify(spec, role)
    player = {
        "name": name,
        "class": info["class"],
        "spec": info["spec"],
        "raw_spec": info["raw_spec"],
        "role": role,
        "bucket": info["bucket"],
        "auras": info["auras"],
        "atiesh": None,        # "Mage" / "Priest" / "Warlock" / "Druid"
        # Warcraft Logs standing, when we've looked it up. Decides who gets the
        # best-buffed seats; None just means "not known yet". Percentiles
        # saturate at 99 across a clearing guild's core, so raid DPS breaks the
        # ties — it's only ever compared within the same bucket.
        "parse": None,
        "dps": None,
        "note": info["note"] or "",
        "signup_name": name,   # the Discord nickname, before name resolution
        "discord_id": None,
        "status": "primary",   # primary / late / tentative
        "source": "raidhelper",
        "suggestion": None,    # {"bucket", "reason"} from Warcraft Logs
        "pinned": None,        # group index the RL has locked them into
    }
    player.update(overrides)
    return player


def set_bucket(player, bucket):
    """Re-bucket a player, keeping the aura tags that still make sense.

    A paladin moved to the shockadin bucket keeps nothing (Holy paladins carry
    no party aura); a hunter moved to melee keeps Trueshot, which is party
    scoped wherever they sit.
    """
    player["bucket"] = bucket
    if bucket == SHOCKADIN:
        player["auras"] = [a for a in player["auras"] if a != SANCTITY]
    return player


# ---------------------------------------------------------------------------
# Group budget
# ---------------------------------------------------------------------------
def default_group_count(attending, raid_slots=40):
    """How many parties to build.

    The raid's size caps it, but a 29-person signup shouldn't be smeared over
    eight groups — pack them into as few full parties as they fill, and let the
    raid lead add groups from the UI if they want the empty seats visible.
    """
    by_head = math.ceil(max(attending, 1) / GROUP_SIZE)
    return max(1, min(by_head, max(1, raid_slots // GROUP_SIZE)))


def allocate_groups(counts, group_count):
    """Split `group_count` groups between archetypes by head-count.

    Largest-remainder apportionment, so a bucket with a real share of the raid
    always gets at least the groups its numbers justify and the totals still
    add up exactly.
    """
    total = sum(counts.values())
    if total <= 0 or group_count <= 0:
        return {}
    exact = {k: v * group_count / total for k, v in counts.items() if v}
    floors = {k: int(math.floor(v)) for k, v in exact.items()}
    spare = group_count - sum(floors.values())
    # Hand the leftover groups to the biggest fractional remainders; ties break
    # on head-count so the larger bucket wins, then on name for determinism.
    order = sorted(
        exact, key=lambda k: (-(exact[k] - floors[k]), -counts[k], k)
    )
    for key in order[:max(0, spare)]:
        floors[key] += 1
    return {k: v for k, v in floors.items() if v}


def _archetype_plan(players, group_count):
    """Archetype for each group index, melee first then caster/healer/ranged."""
    counts = {
        MELEE: sum(1 for p in players if p["bucket"] in (TANK, MELEE)),
        CASTER: sum(1 for p in players if p["bucket"] in (CASTER, SHOCKADIN)),
        HEALER: sum(1 for p in players if p["bucket"] == HEALER),
        RANGED: sum(1 for p in players if p["bucket"] == RANGED),
    }
    alloc = allocate_groups(counts, group_count)
    plan = []
    for archetype in (MELEE, CASTER, HEALER, RANGED):
        plan.extend([archetype] * alloc.get(archetype, 0))
    # Rounding can leave a group unassigned when every bucket is empty.
    while len(plan) < group_count:
        plan.append(MELEE)
    return plan[:group_count]


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
class _Group:
    def __init__(self, index, archetype):
        self.index = index
        self.archetype = archetype
        self.players = []

    @property
    def free(self):
        return GROUP_SIZE - len(self.players)

    def has_aura(self, aura):
        return any(aura in (p.get("auras") or []) for p in self.players)

    def has_bucket(self, bucket):
        return any(p.get("bucket") == bucket for p in self.players)

    def atiesh_versions(self):
        return {p.get("atiesh") for p in self.players if p.get("atiesh")}

    def add(self, player):
        self.players.append(player)


def _place(groups, player, prefer):
    """Seat a player in the best group `prefer` ranks highest.

    `prefer` scores a group; higher is better, and None means "never here".
    Falls back to any group with a free seat so nobody is silently benched
    while seats remain.
    """
    scored = []
    for g in groups:
        if g.free <= 0:
            continue
        score = prefer(g)
        if score is None:
            continue
        scored.append((score, -g.free, g.index, g))
    if not scored:
        return None
    # Highest score wins; then the emptiest group, so parties fill evenly.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    group = scored[0][3]
    group.add(player)
    return group


def _by_standing(players):
    """Best Warcraft Logs standing first; unknown last, not bottom-ranked.

    Used everywhere a limited good seat is handed out, so the player who
    converts a buff best is the one who claims it.
    """
    return sorted(
        players,
        key=lambda p: (
            -(p["parse"] if p.get("parse") is not None else -1),
            -(p.get("dps") or 0),
        ),
    )


def _spread(groups, pool, aura, archetypes, note=None, prefer=None, prefer_why="",
            buckets=None):
    """Seat one aura carrier per group before doubling any of them up.

    This is the whole point of the builder: Sanctity/LotP/Moonkin and friends
    are party-scoped and don't stack, so the second one in a party is wasted.
    `archetypes` is in preference order — a spare boomkin does more good in the
    healer group (spell crit still applies) than as a second one in a caster
    group, so covering a new group always beats the preferred archetype.

    `prefer` picks between groups that are otherwise equal (e.g. a feral wants
    the tank's group for the melee crit). It never outranks covering a group
    that has none of the aura yet.

    `buckets` limits which carriers this pass may move. Some players carry an
    aura but have a placement rule of their own — a shockadin brings the Horn,
    but "group with a boomkin" outranks spreading it — so spreading must not
    seat them first and quietly win the argument.
    """
    label = AURA_LABELS.get(aura, aura)
    carriers = _by_standing([
        p for p in pool
        if aura in p["auras"] and (buckets is None or p["bucket"] in buckets)
    ])
    for player in carriers:
        # An aura carrier may also hold an Atiesh, and being seated here means
        # they skip the Atiesh pass entirely — so break ties away from a group
        # that already has their version. Half a point: it separates equals
        # without ever outranking the aura itself.
        version = player.get("atiesh")
        placed = _place(
            groups,
            player,
            lambda g: (
                None if g.archetype not in archetypes
                else (10 if not g.has_aura(aura) else 0)
                + len(archetypes) - archetypes.index(g.archetype)
                + (3 if prefer and prefer(g) else 0)
                - (0.5 if version and version in g.atiesh_versions() else 0)
            ),
        )
        if placed is not None:
            pool.remove(player)
            if note:
                carrying = sum(
                    1 for p in placed.players if aura in (p.get("auras") or [])
                )
                if carrying > 1:
                    reason = (
                        f"carries {label}, but this group already had one "
                        f"({carrying} now): every {placed.archetype} group was covered"
                    )
                elif prefer and prefer(placed) and prefer_why:
                    reason = f"carries {label} — {prefer_why}"
                else:
                    reason = (
                        f"carries {label} — first in this group, and it's party-only"
                    )
                note(player, placed, aura, reason)


# The priest staff buffs healing, so it earns its seat in the healer group;
# the other three are spell damage/haste/crit and belong with the casters.
ATIESH_HOME = {"Priest": HEALER}


def _spread_atiesh(groups, pool, archetypes, note=None):
    """Spread Atiesh holders so no party holds two of the same version."""
    holders = _by_standing([p for p in pool if p["atiesh"]])
    for player in holders:
        version = player["atiesh"]
        home = ATIESH_HOME.get(version, CASTER)
        placed = _place(
            groups,
            player,
            lambda g: (
                None if g.archetype not in archetypes
                else 0 if version in g.atiesh_versions()
                else 3 if g.archetype == home
                else 2
            ),
        )
        if placed is not None:
            pool.remove(player)
            if note:
                aura = ATIESH_AURAS.get(version, "party aura")
                where = (
                    f"the {home} group, which is where {aura} does most good"
                    if placed.archetype == home
                    else f"a {placed.archetype} group"
                )
                note(
                    player,
                    placed,
                    "atiesh",
                    f"holds the {version} Atiesh ({aura}) — placed in {where}",
                )


def layout_positions(groups):
    """{player name: group index} from a saved comp's groups."""
    positions = {}
    for group in groups or []:
        index = group.get("index")
        for player in group.get("players") or []:
            name = (player or {}).get("name")
            if name and index:
                positions[name] = index
    return positions


def build_comp(players, group_count, pins=None, stack_tanks=False, layout=None):
    """Assign `players` to `group_count` parties of five.

    Returns {"groups": [...], "bench": [...], "warnings": [...]}. Players the
    raid lead has pinned keep their seat; everyone else is placed by the rules.

    `stack_tanks` puts every tank in group 1 instead of spreading one per melee
    group — the usual arrangement when the tanks want to share a healer or an
    assignment callout rather than anchor a group each.

    `layout` reseats people where a saved comp had them. Signups move after a
    comp is saved, so it's applied as a starting point rather than a demand:
    anyone in it keeps their seat, anyone new is placed by the rules around
    them, and anyone who dropped out simply isn't there. Distinct from `pins`,
    which are explicit locks the raid lead set and survive a re-arrange.
    """
    pins = pins or {}
    plan = _archetype_plan(players, group_count)
    groups = [_Group(i + 1, plan[i]) for i in range(group_count)]

    # Every placement records why it happened, so the page can explain the comp
    # from what the rules actually did rather than a second description of them.
    steps = []

    def note(player, group, stage, reason):
        steps.append(
            {
                "player": player["name"],
                "group": group.index if group is not None else None,
                "stage": stage,
                "reason": reason,
            }
        )

    layout = layout or {}
    pool = []
    for player in players:
        pinned = pins.get(player["name"])
        target = pinned or layout.get(player["name"])
        if target and 1 <= target <= group_count and groups[target - 1].free > 0:
            player["pinned"] = pinned or None
            groups[target - 1].add(player)
            note(
                player,
                groups[target - 1],
                "pin" if pinned else "saved",
                "pinned here by the raid lead — the rules left this seat alone"
                if pinned
                else "where the saved comp had them",
            )
        else:
            player["pinned"] = None
            pool.append(player)

    melee_archetypes = (MELEE,)
    caster_archetypes = (CASTER, HEALER)

    # --- Tanks -------------------------------------------------------------
    # Stacked: every tank into group 1, first come first served. Anyone who
    # doesn't fit falls through to the per-class rules below rather than being
    # left in the pool for the generic fill to scatter.
    if stack_tanks and groups:
        for player in [p for p in pool if p["bucket"] == TANK]:
            if groups[0].free <= 0:
                break
            groups[0].add(player)
            pool.remove(player)
            note(player, groups[0], "tank", "tank, and tanks are stacked in group 1")

    # A stacked tank group with a paladin in it already has the Horn and the
    # aura, so the seat left over is worth more to a warrior's shout than to a
    # second paladin. Claim it now: the ret pass below would otherwise take the
    # last seat and there'd be nothing to give the warrior.
    if stack_tanks and groups and any(
        p.get("class") == "Paladin" for p in groups[0].players
    ):
        for player in _by_standing([p for p in pool if SHOUT in p["auras"]]):
            if groups[0].free <= 0 or groups[0].has_aura(SHOUT):
                break
            groups[0].add(player)
            pool.remove(player)
            note(
                player,
                groups[0],
                SHOUT,
                "shout for the stacked tanks — a paladin is already covering the "
                "Horn and aura there",
            )

    # One per melee group. A paladin tank wants a Retribution paladin in the
    # party for the aura, so it claims a melee group and the ret pass below
    # fills it first.
    #
    # Warlock tanks are deliberately NOT placed here: they want a boomkin's
    # group, and boomkins aren't seated until further down. Placing them now
    # would mean the rule could never see one.
    for player in [
        p for p in pool if p["bucket"] == TANK and p["class"] != "Warlock"
    ]:
        placed = _place(
            groups,
            player,
            lambda g: (
                None if g.archetype != MELEE
                else (2 if not g.has_bucket(TANK) else 0)
            ),
        )
        if placed is not None:
            pool.remove(player)
            note(
                player,
                placed,
                "tank",
                f"{player['class']} tank — anchors a melee group",
            )

    pala_tank_groups = {
        g.index for g in groups
        if any(
            p.get("bucket") == TANK and p.get("class") == "Paladin"
            for p in g.players
        )
    }

    # --- Melee groups ------------------------------------------------------
    # Ret paladins first, and the paladin tank's group gets one before anyone
    # else does. Then one feral per group (Leader of the Pack), then warriors
    # spread for the shout.
    rets = _by_standing([p for p in pool if SANCTITY in p["auras"]])
    for player in rets:
        placed = _place(
            groups,
            player,
            lambda g: (
                None if g.archetype != MELEE
                else (4 if g.index in pala_tank_groups and not g.has_aura(SANCTITY)
                      else 2 if not g.has_aura(SANCTITY)
                      else 0)
            ),
        )
        if placed is not None:
            pool.remove(player)
            note(
                player,
                placed,
                SANCTITY,
                "ret paladin — Sanctity Aura for the paladin tank in this group"
                if placed.index in pala_tank_groups
                else "ret paladin — Sanctity Aura, one per melee group",
            )

    # Ferals go to a tank's group where there's a seat: Leader of the Pack is
    # melee crit, and the tanks want it. A bear tank already carries LotP
    # himself, so has_aura keeps ferals out of his group and sends them to the
    # rogue/warrior/paladin tank who doesn't have it.
    _spread(
        groups,
        pool,
        LOTP,
        melee_archetypes,
        note,
        prefer=lambda g: g.has_bucket(TANK),
        prefer_why="Leader of the Pack, in with the tank for the melee crit",
    )
    # Any leftover melee paladin covers a melee group with no Horn of Lordaeron
    # yet — the ret pass usually does it, but a prot paladin counts too.
    # Shockadins and paladin healers are excluded: they carry the Horn, but
    # their own rules decide where they sit.
    _spread(groups, pool, HORN, melee_archetypes, note, buckets=(MELEE, TANK))

    _spread(groups, pool, SHOUT, melee_archetypes, note)

    # --- Caster / healer groups -------------------------------------------
    # Boomkins seed the caster groups (Moonkin aura), then Atiesh holders are
    # spread by version, then a shadow priest per group for mana sustain.
    _spread(groups, pool, MOONKIN, caster_archetypes, note)

    # Warlock tanks, now that the boomkins and warriors they want to sit with
    # are actually on the board. Ordering matters: run this before the boomkins
    # and the rule can never see one.
    for player in [
        p for p in pool if p["bucket"] == TANK and p["class"] == "Warlock"
    ]:
        placed = _place(
            groups,
            player,
            lambda g: (
                3 if g.has_aura(MOONKIN)
                else 2 if g.has_aura(SHOUT)
                else 1 if g.archetype == CASTER
                else 0
            ),
        )
        if placed is not None:
            pool.remove(player)
            why = (
                "with a boomkin for the spell crit" if placed.has_aura(MOONKIN)
                else "with a warrior for the shout HP" if placed.has_aura(SHOUT)
                else "no boomkin or warrior free, so any group"
            )
            note(player, placed, "tank", f"warlock tank — {why}")

    _spread_atiesh(groups, pool, caster_archetypes, note)

    # Vampiric Touch is party-only, and healers are the ones who actually run
    # dry — mages and locks have their own answers to mana. So once there's
    # more than one shadow priest to go round, one is reserved for the healer
    # group before the casters get a second. With only one, the casters keep it.
    shadow_priests = [p for p in pool if SUSTAIN in p["auras"]]
    if len(shadow_priests) > 1:
        player = shadow_priests[0]
        placed = _place(
            groups,
            player,
            lambda g: None if g.archetype != HEALER or g.has_aura(SUSTAIN) else 2,
        )
        if placed is not None:
            pool.remove(player)
            note(
                player,
                placed,
                SUSTAIN,
                f"shadow priest — {len(shadow_priests)} available, so one is "
                "reserved for the healers (Vampiric Touch is party-only)",
            )
    _spread(groups, pool, SUSTAIN, caster_archetypes, note)
    # Horde: totems are party-only, same spreading logic.
    _spread(groups, pool, WINDFURY, melee_archetypes, note)
    _spread(groups, pool, MANASPRING, caster_archetypes, note)
    _spread(groups, pool, TRUESHOT, (RANGED,), note)

    # --- Shockadins --------------------------------------------------------
    # House rule: group with a boomkin > group with a ret paladin > whatever.
    for player in [p for p in pool if p["bucket"] == SHOCKADIN]:
        placed = _place(
            groups,
            player,
            lambda g: (
                4 if g.has_aura(MOONKIN)
                else 3 if g.has_aura(SANCTITY)
                else 1
            ),
        )
        if placed is not None:
            pool.remove(player)
            why = (
                "grouped with a boomkin" if placed.has_aura(MOONKIN)
                else "no boomkin free, so grouped with a ret paladin"
                if placed.has_aura(SANCTITY)
                else "no boomkin or ret paladin free, so any seat"
            )
            note(player, placed, SHOCKADIN, f"shockadin — {why}")

    # --- Paladin healers ---------------------------------------------------
    # They never really run out of mana, so they're the healer who can sit in a
    # caster group — which leaves the healer-group seats for the ones who do
    # run dry, and for the shadow priest's Vampiric Touch to land on.
    for player in [
        p for p in pool if p["bucket"] == HEALER and p["class"] == "Paladin"
    ]:
        placed = _place(groups, player, lambda g: 3 if g.archetype == CASTER else 1)
        if placed is not None:
            pool.remove(player)
            note(
                player,
                placed,
                "pala-healer",
                "paladin healer — parked with the casters, since they don't run "
                "out of mana and this frees a healer seat"
                if placed.archetype == CASTER
                else "paladin healer — no caster seat free, so a healer seat",
            )

    # --- Everyone else -----------------------------------------------------
    # Two phases, and the split matters. Filling one bucket completely before
    # starting the next lets an over-subscribed bucket eat another's group:
    # twelve casters would take every seat in the healer group and leave the
    # healers to spill into melee. So phase one seats everybody in their OWN
    # archetype only, and nobody leaves it until every bucket has had its turn.
    for bucket, archetypes in (
        (TANK, (MELEE,)),
        (MELEE, (MELEE,)),
        (CASTER, (CASTER,)),
        (HEALER, (HEALER,)),
        (RANGED, (RANGED,)),
    ):
        # Best parses first, and each takes the best-buffed seat still open: a
        # feral's crit aura or a boomkin's is worth most on whoever converts it.
        # Players with no parse on record go last rather than being penalised.
        for player in _by_standing([p for p in pool if p["bucket"] == bucket]):
            placed = _place(
                groups,
                player,
                lambda g: (
                    None if g.archetype not in archetypes
                    else 100 + _buff_score(g, bucket)
                ),
            )
            if placed is not None:
                pool.remove(player)
                buffs = _buff_score(placed, bucket)
                if player["parse"] is not None and buffs:
                    reason = (
                        f"{player['parse']:.0f}% parse — given a {bucket} seat in "
                        f"the best-buffed group still open ({buffs} auras)"
                    )
                else:
                    reason = f"no aura to place around — filled a {bucket} seat"
                note(player, placed, "fill", reason)

    # Phase two: whoever's archetype ran out of seats. Compatible groups first
    # — a caster loses less in the healer group than in a melee one — and the
    # weakest parses spill, since phase one seated the best players already.
    for player in _by_standing(list(pool)):
        bucket = player["bucket"]
        placed = _place(
            groups,
            player,
            lambda g: (
                (10 if g.archetype in SPILL_COMPATIBLE.get(bucket, ()) else 0)
                + _buff_score(g, bucket)
            ),
        )
        if placed is not None:
            pool.remove(player)
            note(
                player,
                placed,
                "spill",
                f"no {bucket} seat left anywhere — {placed.archetype} group was "
                "the closest fit",
            )

    for player in list(pool):
        placed = _place(groups, player, lambda g: 1)
        if placed is not None:
            pool.remove(player)
            note(player, placed, "overflow", "last seat available")

    for player in pool:
        note(player, None, "bench", "every seat was taken")

    return {
        "groups": [
            {"index": g.index, "archetype": g.archetype, "players": g.players}
            for g in groups
        ],
        "bench": pool,
        "warnings": warnings_for(groups, pool, group_count, stack_tanks=stack_tanks),
        "explain": {
            "group_count": group_count,
            "stack_tanks": stack_tanks,
            "budget": _budget_explain(players, groups),
            "steps": steps,
        },
    }


def _buff_score(group, bucket):
    """How much a group's party auras are worth to this kind of player."""
    score = sum(1 for aura in BUFF_AURAS.get(bucket, ()) if group.has_aura(aura))
    if bucket in (CASTER, HEALER, SHOCKADIN):
        score += len(group.atiesh_versions())
    return score


def _budget_explain(players, groups):
    """How the group budget was decided, for the debug popup."""
    counts = {
        MELEE: sum(1 for p in players if p["bucket"] in (TANK, MELEE)),
        CASTER: sum(1 for p in players if p["bucket"] in (CASTER, SHOCKADIN)),
        HEALER: sum(1 for p in players if p["bucket"] == HEALER),
        RANGED: sum(1 for p in players if p["bucket"] == RANGED),
    }
    allocated = {}
    for group in groups:
        allocated[group.archetype] = allocated.get(group.archetype, 0) + 1
    return {
        "attending": len(players),
        "counts": counts,
        "allocated": allocated,
    }


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
def evaluate_layout(groups, bench, group_count=None, stack_tanks=False):
    """Warnings for a layout the raid lead arranged by hand.

    Dragging someone into a second boomkin's party has to raise the same
    warning an auto-build would, so manual layouts are fed back through the
    same checks rather than a second copy of the rules in the browser.
    """
    built = []
    for raw in groups:
        group = _Group(raw.get("index") or len(built) + 1, raw.get("archetype") or MELEE)
        group.players = list(raw.get("players") or [])
        built.append(group)
    return warnings_for(
        built, bench or [], group_count or len(built), stack_tanks=stack_tanks
    )


def warnings_for(groups, bench, group_count, stack_tanks=False):
    """Everything the raid lead should look at before pulling.

    Doubled auras are only worth flagging when the raid lead could actually
    fix them. With six ret paladins and four melee groups the stacking is
    forced, and a warning nobody can act on just trains people to ignore the
    list — so each one is checked against "is there somewhere better to go?".
    """
    out = []

    for g in groups:
        if len(g.players) > GROUP_SIZE:
            out.append(f"Group {g.index} has {len(g.players)} players (max {GROUP_SIZE}).")

        # Doubled-up non-stacking auras — the second carrier is wasted here.
        for aura, label in AURA_LABELS.items():
            carriers = [p.get("name") for p in g.players if aura in (p.get("auras") or [])]
            # The stacked-tank group is composed on purpose — a warrior's shout
            # instead of a second paladin — so it doesn't count as a group
            # crying out for whatever it hasn't got.
            if len(carriers) > 1 and any(
                other is not g
                and other.archetype == g.archetype
                and not other.has_aura(aura)
                and not (stack_tanks and other.index == 1)
                for other in groups
            ):
                out.append(
                    f"Group {g.index}: {len(carriers)} × {label} "
                    f"({', '.join(carriers)}) — it doesn't stack, and another "
                    f"{g.archetype} group has none."
                )

        versions = [p.get("atiesh") for p in g.players if p.get("atiesh")]
        for version in set(versions):
            if versions.count(version) > 1:
                out.append(
                    f"Group {g.index}: {versions.count(version)} × {version} Atiesh "
                    "— the aura doesn't stack, spread them out."
                )

        if g.archetype == CASTER and g.players:
            if not g.atiesh_versions() and not g.has_aura(MOONKIN):
                out.append(
                    f"Group {g.index} is a caster group with no Atiesh and no boomkin."
                )

        # Every melee group wants a paladin: Horn of Lordaeron is +6 Str/Agi to
        # the party and nothing else supplies it.
        if g.archetype == MELEE and g.players and not g.has_aura(HORN):
            out.append(
                f"Group {g.index} is a melee group with no paladin — no Horn of "
                "Lordaeron."
            )

    tanks = sum(
        1 for g in groups for p in g.players if p.get("bucket") == TANK
    )
    melee_groups = sum(1 for g in groups if g.archetype == MELEE)
    # Only meaningful when tanks are meant to anchor a group each; with them
    # stacked in group 1 by choice, a shortfall isn't a problem to report.
    if tanks < melee_groups and not stack_tanks:
        out.append(
            f"Only {tanks} tank{'s' if tanks != 1 else ''} signed for {melee_groups} "
            "melee groups."
        )

    if bench:
        out.append(
            f"{len(bench)} player{'s' if len(bench) != 1 else ''} benched "
            f"({', '.join(str(p.get('name')) for p in bench[:6])}"
            f"{'…' if len(bench) > 6 else ''}) — the raid holds "
            f"{group_count * GROUP_SIZE}."
        )
    return out


def parses_from_logs(names, leaderboard_rows):
    """{name: {"parse", "dps"}} for the roster, from the guild standings.

    The whole-raid percentile is the headline number, but it saturates: nearly
    everyone in a clearing guild's core has a 99 somewhere, so on its own it
    can't order the people it most matters for. Raid DPS rides along to break
    those ties (only ever compared within the same bucket, where it means
    something). Players absent from our logs stay unknown and are seated after
    those who have a standing, not below them.
    """
    by_name = {
        (row.get("name") or "").strip().lower(): row
        for row in leaderboard_rows or []
    }
    out = {}
    for name in names:
        row = by_name.get(name.strip().lower())
        if not row:
            continue
        overall = row.get("overall") or {}
        parse = overall.get("rank_percent")
        if parse is None:
            parse = row.get("best_parse")
        dps = overall.get("dps") or (row.get("best_boss") or {}).get("dps")
        if parse is None and dps is None:
            continue
        out[name] = {"parse": parse, "dps": dps}
    return out


def suggestions_from_logs(players, leaderboard_rows):
    """Cross-check softres registrations against what people actually played.

    Warcraft Logs knows the SoD specs softres can't express — Hunter "Melee",
    Rogue/Warlock "Tank", and Shockadin (derived in wcl.py from damage beating
    healing). We never apply these automatically: somebody who signed as a
    healer is a healer until the raid lead says otherwise.
    """
    by_name = {}
    for row in leaderboard_rows or []:
        by_name[(row.get("name") or "").strip().lower()] = row

    spec_buckets = {
        "melee": MELEE,
        "tank": TANK,
        "shockadin": SHOCKADIN,
    }
    for player in players:
        row = by_name.get(player["name"].strip().lower())
        if not row:
            continue
        logged = ((row.get("overall") or row.get("best_boss") or {}).get("spec") or "")
        bucket = spec_buckets.get(logged.strip().lower())
        if bucket and bucket != player["bucket"]:
            player["suggestion"] = {
                "bucket": bucket,
                "spec": logged,
                "reason": f"Warcraft Logs has them parsing as {logged}",
            }
    return players


def rules_summary():
    """The rules the builder applies, for the Rules modal.

    Built from the same tables the placement code reads, so the documented
    rules can't drift away from the enforced ones.
    """
    sod_readings = [
        f"{SPEC_LABELS.get(spec, spec)} ({SPEC_CLASS.get(spec, '?')}) signed as "
        f"{bucket} → {reason}"
        for (spec, bucket), reason in sorted(AMBIGUOUS.items())
    ]
    return [
        {
            "title": "Where the roster comes from",
            "items": [
                "Raid-Helper signups are the source of truth: the role someone "
                "picked (Tank / Melee / Ranged / Healer) plus their spec.",
                "The event names its own softres sheet. Signups and reserves both "
                "carry the signer's Discord id, so joining on it turns Discord "
                "nicknames into real character names.",
                "No reserve? The nickname is split up and matched against the GRM "
                "roster, preferring the toon whose class matches the signed spec.",
                "Absence and Bench signups never take a seat. Late and Tentative "
                "are seated but marked.",
                "A signup is taken at its word. Warcraft Logs can disagree and "
                "raise a ⚑ suggestion, but it never overrules on its own.",
            ],
        },
        {
            "title": "How many groups",
            "items": [
                f"Parties of {GROUP_SIZE}. As few full groups as the signups fill, "
                "capped by the raid size from the softres sheet. Change it in the UI.",
                "Groups are split between melee / caster / healer / ranged by "
                "head-count (largest remainder), so the totals always add up.",
            ],
        },
        {
            "title": "Placement order",
            "items": [
                "Pinned players first — the rules never move them.",
                "Tanks: one per melee group, or all in group 1 if 'stack tanks' is "
                "on. A paladin tank gets a ret paladin for the aura. A warlock tank "
                "prefers a boomkin's group, else a warrior for shout HP.",
                "Melee: one ret paladin per group (the paladin tank's group first), "
                "then warriors spread for the shout.",
                "Every melee group wants a paladin: Horn of Lordaeron is +6 Str "
                "and Agi to the party, and nothing else supplies it. Every "
                "paladin has it, whatever they signed as.",
                "With tanks stacked, a paladin among them already covers the Horn "
                "and the aura, so the spare seat goes to a warrior's shout rather "
                "than a second paladin.",
                "Ferals go in with a tank where there's a seat — Leader of the Pack "
                "is melee crit and the tanks want it. A bear tank already carries "
                "it, so ferals are sent to a tank who doesn't.",
                "Casters: a boomkin per group, then Atiesh by version, then a "
                "shadow priest per group.",
                "Shadow priests: with two or more, one is reserved for the healer "
                "group before the casters get a second. A lone one stays with the "
                "casters.",
                "Paladin healers sit with the casters — they don't run out of mana, "
                "so it frees a healer seat for someone who does.",
                "Shockadins: boomkin's group > ret paladin's group > any free seat.",
                "Ranged hunters are the filler pool and take leftover seats.",
                "Everyone is seated in their OWN archetype first, before anyone "
                "is allowed to leave it — otherwise an over-subscribed bucket "
                "eats another's group (twelve casters will take every healer "
                "seat and exile the healers to melee).",
                "Only then does anyone spill, into the closest fit: casters and "
                "healers share before either is sent to the melee.",
                "Within that, everyone fills their own archetype best parse first, and "
                "each takes the best-buffed seat still open — a crit or damage "
                "aura is worth most on whoever converts it. Players with no "
                "parse on record are seated after those who have one, not below "
                "them.",
                "Nobody is benched while a seat is open.",
            ],
        },
        {
            "title": "Party auras — none of these stack",
            "items": [
                f"{label} ({', '.join(sorted(s for s, a in SPEC_AURAS.items() if aura in a))})"
                for aura, label in AURA_LABELS.items()
            ],
        },
        {
            "title": "Atiesh (party-only, non-stacking)",
            "items": [
                f"{version}: {effect}" for version, effect in ATIESH_AURAS.items()
            ]
            + [
                "Read from the Blizzard armory and remembered; set by hand where "
                "Blizzard can't see the character (manual always wins).",
                "No group is ever given two of the same version. The priest staff "
                "prefers the healer group; the rest prefer casters.",
            ],
        },
        {"title": "SoD readings of an ordinary spec", "items": sod_readings},
        {
            "title": "Warnings",
            "items": [
                "A doubled aura is only flagged when another group of the same "
                "kind has none — forced stacking stays quiet so the list stays "
                "worth reading.",
                "Duplicate Atiesh in one group is always flagged.",
                "A caster group with no Atiesh and no boomkin is flagged.",
                "Too few tanks is flagged, unless tanks are stacked on purpose.",
            ],
        },
    ]


def as_text(groups, bench=None):
    """Plain-text comp for pasting into Discord."""
    lines = []
    for g in groups:
        label = BUCKET_LABELS.get(g["archetype"], g["archetype"].title())
        lines.append(f"Group {g['index']} ({label})")
        for p in g["players"]:
            bits = [p["name"]]
            spec = " ".join(x for x in (p.get("spec"), p.get("class")) if x)
            if spec:
                bits.append(f"({spec})")
            if p.get("bucket") == TANK:
                bits.append("[TANK]")
            if p.get("atiesh"):
                bits.append(f"[{p['atiesh']} Atiesh]")
            lines.append("  " + " ".join(bits))
        lines.append("")
    if bench:
        lines.append("Bench: " + ", ".join(p["name"] for p in bench))
    return "\n".join(lines).strip()
