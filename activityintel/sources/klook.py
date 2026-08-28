"""Klook adapter — OFF by default, reachable only under an explicit override.

**Read this before using it.** Every route into Klook's activity data conflicts
with something, measured 2026-08-26 and re-verified 2026-08-27:

======================================  ==========================================
route                                   result
======================================  ==========================================
``complete_search_v3`` (this module)    HTTP 200, complete JSON — but robots.txt
                                        ``Disallow: */search/*`` under
                                        ``User-Agent: *`` matches the path
                                        ``/v1/cardinfocenterservicesrv/search/...``.
``/en-US/activity/<id>-<slug>/``        **HTTP 403** (Akamai) — allowed by
                                        robots.txt, blocked by bot protection.
``/en-US/city/34-hanoi/``               HTTP 403 (Akamai), browser included.
``sitemap.xml`` / ``llms.txt``          HTTP 200 and explicitly Allow-listed, but
                                        they carry URLs and marketing prose — no
                                        price, rating, review count or language.
======================================  ==========================================

So the only surface that returns structured data is the one robots.txt forbids,
and the surfaces robots.txt permits either 403 or contain no data.

**The policy split this module encodes.** Two different things are being asked
of a client here and they do not deserve the same answer:

* robots.txt is a *voluntary crawl directive*. Overriding it is a judgement the
  operator is entitled to make for their own low-volume personal research, and
  the owner of this machine made it explicitly on 2026-08-27. So the search
  endpoint is reachable — behind ``--ignore-robots``, never by default, and
  never silently (``robots.RobotsGate`` announces every override on stderr).
* The 403 on activity pages is an *active bot-protection block*. Defeating it
  is a different act, and this module does not attempt it and must not grow
  code that does. Everything below reads the search JSON only.

``AVAILABLE`` stays ``False`` — it is the answer under default policy, and the
``SOURCES`` map and ``doctor`` both key on it, so nothing turns Klook on by
accident. ``available(ignore_robots=True)`` is the only way in.

Klook runs an affiliate/partner programme; that remains the route that needs no
override at all, and the parsing code here is what such an integration starts
from.

**What Klook still cannot answer: "is this class taught in Chinese?"** Its
search payload has no language filter (``aggr_condition.filter_list`` offers
Price range / Others / Location and nothing else, checked 2026-08-27), and only
``nature_language_en`` has ever appeared in a Hanoi response — 19 of 50 cards,
English only. The per-activity language list lives on the detail page, which is
the 403 surface. Use Airbnb's ``--language`` for that question; do not let a
missing language tag here read as "not offered in Chinese".

Two behaviours documented here so a future integration does not rediscover them:

* **Klook never returns "no results".** A nonsense query
  (``zzzznonexistentquery``) returns 15 confident, unrelated listings from
  Taipei, and nothing in the payload flags it — ``less_result``,
  ``refresh_text``, ``search_city`` are all null for a good query and a garbage
  one alike. Any client must score relevance itself; "are there X in city Y?"
  otherwise gets a plausible wrong answer. ``relevance_filter`` below exists for
  exactly this.
* **The tag service degrades silently.** The same URL returned 1 distinct tagKey
  (44 tags, zero language tags) on one call and 15 distinct tagKeys (163 tags,
  19 language tags) on the next two — all ``HTTP 200 / success: true``. A run
  during that window records "no language tag" for every activity and looks
  clean. ``tag_health`` is the canary.

----

Original endpoint notes (public, unauthenticated, verified with plain curl and
no cookies) follow, and remain accurate:

    GET https://www.klook.com/v1/cardinfocenterservicesrv/search/platform/complete_search_v3
        ?query=<text>&size=<=50>&start=<1-based page>&k_lang=<locale>

Measured pagination facts, each of which produces a wrong answer if assumed:

* ``start`` is a **page number**, not an offset. ``start=1,2,3`` returned three
  disjoint id sets.
* ``size`` is **clamped to 50**. Asking for 100 returns 50 — silently. We clamp
  before sending and report what we actually used, because a caller that thinks
  it walked 100-item pages will believe it covered twice the pool it did.
* The pool is **hard-capped at 1000** (20 pages x 50). ``start=21`` returns
  ``cards: []`` **while still reporting ``total: 1000``**, and ``start=100``
  returns non-JSON. So ``total`` is a display ceiling, not a count: a broad
  query like "Hanoi" reports exactly 1000 and cannot be walked past it.
  Reaching that ceiling means the result is a *sample*, and this module says so
  rather than letting a truncated pool look complete.
* ``k_currency`` is accepted and **ignored** — see model.parse_price. Prices
  come back in a server-chosen currency, so the currency is read from the
  string, never from what we asked for.
* ``k_lang`` is likewise **not a filter**. ``k_lang=zh_CN`` and ``k_lang=en_US``
  returned byte-identical card sets for "Hanoi" (same 50 ids, same English tag
  text), so it changes neither the result set nor this payload's display
  language. Do not offer it to a caller as a way to find Chinese-guided tours.

Because ``total`` caps at 1000 for any broad query, **the way to increase reach
is to partition the query, not to page deeper**: run several narrower keywords
and union the ids. `sweep.py` owns that, and reports the union size alongside
each query's own cap flag so a capped sub-query is never read as exhaustive.
"""
from __future__ import annotations

