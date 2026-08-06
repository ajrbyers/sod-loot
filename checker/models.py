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
