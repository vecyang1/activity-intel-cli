"""The 2026-08-27 additions: the robots override, currency, scope, and matching.

Every test here guards a property that produced a *confident wrong answer*
during the build rather than an error, which is the only reason each one exists:

  * a source that turns on by accident,
  * an override that happens silently,
  * HK$236 ranked against $33 as if they were the same unit,
  * a city filter that deleted a third of the catalogue,
  * a matcher that decides every Hanoi tour is the same product.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import io
import unittest
from contextlib import redirect_stderr

from activityintel import cli, config, model, places, robots
from activityintel.model import RATED, Activity
from activityintel.sources import airbnb, klook, viator


class SourcePolicy(unittest.TestCase):
    """Klook must be reachable ONLY through an explicit operator choice."""

    def test_klook_is_off_under_default_policy(self):
        self.assertFalse(klook.available())
        self.assertFalse(cli.source_available(klook))
        self.assertNotIn(klook.NAME, cli.enabled_sources())

    def test_klook_turns_on_with_the_override_and_only_that(self):
        self.assertTrue(klook.available(ignore_robots=True))
        self.assertIn(klook.NAME, cli.enabled_sources(ignore_robots=True))

    def test_the_module_constant_still_reports_default_policy(self):
        """`AVAILABLE` is what a reader and the doctor check key on."""
        self.assertFalse(klook.AVAILABLE)
        self.assertTrue(klook.REQUIRES_ROBOTS_OVERRIDE)

    def test_override_does_not_leak_into_other_sources(self):
        """--ignore-robots must not conjure a Viator key or change Airbnb."""
        self.assertTrue(cli.source_available(airbnb, ignore_robots=False))
        self.assertEqual(cli.source_available(viator, ignore_robots=True),
                         viator.available())

    def test_unavailable_reason_names_the_flag_that_lifts_it(self):
        """A refusal that does not say how to proceed is a dead end."""
        self.assertIn("--ignore-robots", klook.UNAVAILABLE_REASON)

    def test_reason_states_the_403_is_not_lifted_by_the_flag(self):
        """robots is a directive; the Akamai block is not, and stays refused."""
        self.assertIn("403", klook.UNAVAILABLE_REASON)


class RobotsOverrideIsLoud(unittest.TestCase):
    def _gate(self, enabled, sink):
        return robots.RobotsGate(lambda url: "", enabled=enabled,
                                 warn=lambda m: sink.append(m))

    def test_disabled_gate_announces_the_host_it_is_skipping(self):
        sink = []
        self._gate(False, sink).check("https://www.klook.com/v1/search/x")
        self.assertEqual(len(sink), 1)
        self.assertIn("www.klook.com", sink[0])
        self.assertIn("OVERRIDE", sink[0])

    def test_it_announces_once_per_host_not_once_per_request(self):
        """A warning that fires 400 times gets scrolled past, i.e. muted."""
        sink = []
        gate = self._gate(False, sink)
        for i in range(5):
            gate.check(f"https://www.klook.com/v1/search/{i}")
        gate.check("https://www.airbnb.com/api/v3/x")
        self.assertEqual(len(sink), 2)

    def test_enabled_gate_says_nothing_on_the_happy_path(self):
        """A warning that fires on healthy input is a warning people disable."""
        sink = []
        gate = robots.RobotsGate(lambda url: "User-agent: *\nAllow: /",
                                 enabled=True, warn=lambda m: sink.append(m))
        gate.check("https://example.com/ok")
        self.assertEqual(sink, [])

    def test_override_records_hosts_for_the_caller_to_report(self):
        gate = self._gate(False, [])
        gate.check("https://www.klook.com/x")
        self.assertEqual(gate.overridden_hosts, {"www.klook.com"})


class OverrideIsNoWiderThanItsNeed(unittest.TestCase):
    """--ignore-robots must exempt Klook and NOTHING else.

    The first version flipped the whole gate off, so a flag passed to reach
    Klook also stopped checking Airbnb — whose `/api/v3/` path is *allowed* and
    needed no exemption, and whose `/s/*/*` path is the one an earlier version
    of this tool fetched by mistake. A global override retires the guard that
    would catch the next unrelated bug.
    """

    AIRBNB_ROBOTS = "User-agent: *\nDisallow: /s/*/*\nAllow: /api/v3/\n"

    def test_no_flag_exempts_nothing(self):
        self.assertEqual(cli.override_hosts(False), frozenset())

    def test_the_flag_exempts_only_sources_that_declare_they_need_it(self):
        hosts = cli.override_hosts(True)
        self.assertEqual(hosts, frozenset({klook.HOST}))
        self.assertNotIn(airbnb.HOST, hosts)

    def test_airbnbs_disallowed_path_is_STILL_refused_under_the_override(self):
        """The property that a global switch destroyed."""
        gate = robots.RobotsGate(lambda url: self.AIRBNB_ROBOTS,
                                 exempt_hosts=cli.override_hosts(True),
                                 warn=lambda m: None)
        with self.assertRaises(robots.Disallowed):
            gate.check(f"https://{airbnb.HOST}/s/Hanoi--Vietnam/experiences")

    def test_airbnbs_allowed_path_still_passes_under_the_override(self):
        gate = robots.RobotsGate(lambda url: self.AIRBNB_ROBOTS,
                                 exempt_hosts=cli.override_hosts(True),
                                 warn=lambda m: None)
        gate.check(f"https://{airbnb.HOST}/api/v3/ExperiencesSearch/x")  # no raise

    def test_klook_is_exempt_and_announced_airbnb_is_neither(self):
        sink = []
        gate = robots.RobotsGate(lambda url: self.AIRBNB_ROBOTS,
                                 exempt_hosts=cli.override_hosts(True),
                                 warn=lambda m: sink.append(m))
        gate.check(f"https://{klook.HOST}/v1/search/x")
        gate.check(f"https://{airbnb.HOST}/api/v3/ExperiencesSearch/x")
        self.assertEqual(len(sink), 1, sink)
        self.assertIn(klook.HOST, sink[0])
        self.assertEqual(gate.overridden_hosts, {klook.HOST})

    def test_coverage_names_the_exempted_hosts_not_just_a_boolean(self):
        """'an override was active' and 'which sites' are different facts."""
        self.assertEqual(sorted(cli.override_hosts(True)), [klook.HOST])

    def test_the_cli_actually_WIRES_the_per_host_gate(self):
        """The connection, not the two parts.

        A mutation run caught this gap: `override_hosts` and `RobotsGate` were
        both tested and both correct, while `_client` could have gone back to
        `enabled=not ignore_robots` — a global kill switch — and every test
        stayed green. Testing two components does not test the wire between
        them, and the wire is where this defect lived in the first place.
        """
        args = cli.build_parser().parse_args(["catalog", "hanoi", "--ignore-robots"])
        client, conn = cli._client(args)
        try:
            gate = client.robots
            self.assertTrue(gate.enabled,
                            "the gate must stay ENABLED; only named hosts are exempt")
            self.assertEqual(set(gate.exempt_hosts), {klook.HOST})
            self.assertNotIn(airbnb.HOST, gate.exempt_hosts)
        finally:
            conn.close()

    def test_without_the_flag_the_cli_exempts_nothing(self):
        args = cli.build_parser().parse_args(["catalog", "hanoi"])
        client, conn = cli._client(args)
        try:
            self.assertTrue(client.robots.enabled)
            self.assertEqual(set(client.robots.exempt_hosts), set())
        finally:
            conn.close()


class CurrencyNormalization(unittest.TestCase):
    """Klook is hard-pinned to HKD; a mixed-currency ranking is a wrong answer."""

    def test_hkd_converts_using_the_pegged_rate(self):
        self.assertAlmostEqual(model.to_usd(236.0, "HKD"), 30.26, places=2)

    def test_usd_passes_through_unchanged(self):
        self.assertEqual(model.to_usd(33.0, "USD"), 33.0)

    def test_unknown_currency_refuses_rather_than_guessing(self):
        """Neither the raw number nor 0.0 — both are confidently wrong."""
        self.assertIsNone(model.to_usd(500.0, "XYZ"))
        self.assertIsNone(model.to_usd(500.0, None))

    def test_absent_price_stays_absent(self):
        self.assertIsNone(model.to_usd(None, "HKD"))

    def test_native_price_is_never_overwritten_by_the_conversion(self):
        a = Activity("klook", "1", "x", price_amount=236.0, price_currency="HKD")
        d = a.to_dict()
        self.assertEqual((d["price_amount"], d["price_currency"]), (236.0, "HKD"))
        self.assertAlmostEqual(d["price_usd"], 30.26, places=2)

    def test_hkd_rate_stays_inside_the_hkma_peg_band(self):
        """The one thing that makes pinning this rate in source defensible."""
        rate, pegged = config.FX_TO_USD["HKD"]
        self.assertTrue(pegged)
        self.assertTrue(7.75 <= rate <= 7.85, rate)

    def test_price_sort_orders_by_cost_not_by_currency(self):
        """HK$100 (~$12.8) is cheaper than $30; sorting raw numbers says otherwise."""
        cheap_hkd = Activity("klook", "1", "cheap", price_amount=100.0,
                             price_currency="HKD")
        dearer_usd = Activity("airbnb", "2", "dear", price_amount=30.0,
                              price_currency="USD")
        order = cli._sorted([dearer_usd, cheap_hkd], "price")
        self.assertEqual([a.title for a in order], ["cheap", "dear"])

    def test_unconvertible_price_sorts_last_not_free(self):
        known = Activity("airbnb", "1", "known", price_amount=99.0,
                         price_currency="USD")
        opaque = Activity("klook", "2", "opaque", price_amount=1.0,
                          price_currency="XYZ")
        order = cli._sorted([opaque, known], "price")
        self.assertEqual([a.title for a in order], ["known", "opaque"])

    def test_fx_note_is_silent_while_the_pins_are_fresh(self):
        self.assertIsNone(model.fx_note())


class PlaceScope(unittest.TestCase):
    """The filter that deleted 350 of 1,068 rows before this existed."""

    def setUp(self):
        self.hanoi = places.PLACES["hanoi"]

    def _a(self, title, city=None):
        return Activity("klook", "x", title, city=city)

    def test_day_trip_titled_from_hanoi_is_kept_despite_its_city_field(self):
        """The exact regression: city_name is the destination, not the traveller."""
        a = self._a("Hoa Lu, Tam Coc and Mua Cave Day Tour from Hanoi", city="Hoa Lu")
        self.assertEqual(self.hanoi.scope_of(a), places.IN_CITY)

    def test_alternate_spelling_counts_as_in_city(self):
        a = self._a("Ninh Binh Day Tour from Ha Noi", city="Ninh Binh")
        self.assertEqual(self.hanoi.scope_of(a), places.IN_CITY)

    def test_declared_day_trip_destination_is_kept_without_the_city_name(self):
        a = self._a("Hercules Grand Luxury Day Cruise: Ha Long Bay", city="Ha Long")
        self.assertEqual(self.hanoi.scope_of(a), places.DAY_TRIP)

    def test_day_trip_matches_on_title_when_the_operator_city_is_obscure(self):
        """'La Regina Cruise: Ha Long Bay' registers in Cam Pha."""
        a = self._a("La Regina Classic 2D1N Cruise: Ha Long Bay", city="Cam Pha")
        self.assertEqual(self.hanoi.scope_of(a), places.DAY_TRIP)

    def test_a_different_city_is_refused(self):
        """Klook answers every query with something; this is the something."""
        self.assertIsNone(self.hanoi.scope_of(self._a("Seoul Kimchi Class", city="Seoul")))
        self.assertIsNone(self.hanoi.scope_of(
            self._a("Alluvia Chocolate Souvenir", city="Ho Chi Minh")))

    def test_nationwide_products_are_refused(self):
        self.assertIsNone(self.hanoi.scope_of(
            self._a("eSIM for Vietnam (QR code activation)", city="Vietnam")))

    def test_scope_can_return_none_at_all(self):
        """A predicate that never says no is a constant wearing a filter's name."""
        rows = [self._a("Seoul tour", city="Seoul"),
                self._a("Hanoi tour", city="Hanoi")]
        verdicts = {self.hanoi.scope_of(a) for a in rows}
        self.assertIn(None, verdicts)
        self.assertIn(places.IN_CITY, verdicts)


