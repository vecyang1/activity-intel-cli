"""Known cities and the per-source identifiers each one needs.

A place is not one string. Klook indexes free text; Airbnb needs a Google
``place_id`` paired with the location string its own UI would send. Keeping
them together here means a caller says ``hanoi`` and cannot accidentally send
Klook's keyword to Airbnb, where it would be silently ignored (Airbnb's
experiences search has no free-text filter) and return the whole city while
looking filtered.

``extra_queries`` exists because of Klook's 1000-result ceiling: broad queries
cap, so reach comes from partitioning the keyword space and unioning ids.
"""
from __future__ import annotations

import dataclasses
import re


IN_CITY = "city"
DAY_TRIP = "day_trip"


@dataclasses.dataclass(frozen=True)
class Place:
    key: str
    name: str
    klook_query: str
    airbnb_query: str | None = None
    airbnb_place_id: str | None = None
    extra_queries: tuple[str, ...] = ()
    # Strings that mean "this place" wherever they appear. Needed because the
    # same city is spelled several ways across sources ("Hanoi" / "Ha Noi").
    match_terms: tuple[str, ...] = ()
    # Destinations that are legitimately part of this city's bookable catalogue
    # because they are sold as day trips FROM it. Declared, never inferred.
    day_trip_cities: tuple[str, ...] = ()

    def scope_of(self, activity) -> str | None:
        """Is this listing part of this place's catalogue, and how?

        Returns ``IN_CITY``, ``DAY_TRIP``, or ``None`` for out of scope.

        Filtering on the source's own ``city_name`` alone is the trap this
        replaces, and it is not a small one: measured 2026-08-27 it discarded
        350 of 1,068 Klook rows for Hanoi, including "Hoa Lu, Tam Coc and Mua
        Cave Day Tour **from Hanoi**" (city_name "Hoa Lu"), "Ninh Binh Day Tour
        **from Ha Noi**", and "**Hanoi** to Lao Cai Sleeper Train" — which is
        precisely the inventory ``extra_queries`` exists to reach. A day trip's
        city_name is its destination; the traveller is still in Hanoi.

        The filter is still needed: the same union carried Seoul and Ho Chi Minh
        listings, because Klook answers every query with something.
        """
        terms = self.match_terms or (self.klook_query.lower(),)
        hay = " ".join(filter(None, (getattr(activity, "title", None),
                                     getattr(activity, "city", None),
                                     getattr(activity, "category", None)))).lower()
        if _contains_phrase(hay, terms):
            return IN_CITY
        # Match the day-trip destination in the TITLE as well as the city field.
        # What a product *is* beats where its operator is registered: measured
        # 2026-08-27, "La Regina Classic 2D1N Cruise: Ha Long Bay" carries
        # city_name "Cam Pha", and enumerating every operator district is a
        # list that silently goes stale. The title is the durable signal.
        if _contains_phrase(hay, self.day_trip_cities):
            return DAY_TRIP
        return None


_PHRASE_CACHE: dict[str, re.Pattern] = {}


def _contains_phrase(haystack: str, phrases) -> bool:
    """Word-boundary containment. Plain ``in`` is the bug this replaces.

    Vietnamese place names are short and space-separated, which makes them
    substrings of ordinary English. Measured 2026-08-27, raw ``in`` matching
    kept four out-of-scope listings, each from a *different* city:

        "tan lac"  matches "rat[tan lac]quer"   -> a Ho Chi Minh craft class
        "quan ba"  matches "[Quan Ba]r"         -> a Ho Chi Minh rooftop bar
        "ba vi"    matches "[Ba Vi]en Temple"   -> a Da Nang temple tour
        "sapa"     matches "[Sapa]way Cafe"     -> a Seoul cafe

    Every one was reported as a Hanoi day trip and counted in
    ``coverage.day_trip``. Nothing errored — the filter simply agreed.
    """
    for phrase in phrases:
        pattern = _PHRASE_CACHE.get(phrase)
        if pattern is None:
            pattern = re.compile(rf"\b{re.escape(phrase)}\b")
            _PHRASE_CACHE[phrase] = pattern
        if pattern.search(haystack):
            return True
    return False


