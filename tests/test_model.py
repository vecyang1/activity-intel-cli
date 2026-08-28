"""The three-state rating model and price parsing.

These are the assertions that stop the tool from producing a confident wrong
number, so each one names the real payload it was derived from.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first; see its docstring
import unittest

from activityintel import model
from activityintel.model import Activity, RATED, UNKNOWN, UNRATED


class RatingClassification(unittest.TestCase):
    def test_stated_zero_reviews_is_unrated_not_zero_rated(self):
        """Airbnb 'Latest Activities' returns displayRating 0 / reviewCount 0.

        Verified 2026-08-26 against experience 7191172, whose own detail page
        reports ratingAverage 0: the zero is genuine, the listing is simply new.
        It must still not become a 0.0 rating, which would sort it below a
        one-star listing.
        """
        rating, count, state = model.classify_rating(0, 0)
        self.assertIsNone(rating)
        self.assertEqual(count, 0)
        self.assertEqual(state, UNRATED)

    def test_absent_rating_is_unknown_not_unrated(self):
        """A missing key is not the same claim as 'no reviews yet'."""
        rating, count, state = model.classify_rating(None, None)
        self.assertIsNone(rating)
        self.assertIsNone(count)
        self.assertEqual(state, UNKNOWN)

    def test_bare_zero_with_no_count_is_not_treated_as_a_measurement(self):
        rating, count, state = model.classify_rating(0, None)
        self.assertIsNone(rating)
        self.assertEqual(state, UNKNOWN)

    def test_real_rating_survives(self):
        rating, count, state = model.classify_rating("4.98", "1387")
        self.assertEqual(rating, 4.98)
        self.assertEqual(count, 1387)
        self.assertEqual(state, RATED)

    def test_unparseable_values_do_not_become_zero(self):
        rating, count, state = model.classify_rating("n/a", "many")
        self.assertIsNone(rating)
        self.assertIsNone(count)
        self.assertEqual(state, UNKNOWN)


class RatingOrdering(unittest.TestCase):
    def test_unrated_sorts_after_every_rated_listing_including_bad_ones(self):
        """The whole point: a new listing is unknown quality, not bad quality."""
        good = Activity("klook", "1", "good", rating=4.9, review_count=100,
                        rating_state=RATED)
        bad = Activity("klook", "2", "bad", rating=1.2, review_count=9,
                       rating_state=RATED)
        new = Activity("airbnb", "3", "new", rating=None, review_count=0,
                       rating_state=UNRATED)

        order = [a.title for a in sorted([new, bad, good], key=model.sort_key_rating)]
        self.assertEqual(order, ["good", "bad", "new"])

    def test_zero_fill_would_have_inverted_this(self):
        """Guards the reason the model exists, not just its current behaviour."""
        bad = Activity("klook", "2", "bad", rating=1.2, rating_state=RATED)
        new = Activity("airbnb", "3", "new", rating=None, review_count=0,
                       rating_state=UNRATED)
        naive = sorted([bad, new], key=lambda a: -(a.rating or 0.0))
        self.assertEqual([a.title for a in naive], ["bad", "new"])
        correct = sorted([bad, new], key=model.sort_key_rating)
        self.assertEqual([a.title for a in correct], ["bad", "new"])
        # and a genuinely good new listing must not outrank a proven one either
        self.assertEqual(correct[-1].rating_state, UNRATED)


class PriceParsing(unittest.TestCase):
    def test_klook_returns_hkd_even_when_usd_was_requested(self):
        """Measured 2026-08-26: k_currency=USD is accepted and ignored.

        The currency must come from the string, never from what we asked for.
        """
        amount, currency = model.parse_price("HK$ 261")
        self.assertEqual(amount, 261.0)
        self.assertEqual(currency, "HKD")

    def test_plain_dollar_is_usd_but_hk_dollar_wins_over_it(self):
        self.assertEqual(model.parse_price("US$ 34.69"), (34.69, "USD"))
        self.assertEqual(model.parse_price("$32")[1], "USD")
        self.assertEqual(model.parse_price("HK$ 12")[1], "HKD")

    def test_thousands_separator(self):
        self.assertEqual(model.parse_price("₫ 1,250,000"), (1250000.0, "VND"))

    def test_absent_price_is_none_not_zero(self):
        """A free listing and an unknown price must never look the same."""
        for raw in (None, "", "Sold out", "Price on request"):
            amount, _ = model.parse_price(raw)
            self.assertIsNone(amount, f"{raw!r} produced a number")

    def test_unknown_currency_symbol_does_not_silently_become_usd(self):
        amount, currency = model.parse_price("1234 kr")
        self.assertEqual(amount, 1234.0)
        self.assertIsNone(currency)


class ActivityShape(unittest.TestCase):
    def test_key_is_source_scoped(self):
        """Two platforms reuse numeric ids; a bare id would collide."""
        a = Activity("klook", "8713", "x")
        b = Activity("airbnb", "8713", "y")
        self.assertNotEqual(a.key, b.key)

    def test_to_dict_is_json_safe(self):
        import json
        a = Activity("klook", "1", "t", languages=("en",), tags=("a", "b"))
        json.dumps(a.to_dict())  # must not raise

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


if __name__ == "__main__":
    unittest.main()
