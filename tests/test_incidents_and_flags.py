"""2026-09-02 debug pass: the sweep swallowed incidents, and flags were accepted
that nothing read.

Every guard here was found by running the shipped binary from `/` with inputs
the suite had never sent it, and each produced a *confident wrong answer*:

  * a 429 storm exited 7 ("partial") and kept sending to the throttling host;
  * `--limit -1` silently dropped the last row; `--size 0` silently became 50;
  * `--language xx` exited 7 with the real cause visible only in JSON;
  * `compare` in table mode exited 7 and printed no coverage warning at all;
  * `doctor --limit 3 --sort price --language zh` was accepted and ignored.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import argparse
import contextlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

import os
import pathlib
import tempfile
from unittest import mock

from activityintel import cli, config, doctor, exit_codes, render, robots, store, sweep, transport
from activityintel.model import Activity
from activityintel.sources import airbnb, viator


class _Conn:
    def close(self):
        pass


class _Raising:
    """A client whose every request raises ``exc``. Counts what it was asked."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0
        self.requests_sent = self.cache_hits = 0

    def _clock(self):
        return 0.0

    def get(self, url, **kw):
        self.calls += 1
        raise self.exc


class _Source:
    NAME = "fake"
    MAX_PAGE_SIZE = 10
    MAX_PAGE = 20

    def fetch_search(self, client, query, *, page, size, lang):
        return client.get(f"https://fake.test/{query}/{page}")


def _args(**over) -> argparse.Namespace:
    base = dict(city="hanoi", sources=["klook"], ignore_robots=True, json=False,
                csv=False, limit=0, sort="score", size=50, max_pages=2,
                gap=None, language=None, cache_only=False, categories=None,
                match=None)
    base.update(over)
    return argparse.Namespace(**base)


@contextlib.contextmanager
def _client(client):
    orig = cli._client
    cli._client = lambda args: (client, _Conn())
    try:
        yield
    finally:
        cli._client = orig


class RateLimitIsAnIncidentNotAPageToSkip(unittest.TestCase):
    """`exit_codes.RATE_LIMIT` says "an incident, never a page to skip", and
    `transport` raises it after three retries. Both sweep loops then caught
    `Exception` per query, so the incident was recorded as "query failed",
    the next 25 keywords were sent to the same throttling host, and the run
    exited 7 as if the market were small. Measured with a fake client on
    2026-09-02: 26 calls after the first 429; exit PARTIAL, never RATE_LIMIT.
    """

    def test_the_klook_sweep_stops_at_the_first_rate_limit(self):
        c = _Raising(transport.RateLimited("www.klook.com returned HTTP 429"))
        with self.assertRaises(transport.RateLimited):
            sweep.sweep(c, _Source(), ["a", "b", "c"], page_size=10)
        self.assertEqual(c.calls, 1, "kept sending to a host that said stop")

    def test_the_airbnb_sweep_stops_at_the_first_rate_limit(self):
        c = _Raising(transport.RateLimited("www.airbnb.com returned HTTP 429"))
        with self.assertRaises(transport.RateLimited):
            airbnb.sweep_place(c, "ChIJx", "Hanoi, Vietnam")
        self.assertEqual(c.calls, 1)

    def test_a_policy_refusal_stops_the_sweep_too(self):
        """robots.txt changing mid-run is a verdict, not a flaky keyword."""
        for exc in (robots.Disallowed("no"), robots.RobotsUnavailable("no")):
            with self.subTest(exc=type(exc).__name__):
                c = _Raising(exc)
                with self.assertRaises(type(exc)):
                    sweep.sweep(c, _Source(), ["a", "b"], page_size=10)
                with self.assertRaises(type(exc)):
                    airbnb.sweep_place(_Raising(exc), "ChIJx", "Hanoi, Vietnam")

    def test_an_ordinary_upstream_failure_still_loses_only_its_own_query(self):
        """The property the old code was protecting: one dead keyword must not
        discard the other nine. A single 5xx after retries is that case."""
        c = _Raising(transport.UpstreamError("HTTP 503 from fake.test", 503))
        rep = sweep.sweep(c, _Source(), ["a", "b", "c"], page_size=10)
        self.assertEqual(rep.failed_queries, ["a", "b", "c"])
        self.assertEqual(c.calls, 3)
        self.assertFalse(rep.is_complete)

    def test_the_command_exits_RATE_LIMIT_not_PARTIAL(self):
        """The wire: `cmd_catalog` already had an `except RateLimited` mapping
        to exit 5. It was dead code, because nothing ever reached it."""
        c = _Raising(transport.RateLimited("www.klook.com returned HTTP 429"))
        with _client(c), redirect_stderr(io.StringIO()):
            rc, payload = cli.cmd_catalog(_args(), emit=False)
        self.assertEqual(rc, exit_codes.RATE_LIMIT)
        self.assertEqual(payload, {})

    def test_the_command_exits_CONFIG_on_a_policy_refusal(self):
        c = _Raising(robots.Disallowed("robots.txt disallows /x"))
        with _client(c), redirect_stderr(io.StringIO()):
            rc, _ = cli.cmd_catalog(_args(sources=["airbnb"]), emit=False)
        self.assertEqual(rc, exit_codes.CONFIG)


