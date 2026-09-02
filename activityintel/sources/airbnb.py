"""Airbnb Experiences adapter — builds URLs and parses bodies. Opens no sockets.

Endpoint (persisted GraphQL query, public frontend key, verified cookieless
2026-08-26):

    GET https://www.airbnb.com/api/v3/ExperiencesSearch/<sha256Hash>
        ?operationName=ExperiencesSearch&locale=en&currency=USD
        &variables=<json>&extensions=<json>
    X-Airbnb-Api-Key: <public frontend key>

**Why not the HTML page.** The obvious route — fetching
``/s/Hanoi--Vietnam/experiences`` and parsing its embedded JSON — is
**disallowed by Airbnb's robots.txt**, which carries ``Disallow: /s/*/*`` in
the ``*`` group. An earlier version of this capability did exactly that on
every call; nothing errored, because a disallowed fetch is indistinguishable
from an allowed one at the client. `robots.py` now makes that unreachable.
The ``/api/v3/ExperiencesSearch/`` path is *not* disallowed (Airbnb blocklists
individual operations by name, and this one is absent).

That is a narrow permission and it is worth stating plainly: robots.txt is not
a licence, and Airbnb's Terms separately restrict automated collection. This
adapter is therefore built for **personal, low-volume trip research** — a few
dozen paced requests — and `docs/SOURCES.md` records that boundary. It is not a
bulk harvester and must not be repurposed as one.

**Two measured traps:**

* The unfiltered grid is a *ranked feed that drops items*. A Cooking-filtered
  sweep returned 35 results; the unfiltered sweep of the same city returned 34
  and never surfaced id ``910096`` across five pages. So "walk the pagination
  until the cursor is null" does **not** yield the catalogue. Completeness
  requires sweeping the category tags and unioning — see ``CATEGORY_TAGS``.
* Prices carry a **qualifier**: ``/ guest`` or ``/ group``. 14 of 202 Hanoi
  listings are per-group. Comparing those numbers against per-guest prices
  without normalising is a straightforward way to produce a wrong ranking, so
  the qualifier is preserved on every row and never discarded.
"""
from __future__ import annotations

import base64
import json
import urllib.parse

from .. import config, model, transport
from ..model import Activity

NAME = "airbnb"
HOST = "www.airbnb.com"
API_ROOT = "https://www.airbnb.com/api/v3/ExperiencesSearch"

# Both of these are PUBLIC frontend values lifted from Airbnb's own page source,
# not secrets. Both can rotate; `explain_rotation()` turns that into an
# actionable message instead of a generic failure.
DEFAULT_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
DEFAULT_QUERY_HASH = (
    "36fd308996f11beada0a01727f737d6a46171eaf5fcc6eb1eafe539c3f461642"
)

PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
MAX_PAGE = 40

# Category pills, scraped from the live filter bar. Sweeping these and unioning
# is the only way to get a complete catalogue (see module docstring).
CATEGORY_TAGS = {
    "Cooking": "Tag:8957",
    "Food tours": "Tag:8960",
    "Tastings": "Tag:9012",
    "Dining": "Tag:8972",
    "Cultural tours": "Tag:8970",
    "Landmarks": "Tag:8971",
    "Museums": "Tag:9013",
    "Architecture": "Tag:9010",
    "Galleries": "Tag:8954",
    "Art workshops": "Tag:8953",
    "Performances": "Tag:9048",
    "Outdoors": "Tag:8961",
    "Wildlife": "Tag:8963",
    "Water sports": "Tag:8962",
    "Flying": "Tag:9011",
    "Wellness": "Tag:8968",
    "Workouts": "Tag:8969",
    "Beauty": "Tag:8959",
    "Shopping & fashion": "Tag:8955",
}


# Server-side "Language offered" filter, scraped from the live filter bar. The
# values are bit flags on `experience_languages`. This is the ONE way to answer
# "is this class taught in Chinese?" — Klook's language tags exist but Klook is
# disabled, and no free-text or description heuristic is trustworthy for it.
LANGUAGE_CODES = {
    "en": "1", "fr": "2", "de": "4", "ja": "8", "it": "16", "ru": "32",
    "es": "64", "zh": "128", "ar": "256", "hi": "512", "pt": "1024",
    "tr": "2048", "id": "4096", "nl": "8192", "ko": "16384", "bn": "32768",
    "th": "65536", "pa": "131072", "el": "262144", "sgn": "524288",
    "he": "1048576", "pl": "2097152", "ms": "4194304", "tl": "8388608",
    "da": "16777216", "sv": "33554432", "no": "67108864", "fi": "134217728",
    "cs": "268435456", "hu": "536870912", "uk": "1073741824",
}
LANGUAGE_NAMES = {"zh": "Chinese (Simplified)", "en": "English", "ko": "Korean",
                  "ja": "Japanese", "fr": "French", "de": "German"}


