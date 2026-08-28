"""The normalized Activity record every source adapter must produce.

One module owns the shape so a consumer can compare a Klook row against an
Airbnb row without knowing which is which.

Three decisions here are load-bearing, and each exists because getting it wrong
produces a *confident wrong answer* rather than an error:

1. **`rating` has three states, not two.** ``rated`` / ``unrated`` / ``unknown``.
   Measured 2026-08-26: Airbnb's "Latest Activities" rail returns
   ``displayRating: 0, reviewCount: 0`` for newly listed experiences, and that
   zero is *genuine* — the experience page itself reports ``ratingAverage: 0``.
   But a genuine 0.0 still sorts **below** a 3.1 in every ranking, so writing it
   through as a number makes every new listing look like the worst thing in the
   city. An unreviewed listing is unknown quality, not terrible quality, so it
   carries ``rating=None`` and is *excluded* from rating-ordered output rather
   than zero-filled. ``unknown`` (the field was absent) is kept distinct from
   ``unrated`` (the source stated there are no reviews) because only the second
   is a fact about the listing.

2. **`price` keeps the currency the source actually returned.** Measured:
   Klook's search endpoint accepts ``k_currency=USD`` and ignores it, returning
   ``HK$`` — and so do a ``currency`` param, an ``X-Klook-Currency`` header, two
   currency cookies and a different locale (all re-checked 2026-08-27).
   Labelling that number USD because USD was requested is the whole family of
   bug this module exists to prevent, so ``price_amount``/``price_currency``
   are never converted or overwritten.

   ``to_usd`` exists on top of that, not instead of it: comparing a Klook row
   against an Airbnb row in one ranked table is the tool's entire job, and
   sorting ``236`` next to ``33`` as if they were the same unit is its own
   confident wrong answer. The conversion is a *separate, derived* field, it
   names its rate and date, and it returns ``None`` — never a guess — for any
   currency not pinned in config.

3. **Absent is not zero for counts either.** ``review_count=None`` means the
   source did not say; ``0`` means the source said none. A caller summing
   review counts must be able to tell those apart.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from . import config

RATED = "rated"
UNRATED = "unrated"      # source stated there are no reviews yet
UNKNOWN = "unknown"      # source did not report a rating at all

# Currency tokens seen in OTA price strings, longest-first so "HK$" wins over "$".
_CURRENCY_TOKENS = (
    ("HK$", "HKD"), ("NT$", "TWD"), ("US$", "USD"), ("A$", "AUD"), ("C$", "CAD"),
    ("S$", "SGD"), ("R$", "BRL"), ("RM", "MYR"), ("₫", "VND"), ("¥", "JPY"),
    ("₩", "KRW"), ("£", "GBP"), ("€", "EUR"), ("₹", "INR"), ("฿", "THB"),
    ("₱", "PHP"), ("Rp", "IDR"), ("CN¥", "CNY"), ("$", "USD"),
)


def parse_price(raw: str | None) -> tuple[float | None, str | None]:
    """Split a display price into (amount, ISO-ish currency code).

    Returns ``(None, None)`` when the string carries no number — an absent price
    must never surface as ``0.0``, which would rank a listing as free.
    The currency is read from the string itself, never from what we requested.
    """
    if not raw or not isinstance(raw, str):
        return None, None

    currency = None
    for token, code in _CURRENCY_TOKENS:
        if token in raw:
            currency = code
            break
    if currency is None:
        m = re.search(r"\b([A-Z]{3})\b", raw)
        if m:
            currency = m.group(1)

    # Take the first number; "From $40 $32" style strings lead with the pre-discount
    # figure, so callers wanting the payable price should pass the discounted token.
    m = re.search(r"(\d[\d,]*\.?\d*)", raw.replace(" ", " "))
    if not m:
        return None, currency
    try:
        return float(m.group(1).replace(",", "")), currency
    except ValueError:
        return None, currency


def classify_rating(rating: Any, review_count: Any) -> tuple[float | None, int | None, str]:
    """Collapse a source's rating/review pair into the three-state model.

    ``rating`` and ``review_count`` are whatever the source gave us, including
    ``None`` for "the key was absent". The rules, in order:

        review_count == 0                  -> unrated  (stated: no reviews yet)
        rating missing/None                -> unknown  (source said nothing)
        rating == 0 and count missing      -> unknown  (a bare 0 is not evidence)
        otherwise                          -> rated
    """
    count: int | None
    try:
        count = int(review_count) if review_count is not None else None
    except (TypeError, ValueError):
        count = None

    value: float | None
    try:
        value = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        value = None

    if count == 0:
        return None, 0, UNRATED
    if value is None:
        return None, count, UNKNOWN
    if value == 0:
        # A zero with no stated review count is not a measurement. Do not let it
        # sort below every real rating.
        return None, count, UNRATED if count is not None else UNKNOWN
    return value, count, RATED


@dataclasses.dataclass(frozen=True)
class Activity:
    """One bookable activity, normalized across sources. Immutable by design."""

    source: str
    source_id: str
    title: str
    url: str | None = None

    category: str | None = None
    city: str | None = None
    neighborhood: str | None = None

    price_amount: float | None = None
    price_currency: str | None = None
    price_display: str | None = None

    rating: float | None = None
    review_count: int | None = None
    rating_state: str = UNKNOWN

    booked_count: str | None = None
    duration_text: str | None = None
    languages: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    lat: float | None = None
    lng: float | None = None
    image_url: str | None = None
    description: str | None = None

    fetched_at: float | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["languages"] = list(self.languages)
        d["tags"] = list(self.tags)
        d["key"] = self.key
        # Derived, never authoritative. None means "we will not guess", which a
        # consumer must render as unknown rather than fall back to the raw
        # number in whatever currency it happened to be.
        d["price_usd"] = to_usd(self.price_amount, self.price_currency)
        return d


def to_usd(amount: float | None, currency: str | None) -> float | None:
    """Convert to USD for cross-source comparison, or return None.

    None has one meaning here and it is not zero: *we do not have an honest
    rate for this currency*. Falling back to the raw amount would silently
    declare HK$236 to be $236 and rank it as the most expensive thing in the
    city; falling back to 0.0 would rank it as free. Both are worse than a
    blank cell, so this refuses.
    """
    if amount is None or not currency:
        return None
    entry = config.FX_TO_USD.get(currency.upper())
    if entry is None:
        return None
    per_usd, _pegged = entry
    if not per_usd:
        return None
    return round(amount / per_usd, 2)


def fx_note() -> str | None:
    """Warn when an unpegged pinned rate has gone unreviewed. None when fine."""
    import datetime
    try:
        as_of = datetime.date.fromisoformat(config.FX_AS_OF)
    except ValueError:
        return f"FX_AS_OF is not a date ({config.FX_AS_OF!r}); USD columns are unverified."
    age = (datetime.date.today() - as_of).days
    if age <= config.FX_STALE_AFTER_DAYS:
        return None
    unpegged = sorted(c for c, (_, pegged) in config.FX_TO_USD.items() if not pegged)
    return (f"USD columns use rates pinned {age} days ago ({config.FX_AS_OF}). "
            f"Pegged currencies (HKD) are still accurate; {', '.join(unpegged)} "
            f"may have drifted. Refresh config.FX_TO_USD.")


# Prior strength for the confidence-weighted score, in "pseudo-reviews". A
# listing with this many reviews is weighted half by its own average and half by
# the population mean. 20 is chosen to be roughly the point where an Airbnb
# experience's average stops swinging on a single review.
BAYES_PRIOR_REVIEWS = 20.0


# Uncertainty penalty. `RATING_SPREAD` is a conservative stand-in for the
# per-listing spread of individual reviews on a 5-point OTA scale; `Z` is how
# many standard errors we subtract. Together they turn the posterior mean into a
# *lower confidence bound*: what this listing is worth at least.
RATING_SPREAD = 0.5
Z = 1.0


def bayesian_score(rating: float | None, review_count: int | None,
                   population_mean: float, prior: float = BAYES_PRIOR_REVIEWS
                   ) -> float | None:
    """Rank by a lower confidence bound, not by the raw average.

    Raw rating is the wrong default sort for a booking decision: measured live
    on the Hanoi catalogue, it put a 5.00 with 2 reviews above a 4.98 with
    1,387. Those are not comparable claims — the first is nearly noise.

    Shrinking toward the population mean is the usual fix, and on its own it is
    **not enough**, which a test caught before this shipped: when the population
    mean sits *above* the proven listing's rating (easy on OTA data, where
    everything clusters at 4.8–5.0), shrinkage pulls the thin listing *up* and
    it still wins. So the posterior mean also pays an uncertainty penalty that
    decays with sqrt(n). A listing must then be both good and evidenced to rank.

    Returns None for anything unrated: absence of evidence is not evidence of
    mediocrity, so it is excluded from the band rather than scored low.
    """
    if rating is None or review_count is None or review_count <= 0:
        return None
    n = float(review_count)
    posterior = (prior * population_mean + n * rating) / (prior + n)
    standard_error = RATING_SPREAD / ((n + prior) ** 0.5)
    return posterior - Z * standard_error


def population_mean_rating(activities, default: float = 4.8) -> float:
    """Mean of the rated listings. Falls back rather than dividing by zero."""
    vals = [a.rating for a in activities
            if a.rating_state == RATED and a.rating is not None]
    return sum(vals) / len(vals) if vals else default


def sort_key_score(activities):
    """Return a sort key closure ranking by confidence-weighted score.

    Built as a closure because the prior depends on the population actually in
    hand — a key function that recomputed the mean per comparison would be both
    wrong and quadratic.
    """
    mean = population_mean_rating(activities)

    def key(a: Activity) -> tuple:
        score = bayesian_score(a.rating, a.review_count, mean)
        if score is None:
            return (1, 0.0, 0)          # unrated band, after everything scored
        return (0, -score, -(a.review_count or 0))

    return key


def sort_key_rating(a: Activity) -> tuple:
    """Order by rating, keeping unrated/unknown OUT of the rated band.

    Returns a tuple whose first element is a band: 0 = rated, 1 = everything
    else. Callers get "best rated first, unproven listings after" instead of
    "new listings buried below one-star listings", which is what a 0.0 fill
    would produce.
    """
    if a.rating_state == RATED and a.rating is not None:
        return (0, -a.rating, -(a.review_count or 0))
    return (1, 0.0, -(a.review_count or 0))


# Airbnb's parser writes a structured qualifier tag, `priced / guest` or
# `priced / group`. Nothing else does. Reading a bare "group" substring out of
# ANY tag collides that with marketing badges from other sources ("Small group",
# "Private group tour"), which describe party size, not pricing unit.
PRICED_TAG_PREFIX = "priced "
UNIT_GUEST = "guest"
UNIT_GROUP = "group"


def price_unit(tags) -> str | None:
    """"guest" / "group" / None. None means *the source did not say*.

    Three states, for the same reason ratings have three: Klook emits no
    pricing-unit signal at all, so labelling its rows "/pp" asserts a per-person
    unit on the strength of nothing. A per-group total shown as a per-person
    rate understates the real cost by roughly the group size — the exact
    "price table starts lying" failure the renderer exists to prevent, wearing
    the label meant to prevent it.
    """
    for tag in tags or ():
        if isinstance(tag, str) and tag.startswith(PRICED_TAG_PREFIX):
            rest = tag[len(PRICED_TAG_PREFIX):].strip().lstrip("/").strip().lower()
            if rest.startswith(UNIT_GROUP):
                return UNIT_GROUP
            if rest.startswith(UNIT_GUEST):
                return UNIT_GUEST
    return None


def query_relevance_filter(activities, query: str, *, city: str | None = None):
    """Keep only rows sharing a distinctive word with ``query``.

    Needed because a *server-side* keyword filter is not always a filter. Klook
    answers EVERY query with something — a nonsense string returns a confident
    page of unrelated listings and flags nothing — so "the source already
    filtered this" cannot be trusted as a blanket pass.

    Deliberately looser than a substring test: a server that matched
    "cooking class" legitimately returns "Hanoi Cooking Experience", which
    contains neither word pair. Requiring the literal phrase would delete
    correct results; requiring *no* overlap admits the garbage. One shared word
    is the honest middle.
    """
    words = {w for w in query.lower().split() if len(w) > 2}
    if not words:
        return list(activities)
    kept = []
    for a in activities:
        hay = " ".join(filter(None, (a.title, a.category, a.city))).lower()
        if city and a.city and city.lower() not in a.city.lower():
            continue
        if any(w in hay for w in words):
            kept.append(a)
    return kept


# -- cross-source matching -----------------------------------------------------
#
# The one question a two-source tool can answer that neither source can: *is
# this the same experience, and does it cost the same on both?* Measured live
# 2026-08-27, "Hanoi Cooking Class with Local Market Tour" was $32 on Airbnb
# (4.97, 557 reviews) and $48.72 on Klook (5.00, 13 reviews) — a 52% gap on what
# reads as the same product, invisible to anyone looking at one platform.
#
# Matches are REPORTED, never merged. Merging would collapse exactly the two
# fields that make the finding useful — the price and the review count — and a
# false merge would silently delete a real listing. So this returns groups and
# leaves the judgement to a human.

# Words that make every Hanoi activity look like every other one. Similarity
# computed without stripping these is ~0.6 between unrelated listings, which is
# how a naive matcher "discovers" that the whole city is one product.
_GENERIC_TITLE_WORDS = frozenset("""
a an and the with by for from to in of on at or your our my
hanoi vietnam vietnamese ha noi
tour tours class classes lesson lessons experience experiences workshop
day half full private group small join local locals guide guided
best top authentic traditional real unique special
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(title: str | None) -> frozenset[str]:
    """Distinctive words only — the ones that could identify a specific product."""
    if not title:
        return frozenset()
    words = _TOKEN_RE.findall(title.lower())
    return frozenset(w for w in words
                     if len(w) > 2 and w not in _GENERIC_TITLE_WORDS)


