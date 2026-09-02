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
import csv
import io
import json
import argparse
import contextlib
import pathlib
import unittest
from contextlib import redirect_stderr

from activityintel import cli, config, exit_codes, model, places, render, robots
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


class VerticalFilterIsWired(unittest.TestCase):
    """The parts can both be right while the wire between them is missing.

    `split_verticals` is unit-tested above and `cmd_catalog` is where it has to
    be called. This drives the real command with a real captured payload and
    asserts on what a caller actually receives, because a filter that exists and
    is never invoked looks exactly like a filter that works.
    """

    FIX = pathlib.Path(__file__).parent / "fixtures" / "klook_search_mixed_verticals.json"

    class _Conn:
        closed = False

        def close(self):
            self.closed = True

    class _Client:
        """Serves the same mixed page once per query, then an empty one.

        Empty-second-page rather than repeating: `sweep` stops on a short page,
        and a source that answers every page identically would page to the
        ceiling and report `capped`, which is a different test.
        """

        EMPTY = '{"success": true, "result": {"search_result": '\
                '{"cards": [], "total": 37}}}'

        def __init__(self, body):
            self.body, self.seen = body, set()
            self.requests_sent = self.cache_hits = 0
            # Recorded even though nothing asserts on it yet: a double that
            # accepts a keyword and drops it makes that keyword untestable
            # while reading as covered.
            self.ttls = []

        def _clock(self):
            return 0.0

        def get(self, url, *, ttl_s=None):
            if url in self.seen:
                raise AssertionError("same URL fetched twice")
            self.seen.add(url)
            self.ttls.append(ttl_s)
            return self.body if "&start=1&" in url else self.EMPTY

    def setUp(self):
        body = self.FIX.read_text(encoding="utf-8")
        self._orig = cli._client
        client = self._Client(body)
        cli._client = lambda args: (client, self._Conn())
        self.addCleanup(lambda: setattr(cli, "_client", self._orig))

    def _run(self):
        args = argparse.Namespace(
            city="hanoi", sources=["klook"], ignore_robots=True, json=False,
            limit=0, sort="score", size=50, max_pages=2, lang="en_US", gap=None,
            language=None, cache_only=False, categories=None, match=None, gap_s=None)
        rc, payload = cli.cmd_catalog(args, emit=False)
        return rc, payload

    def test_no_hotel_reaches_the_catalogue(self):
        _, payload = self._run()
        urls = [a.get("url") or "" for a in payload["activities"]]
        self.assertTrue(urls, "fixture produced no rows at all")
        self.assertEqual([u for u in urls if "/hotels/" in u], [])
        self.assertTrue(all(a["vertical"] == "ttd" for a in payload["activities"]))

    def test_coverage_reports_the_drop_instead_of_shrinking_silently(self):
        """A smaller number with no explanation is indistinguishable from a
        small market. The count and the reason both have to survive."""
        _, payload = self._run()
        kl = payload["coverage"]["sources"]["klook"]
        self.assertEqual(kl["dropped_not_activity"], {"hotel": 22})
        self.assertIn("not activities", kl["note"])
        self.assertIn("hotel: 22", kl["note"])

    def test_returned_counts_only_what_the_caller_got(self):
        _, payload = self._run()
        kl = payload["coverage"]["sources"]["klook"]
        self.assertEqual(kl["returned"], len(payload["activities"]))


