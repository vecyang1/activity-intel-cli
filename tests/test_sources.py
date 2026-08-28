"""Adapter contracts, checked against REAL captured payloads.

Every fixture in tests/fixtures/ is an unedited response body saved straight
from the live endpoint. That matters: the first version of the Klook parser
read `data.review_count`, which does not exist — the field lives on the
sibling `card.track_info`. A hand-written fixture would have contained whatever
the author believed, and the bug (every row silently "unknown rating") would
have shipped behind a green suite.
"""
from __future__ import annotations

import _sandbox  # noqa: F401  -- MUST be first
import collections
import dataclasses
import json
import pathlib
import unittest

from activityintel import model
from activityintel.model import RATED, UNRATED
from activityintel.sources import airbnb, klook, viator

FIX = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class KlookParsing(unittest.TestCase):
    def setUp(self):
        self.parsed = klook.parse_search(fixture("klook_search_hanoi_cooking.json"),
                                         fetched_at=0.0)

    def test_review_count_comes_from_track_info_not_data(self):
        """The exact bug a real fixture caught; a fake one would have hidden it."""
        top = self.parsed["activities"][0]
        self.assertEqual(top.rating_state, RATED)
        self.assertEqual(top.review_count, 1050)
        self.assertAlmostEqual(top.rating, 4.9)

    def test_price_currency_read_from_the_string_not_the_request(self):
        """k_lang/k_currency were en_US/USD; Klook answered in HKD anyway."""
        top = self.parsed["activities"][0]
        self.assertEqual(top.price_currency, "HKD")
        self.assertIsNotNone(top.price_amount)

    def test_narrow_query_total_is_honest_and_uncapped(self):
        self.assertEqual(self.parsed["total"], 29)
        self.assertFalse(self.parsed["capped"])

    def test_past_ceiling_page_is_empty_but_still_reports_the_cap(self):
        """The trap: total says 1000 while zero cards come back."""
        parsed = klook.parse_search(fixture("klook_search_past_ceiling.json"))
        self.assertEqual(parsed["activities"], [])
        self.assertEqual(parsed["total"], klook.RESULT_CAP)
        self.assertTrue(parsed["capped"])

    def test_non_json_body_raises_contract_error_naming_the_likely_cause(self):
        with self.assertRaises(klook.ContractError) as ctx:
            klook.parse_search("<html>403</html>")
        self.assertIn("page numbers past", str(ctx.exception))

    def test_source_stays_off_under_default_policy(self):
        """Klook is robots-disallowed, so it needs an explicit operator override.

        The policy itself — default off, on with --ignore-robots, and the 403
        never overridden — is pinned in test_policy_and_compare.SourcePolicy.
        This asserts only that the adapter still declares it.
        """
        self.assertFalse(klook.AVAILABLE)
        self.assertTrue(klook.REQUIRES_ROBOTS_OVERRIDE)
        self.assertIn("robots.txt", klook.UNAVAILABLE_REASON)

    def test_size_is_clamped_to_the_servers_real_limit(self):
        """Asking 100 silently yields 50; a caller must not believe it got 100."""
        self.assertIn("size=50", klook.search_url("x", size=100))

    def test_relevance_filter_rejects_the_confident_garbage_response(self):
        """Klook answers nonsense queries with unrelated Taipei listings."""
        from activityintel.model import Activity
        rows = [Activity("klook", "1", "UNIQUE Slime Lab Party", city="Taipei"),
                Activity("klook", "2", "Hanoi cooking class", city="Hanoi")]
        kept = klook.relevance_filter(rows, "Hanoi cooking class")
        self.assertEqual([a.source_id for a in kept], ["2"])

    def test_tag_health_flags_the_silently_degraded_tag_service(self):
        from activityintel.model import Activity
        degraded = [Activity("klook", str(i), "t", tags=("Instant confirmation",))
                    for i in range(10)]
        self.assertTrue(klook.tag_health(degraded)["suspect_degraded"])
        healthy = [Activity("klook", str(i), "t",
                            tags=("English guided", "3-5 hrs", "Free cancellation"))
                   for i in range(10)]
        self.assertFalse(klook.tag_health(healthy)["suspect_degraded"])