def _jaccard(ta: frozenset[str], tb: frozenset[str]) -> float:
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(a: str | None, b: str | None) -> float:
    """Jaccard over distinctive tokens. 0.0 when either side has none."""
    return _jaccard(title_tokens(a), title_tokens(b))


# Two distinctive words in common is the floor. One is coincidence ("market"
# appears in a dozen unrelated Hanoi listings); the threshold alone is not
# enough because a two-token title trivially scores 0.5 on a single match.
MIN_SHARED_TOKENS = 2
DEFAULT_MATCH_THRESHOLD = 0.45


def find_cross_source_matches(activities, *, threshold: float = DEFAULT_MATCH_THRESHOLD,
                              min_shared: int = MIN_SHARED_TOKENS) -> list[dict]:
    """Group likely same-product listings that came from DIFFERENT sources.

    Same-source near-duplicates are deliberately ignored: one platform listing
    a product twice is its own business, and pairing them tells a traveller
    nothing about where to book.

    **Every pair inside a group must clear the bar, not just each member
    against the first one.** The anchor-only version put three listings in one
    group and reported the anchor pair's 0.80 similarity for all of them, while
    the other two scored 0.4286 against each other — below the very threshold
    every other pair is held to. Their prices still fed `spread_usd`, so the
    output claimed a validated 3-way match and a cross-product price gap that
    no pairwise check supported.

    The reported `similarity` is therefore the group's **weakest** pair, and
    `shared_terms` is intersected across **all** members. Both are the honest
    summary: a group is only as good as its worst link.
    """
    rows = [a for a in activities if title_tokens(a.title)]
    # Precomputed once per row rather than per comparison — the loop is O(n^2)
    # and this is ~780K tokenisations on a 1,250-row catalogue otherwise.
    tokens = {a.key: title_tokens(a.title) for a in rows}

    groups: list[dict] = []
    used: set[str] = set()

    for i, left in enumerate(rows):
        if left.key in used:
            continue
        members = [left]
        for right in rows[i + 1:]:
            if right.key in used or right.source == left.source:
                continue
            # Must clear the bar against EVERY member already in the group.
            if all(len(tokens[m.key] & tokens[right.key]) >= min_shared
                   and _jaccard(tokens[m.key], tokens[right.key]) >= threshold
                   for m in members):
                members.append(right)
        if len(members) < 2:
            continue
        for m in members:
            used.add(m.key)

        pair_sims = [_jaccard(tokens[a.key], tokens[b.key])
                     for x, a in enumerate(members) for b in members[x + 1:]]
        shared_all = set(tokens[members[0].key])
        for m in members[1:]:
            shared_all &= tokens[m.key]

        prices = {m.source: to_usd(m.price_amount, m.price_currency) for m in members}
        known = [p for p in prices.values() if p is not None]
        groups.append({
            "shared_terms": sorted(shared_all),
            # The weakest link, not a flattering representative pair.
            "similarity": round(min(pair_sims), 3) if pair_sims else 0.0,
            "members_count": len(members),
            "price_usd_by_source": prices,
            # Both fields need TWO comparable prices, not one. With a single
            # known price the spread is undefined and "cheapest" is a claim
            # about a comparison that never happened — naming the only priced
            # side as the winner is the confident wrong answer, not a fallback.
            "spread_usd": (round(max(known) - min(known), 2)
                           if len(known) > 1 else None),
            "cheapest_source": (min((s for s, p in prices.items() if p is not None),
                                    key=lambda s: prices[s])
                                if len(known) > 1 else None),
            "members": [m.to_dict() for m in members],
        })

    groups.sort(key=lambda g: (g["spread_usd"] is None, -(g["spread_usd"] or 0.0)))
    return groups
