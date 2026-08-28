"""Viator adapter — the one source with a *sanctioned* API. Needs an owner key.

Why this exists while Klook does not: Viator publishes a partner API whose
Basic Access tier is self-serve, free, and has no traffic minimum. Using it is
consent, not a robots.txt technicality — which is the difference between a tool
that can be pointed at a real trip and one that cannot.

**Verified 2026-08-26 (no key):**

    POST https://api.viator.com/partner/products/search
    -> 400 {"code":"MISSING_HEADER_VALUE",
            "message":"Missing required header: exp-api-key"}

That is a validation error, not a bot challenge: ``api.viator.com`` is a
separate host from the DataDome-protected ``www.viator.com``, and it answers
plain curl. So the transport works and only the key is missing.

**Honest status: the request body shape below has never been validated against
a real key.** It follows Viator's published `/products/search` contract, but no
live 200 has been observed, so `AVAILABLE` stays False until a key exists and
`doctor` reports this source as `needs-key` rather than pretending readiness.
Do not report Viator data as available until that changes.

**Getting a key (owner action — an agent must not create accounts):**
1. Sign up at https://www.viator.com/partner/ (affiliate).
2. Basic Access is granted on signup; copy the API key.
3. ``export VIATOR_API_KEY=...`` — read here through ``os.environ`` only.
   Never write it into this repo, a config file in the project, or a fixture.

Note the licence question is separate from the access question: the affiliate
API is provisioned for partners sending booking traffic to viator.com. Read the
affiliate terms before this becomes anything user-facing — the same shape as the
Etsy API Terms note in the estate's memory, where the bar was on *analytics*
use rather than on access.
"""
from __future__ import annotations

import json
import os

from .. import model
from ..model import Activity

NAME = "viator"
HOST = "api.viator.com"
SEARCH_URL = "https://api.viator.com/partner/products/search"

MAX_PAGE_SIZE = 50
MAX_PAGE = 40

KEY_ENV = "VIATOR_API_KEY"


def api_key() -> str | None:
    """Read the key from the environment. Never from the repo."""
    key = os.environ.get(KEY_ENV)
    return key.strip() if key and key.strip() else None


AVAILABLE = False           # flipped by `available()` at import time below
UNAVAILABLE_REASON = (
    f"No {KEY_ENV} in the environment. Viator's Basic Access tier is free and "
    f"self-serve at https://www.viator.com/partner/ — sign up, then export the "
    f"key. This is an owner action; an agent must not create the account."
)


def available() -> bool:
    return api_key() is not None


AVAILABLE = available()


class ContractError(RuntimeError):
    """The response no longer has the shape we parse."""


class MissingKey(RuntimeError):
    """No API key. Actionable, and never silently degraded to an empty result."""


def headers(key: str | None = None) -> dict:
    key = key or api_key()
    if not key:
        raise MissingKey(UNAVAILABLE_REASON)
    return {
        "exp-api-key": key,
        "Accept": "application/json;version=2.0",
        "Content-Type": "application/json;version=2.0",
        "Accept-Language": "en-US",
    }


def search_body(destination_id: str, *, start: int = 1, count: int = MAX_PAGE_SIZE,
                currency: str = "USD") -> dict:
    count = max(1, min(count, MAX_PAGE_SIZE))
    return {
        "filtering": {"destination": str(destination_id)},
        "pagination": {"start": start, "count": count},
        "currency": currency,
    }


def parse_search(body: str, *, fetched_at: float | None = None) -> dict:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ContractError(f"Viator returned non-JSON: {body[:120]!r}") from exc

    if "products" not in payload:
        raise ContractError(
            f"Viator payload has no 'products' key (got {list(payload)[:6]}). "
            f"If this is an error envelope, the message is: "
            f"{payload.get('message') or payload.get('code')}")

    products = payload.get("products") or []
    total = payload.get("totalCount")
    activities = [a for a in (_product_to_activity(p, fetched_at) for p in products) if a]
    return {
        "activities": activities,
        "total": int(total) if isinstance(total, (int, float)) else None,
        # Viator reports a real totalCount, so completeness is checkable rather
        # than assumed: the caller compares len(union) against it.
        "capped": False,
    }


def _product_to_activity(p: dict, fetched_at: float | None) -> Activity | None:
    code = (p or {}).get("productCode")
    if not code:
        return None

    reviews = p.get("reviews") or {}
    rating, count, state = model.classify_rating(
        reviews.get("combinedAverageRating"), reviews.get("totalReviews"))

    pricing = (p.get("pricing") or {}).get("summary") or {}
    amount = pricing.get("fromPrice")
    currency = (p.get("pricing") or {}).get("currency")
    display = f"{currency} {amount}" if amount is not None and currency else None

    duration = None
    dur = (p.get("duration") or {})
    mins = dur.get("fixedDurationInMinutes")
    if isinstance(mins, (int, float)) and mins > 0:
        duration = f"{mins / 60:g} hr" if mins >= 60 else f"{int(mins)} min"

    images = p.get("images") or []
    image = None
    if images:
        variants = (images[0] or {}).get("variants") or []
        if variants:
            image = variants[-1].get("url")

    return Activity(
        source=NAME,
        source_id=str(code),
        title=p.get("title") or "",
        url=(p.get("productUrl")
             or f"https://www.viator.com/tours/d/{code}"),
        price_amount=float(amount) if isinstance(amount, (int, float)) else None,
        price_currency=currency,
        price_display=display,
        rating=rating,
        review_count=count,
        rating_state=state,
        duration_text=duration,
        tags=tuple(t for t in (p.get("flags") or []) if isinstance(t, str)),
        image_url=image,
        description=p.get("description"),
        fetched_at=fetched_at,
    )