class CrossSourceMatching(unittest.TestCase):
    def _a(self, source, sid, title, usd=None, rating=None, count=None):
        return Activity(source, sid, title, price_amount=usd,
                        price_currency="USD" if usd is not None else None,
                        rating=rating, review_count=count,
                        rating_state=RATED if rating else model.UNKNOWN)

    def test_finds_the_measured_live_pair_and_prices_the_gap(self):
        rows = [
            self._a("klook", "843", "Ninh Binh Day Tour from Ha Noi", 33.46, 4.8, 2862),
            self._a("airbnb", "7142008",
                    "Private Highlight Ninh Binh Day Trip From Hanoi", 161.0, 5.0, 1),
        ]
        groups = model.find_cross_source_matches(rows)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["cheapest_source"], "klook")
        self.assertAlmostEqual(g["spread_usd"], 127.54, places=2)

    def test_generic_hanoi_titles_do_not_collapse_into_one_product(self):
        """Without stripping city/category words, everything matches everything."""
        rows = [
            self._a("klook", "1", "Hanoi Private Day Tour with Local Guide"),
            self._a("airbnb", "2", "Hanoi Full Day Tour with a Local Guide"),
        ]
        self.assertEqual(model.find_cross_source_matches(rows), [])

    def test_a_single_shared_word_is_not_a_match(self):
        rows = [self._a("klook", "1", "Hanoi Street Food Market Walk"),
                self._a("airbnb", "2", "Hanoi Ceramic Market Pottery Workshop")]
        self.assertEqual(model.find_cross_source_matches(rows), [])

    def test_min_shared_tokens_is_load_bearing_where_the_threshold_is_not(self):
        """Isolates MIN_SHARED_TOKENS, which the test above does not reach.

        A mutation run caught that: setting the floor to 1 escaped, because the
        case above is rejected by the *similarity threshold* long before the
        token count matters. Short titles are where the floor actually earns its
        keep — strip the generic words from these two and each has exactly one
        distinctive token, so they score a perfect 1.0 on a single coincidence.
        """
        rows = [self._a("klook", "1", "Hanoi Knife Workshop"),
                self._a("airbnb", "2", "Hanoi Knife Class")]
        self.assertEqual(model.title_similarity(rows[0].title, rows[1].title), 1.0)
        self.assertEqual(
            len(model.title_tokens(rows[0].title) & model.title_tokens(rows[1].title)), 1)
        self.assertEqual(model.find_cross_source_matches(rows), [],
                         "a single shared word cleared the bar because its "
                         "similarity was perfect — that is what the floor is for")

    def test_same_source_duplicates_are_never_paired(self):
        """Two Klook listings tell a traveller nothing about where to book."""
        rows = [self._a("klook", "1", "Hanoi Knife Making Masterclass"),
                self._a("klook", "2", "Hanoi Knife Making Masterclass Private")]
        self.assertEqual(model.find_cross_source_matches(rows), [])

    def test_members_are_reported_never_merged(self):
        """Merging would delete the price and review count that make it useful."""
        rows = [self._a("klook", "1", "Hanoi Knife Making Masterclass", 62.95),
                self._a("airbnb", "2", "Hanoi Knife Making Original Masterclass", 46.0)]
        g = model.find_cross_source_matches(rows)[0]
        self.assertEqual(len(g["members"]), 2)
        self.assertEqual({m["source"] for m in g["members"]}, {"klook", "airbnb"})

    def test_missing_price_yields_no_spread_rather_than_zero(self):
        """A 0 spread reads as 'same price on both', which is a different claim."""
        rows = [self._a("klook", "1", "Hanoi Knife Making Masterclass", 62.95),
                self._a("airbnb", "2", "Hanoi Knife Making Original Masterclass", None)]
        g = model.find_cross_source_matches(rows)[0]
        self.assertIsNone(g["spread_usd"])
        self.assertIsNone(g["cheapest_source"])

    def test_untitled_rows_never_match(self):
        rows = [self._a("klook", "1", ""), self._a("airbnb", "2", "")]
        self.assertEqual(model.find_cross_source_matches(rows), [])

    def test_stopword_list_holds_only_ascii_tokens(self):
        """A stray non-ASCII literal here is invisible on screen and never fires."""
        for word in model._GENERIC_TITLE_WORDS:
            self.assertTrue(word.isascii(), word)