def _refused(argv: list[str]) -> str:
    """Parse ``argv`` and return argparse's stderr; fail unless it exited 2."""
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        try:
            cli.build_parser().parse_args(argv)
        except SystemExit as exc:
            if exc.code != exit_codes.USAGE:
                raise AssertionError(f"{argv} exited {exc.code}, not 2")
            return err.getvalue()
    raise AssertionError(f"{argv} was accepted")


class AFlagTheParserAcceptsMustBeConsumed(unittest.TestCase):
    """Every one of these was accepted and then either ignored or misread.

    `--limit -1` became `rows[:-1]` (drops the last row). `--size 0` was
    falsy and became the default. `--max-pages 0` walked nothing and reported
    "hit the source's ceiling". `--threshold 2` found nothing and blamed the
    titles. `search '' hanoi` was a full catalogue sweep. `--language xx`
    failed every Airbnb pass with the cause buried in `passes[].error`.
    """

    def test_negative_limit_is_refused(self):
        self.assertIn("--limit", _refused(["catalog", "hanoi", "--limit", "-1"]))

    def test_zero_limit_still_means_all(self):
        args = cli.build_parser().parse_args(["catalog", "hanoi", "--limit", "0"])
        self.assertEqual(args.limit, 0)

    def test_page_size_below_one_is_refused(self):
        for v in ("0", "-5"):
            with self.subTest(size=v):
                self.assertIn("--size", _refused(["catalog", "hanoi", "--size", v]))

    def test_max_pages_below_one_is_refused(self):
        self.assertIn("--max-pages",
                      _refused(["catalog", "hanoi", "--max-pages", "0"]))

    def test_threshold_outside_the_unit_interval_is_refused(self):
        for v in ("2", "-0.1", "1.01"):
            with self.subTest(threshold=v):
                self.assertIn("--threshold",
                              _refused(["compare", "hanoi", "--threshold", v]))

    def test_threshold_endpoints_are_accepted(self):
        for v in ("0", "1", "0.45"):
            cli.build_parser().parse_args(["compare", "hanoi", "--threshold", v])

    def test_an_empty_search_query_is_refused(self):
        for q in ("", "   "):
            with self.subTest(query=repr(q)):
                self.assertIn("query", _refused(["search", q, "hanoi"]))

    def test_unknown_language_is_refused_at_parse_time_with_the_known_list(self):
        msg = _refused(["catalog", "hanoi", "--language", "xx"])
        self.assertIn("--language", msg)
        self.assertIn("zh", msg)          # the remedy is the list, not a shrug

    def test_a_known_language_still_parses(self):
        args = cli.build_parser().parse_args(["catalog", "hanoi", "--language", "zh"])
        self.assertEqual(args.language, "zh")

    def test_negative_gap_is_refused(self):
        """A negative gap puts the next slot in the past: pacing off, silently."""
        self.assertIn("--gap", _refused(["catalog", "hanoi", "--gap", "-1"]))

    def test_doctor_refuses_the_flags_it_never_reads(self):
        for extra in (["--limit", "3"], ["--sort", "price"], ["--size", "5"],
                      ["--max-pages", "2"], ["--language", "zh"], ["--csv"]):
            with self.subTest(flag=extra[0]):
                _refused(["doctor"] + extra)

    def test_doctor_keeps_the_flags_it_does_read(self):
        cli.build_parser().parse_args(
            ["doctor", "--json", "--ignore-robots", "--cache-only", "--gap", "2"])

    def test_compare_refuses_sort_because_groups_are_ordered_by_spread(self):
        _refused(["compare", "hanoi", "--sort", "price"])

    def test_the_dead_lang_flag_is_gone(self):
        """`k_lang` was measured to change nothing on Klook and never reached
        Airbnb at all. A flag that does nothing on every source is worse than
        none: it promises a filter the tool cannot deliver."""
        for cmd in (["catalog", "hanoi"], ["search", "x", "hanoi"],
                    ["compare", "hanoi"], ["doctor"]):
            with self.subTest(cmd=cmd[0]):
                _refused(cmd + ["--lang", "en_US"])