class PlaceQueryPartition(unittest.TestCase):
    """Klook has no location parameter, so the city lives in the keyword.

    A bare `"cooking class"` returns the planet, and Klook never says "no
    results" — it answers every query with confident, unrelated listings. The
    scope filter would then discard almost all of them, and the run would look
    like a small market rather than a misdirected query.
    """

    def test_every_klook_query_carries_a_term_that_names_the_place(self):
        graded, offenders = 0, []
        for place in places.PLACES.values():
            terms = place.match_terms or (place.klook_query.lower(),)
            for q in (place.klook_query,) + place.extra_queries:
                graded += 1
                if not any(t in q.lower() for t in terms):
                    offenders.append(f"{place.key}: {q!r}")
        self.assertEqual(offenders, [],
                         "queries with no place term — Klook will answer them "
                         "with the whole planet:\n  " + "\n  ".join(offenders))
        print(f"[place-queries] graded {graded} Klook keywords")

    def test_no_duplicate_queries(self):
        """A repeated keyword is a silent extra request on every run."""
        for place in places.PLACES.values():
            qs = (place.klook_query,) + place.extra_queries
            dupes = [q for q in set(qs) if qs.count(q) > 1]
            self.assertEqual(dupes, [], f"{place.key} repeats {dupes}")

    def test_every_declared_day_trip_destination_is_actually_reachable(self):
        """Declaring a destination in `day_trip_cities` only makes a listing
        *acceptable*; it does not make one *arrive*.

        The partition is not required to name every destination — the broad
        keyword reaches many — so this asserts the weaker, decidable property
        that the two lists have not drifted apart entirely, and prints the
        destinations no keyword mentions so the gap stays visible rather than
        being rediscovered by measurement a year from now.
        """
        for place in places.PLACES.values():
            blob = " ".join((place.klook_query,) + place.extra_queries).lower()
            unreached = [d for d in place.day_trip_cities if d not in blob]
            reached = len(place.day_trip_cities) - len(unreached)
            self.assertGreater(
                reached, 0,
                f"{place.key}: not one declared day-trip destination appears in "
                f"any query — the filter accepts what nothing fetches")
            print(f"[day-trips] {place.key}: {reached}/"
                  f"{len(place.day_trip_cities)} destinations named by a keyword; "
                  f"the rest rely on the broad query: {', '.join(unreached[:8])}"
                  f"{' …' if len(unreached) > 8 else ''}")


class CsvOutputKeepsTheThreeStates(unittest.TestCase):
    """A CSV is where three-state fields go to become two-state.

    Every rule this project has about `absent is not zero` lives in the JSON
    payload; flattening to a spreadsheet is exactly the step that turns
    `rating: null, rating_state: "unrated"` into a `0` a reader sorts on. So
    the flattener is tested for the empty cell, not just for the happy row.
    """

    def _payload(self):
        rows = [
            model.Activity(source="klook", source_id="1", title="rated thing",
                           vertical="ttd", price_amount=261.0, price_currency="HKD",
                           price_display="HK$ 261", rating=4.8, review_count=2868,
                           rating_state=model.RATED, tags=("English guided",),
                           languages=("en",)),
            model.Activity(source="airbnb", source_id="2", title="brand new thing",
                           vertical="experience", price_amount=20.0,
                           price_currency="USD", rating=None, review_count=0,
                           rating_state=model.UNRATED),
            model.Activity(source="klook", source_id="3", title="unpriceable thing",
                           vertical="ttd", price_amount=99.0, price_currency="XXX",
                           rating_state=model.UNKNOWN),
        ]
        return {"city": "Hanoi", "activities": [a.to_dict() for a in rows],
                "coverage": {"complete": False, "note": "a truncated sweep"}}

    def _csv(self):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(err):
            render._render_csv(self._payload())
        return list(csv.DictReader(io.StringIO(buf.getvalue()))), err.getvalue()

    def test_one_row_per_activity_plus_a_header(self):
        rows, _ = self._csv()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["title"] for r in rows],
                         ["rated thing", "brand new thing", "unpriceable thing"])

    def test_an_unrated_row_has_an_EMPTY_rating_not_a_zero(self):
        rows, _ = self._csv()
        self.assertEqual(rows[1]["rating"], "")
        self.assertEqual(rows[1]["rating_state"], model.UNRATED)
        self.assertEqual(rows[2]["rating"], "")
        self.assertEqual(rows[2]["rating_state"], model.UNKNOWN)

    def test_a_stated_zero_review_count_is_not_the_same_cell_as_an_unknown_one(self):
        """`0` and blank are two answers. UNRATED means the source said nobody
        has reviewed it; UNKNOWN means the source said nothing. Collapsing them
        is the same defect as a `0.0` rating, one column to the right."""
        rows, _ = self._csv()
        self.assertEqual(rows[1]["review_count"], "0")     # unrated: stated zero
        self.assertEqual(rows[2]["review_count"], "")      # unknown: no statement

    def test_a_price_with_no_honest_rate_has_an_EMPTY_usd_not_the_native_number(self):
        rows, _ = self._csv()
        self.assertEqual(rows[2]["price_usd"], "")
        self.assertEqual(rows[2]["price_amount"], "99.0")
        self.assertEqual(rows[2]["price_currency"], "XXX")

    def test_list_fields_are_joined_not_repr(self):
        rows, _ = self._csv()
        self.assertEqual(rows[0]["tags"], "English guided")
        self.assertEqual(rows[0]["languages"], "en")
        self.assertNotIn("(", rows[0]["tags"])

    def test_the_coverage_warning_goes_to_stderr_so_the_pipe_stays_clean(self):
        rows, err = self._csv()
        self.assertIn("truncated sweep", err)
        for r in rows:
            self.assertNotIn("truncated sweep", "".join(r.values()))

    def test_the_vertical_survives_the_flattening(self):
        """It is the field that decides whether a row belongs here at all."""
        rows, _ = self._csv()
        self.assertEqual([r["vertical"] for r in rows],
                         ["ttd", "experience", "ttd"])