class KlookVerticals(unittest.TestCase):
    """Klook answers a *things-to-do* query with hotel rooms, and says so only
    in ``card_name``.

    Measured 2026-08-28 on the live Hanoi union: 433 of 1,792 cards were
    ``web_search_hotel_activity_01`` — rooms priced per night, linking to
    ``/hotels/detail/``, carrying 5.00 ratings that outranked real experiences
    in a catalogue whose entire subject is experiences. Nothing in the payload
    flags them as a different kind of product; ``category`` says "Hotels", which
    is indistinguishable from any other category string to a filter that does
    not already know the answer.
    """

    def setUp(self):
        self.parsed = klook.parse_search(fixture("klook_search_mixed_verticals.json"),
                                         fetched_at=0.0)

    def test_a_hotel_card_is_classified_as_a_hotel_not_an_activity(self):
        got = collections.Counter(a.vertical for a in self.parsed["activities"])
        self.assertEqual(got, collections.Counter({"ttd": 15, "hotel": 22}))

    def test_parse_returns_every_card_so_paging_stays_honest(self):
        """Filtering inside the parser would silently truncate the sweep.

        ``sweep.sweep`` stops paging when a page comes back shorter than the
        page size — that is how it knows the pool ended. Dropping 22 of 37 rows
        before it sees them makes a full page look like the last page, and the
        union loses everything past it while still reporting complete.
        """
        self.assertEqual(len(self.parsed["activities"]), 37)
        self.assertEqual(self.parsed["total"], 37)

    def test_the_catalogue_keeps_activities_and_drops_rooms_by_count(self):
        kept, dropped, unknown = klook.split_verticals(self.parsed["activities"])
        self.assertEqual(len(kept), 15)
        self.assertEqual(dict(dropped), {"hotel": 22})
        self.assertEqual(unknown, [])
        self.assertTrue(all(a.vertical == "ttd" for a in kept))

    def test_an_unknown_vertical_is_kept_and_named_rather_than_silently_judged(self):
        """A vertical Klook adds tomorrow is neither confirmed junk nor confirmed
        inventory. Dropping it loses real listings; including it quietly is how
        the hotels got in. So: keep it, and say the name out loud."""
        rows = list(self.parsed["activities"][:2])
        rows.append(dataclasses.replace(rows[0], source_id="new-1",
                                        vertical="hydrofoil"))
        kept, dropped, unknown = klook.split_verticals(rows)
        self.assertIn("hydrofoil", unknown)
        self.assertEqual(dropped, collections.Counter())
        self.assertEqual(len(kept), 3)

    def test_a_card_with_no_vertical_at_all_is_unknown_not_an_activity_claim(self):
        row = dataclasses.replace(self.parsed["activities"][0], vertical=None)
        kept, dropped, unknown = klook.split_verticals([row])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, collections.Counter())
        self.assertEqual(unknown, ["(unlabelled)"])

    def test_the_vertical_is_read_from_klooks_own_word_not_the_category_label(self):
        """``category`` is a display string in the user's locale; ``card_name``
        is the vertical. Keying on the label would break on the first
        translation and on every new category name."""
        self.assertEqual(klook.vertical_of({"card_name": "web_search_hotel_activity_01"}),
                         "hotel")
        self.assertEqual(klook.vertical_of({"card_name": "web_search_ttd_activity_07"}),
                         "ttd")
        self.assertIsNone(klook.vertical_of({"data": {"category": "Hotels"}}))


class AirbnbParsing(unittest.TestCase):
    def setUp(self):
        self.parsed = airbnb.parse_search(
            fixture("airbnb_experiences_hanoi_page0.json"), fetched_at=0.0)

    def test_extracts_a_full_page_with_prices_and_ratings(self):
        acts = self.parsed["activities"]
        self.assertEqual(len(acts), 50)
        self.assertTrue(all(a.url.startswith("https://www.airbnb.com/experiences/")
                            for a in acts))
        self.assertGreater(sum(1 for a in acts if a.price_amount is not None), 45)

    def test_rating_count_arrives_as_a_string_and_is_still_an_int(self):
        top = next(a for a in self.parsed["activities"] if a.review_count)
        self.assertIsInstance(top.review_count, int)

    def test_new_listing_is_unrated_not_zero_rated(self):
        unrated = [a for a in self.parsed["activities"] if a.rating_state == UNRATED]
        self.assertTrue(unrated, "fixture should contain at least one new listing")
        for a in unrated:
            self.assertIsNone(a.rating)

    def test_per_group_pricing_is_preserved_not_flattened(self):
        """14 of 202 Hanoi listings price per group; comparing them raw is wrong."""
        tagged = [a for a in self.parsed["activities"]
                  if any("group" in t for t in a.tags)]
        self.assertTrue(tagged, "fixture should contain a per-group listing")

    def test_cursor_is_plaintext_and_advances(self):
        cur = self.parsed["next_cursor"]
        self.assertEqual(airbnb.decode_cursor(cur)["items_offset"], 50)

    def test_filtered_flag_distinguishes_category_sweeps(self):
        cooking = airbnb.parse_search(fixture("airbnb_experiences_hanoi_cooking.json"))
        self.assertTrue(cooking["filtered"])
        self.assertFalse(self.parsed["filtered"])

    def test_never_invents_a_total(self):
        """Airbnb reports no count; a fabricated one would be worse than None."""
        self.assertIsNone(self.parsed["total"])

    def test_search_url_uses_the_robots_allowed_api_path(self):
        url = airbnb.search_url("place", "Hanoi, Vietnam")
        self.assertIn("/api/v3/ExperiencesSearch/", url)
        self.assertNotIn("/s/", url.split("?")[0])

    def test_contract_error_when_shape_moves(self):
        with self.assertRaises(airbnb.ContractError):
            airbnb.parse_search(json.dumps({"data": {"presentation": {}}}))


