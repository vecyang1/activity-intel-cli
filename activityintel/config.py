"""Locations, pacing, and cache policy. No credentials live here or anywhere.

Every source this package reads is a **public, unauthenticated** endpoint. That
is a deliberate constraint, not an accident of the current implementation: the
moment a source needs a key, it must be read through that provider's own
resolver rather than copied here, and `sources/README` records why.

`home()` re-reads the environment on every call so a test can point it at a
sandbox after import. That is the same asymmetry the test sandbox depends on:
a *location* variable must be SET to something disposable, never unset, because
unset means "use the default" and the default is the user's real database.
"""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

APP = "activity-intel"

# Pacing. These are floors on the gap between requests to one host, shared
# across processes through the store's `pace` table. Measured politeness, not a
# published quota: none of these sources documents a rate limit, so the number
# is chosen to stay far below anything that would look like load.
DEFAULT_REQUEST_GAP_S = 1.0
MAX_RETRIES = 3

# Cache TTLs, in seconds. Search results move (price changes, new listings), so
# they expire quickly; a fetched detail page is stable for longer. Nothing here
# is retained indefinitely — see store.purge_expired.
TTL_SEARCH_S = 6 * 3600
TTL_DETAIL_S = 24 * 3600

# A sweep that cannot cover its pool must refuse rather than truncate. This is
# the ceiling on pages any single sweep will walk before it reports PARTIAL.
MAX_SWEEP_PAGES = 40

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# -- currency ------------------------------------------------------------------
#
# Klook hard-pins its search prices to HKD. Measured 2026-08-27: `k_currency`,
# `currency`, an `X-Klook-Currency` header, `kepler_currency`/`currency` cookies
# and a different `k_lang` locale ALL returned the identical `HK$` strings. So a
# multi-source ranking necessarily mixes currencies, and a table that sorts
# "236" next to "33" without saying they are different units is simply wrong.
#
# The conversion below is therefore **explicit and labelled**, never implicit:
# `price_amount`/`price_currency` keep exactly what the source said, and
# `price_usd` is a clearly derived comparison column that is `None` whenever we
# cannot honestly produce it.
#
# Pinning a rate in source is normally a rot hazard. HKD is the exception that
# makes it defensible here: the HKMA operates a Linked Exchange Rate System with
# a hard 7.75-7.85 band, so the worst-case error is ~1.3% and has been for four
# decades. Rates NOT marked `pegged` get a staleness warning instead, because
# for them the hazard is real.
FX_AS_OF = "2026-08-27"
FX_TO_USD = {
    # code: (units per 1 USD, pegged?)
    "USD": (1.0, True),
    "HKD": (7.80, True),    # HKMA band 7.75-7.85
    "VND": (25000.0, False),
    "THB": (34.0, False),
    "SGD": (1.30, False),
    "TWD": (31.0, False),
    "CNY": (7.15, False),
    "EUR": (0.92, False),
    "GBP": (0.78, False),
    "JPY": (150.0, False),
    "KRW": (1350.0, False),
    "AUD": (1.50, False),
    "MYR": (4.40, False),
}
# How long an unpegged pinned rate may go unreviewed before output says so.
FX_STALE_AFTER_DAYS = 120


# -- TLS trust -----------------------------------------------------------------
#
# Measured 2026-08-28, and it decides whether this tool runs at all in the
# owner's own terminal:
#
#     /opt/homebrew/bin/python3 (3.14)   -> 193 CA certificates
#     /usr/local/bin/python3    (3.12)   ->   0 CA certificates
#
# The second is a python.org framework build whose `Install Certificates.command`
# was never run, so `ssl.create_default_context()` trusts nothing and EVERY
# https call dies with CERTIFICATE_VERIFY_FAILED. That interpreter is the one
# first on `PATH` in a login shell — i.e. the one a human gets — while the
# checks all ran under the other one. A green suite and a working `/tmp` run
# said nothing about it.
#
# certifi ships a CA bundle and is installed for both interpreters, so falling
# back to it makes the tool work without asking the user to repair Python. When
# certifi is missing too, `tls_remedy()` gives the error a fix instead of a
# diagnosis.
_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                pass          # leave it unverified-capable; the error explains
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT


def tls_is_usable() -> bool:
    return bool(ssl_context().get_ca_certs())


def tls_remedy() -> str:
    return (
        f"This Python ({sys.executable}) has an EMPTY certificate store, so every "
        f"https request fails. Nothing is wrong with the network or the site.\n"
        f"Fix one of:\n"
        f"  python3 -m pip install --upgrade certifi\n"
        f"  '/Applications/Python 3.12/Install Certificates.command'   "
        f"(python.org builds)\n"
        f"  export SSL_CERT_FILE=$(python3 -m certifi)\n"
        f"Or run activity-intel under an interpreter that has one — "
        f"/opt/homebrew/bin/python3 does."
    )


def home() -> Path:
    """Where the cache/ledger database lives. Overridable via ACTIVITY_INTEL_HOME."""
    override = os.environ.get("ACTIVITY_INTEL_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP


def db_path() -> Path:
    return home() / "activities.db"


def request_gap_s() -> float:
    raw = os.environ.get("ACTIVITY_INTEL_REQUEST_GAP_S")
    if not raw:
        return DEFAULT_REQUEST_GAP_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_REQUEST_GAP_S