class CsvAndJsonAreNotBothOutputFormats(unittest.TestCase):
    def test_asking_for_both_is_refused_rather_than_silently_picking_one(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["catalog", "hanoi", "--json", "--csv"])


class CompareCsvIsTheArtifactPeopleActuallyWant(unittest.TestCase):
    """`compare --csv` used to fall through to the human table.

    That is the quiet kind of wrong: the caller asked for CSV, got a formatted
    table on stdout and exit 0, and only finds out when the spreadsheet opens
    as one column of dashes.
    """

    def _payload(self):
        kl = model.Activity(source="klook", source_id="843", vertical="ttd",
                            title="Ninh Binh Day Tour from Ha Noi",
                            price_amount=261.0, price_currency="HKD", rating=4.8,
                            review_count=2868, rating_state=model.RATED,
                            url="https://k/843")
        ab = model.Activity(source="airbnb", source_id="714", vertical="experience",
                            title="Ninh Binh private highlights",
                            price_amount=161.0, price_currency="USD", rating=5.0,
                            review_count=1, rating_state=model.RATED,
                            url="https://a/714")
        # A klook row with no price at all, so the second group genuinely has
        # one comparable price. The earlier version of this fixture hand-typed
        # `price_usd_by_source: {"airbnb": None}` while carrying a priced airbnb
        # member — a state `find_cross_source_matches` cannot produce, which let
        # the CSV's member-picking fall through an unasserted branch. A fixture
        # the real model would never emit tests a program that does not exist.
        unpriced = model.Activity(source="klook", source_id="99", vertical="ttd",
                                  title="Ninh Binh private highlights",
                                  rating_state=model.UNRATED)
        groups = (model.find_cross_source_matches([kl, ab], threshold=0.4)
                  + model.find_cross_source_matches([unpriced, ab], threshold=0.4))
        assert len(groups) == 2, groups
        return {"city": "Hanoi", "match_count": 2, "scanned": 3,
                "coverage": {"complete": True, "note": None},
                "matches": groups}

    def _csv(self):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(err):
            render._render_compare_csv(self._payload())
        return list(csv.DictReader(io.StringIO(buf.getvalue()))), err.getvalue()

    def test_one_row_per_matched_group_with_both_sides_side_by_side(self):
        rows, _ = self._csv()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["klook_title"], "Ninh Binh Day Tour from Ha Noi")
        self.assertEqual(rows[0]["airbnb_title"], "Ninh Binh private highlights")
        self.assertEqual(rows[0]["spread_usd"], "127.54")
        self.assertEqual(rows[0]["cheapest_source"], "klook")

    def test_a_group_with_only_one_comparable_price_names_no_winner(self):
        """Rule 6: `cheapest_source` is null unless TWO prices exist. A blank
        beats a confident wrong answer about which platform is cheaper."""
        rows, _ = self._csv()
        self.assertEqual(rows[1]["cheapest_source"], "")
        self.assertEqual(rows[1]["spread_usd"], "")

    def test_columns_are_fixed_by_the_source_registry_not_by_the_first_row(self):
        rows, _ = self._csv()
        for src in ("klook", "airbnb", "viator"):
            self.assertIn(f"{src}_price_usd", rows[0])

    def test_csv_is_not_silently_downgraded_to_the_human_table(self):
        """The regression this class exists for: `--csv` reaching
        `_render_compare` instead, which prints a table and exits 0."""
        buf = io.StringIO()
        args = argparse.Namespace(csv=True, json=False)
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            cli._emit_compare(self._payload(), args)
        first = buf.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("group,"), f"not a CSV header: {first!r}")