class ContractError(RuntimeError):
    """The response no longer has the shape we parse. Never guess past this."""


class CredentialRotated(RuntimeError):
    """The public key or persisted-query hash moved. Actionable, not mysterious."""


def explain_rotation(status: int, body: str) -> str:
    return (
        f"Airbnb returned HTTP {status}. The most likely cause is that the public "
        f"frontend API key or the persisted-query hash rotated — both are baked "
        f"into Airbnb's own JS bundle and change on deploy.\n"
        f"  key  currently pinned: {DEFAULT_API_KEY}\n"
        f"  hash currently pinned: {DEFAULT_QUERY_HASH}\n"
        f"Re-derive by loading an ALLOWED Airbnb page (robots.txt disallows /s/*/*) "
        f"and reading api_config.key from the HTML, and operationId from the "
        f"ExperiencesSearchRoute JS bundle on a0.muscache.com.\n"
        f"Body: {body[:200]}"
    )


def _variables(place_id: str, query: str, *, cursor: str | None,
               items_per_grid: int, category_tag: str | None,
               language: str | None = None) -> dict:
    raw_params = [
        {"filterName": "cdnCacheSafe", "filterValues": ["false"]},
        {"filterName": "itemsPerGrid", "filterValues": [str(items_per_grid)]},
        {"filterName": "placeId", "filterValues": [place_id]},
        {"filterName": "query", "filterValues": [query]},
        {"filterName": "refinementPaths", "filterValues": ["/experiences"]},
        {"filterName": "screenSize", "filterValues": ["large"]},
        {"filterName": "tabId", "filterValues": ["experience_tab"]},
        {"filterName": "version", "filterValues": ["1.8.8"]},
    ]
    if category_tag:
        raw_params.append({"filterName": "kgOrTags", "filterValues": [category_tag]})
    if language:
        code = LANGUAGE_CODES.get(language)
        if code is None:
            raise ValueError(
                f"unknown language {language!r}. Known: "
                f"{', '.join(sorted(LANGUAGE_CODES))}")
        raw_params.append({"filterName": "experienceLanguages",
                           "filterValues": [code]})
    if cursor:
        raw_params.append({"filterName": "cursor", "filterValues": [cursor]})
    return {
        "experiencesSearchRequest": {
            "metadataOnly": False,
            "treatmentFlags": [
                "m13_search_input_phase2_treatment",
                "m13_search_input_services_enabled",
                "m13_2025_experiences_p2_treatment",
            ],
            "rawParams": raw_params,
        },
        "isLeanTreatment": False,
    }


def search_url(place_id: str, query: str, *, cursor: str | None = None,
               size: int = PAGE_SIZE, category_tag: str | None = None,
               currency: str = "USD", locale: str = "en",
               language: str | None = None,
               query_hash: str = DEFAULT_QUERY_HASH) -> str:
    size = max(1, min(size, MAX_PAGE_SIZE))
    params = {
        "operationName": "ExperiencesSearch",
        "locale": locale,
        "currency": currency,
        "variables": json.dumps(
            _variables(place_id, query, cursor=cursor, items_per_grid=size,
                       category_tag=category_tag, language=language),
            separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": query_hash}},
            separators=(",", ":")),
    }
    return f"{API_ROOT}/{query_hash}?{urllib.parse.urlencode(params)}"


def headers(api_key: str = DEFAULT_API_KEY) -> dict:
    return {"X-Airbnb-Api-Key": api_key, "Accept": "application/json"}


def decode_cursor(cursor: str) -> dict | None:
    """Airbnb's cursor is plaintext base64 JSON; decoding it lets us verify offset.

    Used to prove the paginator actually advanced rather than re-serving page 1,
    which is the failure that makes a repeated page look like a full sweep.
    """
    try:
        return json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception:
        return None