class ViatorAdapter(unittest.TestCase):
    def test_unavailable_without_a_key_and_says_how_to_get_one(self):
        self.assertFalse(viator.available())
        self.assertIn("viator.com/partner", viator.UNAVAILABLE_REASON)

    def test_headers_refuse_rather_than_send_an_empty_key(self):
        with self.assertRaises(viator.MissingKey):
            viator.headers(None)

    def test_parses_a_product_envelope(self):
        body = json.dumps({
            "products": [{
                "productCode": "5010SYDNEY",
                "title": "Test tour",
                "reviews": {"combinedAverageRating": 4.5, "totalReviews": 120},
                "pricing": {"currency": "USD", "summary": {"fromPrice": 42.0}},
                "duration": {"fixedDurationInMinutes": 180},
            }],
            "totalCount": 1,
        })
        parsed = viator.parse_search(body)
        a = parsed["activities"][0]
        self.assertEqual(a.rating_state, RATED)
        self.assertEqual((a.price_amount, a.price_currency), (42.0, "USD"))
        self.assertEqual(a.duration_text, "3 hr")

    def test_error_envelope_raises_with_the_servers_own_message(self):
        body = json.dumps({"code": "MISSING_HEADER_VALUE",
                           "message": "Missing required header: exp-api-key"})
        with self.assertRaises(viator.ContractError) as ctx:
            viator.parse_search(body)
        self.assertIn("exp-api-key", str(ctx.exception))


class Scoring(unittest.TestCase):
    def test_thin_evidence_five_star_does_not_outrank_a_proven_listing(self):
        """Measured on the live Hanoi set: raw rating put 5.00/2 above 4.98/1387."""
        from activityintel.model import Activity
        proven = Activity("airbnb", "1", "proven", rating=4.98, review_count=1387,
                          rating_state=RATED)
        thin = Activity("airbnb", "2", "thin", rating=5.0, review_count=2,
                        rating_state=RATED)
        rows = [thin, proven]

        naive = sorted(rows, key=model.sort_key_rating)
        self.assertEqual(naive[0].title, "thin")        # the bug

        fixed = sorted(rows, key=model.sort_key_score(rows))
        self.assertEqual(fixed[0].title, "proven")      # the fix

    def test_unrated_gets_no_score_rather_than_a_low_one(self):
        self.assertIsNone(model.bayesian_score(None, 0, 4.8))
        self.assertIsNone(model.bayesian_score(4.9, 0, 4.8))

    def test_score_converges_on_the_raw_rating_as_evidence_grows(self):
        far = model.bayesian_score(4.5, 10, 4.9)
        near = model.bayesian_score(4.5, 10000, 4.9)
        self.assertLess(abs(near - 4.5), abs(far - 4.5))

    def test_population_mean_survives_an_all_unrated_set(self):
        from activityintel.model import Activity
        rows = [Activity("airbnb", "1", "x", rating_state=UNRATED)]
        self.assertEqual(model.population_mean_rating(rows, default=4.7), 4.7)

    def test_sandbox_still_owns_the_store(self):
        _sandbox.assert_real_store_untouched()


class AirbnbLanguageFilter(unittest.TestCase):
    """The one field that answers 'can I take this class in Chinese?'.

    Verified live 2026-08-27: zh -> 13 results, ko -> 2, unfiltered page -> 50,
    each with isFilteredSearch true. Different values give different result
    sets, which is what distinguishes an honoured filter from an ignored one.
    """

    def test_chinese_is_a_known_code(self):
        self.assertEqual(airbnb.LANGUAGE_CODES["zh"], "128")

    def test_language_reaches_the_request_as_a_server_side_filter(self):
        url = airbnb.search_url("p", "Hanoi, Vietnam", language="zh")
        self.assertIn("experienceLanguages", url)
        self.assertIn("128", url)

    def test_absent_language_sends_no_filter(self):
        """An unfiltered sweep must not accidentally pin a language."""
        self.assertNotIn("experienceLanguages",
                         airbnb.search_url("p", "Hanoi, Vietnam"))

    def test_unknown_language_refuses_rather_than_silently_matching_nothing(self):
        """A local allowlist that disagrees with the server is worse than none —
        but an unknown code here would be sent and match zero, which reads as
        'no Chinese classes exist'. Refuse loudly instead."""
        with self.assertRaises(ValueError) as ctx:
            airbnb.search_url("p", "Hanoi, Vietnam", language="klingon")
        self.assertIn("klingon", str(ctx.exception))

    def test_distinct_languages_produce_distinct_requests(self):
        a = airbnb.search_url("p", "q", language="zh")
        b = airbnb.search_url("p", "q", language="ko")
        self.assertNotEqual(a, b)