class CompareHonoursLimit(unittest.TestCase):
    """`cmd_compare` overwrote `args.limit = 0` after argparse accepted the
    flag, so `compare hanoi --limit 1` returned all 45 groups. Measured live
    2026-09-02. `match_count` stays the number found; `shown` says how many
    survived the cap, so a truncated list cannot read as the whole."""

    TITLES = ("Ninh Binh Tam Coc boat", "Halong Bay cruise Sung Sot",
              "Sapa trekking Fansipan cable")

    def _payload(self):
        rows = []
        for i, title in enumerate(self.TITLES):
            rows.append(Activity(source="klook", source_id=f"k{i}", title=title,
                                 price_amount=100.0, price_currency="HKD").to_dict())
            rows.append(Activity(source="airbnb", source_id=f"a{i}", title=title,
                                 price_amount=40.0, price_currency="USD").to_dict())
        return {"city": "Hanoi, Vietnam", "activities": rows,
                "coverage": {"complete": True, "note": None}}

    def _run(self, limit):
        orig_catalog, orig_enabled = cli.cmd_catalog, cli.enabled_sources
        cli.cmd_catalog = lambda args, emit=True: (exit_codes.OK, self._payload())
        cli.enabled_sources = lambda ignore_robots=False: {"klook": None, "airbnb": None}
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = cli.cmd_compare(_args(json=True, limit=limit, threshold=0.45,
                                           sources=None))
        finally:
            cli.cmd_catalog, cli.enabled_sources = orig_catalog, orig_enabled
        return rc, json.loads(out.getvalue())

    def test_limit_caps_the_groups_and_says_so(self):
        rc, p = self._run(1)
        self.assertEqual(rc, exit_codes.OK)
        self.assertEqual(p["match_count"], 3)
        self.assertEqual(p["shown"], 1)
        self.assertEqual(len(p["matches"]), 1)

    def test_zero_limit_shows_everything(self):
        _, p = self._run(0)
        self.assertEqual(p["shown"], 3)
        self.assertEqual(len(p["matches"]), 3)


class MaxPagesReachesAirbnb(unittest.TestCase):
    """`--max-pages` was handed to the Klook sweep and never to
    `airbnb.sweep_place`, which paginated to its own module constant. A flag
    that governs one source and silently not the other is an options bag
    dropping a key."""

    class _EndlessPages:
        """Always hands back a next cursor, so only the budget can stop it."""

        def __init__(self):
            self.urls = []
            self.requests_sent = self.cache_hits = 0

        def _clock(self):
            return 0.0

        def get(self, url, **kw):
            self.urls.append(url)
            n = len(self.urls)
            node = {"__typename": "ExperienceSearchResult", "id": str(n),
                    "listing": {"descriptions": {"name": {"localizedValue": {
                        "localizedStringWithTranslationPreference": f"x{n}"}}}}}
            cursor = "eyJpdGVtc19vZmZzZXQiOiAlZH0=" % n if False else \
                __import__("base64").b64encode(
                    json.dumps({"items_offset": n * 50}).encode()).decode()
            return json.dumps({"data": {"presentation": {"experiencesSearch": {
                "results": {"searchResults": [node],
                            "paginationInfo": {"nextPageCursor": cursor}}}}}})

    def test_the_page_budget_stops_the_unfiltered_pass(self):
        c = self._EndlessPages()
        res = airbnb.sweep_place(c, "ChIJx", "Hanoi, Vietnam", categories=[],
                                 max_pages=2)
        self.assertEqual(len(c.urls), 2)
        self.assertFalse(res["complete"])
        self.assertEqual(res["incomplete_passes"], ["(unfiltered)"])

    def test_the_cli_hands_the_budget_down(self):
        c = self._EndlessPages()
        with _client(c), redirect_stderr(io.StringIO()):
            rc, payload = cli.cmd_catalog(
                _args(sources=["airbnb"], ignore_robots=False, max_pages=3,
                      categories=[]), emit=False)
        self.assertEqual(len(c.urls), 3)
        self.assertEqual(rc, exit_codes.PARTIAL)