class OneSourceCanContributeSeveralListings(unittest.TestCase):
    """`{m.source: price for m in members}` lets the LAST member win.

    Measured on the live Hanoi compare, 2026-08-28: **9 of 44 groups** had more
    than one member from the same platform, and in every one of them the price
    that fed `spread_usd` and `cheapest_source` was whichever listing happened
    to sort last. One group held Airbnb listings at $16, $19 and $25 and
    reported $25; another held $22/$23/$29/$29 and reported $29. Nothing
    errored, the group was real, and the number a traveller would act on was an
    arbitrary pick from a set.
    """

    def _group(self, *prices_by_source):
        rows = []
        for i, (src, amount) in enumerate(prices_by_source):
            rows.append(model.Activity(
                source=src, source_id=str(i), vertical="x",
                title="Hanoi old quarter street food walking tour",
                price_amount=amount, price_currency="USD",
                rating_state=model.UNKNOWN))
        return model.find_cross_source_matches(rows, threshold=0.5)

    def test_a_source_is_represented_by_its_CHEAPEST_listing_not_its_last(self):
        g = self._group(("klook", 100.0), ("airbnb", 29.0), ("airbnb", 22.0))
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["price_usd_by_source"]["airbnb"], 22.0)
        self.assertEqual(g[0]["spread_usd"], 78.0)
        self.assertEqual(g[0]["cheapest_source"], "airbnb")

    def test_order_does_not_change_the_answer(self):
        """The defect's signature: reversing the input flips the number."""
        a = self._group(("klook", 100.0), ("airbnb", 29.0), ("airbnb", 22.0))
        b = self._group(("klook", 100.0), ("airbnb", 22.0), ("airbnb", 29.0))
        self.assertEqual(a[0]["price_usd_by_source"], b[0]["price_usd_by_source"])
        self.assertEqual(a[0]["spread_usd"], b[0]["spread_usd"])

    def test_the_reduction_is_reported_not_hidden(self):
        """A 4-member group that renders as two prices must say so, or the
        reader believes they are looking at a 1:1 comparison."""
        g = self._group(("klook", 100.0), ("airbnb", 29.0), ("airbnb", 22.0))
        self.assertEqual(g[0]["members_by_source"], {"klook": 1, "airbnb": 2})
        self.assertEqual(g[0]["members_count"], 3)

    def test_an_unpriced_sibling_does_not_erase_a_priced_one(self):
        """`min` over a list containing None is the obvious way to break this."""
        g = self._group(("klook", 100.0), ("airbnb", 29.0), ("airbnb", None))
        self.assertEqual(g[0]["price_usd_by_source"]["airbnb"], 29.0)
        self.assertEqual(g[0]["spread_usd"], 71.0)

    def test_a_source_with_no_priced_member_at_all_stays_none(self):
        g = self._group(("klook", 100.0), ("airbnb", None))
        self.assertIsNone(g[0]["price_usd_by_source"]["airbnb"])
        self.assertIsNone(g[0]["spread_usd"])
        self.assertIsNone(g[0]["cheapest_source"])

    def test_n_sources_counts_PLATFORMS_and_n_listings_counts_ROWS(self):
        """A column called `n_sources` showing 6 for a two-platform group is a
        column that lies. Measured in the delivered file: 9 of 44 rows said 3-6
        `n_sources` while exactly two platforms had a title."""
        payload = {"city": "Hanoi", "coverage": {"complete": True, "note": None},
                   "matches": self._group(("klook", 100.0), ("airbnb", 29.0),
                                          ("airbnb", 22.0))}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            render._render_compare_csv(payload)
        row = next(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual(row["n_sources"], "2")
        self.assertEqual(row["n_listings"], "3")
        titled = sum(1 for s in ("klook", "airbnb", "viator") if row[f"{s}_title"])
        self.assertEqual(int(row["n_sources"]), titled)

    def test_the_csv_shows_the_member_whose_price_was_used(self):
        payload = {"city": "Hanoi", "coverage": {"complete": True, "note": None},
                   "matches": self._group(("klook", 100.0), ("airbnb", 29.0),
                                          ("airbnb", 22.0))}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            render._render_compare_csv(payload)
        row = next(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertEqual(row["airbnb_price_usd"], "22.0")
        self.assertEqual(row["airbnb_n"], "2")
        self.assertEqual(row["klook_n"], "1")


class CoverageCountsWhatTheCallerActuallyGot(unittest.TestCase):
    """`returned` was computed before the client-side `--match` filter ran.

    Measured live 2026-08-28: `catalog hanoi --ignore-robots --match cooking`
    handed the caller **59** rows while `coverage.sources` reported
    630 + 232 = **862**. A JSON consumer reading `returned` to decide whether
    the sweep was worth trusting is off by 14x, and the field exists for
    exactly that decision. Rule 5 with the sign flipped: a long answer must not
    look like the answer you were given.
    """

    FIX = pathlib.Path(__file__).parent / "fixtures" / "klook_search_mixed_verticals.json"

    class _Conn:
        def close(self):
            pass

    class _Client:
        EMPTY = '{"success": true, "result": {"search_result": ' \
                '{"cards": [], "total": 37}}}'

        def __init__(self, body):
            self.body, self.seen = body, set()
            self.requests_sent = self.cache_hits = 0

        def _clock(self):
            return 0.0

        def get(self, url, *, ttl_s=None):
            if url in self.seen:
                raise AssertionError("same URL fetched twice")
            self.seen.add(url)
            return self.body if "&start=1&" in url else self.EMPTY

    def _run(self, match):
        body = self.FIX.read_text(encoding="utf-8")
        orig = cli._client
        cli._client = lambda args: (self._Client(body), self._Conn())
        try:
            args = argparse.Namespace(
                city="hanoi", sources=["klook"], ignore_robots=True, json=False,
                csv=False, limit=0, sort="score", size=50, max_pages=2,
                lang="en_US", gap=None, language=None, cache_only=False,
                categories=None, match=match)
            return cli.cmd_catalog(args, emit=False)
        finally:
            cli._client = orig

    def test_without_a_filter_returned_equals_the_rows_handed_over(self):
        _, p = self._run(None)
        self.assertEqual(p["coverage"]["sources"]["klook"]["returned"],
                         len(p["activities"]))

    def test_with_a_filter_returned_still_equals_the_rows_handed_over(self):
        _, p = self._run("museum")
        kl = p["coverage"]["sources"]["klook"]
        self.assertEqual(kl["returned"], len(p["activities"]))
        self.assertLess(len(p["activities"]), 15)

    def test_the_rows_the_filter_removed_are_reported_not_just_missing(self):
        _, p = self._run("museum")
        kl = p["coverage"]["sources"]["klook"]
        self.assertEqual(kl["returned"] + kl["matched_out"], 15)
        self.assertIn("match", p["coverage"])
        self.assertEqual(p["coverage"]["match"], "museum")

    def test_no_match_filter_means_no_phantom_matched_out_key(self):
        _, p = self._run(None)
        self.assertEqual(p["coverage"]["sources"]["klook"]["matched_out"], 0)
        self.assertIsNone(p["coverage"]["match"])


class SpreadsheetFormulasDoNotSurviveTheCsv(unittest.TestCase):
    """Titles are written by third-party sellers and `--csv` exists to be opened
    in a spreadsheet.

    A listing named `=HYPERLINK("http://x")` is a live formula the moment the
    file opens in Excel, Sheets or LibreOffice (CWE-1236). `QUOTE_MINIMAL` does
    not help — it quotes on delimiters, and has no idea what a formula is.

    The value is neutered only in CSV. `--json` stays byte-faithful, because
    that is the lossless channel and the risk lives entirely in the spreadsheet.
    The count of neutered cells goes to stderr so the change is announced, not
    smuggled.
    """

    def _rows(self, *titles):
        acts = [model.Activity(source="klook", source_id=str(i), title=t,
                               vertical="ttd", rating_state=model.UNKNOWN)
                for i, t in enumerate(titles)]
        payload = {"city": "Hanoi", "activities": [a.to_dict() for a in acts],
                   "coverage": {"complete": True, "note": None}}
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(err):
            render._render_csv(payload)
        return list(csv.DictReader(io.StringIO(buf.getvalue()))), err.getvalue()

    def test_every_formula_trigger_is_neutralised(self):
        rows, _ = self._rows('=1+1', '+HYPERLINK("http://x")', '-2+3', '@SUM(A1)',
                             '\tlead tab', '\rlead cr')
        for r in rows:
            self.assertTrue(r["title"].startswith("'"), r["title"][:20])

    def test_an_ordinary_title_is_untouched(self):
        rows, _ = self._rows("Hanoi Coffee Workshop", "Phở & Bún Chả tour")
        self.assertEqual([r["title"] for r in rows],
                         ["Hanoi Coffee Workshop", "Phở & Bún Chả tour"])

    def test_the_change_is_announced_with_a_count(self):
        _, err = self._rows("=1+1", "fine", "@x")
        self.assertIn("2", err)
        self.assertIn("csv", err.lower())

    def test_json_output_is_NOT_neutered(self):
        """The lossless channel stays lossless; only the spreadsheet is defended."""
        a = model.Activity(source="klook", source_id="1", title="=1+1",
                           vertical="ttd", rating_state=model.UNKNOWN)
        self.assertEqual(a.to_dict()["title"], "=1+1")

    def test_urls_are_defended_too_not_only_titles(self):
        a = model.Activity(source="klook", source_id="1", title="ok",
                           vertical="ttd", rating_state=model.UNKNOWN,
                           url="=cmd|'/c calc'!A1")
        payload = {"city": "H", "activities": [a.to_dict()],
                   "coverage": {"complete": True, "note": None}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            render._render_csv(payload)
        self.assertTrue(next(csv.DictReader(io.StringIO(buf.getvalue())))["url"]
                        .startswith("'"))

    def test_the_compare_csv_shares_the_same_defence(self):
        kl = model.Activity(source="klook", source_id="1", vertical="ttd",
                            title="=EVIL() hanoi street food walking tour",
                            price_amount=10.0, price_currency="USD",
                            rating_state=model.UNKNOWN)
        ab = model.Activity(source="airbnb", source_id="2", vertical="experience",
                            title="hanoi street food walking tour",
                            price_amount=20.0, price_currency="USD",
                            rating_state=model.UNKNOWN)
        payload = {"city": "H", "coverage": {"complete": True, "note": None},
                   "matches": model.find_cross_source_matches([kl, ab],
                                                              threshold=0.5)}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            render._render_compare_csv(payload)
        row = next(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertTrue(row["klook_title"].startswith("'"))


class EmitRoutesTheFormatItWasAsked(unittest.TestCase):
    """The mutation harness found this by accident and it is the point of it.

    A mutant meant for `cmd_doctor` landed on `_emit` instead — because three
    lines in this file read `if getattr(args, "csv", False):` — and disabling
    the catalogue's CSV branch entirely **escaped**: every CSV test called
    `_render_csv` directly. The renderer was tested and the wire to it was not,
    which is the same defect `compare --csv` shipped with, one function over.
    """

    def _payload(self):
        a = model.Activity(source="klook", source_id="1", title="a thing",
                           vertical="ttd", rating_state=model.UNKNOWN)
        return {"city": "H", "activities": [a.to_dict()],
                "coverage": {"complete": True, "note": None}}

    def _emit(self, **flags):
        args = argparse.Namespace(**{"csv": False, "json": False, **flags})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), redirect_stderr(io.StringIO()):
            render._emit(self._payload(), args)
        return buf.getvalue()

    def test_csv_reaches_the_csv_renderer(self):
        out = self._emit(csv=True)
        self.assertTrue(out.startswith("source,vertical,"), out[:60])

    def test_json_still_reaches_the_json_renderer(self):
        out = self._emit(json=True)
        self.assertEqual(json.loads(out)["activities"][0]["title"], "a thing")

    def test_neither_flag_gives_the_human_table(self):
        out = self._emit()
        self.assertIn("SCORE", out)


class DoctorDoesNotAcceptAFormatItIgnores(unittest.TestCase):
    """`doctor --csv` parsed, exited 0 and printed JSON.

    Same failure as `compare --csv` before it was fixed: the caller asked for
    one thing, got another, and nothing said so. `doctor`'s output is a check
    list, not rows; refusing is the honest answer.
    """

    def test_csv_is_refused_with_a_usage_error_not_silently_ignored(self):
        err = io.StringIO()
        args = argparse.Namespace(csv=True, json=False, ignore_robots=False,
                                  gap=None, cache_only=False)
        with redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_doctor(args)
        self.assertEqual(rc, exit_codes.USAGE)
        self.assertIn("--csv", err.getvalue())


if __name__ == "__main__":
    unittest.main()
