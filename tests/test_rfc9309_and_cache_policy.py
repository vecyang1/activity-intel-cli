"""The project's two core compliance guarantees, both broken until 2026-08-28.

Neither failure raised anything. Both answered "allowed".

1. **The matcher's verdict depended on the interpreter.** `urllib.robotparser`
   ignores a `*` in the middle of a path on Python 3.12 and honours it on 3.14.
   `bin/activity-intel` runs whichever `python3` is first on PATH, which on this
   machine is 3.12 — so the gate permitted the exact Airbnb path an earlier
   version of this tool fetched by mistake, while every check in the repo ran
   under 3.14 and passed.

2. **A cache hit bypassed the gate entirely.** The check sat *below* the cache
   read, so one `--ignore-robots` run wrote disallowed responses into the store
   and every later strict run served them back without consulting robots.txt.
   An override that persists in a cache cannot be seen or revoked.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import unittest

from activityintel import rfc9309, robots, store, transport


class MatchingIsInterpreterIndependent(unittest.TestCase):
    """Pins the semantics that `urllib.robotparser` gets wrong on 3.12."""

    AIRBNB = ["User-agent: *", "Disallow: /s/*/*", "Allow: /api/v3/"]

    def rules(self, lines=None):
        return rfc9309.parse(lines or self.AIRBNB)

    def test_a_wildcard_in_the_middle_of_a_path_is_honoured(self):
        """The exact regression. 3.12's stdlib returns True here."""
        self.assertFalse(rfc9309.can_fetch(
            self.rules(), "https://www.airbnb.com/s/Hanoi--Vietnam/experiences"))

    def test_the_allowed_sibling_path_still_passes(self):
        self.assertTrue(rfc9309.can_fetch(
            self.rules(), "https://www.airbnb.com/api/v3/ExperiencesSearch/abc"))

    def test_a_leading_wildcard_matches_any_prefix(self):
        """Klook's real rule form: `Disallow: */search/*`, no leading slash."""
        r = self.rules(["User-agent: *", "Disallow: */search/*"])
        self.assertFalse(rfc9309.can_fetch(
            r, "https://www.klook.com/v1/cardinfocenterservicesrv/search/"
               "platform/complete_search_v3?query=x"))

    def test_longest_match_wins(self):
        r = self.rules(["User-agent: *", "Disallow: /a/", "Allow: /a/b/"])
        self.assertTrue(rfc9309.can_fetch(r, "https://x.com/a/b/c"))
        self.assertFalse(rfc9309.can_fetch(r, "https://x.com/a/z"))

    def test_allow_wins_an_equal_length_tie(self):
        r = self.rules(["User-agent: *", "Disallow: /p", "Allow: /p"])
        self.assertTrue(rfc9309.can_fetch(r, "https://x.com/p"))

    def test_dollar_anchors_to_the_end_of_the_path(self):
        r = self.rules(["User-agent: *", "Disallow: /x$"])
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/x"))
        self.assertTrue(rfc9309.can_fetch(r, "https://s.com/xyz"))

    def test_an_empty_disallow_grants_access(self):
        """RFC 9309 2.2.2 — `Disallow:` with no value is not a rule."""
        r = self.rules(["User-agent: *", "Disallow:"])
        self.assertTrue(rfc9309.can_fetch(r, "https://s.com/anything"))

    def test_no_rules_at_all_means_no_restrictions(self):
        self.assertTrue(rfc9309.can_fetch(rfc9309.Rules([]), "https://s.com/x"))

    def test_comments_and_blank_lines_are_ignored(self):
        r = self.rules(["# hello", "", "User-agent: *", "Disallow: /q  # why"])
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/q"))

    def test_consecutive_user_agents_share_the_following_rules(self):
        r = rfc9309.parse(["User-agent: alpha", "User-agent: *",
                           "Disallow: /shared"], "*")
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/shared"))

    def test_a_new_group_starts_after_rules_have_begun(self):
        r = rfc9309.parse(["User-agent: *", "Disallow: /mine",
                           "User-agent: other", "Disallow: /theirs"], "*")
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/mine"))
        self.assertTrue(rfc9309.can_fetch(r, "https://s.com/theirs"))

    def test_the_query_string_is_part_of_the_matched_path(self):
        r = self.rules(["User-agent: *", "Disallow: /*?secret"])
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/a?secret=1"))

    def test_regex_metacharacters_in_a_path_are_literal(self):
        """A '.' or '+' in a rule must not behave as regex syntax."""
        r = self.rules(["User-agent: *", "Disallow: /a.b"])
        self.assertFalse(rfc9309.can_fetch(r, "https://s.com/a.b"))
        self.assertTrue(rfc9309.can_fetch(r, "https://s.com/axb"))

    def test_agent_selection_is_case_insensitive_with_star_fallback(self):
        lines = ["User-agent: SomeBot", "Disallow: /bot",
                 "User-agent: *", "Disallow: /all"]
        self.assertFalse(rfc9309.can_fetch(rfc9309.parse(lines, "somebot"),
                                           "https://s.com/bot"))
        self.assertFalse(rfc9309.can_fetch(rfc9309.parse(lines, "nobody"),
                                           "https://s.com/all"))


class CacheMustNotBypassPolicy(unittest.TestCase):
    """A cached body is still a fetch decision, and policy owns it."""

    DISALLOWING = "User-agent: *\nDisallow: /blocked\n"

    def _client(self, conn, *, gate):
        return transport.Client(conn, robots_gate=gate, sleep=lambda s: None,
                                opener=lambda url, h: ("FRESH", 200))

    def test_a_cached_disallowed_url_is_still_refused(self):
        """The exact bug: prime the cache under an override, then go strict."""
        conn = store.connect()
        try:
            lax = self._client(conn, gate=robots.RobotsGate(
                lambda u: self.DISALLOWING, enabled=False, warn=lambda m: None))
            body = lax.get("https://ex.com/blocked/thing")
            self.assertEqual(body, "FRESH")          # cache is now primed

            strict = self._client(conn, gate=robots.RobotsGate(
                lambda u: self.DISALLOWING, warn=lambda m: None))
            with self.assertRaises(robots.Disallowed):
                strict.get("https://ex.com/blocked/thing")
        finally:
            conn.close()

    def test_an_allowed_cached_url_is_still_served_from_cache(self):
        """The fix must not turn every cache hit into a network call."""
        conn = store.connect()
        try:
            gate = robots.RobotsGate(lambda u: self.DISALLOWING,
                                     warn=lambda m: None)
            c1 = self._client(conn, gate=gate)
            self.assertEqual(c1.get("https://ex.com/fine"), "FRESH")
            c2 = self._client(conn, gate=gate)
            self.assertEqual(c2.get("https://ex.com/fine"), "FRESH")
            self.assertEqual(c2.cache_hits, 1)
            self.assertEqual(c2.requests_sent, 0)
        finally:
            conn.close()

    def test_the_gate_runs_before_any_network_or_pace_slot(self):
        """A refused URL must cost nothing at all."""
        conn = store.connect()
        try:
            def must_not_fetch(url, headers):
                raise AssertionError("opener called for a disallowed URL")
            c = transport.Client(
                conn, opener=must_not_fetch, sleep=lambda s: None,
                robots_gate=robots.RobotsGate(lambda u: self.DISALLOWING,
                                              warn=lambda m: None))
            with self.assertRaises(robots.Disallowed):
                c.get("https://ex.com/blocked/x")
            self.assertEqual(c.requests_sent, 0)
        finally:
            conn.close()

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