def parse_search(body: str, *, fetched_at: float | None = None) -> dict:
    """-> {'activities': [...], 'next_cursor': str|None, 'filtered': bool}"""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ContractError(
            f"Airbnb returned a non-JSON body ({body[:120]!r})."
        ) from exc

    if payload.get("errors"):
        raise ContractError(f"Airbnb GraphQL errors: {payload['errors'][:2]}")

    try:
        results = payload["data"]["presentation"]["experiencesSearch"]["results"]
    except (KeyError, TypeError) as exc:
        raise ContractError(
            "Airbnb payload is missing data.presentation.experiencesSearch.results "
            f"(top-level keys: {list(payload)[:6]}). The persisted query may have "
            "changed shape."
        ) from exc

    nodes = results.get("searchResults")
    if nodes is None:
        raise ContractError("Airbnb results has no 'searchResults' key")

    activities = [
        a for a in (_node_to_activity(n, fetched_at) for n in nodes) if a
    ]
    # The continuation flag gets the same contract check as the rows. Every
    # real page — all 26 cached from a live Hanoi sweep on 2026-09-02,
    # including the 20 last pages — carries `paginationInfo.nextPageCursor`,
    # as an explicit null on the last one. So a MISSING key is a shape
    # change, and reading it as "last page" would let `paginate` mark a
    # one-page sample exhausted and the catalogue complete. Absent is not
    # "no more".
    pagination = results.get("paginationInfo")
    if not isinstance(pagination, dict) or "nextPageCursor" not in pagination:
        raise ContractError(
            "Airbnb results has no paginationInfo.nextPageCursor. Every real "
            "page carries it (null on the last page); its absence means the "
            "persisted query changed shape, not that the pool ended.")
    return {
        "activities": activities,
        "next_cursor": pagination.get("nextPageCursor"),
        "filtered": bool((results.get("pageMetadata") or {}).get("isFilteredSearch")),
        "total": None,   # Airbnb never reports one; do not invent a number.
    }


def _text(desc: dict | None) -> str | None:
    if not isinstance(desc, dict):
        return None
    val = (desc.get("localizedValue") or {})
    return val.get("localizedStringWithTranslationPreference")


def _node_to_activity(node: dict, fetched_at: float | None) -> Activity | None:
    if not isinstance(node, dict) or node.get("__typename") != "ExperienceSearchResult":
        return None
    eid = node.get("id")
    if not eid:
        return None

    listing = node.get("listing") or {}
    descriptions = listing.get("descriptions") or {}

    stats = ((listing.get("listingRatingStats") or {}).get("overallRatingStats") or {})
    # ratingCount arrives as a STRING ("1387"); classify_rating coerces it.
    rating, count, state = model.classify_rating(stats.get("ratingAverage"),
                                                 stats.get("ratingCount"))

    price_display, qualifier, amount, currency = _price(node.get("displayPrice"))

    duration = None
    edges = (((listing.get("offerings") or {}).get("publishedOfferings") or {})
             .get("edges") or [])
    for e in edges:
        mins = ((e or {}).get("node") or {}).get("durationMinutes")
        if isinstance(mins, (int, float)) and mins > 0:
            duration = _format_duration(int(mins))
            break

    tags = tuple(b for b in (node.get("searchBadges") or []) if isinstance(b, str))
    if qualifier:
        tags = tags + (f"priced {qualifier.strip()}",)

    return Activity(
        source=NAME,
        # Airbnb Experiences sells exactly one vertical. Stating it beats
        # leaving it None, which means "the source did not say".
        vertical="experience",
        source_id=str(eid),
        title=_text(descriptions.get("name")) or "",
        url=f"https://www.airbnb.com/experiences/{eid}",
        category=node.get("primaryThemeFormatted"),
        neighborhood=node.get("activityLocation"),
        price_amount=amount,
        price_currency=currency,
        price_display=price_display,
        rating=rating,
        review_count=count,
        rating_state=state,
        duration_text=duration,
        tags=tags,
        image_url=(node.get("picture") or {}).get("poster"),
        description=_text(descriptions.get("byline")),
        fetched_at=fetched_at,
    )


def _price(display: dict | None) -> tuple[str | None, str | None, float | None, str | None]:
    """Return (display, qualifier, amount, currency).

    The qualifier ('/ guest' vs '/ group') is carried out rather than dropped:
    14 of 202 Hanoi listings are priced per group, and silently ranking those
    against per-guest prices compares two different units.
    """
    if not isinstance(display, dict):
        return None, None, None, None
    line = display.get("primaryLine") or {}
    label = line.get("accessibilityLabel")

    payable = qualifier = None
    for comp in line.get("orderedComponents") or []:
        if not isinstance(comp, dict):
            continue
        if comp.get("discountedPrice") and payable is None:
            payable = comp["discountedPrice"]
        elif comp.get("price") and payable is None:
            payable = comp["price"]
        if comp.get("qualifier"):
            qualifier = comp["qualifier"]

    amount, currency = model.parse_price(payable or label)
    return label, qualifier, amount, currency


def _format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:g} hr" if hours != 1 else "1 hr"


