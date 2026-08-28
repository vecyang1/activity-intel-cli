"""Coverage honesty: a truncated pool must never report as complete.

Every assertion here is about the difference between "the market is small" and
"we stopped early", because those two produce identical row counts.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import unittest

from activityintel import sweep
from activityintel.model import Activity


class FakeClient:
    def __init__(self):
        self.requests_sent = 0
        self.cache_hits = 0


class FakeSource:
    """A source whose pool size and failure behaviour the test controls."""

    NAME = "fake"
    MAX_PAGE_SIZE = 10
    MAX_PAGE = 20

    def __init__(self, pool_by_query, *, reported_total=None, cap_at=None,
                 raise_for=()):
        self.pool_by_query = pool_by_query
        self.reported_total = reported_total
        self.cap_at = cap_at
        self.raise_for = set(raise_for)

    def fetch_search(self, client, query, *, page, size, lang):
        if query in self.raise_for:
            raise RuntimeError("source refused")
        pool = self.pool_by_query.get(query, [])
        start = (page - 1) * size
        window = pool[start:start + size]
        total = self.reported_total if self.reported_total is not None else len(pool)
        return {
            "total": total,
            "capped": bool(self.cap_at is not None and total >= self.cap_at),
            "activities": [
                Activity(source="fake", source_id=str(i), title=f"item {i}")
                for i in window
            ],
        }


class Union(unittest.TestCase):
    def test_walks_all_pages_and_dedupes_across_queries(self):
        src = FakeSource({"a": list(range(25)), "b": list(range(20, 35))})
        rep = sweep.sweep(FakeClient(), src, ["a", "b"], page_size=10)
        self.assertEqual(len(rep.activities), 35)      # 0..34, overlap removed
        self.assertTrue(rep.is_complete)
        self.assertIsNone(rep.coverage_note())

    def test_stops_on_short_page_without_an_extra_request(self):
        src = FakeSource({"a": list(range(15))})
        rep = sweep.sweep(FakeClient(), src, ["a"], page_size=10)
        self.assertEqual(rep.queries[0].pages_walked, 2)  # 10 + 5, then stop


class TruncationHonesty(unittest.TestCase):
    def test_capped_query_makes_the_whole_union_incomplete(self):
        """Klook reports total=1000 at its ceiling; that is a sample, not a count."""
        src = FakeSource({"hanoi": list(range(30))}, reported_total=1000, cap_at=1000)
        rep = sweep.sweep(FakeClient(), src, ["hanoi"], page_size=10)
        self.assertFalse(rep.is_complete)
        self.assertEqual(rep.capped_queries, ["hanoi"])
        self.assertIn("ceiling", rep.coverage_note())

    def test_running_out_of_allowed_pages_counts_as_capped(self):
        """The pool outlived our page budget — that is truncation, not completion."""
        src = FakeSource({"a": list(range(500))})
        rep = sweep.sweep(FakeClient(), src, ["a"], page_size=10, max_pages=3)
        self.assertEqual(rep.queries[0].pages_walked, 3)
        self.assertTrue(rep.queries[0].capped)
        self.assertFalse(rep.is_complete)

    def test_one_failing_query_does_not_discard_the_others_but_is_surfaced(self):
        src = FakeSource({"a": list(range(5)), "b": list(range(100, 105))},
                         raise_for=["b"])
        rep = sweep.sweep(FakeClient(), src, ["a", "b"], page_size=10)
        self.assertEqual(len(rep.activities), 5)        # "a" survived
        self.assertEqual(rep.failed_queries, ["b"])     # "b" was not silent
        self.assertFalse(rep.is_complete)
        self.assertIn("failed", rep.coverage_note())

    def test_a_genuinely_small_market_reports_complete(self):
        """The other direction: this must be able to say 'yes, that is all of it'."""
        src = FakeSource({"niche": list(range(3))})
        rep = sweep.sweep(FakeClient(), src, ["niche"], page_size=10)
        self.assertTrue(rep.is_complete)
        self.assertIsNone(rep.coverage_note())

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