import collections
import json
import re
import urllib.parse

from .. import config, model
from ..model import Activity

NAME = "klook"
HOST = "www.klook.com"

AVAILABLE = False
REQUIRES_ROBOTS_OVERRIDE = True
UNAVAILABLE_REASON = (
    "Klook's search endpoint is disallowed by its robots.txt (Disallow: */search/*), "
    "so it is off under default policy. Pass --ignore-robots to read it anyway — a "
    "deliberate operator choice, announced on stderr every time. Its activity detail "
    "pages return HTTP 403 (Akamai) and that block is NOT overridden by the flag, so "
    "per-activity guided languages remain unavailable from this source. "
    "Klook's affiliate/partner API is the route that needs no override."
)


def available(*, ignore_robots: bool = False) -> bool:
    """Is Klook usable under the caller's policy?

    Deliberately a function of the caller's own choice rather than a module
    constant. A constant would have to be mutated to turn the source on, and a
    mutated constant is exactly the "second entry point routing around the
    first" that the transport chokepoint exists to prevent — any code path could
    flip it and no later reader could tell which one did.
    """
    return bool(ignore_robots)
SEARCH_URL = ("https://www.klook.com/v1/cardinfocenterservicesrv/search/"
              "platform/complete_search_v3")

MAX_PAGE_SIZE = 50      # server clamps above this, silently
MAX_PAGE = 20           # page 21 returns an empty card list
RESULT_CAP = MAX_PAGE_SIZE * MAX_PAGE   # 1000


class ContractError(RuntimeError):
    """The response no longer has the shape we parse. Never guess past this."""


def search_url(query: str, *, page: int = 1, size: int = MAX_PAGE_SIZE,
               lang: str = "en_US") -> str:
    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(1, page)
    params = {"query": query, "size": size, "start": page, "k_lang": lang}
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def parse_search(body: str, *, fetched_at: float | None = None) -> dict:
    """-> {'total': int, 'capped': bool, 'activities': [Activity, ...]}

    ``capped`` is the honest half of ``total``: when the reported total is at or
    above the ceiling we can actually walk, the pool is a sample and every
    downstream ranking must be labelled as such.
    """
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ContractError(
            f"Klook returned a non-JSON body ({body[:120]!r}). This is what "
            f"page numbers past {MAX_PAGE} do — check the page argument before "
            f"assuming the endpoint moved."
        ) from exc

    if not isinstance(payload, dict) or "result" not in payload:
        raise ContractError(f"Klook payload has no 'result' key: {list(payload)[:8]}")
    if not payload.get("success", False):
        err = (payload.get("error") or {}).get("message") or "unspecified"
        raise ContractError(f"Klook reported failure: {err}")

    section = (payload.get("result") or {}).get("search_result")
    if section is None:
        raise ContractError("Klook payload has result but no 'search_result'")

    cards = section.get("cards")
    if cards is None:
        raise ContractError("Klook search_result has no 'cards' key")

    total = section.get("total")
    total = int(total) if isinstance(total, (int, float)) else None

    activities = [a for a in (_card_to_activity(c, fetched_at) for c in cards) if a]
    return {
        "total": total,
        "capped": bool(total is not None and total >= RESULT_CAP),
        "activities": activities,
    }


