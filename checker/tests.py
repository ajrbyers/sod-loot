"""Regression tests for the roster autosuggest (GRM CSV export parsing) and
the Warcraft Logs lookup chain."""
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from checker import roster, wcl

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


class RosterTests(SimpleTestCase):
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