class ACoverageNoteNamesTheCause(unittest.TestCase):
    """`--cache-only` on a cold cache printed "passes that did not reach the
    end: (unfiltered), Cooking, …" — true, and useless. The real reason
    (`CacheMiss: not cached and --cache-only was requested`) lived only in
    `passes[].error`, which the table and CSV renderers never show. Same for
    a sweep whose queries failed: the note named the keywords, not the fault.
    """

    def test_airbnb_note_carries_the_first_error(self):
        c = _Raising(transport.CacheMiss(
            "not cached and --cache-only was requested: https://www.airbnb.com/x"))
        with _client(c), redirect_stderr(io.StringIO()):
            _, p = cli.cmd_catalog(_args(sources=["airbnb"], ignore_robots=False,
                                         cache_only=True), emit=False)
        note = p["coverage"]["sources"]["airbnb"]["note"]
        self.assertIn("not cached", note)
        self.assertIn("--cache-only", note)

    def test_sweep_note_carries_the_first_error(self):
        c = _Raising(transport.UpstreamError("HTTP 503 from fake.test", 503))
        rep = sweep.sweep(c, _Source(), ["a", "b"], page_size=10)
        self.assertIn("HTTP 503", rep.coverage_note())

    def test_klook_coverage_names_failed_queries_and_the_first_error(self):
        """The JSON consumer got `queries: 26` (a count) and nothing else; a
        429, a schema change and a DNS blip all read as "N queries failed"."""
        c = _Raising(transport.UpstreamError("HTTP 503 from www.klook.com", 503))
        with _client(c), redirect_stderr(io.StringIO()):
            _, p = cli.cmd_catalog(_args(), emit=False)
        kl = p["coverage"]["sources"]["klook"]
        self.assertEqual(len(kl["failed_queries"]), kl["queries"])
        self.assertIn("HTTP 503", kl["first_error"])
        self.assertIn("HTTP 503", kl["note"])