class CompareCommandGuards(unittest.TestCase):
    def test_compare_refuses_with_a_single_source(self):
        """'no cross-listings' and 'we only looked at one place' must differ."""
        args = cli.build_parser().parse_args(["compare", "hanoi"])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.cmd_compare(args)
        self.assertNotEqual(rc, 0)
        self.assertIn("two enabled sources", err.getvalue())

    def test_compare_accepts_the_documented_flags(self):
        args = cli.build_parser().parse_args(
            ["compare", "hanoi", "--ignore-robots", "--threshold", "0.3", "--json"])
        self.assertTrue(args.ignore_robots)
        self.assertEqual(args.threshold, 0.3)

    def test_sources_choices_include_the_override_only_source(self):
        """klook must be selectable, or --sources klook is an argparse error."""
        args = cli.build_parser().parse_args(
            ["catalog", "hanoi", "--sources", "klook", "--ignore-robots"])
        self.assertEqual(args.sources, ["klook"])

    def test_activity_field_map_matches_the_dataclass(self):
        """compare round-trips dicts back into Activity; a drifted field set
        would silently drop columns rather than raise."""
        d = Activity("klook", "1", "x").to_dict()
        rebuilt = model.Activity(**{k: v for k, v in d.items()
                                    if k in cli._ACTIVITY_FIELDS})
        self.assertEqual(rebuilt.key, "klook:1")
        self.assertTrue(cli._ACTIVITY_FIELDS <= set(d))

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