def fetch_search(client, query: str, *, page: int = 1, size: int = PAGE_SIZE,
                 lang: str = "en", place_id: str | None = None,
                 category_tag: str | None = None, cursor: str | None = None,
                 currency: str = "USD", language: str | None = None) -> dict:
    """One page. The client owns the socket, the cache, the robots gate, pacing.

    ``query``/``place_id`` are a pair: Airbnb's experiences search has **no
    free-text keyword filter**, so ``query`` is the location string that
    accompanies the placeId, never a search term. A caller passing "cooking
    class" here would get the whole city back and quietly believe it was
    filtered — so callers filter by ``category_tag`` or client-side, and
    `sweep_place` below owns the paging.
    """
    if not place_id:
        raise ValueError("airbnb.fetch_search requires place_id")
    url = search_url(place_id, query, cursor=cursor, size=size,
                     category_tag=category_tag, currency=currency, locale=lang,
                     language=language)
    body = client.get(url, ttl_s=config.TTL_SEARCH_S, headers=headers())
    return parse_search(body, fetched_at=client._clock())


def paginate(client, place_id: str, query: str, *, category_tag: str | None = None,
             size: int = PAGE_SIZE, max_pages: int = MAX_PAGE,
             currency: str = "USD",
             language: str | None = None) -> tuple[list[Activity], bool]:
    """Follow the cursor to exhaustion. Returns (activities, walked_to_the_end).

    Two guards, both earned from measured behaviour:

    * **Cursor must advance.** Airbnb hands back a plaintext base64 offset; if a
      response repeats the previous offset we stop rather than loop, because a
      re-served page looks exactly like fresh data to a naive `while cursor`.
    * ``walked_to_the_end`` is False when we stopped on the page budget rather
      than on a null cursor — the caller needs to know it holds a prefix, not a
      catalogue.
    """
    seen: dict[str, Activity] = {}
    cursor: str | None = None
    last_offset: int | None = None
    exhausted = False

    for _ in range(max_pages):
        batch = fetch_search(client, query, size=size, place_id=place_id,
                             category_tag=category_tag, cursor=cursor,
                             currency=currency, language=language)
        for a in batch["activities"]:
            seen.setdefault(a.source_id, a)

        cursor = batch.get("next_cursor")
        if not cursor:
            exhausted = True
            break

        decoded = decode_cursor(cursor) or {}
        offset = decoded.get("items_offset")
        if offset is not None and last_offset is not None and offset <= last_offset:
            # Not progress. Stop and report a prefix rather than spin.
            break
        last_offset = offset

    return list(seen.values()), exhausted


def sweep_place(client, place_id: str, query: str, *, categories=None,
                size: int = PAGE_SIZE, currency: str = "USD",
                language: str | None = None, max_pages: int = MAX_PAGE,
                on_progress=None) -> dict:
    """Union the per-category sweeps — the only route to a complete catalogue.

    Measured 2026-08-26: the *unfiltered* grid is a ranked feed that omits
    listings. A Cooking-filtered sweep returned 35 rows; the unfiltered sweep of
    the same city returned 34 and never surfaced id 910096 across five pages.
    So paginating the unfiltered grid to a null cursor yields a *ranked sample*
    that looks exactly like a catalogue. Sweeping the category tags and unioning
    is what actually covers it.

    The unfiltered pass is still run first: it is the only thing that catches
    listings whose theme is not among the known pills.
    """
    tags = CATEGORY_TAGS if categories is None else {
        k: v for k, v in CATEGORY_TAGS.items() if k in set(categories)
    }

    seen: dict[str, Activity] = {}
    passes: list[dict] = []
    incomplete: list[str] = []

    def run(label: str, tag: str | None):
        try:
            items, exhausted = paginate(client, place_id, query,
                                        category_tag=tag, size=size,
                                        currency=currency, language=language,
                                        max_pages=max_pages)
        except transport.INCIDENTS:
            raise           # a host-wide stop, not a fact about this pass
        except Exception as exc:  # noqa: BLE001 — recorded, never swallowed
            passes.append({"pass": label, "error": f"{type(exc).__name__}: {exc}",
                           "returned": 0, "new": 0, "exhausted": False})
            incomplete.append(label)
            return
        before = len(seen)
        for a in items:
            seen.setdefault(a.source_id, a)
        passes.append({"pass": label, "returned": len(items),
                       "new": len(seen) - before, "exhausted": exhausted,
                       "error": None})
        if not exhausted:
            incomplete.append(label)
        if on_progress:
            on_progress(passes[-1], len(seen))

    run("(unfiltered)", None)
    for label, tag in tags.items():
        run(label, tag)

    return {
        "source": NAME,
        "activities": list(seen.values()),
        "passes": passes,
        "incomplete_passes": incomplete,
        "complete": not incomplete,
    }
