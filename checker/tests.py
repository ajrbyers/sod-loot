"""Regression tests for the roster autosuggest (GRM CSV export parsing), the
Warcraft Logs lookup chain, and remembered toon links."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from checker import gear, items, roster, softres, views, wcl
from checker.models import SoftresAudit, ToonLink

CSV_HEADER = (
    "Name;Rank;Level;Class;Race;Sex;Last Online (Days);Main/Alt;Player Alts;"
    "Join Date;Promo Date;Rank History;Birthday;Public Note;Officer Note;"
    "Custom Note;Faction"
)

CSV_ROWS = [
    # Alt whose cluster includes the main, tagged "(main)".
    "Akabow;Altling;60;Hunter;Night Elf;Female;50;Alt;"
    "Akaslam-WildGrowth,Shapíe-WildGrowth(main),Sorcerius-WildGrowth;"
    "20 Dec '25;20 Dec '25;;;;;;Alliance",
    # Main with an accented name.
    "Shapíe;Fel Reaver;60;Druid;Night Elf;Female;1;Main;"
    "Akabow-WildGrowth,Akaslam-WildGrowth,Sorcerius-WildGrowth;"
    "20 Dec '25;20 Dec '25;;;;;;Alliance",
    # No main/alt data at all — should still be suggestible, with no alts.
    "Alan;Imp;60;Warlock;Gnome;Male;6;;;27 Jun '26;27 Jun '26;;;;;;Alliance",
    # Substring (not prefix) match for "bow" ranking checks.
    "Rainbow;Imp;60;Mage;Human;Female;2;;;01 Jan '26;01 Jan '26;;;;;;Alliance",
]


class RosterTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.roster_path = Path(tmp.name) / "grm.csv"
        # utf-8-sig mimics the BOM that GRM exports carry.
        cls.roster_path.write_text(
            "\n".join([CSV_HEADER, *CSV_ROWS]), encoding="utf-8-sig"
        )
        cls.settings_override = override_settings(ROSTER_FILE=str(cls.roster_path))
        cls.settings_override.enable()
        cls.addClassCleanup(cls.settings_override.disable)
        roster._load.cache_clear()

    def test_search_is_accent_and_case_insensitive(self):
        matches = roster.search("shapie")
        self.assertEqual([m["name"] for m in matches], ["Shapíe"])

    def test_alts_strip_realm_suffix_and_main_marker(self):
        matches = roster.search("Akabow")
        self.assertEqual(matches[0]["alts"], ["Akaslam", "Shapíe", "Sorcerius"])

    def test_character_without_alt_data_suggests_with_empty_alts(self):
        matches = roster.search("Alan")
        self.assertEqual(matches[0]["alts"], [])
        self.assertEqual(matches[0]["main_or_alt"], "")

    def test_prefix_matches_rank_before_substring_matches(self):
        # "Akabow" and "Alan" start with "a"; the others merely contain it.
        names = [m["name"] for m in roster.search("a")]
        self.assertEqual(names, ["Akabow", "Alan", "Shapíe", "Rainbow"])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(roster.search(""), [])
        self.assertEqual(roster.search("   "), [])

    def test_missing_roster_file_degrades_to_no_matches(self):
        with override_settings(ROSTER_FILE=str(self.roster_path.parent / "gone.csv")):
            self.assertEqual(roster.search("shapie"), [])

    def test_api_endpoint_returns_matches_with_alts(self):
        response = self.client.get("/api/characters", {"q": "akabow"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["matches"][0]["name"], "Akabow")
        self.assertEqual(
            data["matches"][0]["alts"], ["Akaslam", "Shapíe", "Sorcerius"]
        )

    def test_api_endpoint_rejects_post(self):
        self.assertEqual(self.client.post("/api/characters").status_code, 405)


# A minimal character payload as the by-name query returns it: 40-man rankings
# only, mirroring Cameroncrown (the non-guildie whose lookup originally broke).
CHARACTER_PAYLOAD = {
    "characterData": {
        "character": {
            "id": 95355433,
            "name": "Cameroncrown",
            "s40": {
                "bestPerformanceAverage": 80.35,
                "rankings": [
                    {"encounter": {"name": "Lillian Voss"}, "rankPercent": 84.43}
                ],
            },
            "s20": None,
        }
    }
}


class WCLLookupTests(SimpleTestCase):
    def test_graphql_posts_to_partition_scoped_endpoint(self):
        # Name+realm lookups return null for SoD characters on the www endpoint;
        # only the partition endpoint (sod.warcraftlogs.com) resolves them.
        self.assertIn("sod.warcraftlogs.com", settings.WCL_API_URL)
        with mock.patch.object(wcl, "_get_token", return_value="tok"), mock.patch.object(
            wcl, "_http_json", return_value={"data": {}}
        ) as http:
            wcl.graphql("query {}")
        self.assertEqual(http.call_args.args[0], settings.WCL_API_URL)

    def test_non_guildie_resolves_via_name_and_realm_fallback(self):
        # Not on the roster, never in a guild report -> the name+realm fallback
        # must still find the character and return their parse.
        def fake_graphql(query, variables=None):
            self.assertEqual(query, wcl._CHARACTER_BY_NAME_QUERY)
            self.assertEqual(variables["name"], "Cameroncrown")
            self.assertEqual(variables["server"], "wild-growth")
            self.assertEqual(variables["region"], "eu")
            return CHARACTER_PAYLOAD

        with mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            wcl, "_fetch_all_raids", return_value=[]
        ), mock.patch.object(wcl, "graphql", side_effect=fake_graphql):
            result = wcl._get_best_parse("Cameroncrown", "wild-growth", "eu")

        self.assertTrue(result["found"])
        self.assertEqual(result["best_average"], 80.35)
        self.assertEqual(result["size"], 40)
        self.assertEqual(result["top_parse"]["encounter"], "Lillian Voss")

    def test_roster_member_resolves_by_id_without_name_lookup(self):
        def fake_graphql(query, variables=None):
            self.assertEqual(query, wcl._CHARACTER_BY_ID_QUERY)
            self.assertEqual(variables["id"], 42)
            return CHARACTER_PAYLOAD

        member_map = {"cameroncrown": {"id": 42, "name": "Cameroncrown"}}
        with mock.patch.object(
            wcl, "get_member_map", return_value=member_map
        ), mock.patch.object(wcl, "graphql", side_effect=fake_graphql):
            result = wcl._get_best_parse("Cameroncrown", "wild-growth", "eu")

        self.assertTrue(result["found"])
        self.assertEqual(result["best_average"], 80.35)


# Game-data payload for the top-DPS lookup: spec lists come from WCL, not a
# hardcoded map.
CLASSES_PAYLOAD = {
    "gameData": {
        "classes": [
            {
                "id": 11,
                "name": "Warrior",
                "specs": [{"name": "Arms"}, {"name": "Fury"}, {"name": "Protection"}],
            }
        ]
    }
}

# Aliases follow _build_spec_rankings_query: s<size>_<spec index>. Arms (i=0)
# has the better parse on 40-man, but Fury (i=1) on 20-man has the higher DPS
# amount — the amount must win. Fury 40-man out-damages Arms on Beastmaster, so
# the per-boss matrix must pick it; Lillian Voss is unkilled (bestAmount 0) and
# Protection (i=2) is unplayed entirely.
TOP_DPS_RANKINGS_PAYLOAD = {
    "characterData": {
        "character": {
            "id": 7,
            "name": "Slamster",
            "s40_0": {
                "bestPerformanceAverage": 95.5,
                "rankings": [
                    {
                        "encounter": {"name": "Beastmaster"},
                        "bestAmount": 2400.0,
                        "rankPercent": 96.2,
                    },
                    {
                        "encounter": {"name": "Lillian Voss"},
                        "bestAmount": 0,
                        "rankPercent": None,
                    },
                ],
            },
            "s20_0": None,
            "s40_1": {
                "bestPerformanceAverage": 88.0,
                "rankings": [
                    {
                        "encounter": {"name": "Beastmaster"},
                        "bestAmount": 2500.0,
                        "rankPercent": 90.1,
                    }
                ],
            },
            "s20_1": {
                "bestPerformanceAverage": 68.4,
                "rankings": [
                    {
                        "encounter": {"name": "Balnazzar"},
                        "bestAmount": 2600.0,
                        "rankPercent": 71.9,
                    }
                ],
            },
            "s40_2": {
                "bestPerformanceAverage": None,
                "rankings": [
                    {"encounter": {"name": "Balnazzar"}, "bestAmount": 0, "rankPercent": None}
                ],
            },
            "s20_2": None,
        }
    }
}


class TopDpsTests(SimpleTestCase):
    def get_top_dps_for_roster_warrior(self):
        """Drive _get_top_dps for a roster warrior with all WCL calls stubbed."""

        def fake_graphql(query, variables=None):
            if query == wcl._CHAR_CLASS_BY_ID_QUERY:
                self.assertEqual(variables["id"], 7)
                return {
                    "characterData": {
                        "character": {"id": 7, "name": "Slamster", "classID": 11}
                    }
                }
            if query == wcl._CLASS_SPECS_QUERY:
                return CLASSES_PAYLOAD
            # The dynamically built per-spec query: every warrior spec must be
            # fanned out, on both sizes.
            for spec in ("Arms", "Fury", "Protection"):
                self.assertIn(f'specName: "{spec}"', query)
            self.assertIn("size: 40", query)
            self.assertIn("size: 20", query)
            self.assertEqual(variables["id"], 7)
            return TOP_DPS_RANKINGS_PAYLOAD

        member_map = {"slamster": {"id": 7, "name": "Slamster"}}
        with mock.patch.object(
            wcl, "get_member_map", return_value=member_map
        ), mock.patch.dict(
            wcl._class_specs_cache, {"map": None}
        ), mock.patch.object(wcl, "graphql", side_effect=fake_graphql):
            return wcl._get_top_dps("Slamster")

    def test_highest_dps_amount_wins_across_specs_and_sizes(self):
        result = self.get_top_dps_for_roster_warrior()
        self.assertTrue(result["found"])
        self.assertEqual(result["class"], "Warrior")
        # Fury 20-man's 2600 DPS beats Arms 40-man's 2400 despite a worse parse.
        best = result["best"]
        self.assertEqual(best["spec"], "Fury")
        self.assertEqual(best["size"], 20)
        self.assertEqual(best["dps"], 2600.0)
        self.assertEqual(best["rank_percent"], 71.9)
        self.assertEqual(best["encounter"], "Balnazzar")
        self.assertEqual(best["best_average"], 68.4)

    def test_unplayed_specs_are_skipped_and_candidates_sorted(self):
        result = self.get_top_dps_for_roster_warrior()
        # Protection's zero-amount placeholder rankings must not appear.
        self.assertEqual(
            [(c["spec"], c["size"], c["dps"]) for c in result["specs"]],
            [("Fury", 20, 2600.0), ("Fury", 40, 2500.0), ("Arms", 40, 2400.0)],
        )

    def test_per_boss_matrix_takes_the_best_of_any_spec_or_size(self):
        result = self.get_top_dps_for_roster_warrior()
        bosses = {b["encounter"]: b for b in result["bosses"]}
        # Beastmaster: Fury 40-man's 2500 beats Arms 40-man's 2400.
        self.assertEqual(
            (bosses["Beastmaster"]["dps"], bosses["Beastmaster"]["spec"],
             bosses["Beastmaster"]["size"], bosses["Beastmaster"]["rank_percent"]),
            (2500.0, "Fury", 40, 90.1),
        )
        self.assertEqual(
            (bosses["Balnazzar"]["dps"], bosses["Balnazzar"]["size"]), (2600.0, 20)
        )
        # Unkilled bosses stay in the list (for stable columns) with dps None.
        self.assertIsNone(bosses["Lillian Voss"]["dps"])
        # Zone order is preserved: first-seen encounter order across the blobs.
        self.assertEqual(
            [b["encounter"] for b in result["bosses"]],
            ["Beastmaster", "Lillian Voss", "Balnazzar"],
        )

    def test_spec_names_are_unspaced_in_the_rankings_query(self):
        # WCL's specName argument silently ignores the spaced display names
        # gameData reports ("Beast Mastery"), returning UNFILTERED rankings —
        # which misattributed Melee hunters' parses to Beast Mastery.
        query = wcl._build_spec_rankings_query(["Beast Mastery", "Melee"])
        self.assertIn('specName: "BeastMastery"', query)
        self.assertNotIn("Beast Mastery", query)

    def test_ranking_reported_spec_wins_over_requested_spec(self):
        # Even when a blob leaks parses from another spec, the parse's own
        # spec field (unspaced) is what gets displayed.
        character = {
            "s40_0": {
                "bestPerformanceAverage": 80.0,
                "rankings": [
                    {
                        "encounter": {"name": "Balnazzar"},
                        "bestAmount": 8369.0,
                        "rankPercent": 84.2,
                        "spec": "Melee",
                    }
                ],
            },
            "s20_0": None,
        }
        candidates = wcl._top_dps_candidates(character, ["Beast Mastery"])
        self.assertEqual(candidates[0]["spec"], "Melee")
        bosses = wcl._best_per_boss(character, ["Beast Mastery"])
        self.assertEqual(bosses[0]["spec"], "Melee")

    def test_unspaced_api_spec_names_display_with_spaces(self):
        self.assertEqual(wcl._spec_display("BeastMastery", None), "Beast Mastery")
        self.assertEqual(wcl._spec_display("Fury", None), "Fury")
        self.assertEqual(wcl._spec_display(None, "Arms"), "Arms")

    def test_unknown_character_reports_not_found(self):
        def fake_graphql(query, variables=None):
            self.assertEqual(query, wcl._CHAR_CLASS_BY_NAME_QUERY)
            self.assertEqual(variables["name"], "Nosuchtoon")
            return {"characterData": {"character": None}}

        with mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            wcl, "_fetch_all_raids", return_value=[]
        ), mock.patch.object(
            wcl, "get_guild_server", return_value=("wild-growth", "eu", None)
        ), mock.patch.object(wcl, "graphql", side_effect=fake_graphql):
            result = wcl._get_top_dps("Nosuchtoon")

        self.assertFalse(result["found"])
        self.assertIsNone(result["best"])


# Two SE reports (Irvh improves between them; a Beast Mastery hunter and a
# healer appear) plus a BWL report that must never be queried.
def complete_raid_report(irvh_amount):
    return {
        "reportData": {
            "report": {
                "rankings": {
                    "data": [
                        {
                            "fightID": 3,
                            "encounter": {"name": "Balnazzar"},
                            "size": 36,
                            "roles": {
                                "dps": {
                                    "characters": [
                                        {"name": "Ignored", "spec": "Fury", "amount": 1.0}
                                    ]
                                }
                            },
                        },
                        {
                            "fightID": 10000,
                            "encounter": {"name": "Scarlet Enclave"},
                            "size": 36,
                            "roles": {
                                "dps": {
                                    "characters": [
                                        {
                                            "name": "Irvh",
                                            "spec": "Melee",
                                            "amount": irvh_amount,
                                            "rankPercent": 76,
                                        },
                                        {
                                            "name": "Galbahunt",
                                            "spec": "BeastMastery",
                                            "amount": 11830.0,
                                            "rankPercent": 99,
                                        },
                                    ]
                                },
                                "healers": {
                                    "characters": [
                                        {
                                            "name": "Kradashdin",
                                            "spec": "Holy",
                                            "amount": 9686.0,
                                            "rankPercent": 93,
                                        }
                                    ]
                                },
                            },
                        },
                    ]
                }
            }
        }
    }


class CompleteRaidMapTests(SimpleTestCase):
    def test_map_takes_best_across_guild_se_logs_only(self):
        raids = [
            {"code": "SE1", "zone": "Scarlet Enclave", "start": 3, "present": set()},
            {"code": "BWL1", "zone": "Blackwing Lair", "start": 2, "present": set()},
            {"code": "SE2", "zone": "Scarlet Enclave", "start": 1, "present": set()},
        ]
        reports = {
            "SE1": complete_raid_report(8021.5),
            "SE2": complete_raid_report(7500.0),
        }
        queried = []

        def fake_rankings(code, metric="dps", force=False):
            queried.append(code)
            return reports[code]["reportData"]["report"]["rankings"], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            mapping = wcl._fetch_complete_raid_map()

        # Non-SE reports are never fetched.
        self.assertEqual(queried, ["SE1", "SE2"])
        # Best of Irvh's two clears wins; only fight 10000 counts.
        self.assertEqual(mapping["irvh"]["dps"], 8021.5)
        self.assertEqual(mapping["irvh"]["rank_percent"], 76)
        self.assertEqual(mapping["irvh"]["spec"], "Melee")
        self.assertNotIn("ignored", mapping)
        # 36 players files under the 40-man bracket.
        self.assertEqual(mapping["irvh"]["size"], 40)
        self.assertEqual(mapping["irvh"]["raid_size"], 36)
        # Unspaced API spec names are displayed spaced; healers are included.
        self.assertEqual(mapping["galbahunt"]["spec"], "Beast Mastery")
        self.assertEqual(mapping["kradashdin"]["dps"], 9686.0)


class TopDpsZoneTests(SimpleTestCase):
    """The Highest DPS lookup can be pointed at Naxxramas, not just the SE
    parse zone. Everything defaults to the parse zone when no zone is given."""

    def zone_queried(self, zone_id=None):
        """Drive _get_top_dps and report the zone its rankings query asked for."""
        seen = {}

        def fake_graphql(query, variables=None):
            if query == wcl._CHAR_CLASS_BY_ID_QUERY:
                return {
                    "characterData": {
                        "character": {"id": 7, "name": "Slamster", "classID": 11}
                    }
                }
            if query == wcl._CLASS_SPECS_QUERY:
                return CLASSES_PAYLOAD
            seen["zone"] = variables["zone"]
            return TOP_DPS_RANKINGS_PAYLOAD

        with mock.patch.object(
            wcl, "get_member_map", return_value={"slamster": {"id": 7, "name": "Slamster"}}
        ), mock.patch.dict(
            wcl._class_specs_cache, {"map": None}
        ), mock.patch.object(wcl, "graphql", side_effect=fake_graphql):
            wcl._get_top_dps("Slamster", zone_id)
        return seen["zone"]

    def test_rankings_query_defaults_to_the_parse_zone(self):
        self.assertEqual(self.zone_queried(), settings.PARSE_ZONE_ID)

    def test_rankings_query_uses_the_requested_zone(self):
        self.assertEqual(
            self.zone_queried(settings.NAXX_ZONE_ID), settings.NAXX_ZONE_ID
        )

    def test_cache_keys_are_zone_scoped(self):
        keys = []

        def fake_get_or_set(key, fn, force=False):
            keys.append(key)
            return {}, None

        with mock.patch.object(
            wcl.apicache, "get_or_set", side_effect=fake_get_or_set
        ):
            wcl.get_top_dps("Slamster")
            wcl.get_top_dps("Slamster", zone_id=settings.NAXX_ZONE_ID)
            wcl.get_complete_raid_map()
            wcl.get_complete_raid_map(zone_id=settings.NAXX_ZONE_ID)

        # Two zones must never share one cached entry for the same toon.
        self.assertEqual(keys[0], f"topdps3:{settings.PARSE_ZONE_ID}:slamster")
        self.assertEqual(keys[1], f"topdps3:{settings.NAXX_ZONE_ID}:slamster")
        self.assertNotEqual(keys[2], keys[3])

    def test_complete_raid_map_sweeps_only_the_requested_zone(self):
        raids = [
            {"code": "SE1", "zone": "Scarlet Enclave", "start": 3, "present": set()},
            {"code": "NX1", "zone": "Naxxramas", "start": 2, "present": set()},
        ]
        naxx_report = complete_raid_report(4242.0)
        # The pseudo-fight is named for its own zone.
        naxx_report["reportData"]["report"]["rankings"]["data"][1]["encounter"] = {
            "name": "Naxxramas"
        }
        queried = []

        def fake_rankings(code, metric="dps", force=False):
            queried.append(code)
            return naxx_report["reportData"]["report"]["rankings"], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            mapping = wcl._fetch_complete_raid_map("Naxxramas")

        # The Scarlet Enclave log is never even fetched.
        self.assertEqual(queried, ["NX1"])
        self.assertEqual(mapping["irvh"]["dps"], 4242.0)


def se_raid(code, when):
    """A guild SE raid row as _cached_all_raids returns it."""
    import datetime as dt

    start = dt.datetime(*when, 19, 0, tzinfo=dt.timezone.utc).timestamp() * 1000
    return {"code": code, "zone": "Scarlet Enclave", "start": start, "present": set()}


def fight(fid, name, roles, size=36, zone=2018):
    return {
        "fightID": fid,
        "size": size,
        "zone": zone,
        "encounter": {"name": name},
        "roles": roles,
    }


def chars(role, *entries):
    return {
        role: {
            "characters": [
                {"name": n, "class": k, "spec": s, "amount": a, "rankPercent": p}
                for n, k, s, a, p in entries
            ]
        }
    }


# Leaderboard fixture: two SE logs a reset week apart (plus a Naxx log that
# must be ignored). Slammer's overall comes from SE1's fight 10000; their best
# boss DPS from SE2; their best parse from SE1.
LEADERBOARD_RANKINGS = {
    ("SE1", "dps"): {
        "data": [
            fight(3, "Balnazzar", {
                **chars("dps", ("Slammer", "Warrior", "Fury", 2400.0, 90)),
                **chars("tanks", ("Tanky", "Druid", "Guardian", 900.0, 60)),
                **chars("healers", ("Healy", "Priest", "Holy", 300.0, 20)),
            }),
            fight(10000, "Scarlet Enclave", chars("dps", ("Slammer", "Warrior", "Fury", 2000.0, 85))),
        ]
    },
    ("SE1", "hps"): {
        "data": [
            fight(3, "Balnazzar", chars("healers", ("Healy", "Priest", "Holy", 1500.0, 88))),
        ]
    },
    ("SE2", "dps"): {
        "data": [
            fight(3, "Balnazzar", chars("dps", ("Slammer", "Warrior", "Arms", 2600.0, 80))),
        ]
    },
    ("SE2", "hps"): {"data": []},
}


class LeaderboardTests(SimpleTestCase):
    def aggregate(self):
        raids = [
            se_raid("SE1", (2026, 7, 9)),
            {"code": "NAXX1", "zone": "Naxxramas", "start": 1, "present": set()},
            se_raid("SE2", (2026, 7, 2)),
        ]

        def fake_rankings(code, metric="dps", force=False):
            self.assertNotEqual(code, "NAXX1")
            return LEADERBOARD_RANKINGS[(code, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(
            wcl, "get_member_map", return_value={"slammer": {"id": 1, "name": "Slammer"}}
        ), mock.patch.object(
            roster, "characters", return_value=[]
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            return wcl._aggregate_leaderboard()

    def test_rows_aggregate_across_logs(self):
        result = self.aggregate()
        self.assertEqual(result["raids_swept"], 2)
        rows = {r["name"]: r for r in result["players"]}

        slammer = rows["Slammer"]
        self.assertTrue(slammer["guildie"])
        self.assertEqual(slammer["role"], "DPS")
        # Overall only from fight 10000; best boss/parse only from boss fights.
        self.assertEqual(slammer["overall"]["dps"], 2000.0)
        self.assertEqual(slammer["best_boss"]["dps"], 2600.0)
        self.assertEqual(slammer["best_boss"]["spec"], "Arms")
        self.assertEqual(slammer["best_parse"], 90)
        # Two logs in different reset weeks -> two weeks attended.
        self.assertEqual(slammer["weeks"], 2)
        self.assertEqual(slammer["last_seen"], "2026-07-09")

        healy = rows["Healy"]
        self.assertFalse(healy["guildie"])
        self.assertEqual(healy["role"], "Healer")
        # HPS headline from the hps sweep, not their token dps figure.
        self.assertEqual(healy["best_hps"]["hps"], 1500.0)
        self.assertEqual(healy["best_hps"]["rank_percent"], 88)

    def test_grm_clusters_merge_attendance_across_alts(self):
        # Slammer raids week one, their GRM-listed alt raids week two — the
        # cluster count covers both while per-toon weeks stay honest.
        raids = [se_raid("SE1", (2026, 7, 9)), se_raid("SE2", (2026, 7, 2))]
        rankings = {
            ("SE1", "dps"): {
                "data": [fight(3, "Balnazzar",
                               chars("dps", ("Slammer", "Warrior", "Fury", 2400.0, 90)))]
            },
            ("SE1", "hps"): {"data": []},
            ("SE2", "dps"): {
                "data": [fight(3, "Balnazzar",
                               chars("dps", ("Slamalt", "Mage", "Fire", 1200.0, 50)))]
            },
            ("SE2", "hps"): {"data": []},
        }
        grm = [
            {"name": "Slammer", "alts": ["Slamalt"], "level": "60",
             "class": "Warrior", "main_or_alt": "Main"},
            {"name": "Slamalt", "alts": ["Slammer"], "level": "60",
             "class": "Mage", "main_or_alt": "Alt"},
        ]

        def fake_rankings(code, metric="dps", force=False):
            return rankings[(code, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            roster, "characters", return_value=grm
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            result = wcl._aggregate_leaderboard()

        rows = {r["name"]: r for r in result["players"]}
        self.assertEqual(rows["Slammer"]["weeks"], 1)
        self.assertEqual(rows["Slammer"]["cluster_weeks"], 2)
        self.assertEqual(rows["Slammer"]["cluster_toons"], ["Slamalt"])
        # The alt's row sees the same merged count, pointing back at the main.
        self.assertEqual(rows["Slamalt"]["cluster_weeks"], 2)
        self.assertEqual(rows["Slamalt"]["cluster_toons"], ["Slammer"])

    def test_toon_without_grm_data_keeps_its_own_weeks(self):
        result = self.aggregate()  # roster.characters mocked empty
        slammer = {r["name"]: r for r in result["players"]}["Slammer"]
        self.assertEqual(slammer["cluster_weeks"], slammer["weeks"])
        self.assertEqual(slammer["cluster_toons"], [])

    def test_other_zones_bundled_into_a_log_are_excluded(self):
        # Mixed logs carry fights (and even another zone's complete-raid
        # pseudo-fight) from other raids; neither may leak into SE standings.
        rankings = {
            ("SE1", "dps"): {
                "data": [
                    *LEADERBOARD_RANKINGS[("SE1", "dps")]["data"],
                    fight(50, "Patchwerk",
                          chars("dps", ("Slammer", "Warrior", "Fury", 99999.0, 100)),
                          zone=9999),
                    fight(10000, "Naxxramas",
                          chars("dps", ("Slammer", "Warrior", "Fury", 88888.0, 100)),
                          zone=9999),
                ]
            },
            ("SE1", "hps"): LEADERBOARD_RANKINGS[("SE1", "hps")],
        }

        def fake_rankings(code, metric="dps", force=False):
            return rankings[(code, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids",
            return_value=([se_raid("SE1", (2026, 7, 9))], None),
        ), mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            roster, "characters", return_value=[]
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            result = wcl._aggregate_leaderboard()

        slammer = {r["name"]: r for r in result["players"]}["Slammer"]
        self.assertEqual(slammer["best_boss"]["dps"], 2400.0)  # not Patchwerk
        self.assertEqual(slammer["overall"]["dps"], 2000.0)  # not Naxx overall

    def test_failing_report_is_skipped_not_fatal(self):
        raids = [se_raid("SE1", (2026, 7, 9)), se_raid("SE2", (2026, 7, 2))]

        def fake_rankings(code, metric="dps", force=False):
            if code == "SE2":
                raise wcl.WCLError("Warcraft Logs timed out")
            return LEADERBOARD_RANKINGS[(code, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            roster, "characters", return_value=[]
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            result = wcl._aggregate_leaderboard()

        self.assertEqual(result["raids_swept"], 1)
        self.assertEqual(result["raids_failed"], 1)
        rows = {r["name"]: r for r in result["players"]}
        # SE2's better boss DPS is absent, but SE1's data survived.
        self.assertEqual(rows["Slammer"]["best_boss"]["dps"], 2400.0)

    def test_sorted_by_overall_with_nulls_last(self):
        result = self.aggregate()
        names = [r["name"] for r in result["players"]]
        # Slammer has an overall; Tanky (900) and Healy (300) sort after by
        # best boss DPS.
        self.assertEqual(names, ["Slammer", "Tanky", "Healy"])


class ReportCardTests(SimpleTestCase):
    def build_card(self, code="SE1"):
        raids = [se_raid("SE1", (2026, 7, 9))]

        def fake_rankings(c, metric="dps", force=False):
            return LEADERBOARD_RANKINGS[(c, metric)], {"cached": False, "age": 0, "ttl": 1}

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            return wcl.get_report_card(code)

    def test_card_groups_roles_and_orders_fights(self):
        card, metas = self.build_card()
        self.assertEqual(card["zone"], "Scarlet Enclave")
        self.assertEqual(card["date"], "2026-07-09")
        # Bosses first, Overall (fight 10000) last.
        self.assertEqual([f["name"] for f in card["fights"]], ["Balnazzar", "Overall"])
        self.assertEqual([p["name"] for p in card["tanks"]], ["Tanky"])
        self.assertEqual([p["name"] for p in card["dps"]], ["Slammer"])
        self.assertEqual(len(metas), 2)
        # DPS cells carry dps + parse; keyed by fight id as strings (JSON-safe).
        cell = card["dps"][0]["cells"]["3"]
        self.assertEqual((cell["dps"], cell["dps_percent"]), (2400.0, 90))

    def test_healer_rows_merge_hps_onto_dps_cells(self):
        card, _ = self.build_card()
        healy = card["healers"][0]
        cell = healy["cells"]["3"]
        # dps figures from the dps sweep, hps figures folded in from hps.
        self.assertEqual(cell["dps"], 300.0)
        self.assertEqual(cell["hps"], 1500.0)
        self.assertEqual(cell["hps_percent"], 88)

    def test_unknown_code_returns_none(self):
        card, metas = self.build_card(code="NOPE")
        self.assertIsNone(card)
        self.assertIsNone(metas)


# Shockadin fixture. WCL files every Holy paladin under `healers`, so all three
# paladins below arrive in that bucket:
#   Shockà    — thousands of DPS, token healing        -> a shockadin
#   Realheal  — no damage to speak of, real healing    -> a genuine Holy healer
#   Dualbuck  — bucketed dps on one fight, healers on  -> one merged DPS row
#               another
SHOCKADIN_RANKINGS = {
    ("SHOCK", "dps"): {
        "data": [
            fight(3, "Balnazzar", {
                **chars(
                    "healers",
                    ("Shockà", "Paladin", "Holy", 9650.0, 98),
                    ("Realheal", "Paladin", "Holy", 31.0, 12),
                ),
                **chars("dps", ("Dualbuck", "Paladin", "Retribution", 5200.0, 74)),
            }),
            fight(4, "Beatrix", chars(
                "healers", ("Dualbuck", "Paladin", "Holy", 4800.0, 70)
            )),
            fight(10000, "Scarlet Enclave", chars(
                "healers",
                ("Shockà", "Paladin", "Holy", 8100.0, 96),
                ("Realheal", "Paladin", "Holy", 24.0, 10),
            )),
        ]
    },
    ("SHOCK", "hps"): {
        "data": [
            fight(3, "Balnazzar", chars(
                "healers",
                ("Shockà", "Paladin", "Holy", 203.0, 8),
                ("Realheal", "Paladin", "Holy", 2314.0, 91),
            )),
            fight(4, "Beatrix", chars(
                "healers", ("Dualbuck", "Paladin", "Holy", 260.0, 9)
            )),
        ]
    },
}


class ShockadinReportCardTests(SimpleTestCase):
    """SoD Holy paladins are shockadins — damage dealers WCL files as healers."""

    def build_card(self):
        raids = [se_raid("SHOCK", (2026, 7, 9))]

        def fake_rankings(c, metric="dps", force=False):
            return SHOCKADIN_RANKINGS[(c, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            card, _ = wcl.get_report_card("SHOCK")
        return card

    def test_damage_dealing_paladin_moves_to_dps_as_shockadin(self):
        card = self.build_card()
        self.assertIn("Shockà", [p["name"] for p in card["dps"]])
        self.assertNotIn("Shockà", [p["name"] for p in card["healers"]])
        row = next(p for p in card["dps"] if p["name"] == "Shockà")
        self.assertEqual(row["spec"], "Shockadin")
        # The damage figures travel with them.
        self.assertEqual(row["cells"]["3"]["dps"], 9650.0)

    def test_genuine_holy_healer_stays_a_healer(self):
        card = self.build_card()
        self.assertIn("Realheal", [p["name"] for p in card["healers"]])
        self.assertNotIn("Realheal", [p["name"] for p in card["dps"]])
        row = next(p for p in card["healers"] if p["name"] == "Realheal")
        self.assertEqual(row["spec"], "Holy")
        self.assertEqual(row["cells"]["3"]["hps"], 2314.0)

    def test_paladin_in_both_buckets_merges_into_one_dps_row(self):
        card = self.build_card()
        rows = [p for p in card["dps"] if p["name"] == "Dualbuck"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("Dualbuck", [p["name"] for p in card["healers"]])
        # Fight 3 came from the dps bucket, fight 4 from the healers bucket.
        cells = rows[0]["cells"]
        self.assertEqual(cells["3"]["dps"], 5200.0)
        self.assertEqual(cells["4"]["dps"], 4800.0)
        self.assertEqual(cells["4"]["hps"], 260.0)
        # Retribution is left alone; only "Holy" is rewritten.
        self.assertEqual(cells["3"]["spec"], "Retribution")
        self.assertEqual(cells["4"]["spec"], "Shockadin")


class ShockadinLeaderboardTests(SimpleTestCase):
    def aggregate(self, rankings, raids=None):
        raids = raids or [se_raid("SHOCK", (2026, 7, 9))]

        def fake_rankings(code, metric="dps", force=False):
            return rankings[(code, metric)], None

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(
            wcl, "get_member_map", return_value={}
        ), mock.patch.object(
            roster, "characters", return_value=[]
        ), mock.patch.object(wcl, "get_report_rankings", side_effect=fake_rankings):
            return wcl._aggregate_leaderboard()

    def test_shockadin_ranks_as_dps_and_healer_stays_healer(self):
        rows = {r["name"]: r for r in self.aggregate(SHOCKADIN_RANKINGS)["players"]}
        self.assertEqual(rows["Shockà"]["role"], "DPS")
        self.assertEqual(rows["Shockà"]["overall"]["spec"], "Shockadin")
        # Incidental healing is still recorded, it just doesn't set the role.
        self.assertEqual(rows["Shockà"]["best_hps"]["hps"], 203.0)
        self.assertEqual(rows["Realheal"]["role"], "Healer")
        self.assertEqual(rows["Realheal"]["best_hps"]["hps"], 2314.0)

    def test_shockadin_with_no_overall_still_ranks_as_dps(self):
        # Dovaah's shape from the live standings: absent from the complete-raid
        # fight, but a strong boss parse. Judged on best boss, not overall.
        rankings = {
            ("SHOCK", "dps"): {
                "data": [
                    fight(3, "Balnazzar", chars(
                        "healers", ("Dovaah", "Paladin", "Holy", 5883.0, 73)
                    )),
                ]
            },
            ("SHOCK", "hps"): {
                "data": [
                    fight(3, "Balnazzar", chars(
                        "healers", ("Dovaah", "Paladin", "Holy", 331.0, 15)
                    )),
                ]
            },
        }
        rows = {r["name"]: r for r in self.aggregate(rankings)["players"]}
        self.assertIsNone(rows["Dovaah"]["overall"])
        self.assertEqual(rows["Dovaah"]["role"], "DPS")
        self.assertEqual(rows["Dovaah"]["best_boss"]["spec"], "Shockadin")


# TestCase (not SimpleTestCase): the unlocked /softres page reads the
# recent-audits list from the database.
class PasswordGateTests(TestCase):
    def enter_password(self, password="carnage"):
        return self.client.post("/leaderboard", {"password": password})

    def test_gated_pages_show_the_password_form(self):
        for path in ("/leaderboard", "/reports", "/softres"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'name="password"')

    def test_wrong_password_is_rejected_without_a_cookie(self):
        response = self.enter_password("mercy")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Wrong password", status_code=403)
        self.assertNotIn(views.PW_COOKIE, response.cookies)

    def test_correct_password_sets_cookie_and_unlocks_both_pages(self):
        response = self.enter_password()
        self.assertRedirects(
            response, "/leaderboard", fetch_redirect_response=False
        )
        self.assertIn(views.PW_COOKIE, response.cookies)
        self.assertContains(self.client.get("/leaderboard"), "Roster Leaderboard")
        self.assertContains(self.client.get("/reports"), "After-Action Reports")
        self.assertContains(self.client.get("/softres"), "Soft-Reserve Audit")

    def test_tampered_cookie_is_ignored(self):
        self.client.cookies[views.PW_COOKIE] = "forged-token"
        response = self.client.get("/leaderboard")
        self.assertContains(response, 'name="password"')

    def test_apis_refuse_without_the_cookie(self):
        self.assertEqual(self.client.get("/api/leaderboard").status_code, 403)
        self.assertEqual(self.client.get("/api/reportcard").status_code, 403)
        self.assertEqual(self.client.get("/api/softres").status_code, 403)

    def test_apis_work_once_authenticated(self):
        self.enter_password()
        payload = (
            {"players": [], "raids_swept": 0, "raids_failed": 0, "zone": "SE"},
            {"cached": True, "age": 0, "ttl": 1},
        )
        with mock.patch.object(wcl, "get_leaderboard", return_value=payload):
            self.assertEqual(self.client.get("/api/leaderboard").status_code, 200)

    def test_index_and_its_apis_stay_open(self):
        with override_settings(ROSTER_FILE="/nonexistent/grm.csv"):
            self.assertEqual(self.client.get("/").status_code, 200)


class LeaderboardEndpointTests(SimpleTestCase):
    def setUp(self):
        self.client.post("/leaderboard", {"password": "carnage"})

    def test_returns_players_with_cache(self):
        payload = (
            {"players": [{"name": "Slammer"}], "raids_swept": 2, "zone": "Scarlet Enclave"},
            {"cached": True, "age": 120, "ttl": 3600},
        )
        with mock.patch.object(wcl, "get_leaderboard", return_value=payload):
            response = self.client.get("/api/leaderboard")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["players"][0]["name"], "Slammer")
        self.assertTrue(data["cache"]["cached"])

    def test_post_is_rejected(self):
        self.assertEqual(self.client.post("/api/leaderboard").status_code, 405)


class ReportCardEndpointTests(SimpleTestCase):
    def setUp(self):
        self.client.post("/leaderboard", {"password": "carnage"})

    def request_card(self, **params):
        raids = [
            se_raid("SE1", (2026, 7, 9)),
            {"code": "BWL1", "zone": "Blackwing Lair", "start": 1, "present": set()},
        ]
        card = {"code": "SE1", "zone": "Scarlet Enclave", "date": "2026-07-09",
                "fights": [], "tanks": [], "healers": [], "dps": []}

        def fake_card(code, force=False):
            if code != "SE1":
                return None, None
            return card, [{"cached": False, "age": 0, "ttl": 1}]

        with mock.patch.object(
            wcl, "_cached_all_raids", return_value=(raids, None)
        ), mock.patch.object(wcl, "get_report_card", side_effect=fake_card):
            return self.client.get("/api/reportcard", params)

    def test_defaults_to_newest_listed_report(self):
        response = self.request_card()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Non-SE/Naxx zones are excluded from the picker list.
        self.assertEqual([r["code"] for r in data["reports"]], ["SE1"])
        self.assertEqual(data["card"]["code"], "SE1")

    def test_unknown_code_is_a_404(self):
        self.assertEqual(self.request_card(code="NOPE").status_code, 404)

    def test_post_is_rejected(self):
        self.assertEqual(self.client.post("/api/reportcard").status_code, 405)


class TopDpsEndpointTests(SimpleTestCase):
    def test_returns_result_with_cache_meta(self):
        payload = (
            {
                "found": True,
                "name": "Slamster",
                "class": "Warrior",
                "best": {"dps": 2600.0, "spec": "Fury", "size": 20},
                "specs": [],
            },
            {"cached": True, "age": 60, "ttl": 3600},
        )
        overall = (
            {"dps": 8021.5, "spec": "Melee", "size": 40, "raid_size": 36, "rank_percent": 76},
            {"cached": False, "age": 0, "ttl": 3600},
        )
        with mock.patch.object(
            wcl, "get_top_dps", return_value=payload
        ), mock.patch.object(
            wcl, "get_complete_raid_best", return_value=overall
        ) as overall_mock:
            response = self.client.post(
                "/api/topdps",
                json.dumps({"name": "Slamster"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["best"]["spec"], "Fury")
        self.assertEqual(data["overall"]["dps"], 8021.5)
        self.assertTrue(data["cache"]["cached"])
        # The overall lookup uses the WCL-resolved name.
        self.assertEqual(overall_mock.call_args.args[0], "Slamster")

    def test_blank_name_is_rejected(self):
        response = self.client.post(
            "/api/topdps", json.dumps({"name": "  "}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def post_for_raid(self, raid):
        """POST a lookup for one raid; report the zone the view resolved."""
        payload = ({"found": True, "name": "Slamster", "best": None, "specs": []}, None)
        body = {"name": "Slamster"}
        if raid is not None:
            body["raid"] = raid
        with mock.patch.object(
            wcl, "get_top_dps", return_value=payload
        ) as top_mock, mock.patch.object(
            wcl, "get_complete_raid_best", return_value=(None, None)
        ) as overall_mock:
            response = self.client.post(
                "/api/topdps", json.dumps(body), content_type="application/json"
            )
        return response, top_mock, overall_mock

    def test_naxx_raid_resolves_the_naxx_zone(self):
        response, top_mock, overall_mock = self.post_for_raid("naxx")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["zone"], settings.NAXX_ZONE_NAME)
        self.assertEqual(top_mock.call_args.kwargs["zone_id"], settings.NAXX_ZONE_ID)
        self.assertEqual(
            overall_mock.call_args.kwargs["zone_name"], settings.NAXX_ZONE_NAME
        )

    def test_missing_or_unknown_raid_falls_back_to_scarlet_enclave(self):
        for raid in (None, "molten-core"):
            with self.subTest(raid=raid):
                response, top_mock, _ = self.post_for_raid(raid)
                self.assertEqual(response.json()["zone"], settings.PARSE_ZONE_NAME)
                self.assertEqual(
                    top_mock.call_args.kwargs["zone_id"], settings.PARSE_ZONE_ID
                )

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get("/api/topdps").status_code, 405)

    def test_wcl_error_maps_to_502(self):
        with mock.patch.object(
            wcl, "get_top_dps", side_effect=wcl.WCLError("api down")
        ):
            response = self.client.post(
                "/api/topdps",
                json.dumps({"name": "Slamster"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("api down", response.json()["error"])


def post_eligibility(client, main, toons, **extra):
    """POST an eligibility check with the external APIs stubbed out."""
    parse = (
        {
            "found": True,
            "best_average": 80.0,
            "size": 40,
            "sizes": {40: 80.0, 20: None},
            "top_parse": None,
            "rankings": [],
        },
        {"cached": False, "age": 0, "ttl": 1},
    )
    attendance = (
        {"distinct_weeks": 0, "window_start": "2026-06-10", "weeks": {}},
        {"cached": False, "age": 0, "ttl": 1},
    )
    with mock.patch.object(
        wcl, "get_guild_server", return_value=("wild-growth", "EU", "carnage")
    ), mock.patch.object(
        wcl, "get_best_parse", return_value=parse
    ), mock.patch.object(
        wcl, "get_attendance", return_value=attendance
    ), mock.patch.object(
        gear, "analyse_gear", return_value=({"found": False}, None)
    ):
        return client.post(
            "/api/eligibility",
            json.dumps({"main": main, "toons": toons, **extra}),
            content_type="application/json",
        )


class EligibilityFlagTests(SimpleTestCase):
    """The composite verdict flags encode the loot rules: tokens for anyone,
    set bonus for standard, the full battery for rare."""

    def test_flags_with_failing_attendance(self):
        # Stubbed fixture: parse 80% (passes), 0 attendance weeks, no armory.
        d = post_eligibility(self.client, "Newpug", []).json()
        self.assertTrue(d["token_eligible"])
        # Gear unverifiable doesn't block standard loot (it's flagged instead).
        self.assertTrue(d["standard_item_eligible"])
        # Rare still requires the 4 weeks.
        self.assertFalse(d["rare_item_eligible"])


@override_settings(ROSTER_FILE="/nonexistent/grm.csv")
class ToonLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ToonLink.objects.create(
            members=["Cameroncrown", "Magvirgo"], keys=["cameroncrown", "magvirgo"]
        )

    def test_eligibility_remembers_the_cluster(self):
        response = post_eligibility(self.client, "Newpug", ["Newalt"])
        self.assertEqual(response.status_code, 200)
        link = ToonLink.objects.get(keys__icontains="newpug")
        self.assertEqual(link.members, ["Newpug", "Newalt"])

    def test_bare_main_does_not_clobber_remembered_link(self):
        response = post_eligibility(self.client, "Cameroncrown", [])
        self.assertEqual(response.status_code, 200)
        link = ToonLink.objects.get(keys__icontains="cameroncrown")
        self.assertEqual(link.members, ["Cameroncrown", "Magvirgo"])

    def test_overlapping_cluster_is_replaced_not_duplicated(self):
        post_eligibility(self.client, "Magvirgo", ["Cameroncrown", "Thirdalt"])
        links = [
            l for l in ToonLink.objects.all() if "cameroncrown" in l.keys
        ]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].members, ["Magvirgo", "Cameroncrown", "Thirdalt"])

    def test_autosuggest_prefills_alts_for_remembered_main(self):
        data = self.client.get("/api/characters", {"q": "camer"}).json()
        self.assertEqual(data["matches"][0]["name"], "Cameroncrown")
        self.assertEqual(data["matches"][0]["alts"], ["Magvirgo"])
        self.assertTrue(data["matches"][0]["remembered"])

    def test_autosuggest_works_backwards_from_the_alt(self):
        data = self.client.get("/api/characters", {"q": "magv"}).json()
        self.assertEqual(data["matches"][0]["name"], "Magvirgo")
        self.assertEqual(data["matches"][0]["alts"], ["Cameroncrown"])

    def test_roster_match_takes_precedence_over_remembered_link(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "grm.csv"
        path.write_text(
            "\n".join(
                [
                    CSV_HEADER,
                    "Cameroncrown;Pug;60;Paladin;Human;Male;1;Main;"
                    "Grmalt-WildGrowth;20 Dec '25;20 Dec '25;;;;;;Alliance",
                ]
            ),
            encoding="utf-8-sig",
        )
        with override_settings(ROSTER_FILE=str(path)):
            data = self.client.get("/api/characters", {"q": "cameron"}).json()
        names = [m["name"] for m in data["matches"]]
        self.assertEqual(names.count("Cameroncrown"), 1)
        self.assertEqual(data["matches"][0]["alts"], ["Grmalt"])
        self.assertNotIn("remembered", data["matches"][0])

    def test_remember_false_leaves_the_link_table_untouched(self):
        # The SR audit checks whole rosters; it must not write clusters back.
        response = post_eligibility(
            self.client, "Newpug", ["Newalt"], remember=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ToonLink.objects.filter(keys__icontains="newpug").exists()
        )


class SoftresHelperTests(SimpleTestCase):
    def test_parse_raid_id_accepts_a_bare_id(self):
        self.assertEqual(softres.parse_raid_id(" 9b69QNaE "), "9b69QNaE")

    def test_parse_raid_id_accepts_softres_urls(self):
        for text in (
            "https://softres.it/raid/9b69QNaE",
            "softres.it/raid/9b69QNaE/",
            "https://softres.it/raid/9b69QNaE?foo=1",
        ):
            self.assertEqual(softres.parse_raid_id(text), "9b69QNaE", text)

    def test_parse_raid_id_rejects_garbage(self):
        for text in ("", "   ", "not a raid id", "https://example.com/raid/abc!!!"):
            self.assertIsNone(softres.parse_raid_id(text), text)

    def test_classify_covers_all_three_tiers(self):
        self.assertEqual(items.classify("Abandoned Experiment"), "rare")
        self.assertEqual(items.classify("Putress' Completed Diary"), "rare")
        self.assertEqual(items.classify("consecrated gauntlets"), "token")
        self.assertEqual(items.classify("Desecrated Bindings"), "token")
        self.assertEqual(items.classify("Scarlet Steed"), "standard")
        self.assertIsNone(items.classify(None))
        self.assertIsNone(items.classify("  "))

    def test_requirements_follow_the_loot_rules(self):
        # Tokens: nothing. Standard: set bonus. Rare: everything.
        self.assertEqual(
            items.search("Consecrated Gauntlets")["requires"],
            {"attendance": False, "set_bonus": False, "parse": False, "enchants": False},
        )
        self.assertEqual(
            items.search("Some Random Blue")["requires"],
            {"attendance": False, "set_bonus": True, "parse": False, "enchants": False},
        )
        self.assertEqual(
            items.search("Abandoned Experiment")["requires"],
            {"attendance": True, "set_bonus": True, "parse": True, "enchants": True},
        )

    def test_holy_paladins_are_checked_as_dps(self):
        # House rule: our holy paladins are shockadins — damage parse counts.
        self.assertNotIn(65, softres.HEALER_SPECS)
        self.assertIn(257, softres.HEALER_SPECS)  # holy priests still heal

    def test_extra_tokens_count_as_tokens(self):
        # House rule: Crusader's Chalice is a token despite the name.
        self.assertEqual(items.classify("Crusader's Chalice"), "token")
        result = items.search("chalice")
        self.assertEqual(result["item_type"], "token")
        self.assertEqual(result["token_raid"], "se")


# A softres sheet as /api/raid/<id> returns it: one melee stacking a rare ×2
# and holding a token that the healer also wants (contested), one healer with
# an item Wowhead can't resolve.
SOFTRES_RAID = {
    "id": "9b69QNaE",
    "faction": "alliance",
    "locked": False,
    "reserve_limit": 3,
    "raid_date": 1786042800,
    "creator": {"id": 1, "name": "dread_ful"},
    "instances": [{"slug": "scarletenclavesod", "name": "Scarlet Enclave"}],
    "reserves": [
        {
            "name": "Boliath",
            "spec": 254,
            "note": None,
            "items": [111, 111, 222],
            "user": {"name": ".czeq"},
        },
        {
            "name": "Compostel",
            "spec": 257,
            "note": "healer",
            "items": [333, 222],
            "user": None,
        },
    ],
}
SOFTRES_ITEM_NAMES = {111: "Abandoned Experiment", 222: "Consecrated Gauntlets"}


@override_settings(ROSTER_FILE="/nonexistent/grm.csv")
class SoftresEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ToonLink.objects.create(
            members=["Boliath", "Bolialt"], keys=["boliath", "bolialt"]
        )

    def setUp(self):
        self.client.post("/softres", {"password": "carnage"})

    def fetch(self, raid="9b69QNaE"):
        meta = {"cached": False, "age": 0, "ttl": 300}
        with mock.patch.object(
            softres, "get_raid", return_value=(SOFTRES_RAID, meta)
        ), mock.patch.object(
            softres, "item_name", side_effect=SOFTRES_ITEM_NAMES.get
        ):
            return self.client.get("/api/softres", {"raid": raid})

    def test_requires_the_password_cookie(self):
        from django.test import Client

        self.assertEqual(Client().get("/api/softres").status_code, 403)

    def test_reserves_carry_names_types_alts_and_healer_flag(self):
        data = self.fetch().json()
        self.assertEqual(data["raid"]["raid_type"], "se")
        self.assertEqual(data["raid"]["instance"], "Scarlet Enclave")
        self.assertEqual(data["raid"]["creator"], "dread_ful")

        melee, healer = data["reserves"]
        self.assertEqual(melee["name"], "Boliath")
        self.assertFalse(melee["healer"])
        self.assertEqual(melee["discord"], ".czeq")
        # Remembered link supplies the alt for player-wide attendance.
        self.assertEqual(melee["alts"], ["Bolialt"])
        self.assertEqual(
            [(i["name"], i["type"]) for i in melee["items"]],
            [
                ("Abandoned Experiment", "rare"),
                ("Abandoned Experiment", "rare"),
                ("Consecrated Gauntlets", "token"),
            ],
        )
        # Stacking an item ×2 yourself doesn't contest it; sharing one with
        # another reserver does.
        self.assertEqual([i["contested"] for i in melee["items"]], [False, False, True])

        self.assertTrue(healer["healer"])  # spec 257 = holy priest
        # Unresolvable item degrades to name/type None, not an error.
        self.assertEqual(
            healer["items"][0],
            {"id": 333, "name": None, "type": None, "contested": False},
        )
        self.assertTrue(healer["items"][1]["contested"])

    def test_accepts_a_full_softres_url(self):
        response = self.fetch(raid="https://softres.it/raid/9b69QNaE")
        self.assertEqual(response.status_code, 200)

    def test_unparseable_reference_is_a_400(self):
        response = self.client.get("/api/softres", {"raid": "not a raid id"})
        self.assertEqual(response.status_code, 400)

    def test_fetch_upserts_the_recent_audit_list(self):
        self.fetch()
        self.fetch()
        audits = SoftresAudit.objects.filter(raid_id="9b69QNaE")
        self.assertEqual(audits.count(), 1)
        audit = audits.get()
        self.assertEqual(audit.instance, "Scarlet Enclave")
        self.assertEqual(audit.reserve_count, 2)
        self.assertEqual(audit.raid_date, 1786042800)

    def test_page_lists_recent_audits(self):
        SoftresAudit.objects.create(
            raid_id="oldRaid1", instance="Naxxramas", reserve_count=25
        )
        response = self.client.get("/softres")
        self.assertContains(response, "oldRaid1")
        self.assertContains(response, "Naxxramas")

    def test_softres_failure_maps_to_502(self):
        with mock.patch.object(
            softres, "get_raid", side_effect=softres.SoftresError("softres down")
        ):
            response = self.client.get("/api/softres", {"raid": "9b69QNaE"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("softres down", response.json()["error"])


@override_settings(ROSTER_FILE="/nonexistent/grm.csv")
class KnownAltsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ToonLink.objects.create(
            members=["Cameroncrown", "Magvirgo"], keys=["cameroncrown", "magvirgo"]
        )

    def test_roster_row_wins_and_excludes_self(self):
        chars = [
            {
                "name": "Shapíe",
                "level": "60",
                "class": "Druid",
                "main_or_alt": "Main",
                "alts": ["Shapíe", "Akabow"],
            }
        ]
        with mock.patch.object(roster, "characters", return_value=chars):
            self.assertEqual(views.known_alts("shapie"), ["Akabow"])

    def test_falls_back_to_remembered_links(self):
        self.assertEqual(views.known_alts("MAGVIRGO"), ["Cameroncrown"])

    def test_unknown_name_has_no_alts(self):
        self.assertEqual(views.known_alts("Stranger"), [])
        self.assertEqual(views.known_alts(""), [])