# Klook's card vocabulary, e.g. `web_search_ttd_activity_01`. The token in the
# middle is the vertical; the trailing number is a template version and moves.
_VERTICAL_RE = re.compile(r"^web_search_([a-z]+)_activity")

# Verticals that are NOT bookable activities. Measured on the live Hanoi union
# 2026-08-28: 433 of 1,792 cards were `hotel` — rooms at `/hotels/detail/`,
# priced per night, rated 5.00, ranking above real experiences in a catalogue
# whose whole subject is experiences. `carrental` links to a search form and is
# not even a listing.
NON_ACTIVITY_VERTICALS = frozenset({"hotel", "carrental"})

# Verticals confirmed to be bookable activities. `ttd` is things-to-do (it also
# carries private airport transfers, which Klook files there and which are a
# real bookable service); `fnd` is food & dining, whose cards link to ordinary
# `/activity/` pages.
ACTIVITY_VERTICALS = frozenset({"ttd", "fnd"})

UNLABELLED = "(unlabelled)"

# Klook states the vertical twice: as a word in `card_name` and as a number in
# `data.vertical_type`. Measured over every captured Hanoi response, the two
# agree on every card — 100 and 104 are both `ttd` (104 is a private airport
# transfer, which Klook files under things-to-do and which is a real bookable
# service). A code not in this map is *silence*, not disagreement.
VERTICAL_BY_TYPE = {100: "ttd", 102: "hotel", 103: "carrental", 104: "ttd",
                    106: "fnd"}


def vertical_of(card: dict) -> str | None:
    """Klook's own word for what kind of product this card is, or None.

    Read from `card_name`, never from `data.category`. The category is a
    localized display label — "Hotels" in en-US is something else in ja-JP, and
    a new category name appears every season — while the vertical is structural.

    `data.vertical_type` states the same thing numerically, and is used only to
    **contradict**, never to decide. When the two disagree the honest answer is
    neither of them, so this returns a `conflict:` token: `split_verticals`
    treats it as an unknown vertical, which it keeps and names. Trusting one
    string from one server completely is the shape of every silent
    misclassification this module exists to prevent — 441 hotel rooms entered an
    activity catalogue on exactly that kind of unchecked trust.
    """
    m = _VERTICAL_RE.match(((card or {}).get("card_name") or ""))
    named = m.group(1) if m else None
    typed = VERTICAL_BY_TYPE.get(((card or {}).get("data") or {}).get("vertical_type"))
    if named and typed and named != typed:
        return f"conflict:{named}/{typed}"
    return named or typed


def split_verticals(activities):
    """-> (kept, Counter(dropped by vertical), [unknown vertical names])

    Three states, deliberately, because two would be wrong in both directions.
    A vertical Klook adds tomorrow is neither confirmed inventory nor confirmed
    junk: dropping it loses real listings, and waving it through silently is
    exactly how 433 hotel rooms entered an activity catalogue unnoticed. So an
    unrecognised vertical is **kept** — never lose reach on a guess — and its
    name is returned so the caller can print it instead of implying it was
    checked.
    """
    kept, dropped, unknown = [], collections.Counter(), []
    for a in activities:
        v = getattr(a, "vertical", None)
        if v in NON_ACTIVITY_VERTICALS:
            dropped[v] += 1
            continue
        if v not in ACTIVITY_VERTICALS:
            name = v or UNLABELLED
            if name not in unknown:
                unknown.append(name)
        kept.append(a)
    return kept, dropped, unknown