HANOI = Place(
    key="hanoi",
    name="Hanoi, Vietnam",
    klook_query="Hanoi",
    airbnb_query="Hanoi, Vietnam",
    airbnb_place_id="ChIJoRyG2ZurNTERqRfKcnt_iOc",
    # Partition of the Klook keyword space. Each stays well under the 1000 cap
    # while together covering far more of the catalogue than "Hanoi" alone can
    # reach. Verified 2026-08-26: "Hanoi" alone reports total=1000 (the ceiling)
    # and cannot be paged past page 20.
    extra_queries=(
        "Hanoi cooking class",
        "Hanoi food tour",
        "Hanoi coffee",
        "Hanoi street food",
        "Hanoi day trip",
        "Hanoi walking tour",
        "Hanoi water puppet",
        "Hanoi craft workshop",
        "Hanoi spa massage",
        "Hanoi Ninh Binh",
        "Hanoi Halong Bay",
        "Hanoi Sapa",
        "Hanoi motorbike tour",
        "Hanoi cyclo tour",
        "Hanoi night tour",
        "Hanoi museum ticket",
        "Hanoi airport transfer",
        "Hanoi bus ticket",
        # Added 2026-08-28. Every one is a declared day-trip destination or
        # product class that the 19-query partition reached only through the
        # broad "Hanoi" keyword, which caps. Chosen by measuring the in-scope
        # activity ids each adds to the union (hotels excluded), never guessed.
        # Twelve rejected candidates returned literally zero new ids: train
        # street, egg coffee, ao dai, bicycle, Perfume Pagoda, city tour ticket,
        # eSIM, Duong Lam, beer, cyclo rickshaw, buffet, salon. A keyword that
        # adds nothing is not free coverage; it is one more request per run
        # forever.
        #
        # **The gain is +18 ids (612 -> 630, +2.9%), not the +10.8% the
        # selection probe reported.** Both numbers are real and they measure
        # different things: the probe walked 8 pages per keyword, the shipped
        # sweep walks config.MAX_SWEEP_PAGES (40). At depth 8 these keywords
        # look like they add 50 listings, because the existing queries had not
        # been walked far enough to have found them yet. Comparing two sets
        # under a parameter the tool does not use is sound as a *ranking* and
        # worthless as a *forecast* — re-measure a shortlist at the real depth
        # before writing the number down.
        "Hanoi cruise overnight",
        "Hanoi Cat Ba island",
        "Hanoi Tam Coc Trang An",
        "Hanoi Tam Dao",
        "Hanoi Bai Dinh",
        "Hanoi trekking",
        "Hanoi Ha Giang loop",
    ),
    match_terms=("hanoi", "ha noi", "hà nội"),
    # Everything Klook/Airbnb sell as a day or overnight trip departing Hanoi.
    # Derived from the city_name values actually observed in the Hanoi union on
    # 2026-08-27, minus the ones that were genuine noise (Ho Chi Minh, Seoul).
    day_trip_cities=(
        "ha long", "halong", "cat ba", "lan ha", "haiphong", "hai phong",
        "hoa lu", "ninh binh", "trang an", "tam coc", "gia vien",
        "sapa", "sa pa", "lao cai", "ha giang", "quan ba", "meo vac",
        "mèo vạc", "vi xuyen", "vị xuyên", "quang uyen", "quảng uyên",
        "trung khanh", "bat trang", "mai chau", "tan lac", "moc chau",
        "tam dao", "yen tu", "van giang", "yen my", "yên mỹ", "duong lam",
        "perfume pagoda", "chua huong", "ba vi", "cao bang", "ban gioc",
    ),
)

PLACES: dict[str, Place] = {p.key: p for p in (HANOI,)}


def resolve_place(name: str) -> Place | None:
    key = name.strip().lower().replace(" ", "-")
    if key in PLACES:
        return PLACES[key]
    for p in PLACES.values():
        if p.name.lower().startswith(key):
            return p
    return None
