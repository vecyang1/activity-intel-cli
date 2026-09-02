"""Regressions from the 2026-08-28 adversarial review.

Every case below is a *reproduced* failure, not a hypothetical. Each one was
green in the suite that shipped the day before, which is the only reason these
tests are worth their length: they pin the exact inputs that a passing suite,
a live run, and a careful reading all failed to notice.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import unittest
import urllib.error

from activityintel import cli, model, places, render, robots
from activityintel.model import RATED, UNKNOWN, Activity
from activityintel.sources import airbnb, klook


def A(source, sid, title, *, city=None, usd=None, currency="USD", tags=()):
    return Activity(source, sid, title, city=city, price_amount=usd,
                    price_currency=currency if usd is not None else None,
                    tags=tuple(tags), rating=4.9, review_count=100,
                    rating_state=RATED)


class ScopeMatchesWholeWordsOnly(unittest.TestCase):
    """Raw substring matching kept four listings from four different cities.

    Vietnamese place names are short and space-separated, so they are substrings
    of ordinary English. Nothing errored — `scope_of` simply agreed, and each
    row was counted in `coverage.sources.klook.day_trip`.
    """

    def setUp(self):
        self.hanoi = places.PLACES["hanoi"]

    def test_the_four_reproduced_false_keeps_are_now_refused(self):
        cases = [
            # (title, city, the day-trip token that wrongly matched)
            ("Saigon Rattan Lacquer Bracelet Making Class", "Ho Chi Minh City",
             "tan lac"),
            ("Quan Bar Rooftop Night", "Ho Chi Minh", "quan ba"),
            ("Ba Vien Temple", "Da Nang", "ba vi"),
            ("Sapaway Cafe", "Seoul", "sapa"),
        ]
        for title, city, token in cases:
            with self.subTest(token=token):
                self.assertIn(token, self.hanoi.day_trip_cities,
                              "test is stale — the token it probes was removed")
                self.assertIsNone(
                    self.hanoi.scope_of(A("klook", "x", title, city=city)),
                    f"{token!r} matched inside a word again")

    def test_the_real_day_trips_are_still_kept(self):
        """The fix must not swing the other way and delete real inventory."""
        cases = [
            ("Hoa Lu, Tam Coc and Mua Cave Day Tour from Hanoi", "Hoa Lu",
             places.IN_CITY),
            ("Ninh Binh Day Tour from Ha Noi", "Ninh Binh", places.IN_CITY),
            ("La Regina Classic 2D1N Cruise: Ha Long Bay", "Cam Pha",
             places.DAY_TRIP),
            ("Sapa No-Trek Muong Hoa Valley", "Sapa", places.DAY_TRIP),
            ("Hanoi Cooking Class", "Hanoi", places.IN_CITY),
        ]
        for title, city, expected in cases:
            with self.subTest(title=title[:30]):
                self.assertEqual(
                    self.hanoi.scope_of(A("klook", "x", title, city=city)),
                    expected)

    def test_punctuation_still_bounds_a_match(self):
        """"Ha Long Bay, Vietnam" and "cruise:Ha Long" must both match."""
        self.assertEqual(
            self.hanoi.scope_of(A("klook", "x", "Cruise:Ha Long overnight",
                                  city="Quang Ninh")),
            places.DAY_TRIP)


class ServerFilteredIsNotABlanketPass(unittest.TestCase):
    """A source claiming a server-side keyword filter still gets checked.

    Klook answers EVERY query with something — its own module docstring says so
    — yet `--match` exempted every Klook row on the strength of that claim, and
    `relevance_filter`, written for exactly this trap, was called by nothing but
    its own test.
    """

    def test_a_row_sharing_no_word_with_the_query_is_dropped(self):
        rows = [A("klook", "1", "Hanoi Old Quarter Walking Food Tour"),
                A("klook", "2", "Widget Polishing Masterclass in Hanoi")]
        kept = model.query_relevance_filter(rows, "widget polishing")
        self.assertEqual([a.source_id for a in kept], ["2"])

    def test_a_legitimate_server_match_survives_without_the_literal_phrase(self):
        """Why the blunt substring test could not simply be reinstated."""
        rows = [A("klook", "1", "Hanoi Cooking Experience with Market Visit")]
        self.assertEqual(len(model.query_relevance_filter(rows, "cooking class")), 1)

    def test_klooks_wrapper_still_works_and_delegates(self):
        """One implementation, both call sites live."""
        rows = [A("klook", "1", "UNIQUE Slime Lab Party", city="Taipei"),
                A("klook", "2", "Hanoi cooking class", city="Hanoi")]
        self.assertEqual(
            [a.source_id for a in klook.relevance_filter(rows, "Hanoi cooking class")],
            ["2"])

    def test_the_cli_filter_itself_drops_an_off_topic_server_filtered_row(self):
        """Drives the real code path, not the predicate in isolation.

        The first version of this test asserted on `inspect.getsource`, which a
        mutation run walked straight past: the call was still *present* while
        the condition around it had been reverted to `True`. A test that reads
        source text is not a test of behaviour.
        """
        rows = [A("klook", "1", "Hanoi Old Quarter Walking Food Tour"),
                A("klook", "2", "Widget Polishing Masterclass in Hanoi")]
        kept = cli.apply_match_filter(rows, "widget polishing", {"klook"})
        self.assertEqual([a.source_id for a in kept], ["2"])

    def test_the_cli_filter_keeps_a_server_match_lacking_the_literal_phrase(self):
        rows = [A("klook", "1", "Hanoi Cooking Experience with Market Visit")]
        self.assertEqual(len(cli.apply_match_filter(rows, "cooking class", {"klook"})), 1)

    def test_a_source_with_no_server_filter_gets_the_substring_test(self):
        rows = [A("airbnb", "1", "Hanoi Cooking Experience"),
                A("airbnb", "2", "Hanoi cooking class with market tour")]
        kept = cli.apply_match_filter(rows, "cooking class", set())
        self.assertEqual([a.source_id for a in kept], ["2"])

    def test_no_match_string_filters_nothing(self):
        rows = [A("klook", "1", "anything at all")]
        self.assertEqual(len(cli.apply_match_filter(rows, "", {"klook"})), 1)

    def test_empty_query_keeps_everything_rather_than_nothing(self):
        rows = [A("klook", "1", "anything")]
        self.assertEqual(len(model.query_relevance_filter(rows, "a")), 1)


class GroupsAreValidatedPairwise(unittest.TestCase):
    """Anchor-only grouping reported a similarity two members never had."""

    def _trio(self):
        return [A("airbnb", "1", "Hanoi Street Food Motorbike Night Tour", usd=32.0),
                A("klook", "2", "Motorbike Night Street Food Adventure", usd=115.38),
                A("viator", "3", "Motorbike Old Quarter Street Food", usd=60.0)]

    def test_a_member_below_threshold_against_a_sibling_is_excluded(self):
        rows = self._trio()
        # The pair that must not survive: klook x viator scores under the bar.
        self.assertLess(model.title_similarity(rows[1].title, rows[2].title),
                        model.DEFAULT_MATCH_THRESHOLD)
        groups = model.find_cross_source_matches(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["members_count"], 2)
        self.assertEqual({m["source"] for m in groups[0]["members"]},
                         {"airbnb", "klook"})

    def test_reported_similarity_is_the_weakest_pair_not_the_anchor_pair(self):
        rows = self._trio()
        g = model.find_cross_source_matches(rows)[0]
        pairs = [model.title_similarity(a["title"], b["title"])
                 for i, a in enumerate(g["members"]) for b in g["members"][i + 1:]]
        self.assertAlmostEqual(g["similarity"], round(min(pairs), 3), places=3)

    def test_every_reported_pair_clears_the_threshold(self):
        """The invariant the anchor-only version violated."""
        rows = self._trio()
        for g in model.find_cross_source_matches(rows):
            for i, a in enumerate(g["members"]):
                for b in g["members"][i + 1:]:
                    self.assertGreaterEqual(
                        model.title_similarity(a["title"], b["title"]),
                        model.DEFAULT_MATCH_THRESHOLD)

    def _triple_all_above_threshold(self):
        """Three rows that all pair validly, with *unequal* similarities.

        Needed because a 2-member group has exactly one pair, so min == max and
        a min/max mutation is invisible. The escape that revealed this was the
        harness grading a mutant no test could distinguish.
        """
        return [A("airbnb", "1", "Hanoi Egg Coffee Workshop Tasting", usd=20.0),
                A("klook", "2", "Egg Coffee Tasting", usd=15.0),
                A("viator", "3", "Egg Coffee Tasting Roastery", usd=25.0)]

    def test_reported_similarity_is_the_minimum_when_pairs_differ(self):
        rows = self._triple_all_above_threshold()
        g = model.find_cross_source_matches(rows)[0]
        self.assertEqual(g["members_count"], 3)
        pairs = sorted(model.title_similarity(a["title"], b["title"])
                       for i, a in enumerate(g["members"])
                       for b in g["members"][i + 1:])
        self.assertLess(pairs[0], pairs[-1],
                        "fixture is degenerate — min and max coincide, so this "
                        "test cannot tell the two apart")
        self.assertAlmostEqual(g["similarity"], round(pairs[0], 3), places=3)
        self.assertNotAlmostEqual(g["similarity"], round(pairs[-1], 3), places=3)

    def test_shared_terms_hold_for_every_member_not_just_two(self):
        rows = self._trio()
        for g in model.find_cross_source_matches(rows):
            for m in g["members"]:
                self.assertTrue(set(g["shared_terms"]) <= model.title_tokens(m["title"]))

    def test_spread_is_computed_only_over_validated_members(self):
        """The viator $60 must not reach the spread once it is excluded."""
        g = model.find_cross_source_matches(self._trio())[0]
        self.assertNotIn("viator", g["price_usd_by_source"])
        self.assertAlmostEqual(g["spread_usd"], round(115.38 - 32.0, 2), places=2)


class PriceUnitIsThreeState(unittest.TestCase):
    """Only Airbnb states a pricing unit; a bare "group" substring is not one."""

    def test_airbnbs_own_qualifier_tags_are_read(self):
        self.assertEqual(model.price_unit(("priced / group",)), "group")
        self.assertEqual(model.price_unit(("priced / guest",)), "guest")

    def test_a_marketing_badge_mentioning_group_is_NOT_a_pricing_unit(self):
        """"Small group" describes party size. Labelling it /grp understates
        a per-person cost by roughly the group size."""
        for badge in ("Small group", "Private group tour", "Group discount"):
            with self.subTest(badge=badge):
                self.assertIsNone(model.price_unit((badge, "Free cancellation")))

    def test_a_source_that_states_nothing_yields_None_not_a_default(self):
        self.assertIsNone(model.price_unit(()))
        self.assertIsNone(model.price_unit(None))

    def test_real_klook_tags_state_no_unit(self):
        """Measured against the captured fixture, not an invented tag."""
        rows = klook.parse_search(
            (_sandbox._ROOT / "tests" / "fixtures"
             / "klook_search_hanoi_cooking.json").read_text())["activities"]
        self.assertTrue(rows)
        for a in rows:
            self.assertIsNone(model.price_unit(a.tags), a.tags)

    def test_real_airbnb_rows_do_state_one(self):
        rows = airbnb.parse_search(
            (_sandbox._ROOT / "tests" / "fixtures"
             / "airbnb_experiences_hanoi_page0.json").read_text())["activities"]
        stated = [a for a in rows if model.price_unit(a.tags) is not None]
        self.assertTrue(stated, "airbnb fixture should carry priced/ qualifiers")
        self.assertTrue(any(model.price_unit(a.tags) == "group" for a in rows))

    def test_renderer_prints_no_unit_when_the_source_stated_none(self):
        import contextlib
        import io
        row = A("klook", "1", "x", usd=30.0, tags=("Small group",)).to_dict()
        row["score"] = 4.5
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            render._render_table({"activities": [row], "coverage": {}})
        out = buf.getvalue()
        self.assertIn("$30", out)
        self.assertNotIn("/pp", out)
        self.assertNotIn("/grp", out)


class TlsTrustStoreFallback(unittest.TestCase):
    """The last mile: a Python with no CA bundle.

    Measured 2026-08-28 on this machine — /opt/homebrew/bin/python3 loads 193
    CA certificates, /usr/local/bin/python3 loads **0**. The second is what a
    login shell puts first on PATH, so every check in this repo ran under the
    working one while the human got the broken one. The suite was green, a
    `/tmp` run returned 1,252 listings, and `activity-intel` in the owner's own
    terminal returned an honest, complete, entirely empty catalogue.
    """

    def test_the_shared_context_trusts_something_when_it_can(self):
        """Graded 2026-09-02 in a clean venv from the python.org 3.12: the
        store is empty AND certifi is absent, which is the exact state the
        `[tls]` extra exists for. Failing there asserted a property of the
        machine, not of the code; the code's documented answer in that state
        is the remedy, which `test_the_remedy_names_a_command…` grades."""
        from activityintel import config
        if config.tls_is_usable():
            return
        try:
            import certifi  # noqa: F401
        except ImportError:
            self.skipTest("empty CA store and no certifi: the state "
                          "`pip install 'activity-intel[tls]'` fixes; the "
                          "fallback wiring is graded hermetically below")
        self.fail("certifi is importable yet the shared context trusts "
                  "nothing: " + config.tls_remedy())

    def test_context_is_cached_not_rebuilt_per_request(self):
        from activityintel import config
        self.assertIs(config.ssl_context(), config.ssl_context())

    def test_certifi_is_used_when_the_default_store_is_empty(self):
        """Simulates the broken interpreter AND a present certifi, without
        needing either. A fake `certifi` is injected so the test grades the
        wiring — "an empty default store makes config ask certifi for its
        bundle" — rather than whether this machine happens to have certifi
        installed (it did not, in a clean venv, and the test went red for a
        reason that had nothing to do with the code)."""
        import ssl
        import sys as _sys
        import types
        from activityintel import config
        real_default = ssl.create_default_context
        calls = {"cafile": None}
        fake_bundle = "/fake/certifi/cacert.pem"

        def empty_then_certifi(*a, cafile=None, **kw):
            calls["cafile"] = cafile
            ctx = real_default()
            # Shadow the accessor: empty for the default store, populated once
            # the fallback supplied a bundle. No real PEM is needed to grade
            # which path config took.
            ctx.get_ca_certs = (lambda binary_form=False: [{"fake": True}]
                                if cafile else [])
            return ctx

        fake_certifi = types.ModuleType("certifi")
        fake_certifi.where = lambda: fake_bundle
        saved_ctx, saved_mod = config._SSL_CONTEXT, _sys.modules.get("certifi")
        config._SSL_CONTEXT = None
        ssl.create_default_context = empty_then_certifi
        _sys.modules["certifi"] = fake_certifi
        try:
            ctx = config.ssl_context()
        finally:
            ssl.create_default_context = real_default
            config._SSL_CONTEXT = saved_ctx
            if saved_mod is None:
                _sys.modules.pop("certifi", None)
            else:
                _sys.modules["certifi"] = saved_mod
        self.assertEqual(calls["cafile"], fake_bundle,
                         "an empty default store must fall back to certifi's bundle")
        self.assertTrue(ctx.get_ca_certs())

    def test_the_remedy_names_a_command_and_the_interpreter(self):
        """An error that diagnoses without fixing costs the reader the whole trip."""
        import sys as _sys
        from activityintel import config
        remedy = config.tls_remedy()
        self.assertIn(_sys.executable, remedy)
        self.assertIn("certifi", remedy)
        self.assertIn("SSL_CERT_FILE", remedy)

    def test_a_cert_failure_is_not_retried_by_the_robots_fetcher(self):
        """Three timeouts before a message that names the fix helps nobody."""
        import ssl
        import urllib.request
        calls = []

        def bad_cert(url, timeout=None, context=None):
            calls.append(url)
            raise ssl.SSLCertVerificationError("certificate verify failed")

        real = urllib.request.urlopen
        urllib.request.urlopen = bad_cert
        try:
            with self.assertRaises(RuntimeError) as ctx:
                robots.default_fetcher("https://example.com/robots.txt",
                                       sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 1)
        self.assertIn("certifi", str(ctx.exception))


class RobotsFetchSurvivesAHiccup(unittest.TestCase):
    """One dropped packet used to forfeit a 100-second sweep."""

    def test_a_transient_failure_is_retried_then_succeeds(self):
        calls = []

        def flaky(url, timeout=None, context=None):
            calls.append(url)
            if len(calls) < 3:
                raise TimeoutError("transient")
            class R:
                headers = {}
                def read(self): return b"User-agent: *\nAllow: /\n"
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = flaky
        try:
            body = robots.default_fetcher("https://example.com/robots.txt",
                                          sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertIn("Allow", body)
        self.assertEqual(len(calls), 3)

    def test_a_persistent_failure_still_raises_so_the_gate_fails_closed(self):
        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            TimeoutError("down"))
        try:
            with self.assertRaises(RuntimeError):
                robots.default_fetcher("https://example.com/robots.txt",
                                       sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real

    def test_a_404_means_no_rules_published_not_unreadable(self):
        """RFC 9309 2.3.1.3. Failing closed on a 404 refuses a site that
        explicitly published no restrictions."""
        def missing(url):
            raise robots.NoRobotsFile(f"{url} -> HTTP 404")
        gate = robots.RobotsGate(missing, warn=lambda m: None)
        gate.check("https://example.com/anything")   # must not raise

    def test_an_unreadable_robots_still_fails_closed(self):
        def broken(url):
            raise RuntimeError("network down")
        gate = robots.RobotsGate(broken, warn=lambda m: None)
        with self.assertRaises(robots.RobotsUnavailable):
            gate.check("https://example.com/anything")

    def test_4xx_is_not_retried(self):
        """A 404 is a definitive answer; retrying it wastes the sweep's budget."""
        calls = []

        def four_oh_four(url, timeout=None, context=None):
            calls.append(url)
            err = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            err.close()          # synthetic; closing avoids a ResourceWarning
            raise err

        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = four_oh_four
        try:
            with self.assertRaises(robots.NoRobotsFile):
                robots.default_fetcher("https://example.com/robots.txt",
                                       sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 1)

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