class KlookMinorVerticals(unittest.TestCase):
    """`fnd` and `carrental` were in the constants and in no fixture.

    Both sets had exactly one member under test — `ttd` and `hotel` — so
    deleting `"fnd"` from ACTIVITY_VERTICALS or `"carrental"` from
    NON_ACTIVITY_VERTICALS passed every test and every mutant. A constant with
    an unexercised member is a claim nobody has checked.
    """

    def test_food_and_dining_is_an_activity(self):
        parsed = klook.parse_search(fixture("klook_search_minor_verticals.json"))
        got = collections.Counter(a.vertical for a in parsed["activities"])
        self.assertEqual(got["fnd"], 1)
        kept, dropped, unknown = klook.split_verticals(parsed["activities"])
        self.assertIn("fnd", {a.vertical for a in kept})
        self.assertNotIn("fnd", dropped)
        self.assertEqual(unknown, [])

    def test_a_car_rental_search_form_is_not(self):
        parsed = klook.parse_search(fixture("klook_search_carrental.json"))
        got = collections.Counter(a.vertical for a in parsed["activities"])
        self.assertEqual(got["carrental"], 1)
        kept, dropped, unknown = klook.split_verticals(parsed["activities"])
        self.assertEqual(dropped["carrental"], 1)
        self.assertNotIn("carrental", {a.vertical for a in kept})

    def test_every_named_vertical_is_exercised_by_a_real_fixture(self):
        """The guard for the gap itself: a constant gains a member, and this
        fails until a captured response containing it is added."""
        seen = set()
        for name in ("klook_search_mixed_verticals.json",
                     "klook_search_minor_verticals.json",
                     "klook_search_carrental.json",
                     "klook_search_hanoi_cooking.json"):
            seen |= {a.vertical
                     for a in klook.parse_search(fixture(name))["activities"]}
        declared = klook.ACTIVITY_VERTICALS | klook.NON_ACTIVITY_VERTICALS
        self.assertEqual(declared - seen, set(),
                         "declared verticals with no fixture to prove them")
        print(f"[klook-verticals] {len(declared)} declared, all seen in fixtures")


class KlookVerticalCorroboration(unittest.TestCase):
    """`card_name` is one string from one server. Klook also states the vertical
    numerically in `data.vertical_type`, and the two agreed on every card
    measured (100/104 -> ttd, 102 -> hotel, 103 -> carrental, 106 -> fnd).

    Trusting one field completely is the shape of every silent misclassification
    in this file's history, so when the two disagree the answer is neither of
    them: the row becomes an unknown vertical, which this module already keeps
    and names rather than judging.
    """

    def test_the_two_fields_agree_on_every_card_in_every_fixture(self):
        graded = 0
        for name in ("klook_search_mixed_verticals.json",
                     "klook_search_minor_verticals.json",
                     "klook_search_carrental.json",
                     "klook_search_hanoi_cooking.json"):
            payload = json.loads(fixture(name))
            for card in payload["result"]["search_result"]["cards"]:
                vt = (card.get("data") or {}).get("vertical_type")
                if klook.VERTICAL_BY_TYPE.get(vt) is None:
                    continue
                graded += 1
                self.assertEqual(klook.vertical_of(card),
                                 klook.VERTICAL_BY_TYPE[vt],
                                 f"card_name and vertical_type disagree in {name}")
        self.assertGreater(graded, 50)
        print(f"[klook-verticals] card_name agreed with vertical_type on {graded} cards")

    def test_a_disagreement_becomes_unknown_rather_than_a_guess(self):
        card = {"card_name": "web_search_ttd_activity_01",
                "data": {"vertical_type": 102}}   # says ttd, types as a hotel
        v = klook.vertical_of(card)
        self.assertNotIn(v, klook.ACTIVITY_VERTICALS)
        self.assertNotIn(v, klook.NON_ACTIVITY_VERTICALS)
        self.assertIn("ttd", v)
        self.assertIn("hotel", v)

    def test_an_unrecognised_type_code_does_not_override_the_card_name(self):
        """A new numeric code is not evidence against the name; it is silence."""
        card = {"card_name": "web_search_hotel_activity_01",
                "data": {"vertical_type": 999}}
        self.assertEqual(klook.vertical_of(card), "hotel")


if __name__ == "__main__":
    unittest.main()
