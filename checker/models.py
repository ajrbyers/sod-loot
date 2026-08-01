"""Learned toon clusters, remembered from submitted eligibility checks."""
from django.db import models


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
