"""Learned toon clusters, remembered from submitted eligibility checks."""
from django.db import models


class SoftresAudit(models.Model):
    """A softres raid sheet the audit page has fetched, so raid leads can
    re-run recent checks without hunting down the link again."""

    raid_id = models.CharField(max_length=32, unique=True)
    instance = models.CharField(max_length=64, blank=True)
    raid_date = models.BigIntegerField(null=True, blank=True)
    reserve_count = models.IntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]

    def __str__(self):
        return f"{self.raid_id} ({self.instance})"


class RaidComp(models.Model):
    """A saved comp for one softres sheet.

    `groups` is the authoritative layout (a list of {index, archetype, players});
    `overrides` and `pins` capture the raid lead's manual decisions so a rebuild
    after the sheet changes keeps them. Keyed on the softres raid id, so
    re-opening the same link picks up where the RL left off.
    """

    raid_id = models.CharField(max_length=32, unique=True)
    instance = models.CharField(max_length=64, blank=True)
    groups = models.JSONField(default=list)
    bench = models.JSONField(default=list)
    # {"<toon>": {"bucket": "tank", "atiesh": "Mage"}} — survives a rebuild.
    overrides = models.JSONField(default=dict)
    # {"<toon>": <group index>} — players locked to a seat.
    pins = models.JSONField(default=dict)
    # Put every tank in group 1 rather than one per melee group.
    stack_tanks = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated"]

    def __str__(self):
        return f"comp for {self.raid_id}"


class AtieshHolder(models.Model):
    """Who owns an Atiesh, remembered across raids.

    The armory scan fills this in automatically (source="armory") but only sees
    characters Blizzard has a recent login for, so the raid lead can set one by
    hand (source="manual"). Manual entries win: they're a deliberate correction
    of what the scan couldn't see.
    """

    ARMORY = "armory"
    MANUAL = "manual"

    key = models.CharField(max_length=64, unique=True)  # roster.fold(name)
    name = models.CharField(max_length=64)
    # "Mage" / "Priest" / "Warlock" / "Druid", or "" for "checked, hasn't got one".
    version = models.CharField(max_length=16, blank=True)
    source = models.CharField(max_length=16, default=ARMORY)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name}: {self.version or 'none'} ({self.source})"


class ToonLink(models.Model):
    """A cluster of toons the checker has learned belong to one player.

    `members` holds display names as last entered; `keys` holds the same names
    accent/case-folded for matching. Any member can be searched and the rest
    prefill as their alts — the link works in both directions. GRM roster data
    takes precedence for guildies; these rows chiefly serve non-guildies, who
    have no roster row to prefill from.
    """

    members = models.JSONField(default=list)
    keys = models.JSONField(default=list)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return ", ".join(self.members)