class CompareTableWarnsWhenTheSweepWasPartial(unittest.TestCase):
    """Three of the four renderers printed the coverage warning; the human
    `compare` table did not. `compare hanoi --ignore-robots` on a capped
    sweep exited 7 and said nothing about it on either stream."""

    def test_partial_coverage_reaches_stderr(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            render._render_compare({"city": "x", "matches": [], "match_count": 0,
                                 "scanned": 0,
                                 "coverage": {"complete": False,
                                              "note": "klook: 1 query hit the ceiling"}})
        self.assertIn("[coverage]", err.getvalue())
        self.assertIn("ceiling", err.getvalue())
        self.assertIn("SAMPLE", err.getvalue())

    def test_a_complete_sweep_says_nothing(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            render._render_compare({"city": "x", "matches": [], "match_count": 0,
                                 "scanned": 0,
                                 "coverage": {"complete": True, "note": None}})
        self.assertEqual(err.getvalue(), "")


class RequestLogCountsTransportFailuresAsErrors(unittest.TestCase):
    """A timeout is recorded with `status NULL`; `SUM(status >= 400)` reads
    NULL as not-an-error, so `cache` reported `errors: 0` for a host that
    never answered. Absent is not zero here either."""

    def test_null_status_is_an_error(self):
        conn = store.connect(pathlib.Path(tempfile.mkdtemp()) / "t.db")
        try:
            store.record_request(conn, "h", "u", None, now=10.0)
            store.record_request(conn, "h", "u", 200, now=10.0)
            store.record_request(conn, "h", "u", 503, now=10.0)
            stats = store.request_stats(conn, since=0.0)
        finally:
            conn.close()
        self.assertEqual(stats, [{"host": "h", "n": 3, "errors": 2}])

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


class ASourceThatIsOnButNeverAskedCannotReportComplete(unittest.TestCase):
    """Viator is registered in `ALL_SOURCES`, becomes "available" the moment
    `VIATOR_API_KEY` is set — and no command has a sweep for it. Measured
    2026-09-02 with a fake key: `catalog hanoi --sources viator` returned
    `activities: []`, `coverage.complete: true`, exit 0. A zero answer that
    looks complete, which is rule 5 with the sign flipped. `compare` then
    counted it as a second platform and compared Airbnb against nothing.
    """

    KEY = {"VIATOR_API_KEY": "not-a-real-key-fixture"}   # invented; never live

    def test_a_keyed_viator_is_reported_as_not_wired_and_the_run_is_partial(self):
        with mock.patch.dict(os.environ, self.KEY):
            self.assertTrue(cli.source_available(viator))
            with _client(_Raising(AssertionError("no fetch may happen"))), \
                    redirect_stderr(io.StringIO()):
                rc, p = cli.cmd_catalog(_args(sources=["viator"], ignore_robots=False),
                                        emit=False)
        self.assertEqual(rc, exit_codes.PARTIAL)
        v = p["coverage"]["sources"]["viator"]
        self.assertTrue(v["skipped"])
        self.assertFalse(v["complete"])
        self.assertIn("not wired", v["note"])
        self.assertFalse(p["coverage"]["complete"])

    def test_every_wanted_source_gets_a_coverage_entry(self):
        """The structural half: a source added to the registry tomorrow and
        forgotten in `cmd_catalog` must land here, not in silence."""
        with mock.patch.dict(os.environ, self.KEY), \
                _client(_Raising(AssertionError("no fetch may happen"))), \
                redirect_stderr(io.StringIO()):
            expected = set(cli.enabled_sources(ignore_robots=False))
            _, p = cli.cmd_catalog(_args(sources=None, ignore_robots=False),
                                   emit=False)
        self.assertIn("viator", expected)           # the key really was seen
        self.assertEqual(set(p["coverage"]["sources"]), expected)

    def test_compare_counts_only_sources_a_sweep_exists_for(self):
        """Refuses BEFORE sweeping: the trap client turns a regression into a
        failure instead of a live Airbnb sweep from inside the suite."""
        with mock.patch.dict(os.environ, self.KEY), \
                _client(_Raising(AssertionError("no fetch may happen"))):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli.cmd_compare(_args(sources=None, ignore_robots=False,
                                           threshold=0.45))
        self.assertEqual(rc, exit_codes.CONFIG)
        self.assertIn("viator", err.getvalue())
        self.assertIn("not wired", err.getvalue())

    def test_doctor_does_not_call_an_unwired_source_ready(self):
        with mock.patch.dict(os.environ, self.KEY):
            detail = doctor.viator_status()
        self.assertIn("key present", detail)
        self.assertIn("not wired", detail)
        self.assertNotIn("ready", detail.lower())

    def test_the_sweepable_set_names_exactly_the_sources_with_a_sweep(self):
        self.assertEqual(cli.SWEEPABLE, frozenset({"klook", "airbnb"}))


class TheSandboxScrubsTheOneCredential(unittest.TestCase):
    """`_sandbox` documents that a credential variable belongs in its scrub
    list in the same commit that introduces it. `VIATOR_API_KEY` was read by
    the code and not on the list, so a developer with the key exported ran a
    different suite from one without it."""

    def test_viator_key_is_on_the_scrub_list_and_absent(self):
        self.assertIn(viator.KEY_ENV, _sandbox.SCRUBBED)
        self.assertNotIn(viator.KEY_ENV, os.environ)


class AirbnbCursorAbsenceIsNotTheLastPage(unittest.TestCase):
    """`pagination.get("nextPageCursor")` returned None for a renamed key and
    for a genuine last page alike, and `paginate` reads None as "walked to the
    end". Every real page — all 26 cached from a live Hanoi sweep, including
    the 20 last pages — carries `paginationInfo` with an explicit null, so a
    missing key is a shape change and must refuse, never report complete."""

    @staticmethod
    def _body(results):
        return json.dumps({"data": {"presentation": {"experiencesSearch":
                                                     {"results": results}}}})

    def test_missing_paginationInfo_is_a_contract_error(self):
        with self.assertRaises(airbnb.ContractError):
            airbnb.parse_search(self._body({"searchResults": []}))

    def test_missing_nextPageCursor_key_is_a_contract_error(self):
        with self.assertRaises(airbnb.ContractError):
            airbnb.parse_search(self._body(
                {"searchResults": [], "paginationInfo": {"loadMoreButtonTitle": "x"}}))

    def test_an_explicit_null_cursor_is_the_last_page(self):
        r = airbnb.parse_search(self._body(
            {"searchResults": [], "paginationInfo": {"nextPageCursor": None}}))
        self.assertIsNone(r["next_cursor"])


class AnUnopenableStoreIsAConfigExitNotATraceback(unittest.TestCase):
    """`exit_codes` exists so an agent can branch on WHY a run stopped. A
    read-only or missing state directory produced a bare traceback and exit 1
    from every command, naming no path and no code."""

    def test_main_maps_a_database_failure_to_CONFIG_and_names_the_path(self):
        # A path whose parent is a regular FILE: mkdir raises, sqlite never
        # opens. Real, not mocked — the translation inside store.connect is
        # the thing under test.
        blocker = pathlib.Path(tempfile.mkdtemp()) / "not-a-dir"
        blocker.write_text("")
        bad = blocker / "sub" / "activities.db"
        orig = config.db_path
        config.db_path = lambda: bad
        err = io.StringIO()
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli.main(["cache"])
        finally:
            config.db_path = orig
        self.assertEqual(rc, exit_codes.CONFIG)
        self.assertIn(str(bad), err.getvalue())
        self.assertIn("ACTIVITY_INTEL_HOME", err.getvalue())


if __name__ == "__main__":
    unittest.main()