def _card_to_activity(card: dict, fetched_at: float | None) -> Activity | None:
    data = (card or {}).get("data") or {}
    vid = data.get("vertical_id")
    if vid is None:
        return None

    review = data.get("review_obj") or {}
    # `track_info` is a sibling of `data`, not a child of it. Reading
    # `data["review_count"]` returns None for every card, which the three-state
    # model would faithfully report as "unknown" for the entire catalogue — a
    # quiet, total loss of the review signal. Caught only by parsing a real
    # captured response instead of a hand-written fixture.
    track = (card or {}).get("track_info") or {}

    price_display = ((data.get("price") or {}).get("selling_price"))
    amount, currency = model.parse_price(price_display)

    # Prefer track_info's numerics: review_obj.star is a display string and
    # review_obj.number is already abbreviated ("1.0K+ reviews" loses precision).
    rating, count, state = model.classify_rating(
        track.get("review_rating", review.get("star")),
        track.get("review_count"),
    )

    tags = tuple(
        t.get("text") for t in (data.get("general_tag") or [])
        if isinstance(t, dict) and t.get("text")
    )
    languages = tuple(
        code for code in (
            _language_code(t) for t in (data.get("general_tag") or [])
            if isinstance(t, dict)
        ) if code
    )
    duration = next((t for t in tags if _looks_like_duration(t)), None)

    lat = lng = None
    loc = data.get("location")
    if isinstance(loc, str) and "," in loc:
        try:
            lat_s, lng_s = loc.split(",", 1)
            lat, lng = float(lat_s), float(lng_s)
        except ValueError:
            lat = lng = None

    return Activity(
        source=NAME,
        source_id=str(vid),
        vertical=vertical_of(card),
        title=data.get("title") or "",
        url=data.get("deep_link"),
        category=data.get("category"),
        city=data.get("city_name"),
        price_amount=amount,
        price_currency=currency,
        price_display=price_display,
        rating=rating,
        review_count=count,
        rating_state=state,
        booked_count=review.get("booked"),
        duration_text=duration,
        languages=languages,
        tags=tags,
        lat=lat,
        lng=lng,
        image_url=data.get("cover_url"),
        fetched_at=fetched_at,
    )


_LANG_PREFIX = "nature_language_"


def _language_code(tag: dict) -> str | None:
    """Read the guided language from Klook's own vocabulary, not an allowlist.

    A hardcoded map of tag keys is the trap described in coding-style.md: a
    local vocabulary that disagrees with the server looks authoritative and
    silently matches nothing. Only ``nature_language_en`` has actually been
    observed in Hanoi payloads, so any other language is derived from the key
    suffix the server sends. An unrecognised shape yields ``None`` rather than
    a guess.

    **Absence is not "no languages".** Only 19 of 50 Hanoi cards carried any
    language tag at all, so an empty ``languages`` tuple means *the search
    payload did not say* — never that the activity is unguided or
    English-only. Answering "is this class available in Chinese?" needs the
    activity detail page; the search tag cannot settle it.
    """
    key = tag.get("tagKey")
    if not isinstance(key, str) or not key.startswith(_LANG_PREFIX):
        return None
    suffix = key[len(_LANG_PREFIX):].strip()
    if not suffix:
        return None
    parts = suffix.split("_")
    if len(parts) == 1:
        return parts[0].lower()
    return f"{parts[0].lower()}-{parts[1].upper()}"

def relevance_filter(activities, query: str, *, city: str | None = None):
    """Drop rows that do not actually match the query.

    Klook answers every query with something. A nonsense string returns a
    confident page of Taipei listings and flags nothing, so a client that trusts
    the response reports "15 results" for a market that has none.

    The implementation lives in ``model.query_relevance_filter`` because the CLI
    needs the same predicate for any source claiming a server-side filter. This
    wrapper stays because *this* module is where the reason is documented, and a
    reader who finds the trap here must find the tool here too.
    """
    return model.query_relevance_filter(activities, query, city=city)


def tag_health(activities) -> dict:
    """Canary for the silently-degraded tag service. See module docstring.

    Returns counts rather than a verdict: the caller decides what floor is
    acceptable, but it must not be allowed to *not know*.
    """
    distinct = set()
    total = 0
    with_lang = 0
    for a in activities:
        distinct.update(a.tags)
        total += len(a.tags)
        if a.languages:
            with_lang += 1
    return {
        "rows": len(list(activities)),
        "distinct_tags": len(distinct),
        "total_tags": total,
        "rows_with_language": with_lang,
        "suspect_degraded": bool(activities) and len(distinct) <= 2,
    }


_DURATION_HINTS = ("hr", "hrs", "hour", "day", "days", "min", "minute")


def _looks_like_duration(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _DURATION_HINTS) and any(c.isdigit() for c in low)


def fetch_search(client, query: str, *, page: int = 1, size: int = MAX_PAGE_SIZE,
                 lang: str = "en_US") -> dict:
    """One page. The client owns the socket, the cache, and the pacing."""
    url = search_url(query, page=page, size=size, lang=lang)
    body = client.get(url, ttl_s=config.TTL_SEARCH_S)
    return parse_search(body, fetched_at=client._clock())
