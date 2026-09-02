"""Live contract checks behind ``activity-intel doctor``.

This is the command that turns "the tool returned nothing" into a specific
diagnosis. It is separate from the unit suite on purpose: the suite runs
against fixtures and must stay hermetic, so only something that deliberately
touches the network can notice that an endpoint moved.

Split out of ``cli`` on 2026-09-02. ``source_available`` is injected rather
than imported so this module never imports the command layer.
"""
from __future__ import annotations

import sys

from . import config, model, robots, store, transport
from .places import PLACES
from .sources import airbnb, klook, viator


def viator_status() -> str:
    """What `doctor` and a reader should believe about Viator right now.

    "ready" was the word when a key was present, and nothing was ready: no
    command could fetch from it. Say the two facts separately.
    """
    if not viator.available():
        return f"needs-key: {viator.UNAVAILABLE_REASON.splitlines()[0]}"
    return (f"key present in ${viator.KEY_ENV}, but this build has no sweep "
            f"wired for viator (its search is a POST and the transport has "
            f"none) — catalog/search/compare report it as not wired")


def run_checks(client, *, ignore_robots: bool, source_available) -> list[dict]:
    """Run every check and return ``[{check, ok, detail}, ...]`` in order."""
    checks = []

    def record(name, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 — the point is to report it
            checks.append({"check": name, "ok": False,
                           "detail": f"{type(exc).__name__}: {exc}"})

    def klook_default_off_check():
        """Klook must be OFF unless the operator explicitly overrides robots.

        Deliberately built with its own default-policy gate rather than the
        caller's: running ``doctor --ignore-robots`` must not make this check
        pass by adopting the very setting it is here to verify. A check that
        cannot go red under the condition it guards is not evidence.
        """
        if source_available(klook, ignore_robots=False):
            raise RuntimeError(
                "klook is available under DEFAULT policy — it is robots-disallowed "
                "and must require an explicit --ignore-robots")
        if not source_available(klook, ignore_robots=True):
            raise RuntimeError(
                "klook is unavailable even WITH --ignore-robots — the override no "
                "longer reaches the source, so the documented flag is a no-op")

        strict = transport.Client(
            store.connect(),
            robots_gate=robots.RobotsGate(robots.default_fetcher, enabled=True))
        try:
            klook.fetch_search(strict, "Hanoi cooking class", page=1, size=5)
        except robots.Disallowed:
            return ("default policy: klook off and refused by the gate; "
                    "--ignore-robots enables it")
        except transport.TransportError as exc:
            return f"klook unreachable ({type(exc).__name__}); still off by default"
        finally:
            strict.conn.close()
        raise RuntimeError(
            "klook search was NOT refused under default policy — robots.txt may have "
            "changed, or the gate regressed. It was Disallow: */search/* on 2026-08-27.")

    def klook_endpoint_check():
        """Under override, is the pinned search endpoint still the real one?

        Reported separately from the policy check above so 'we choose not to
        read it' and 'it stopped working' never collapse into one status.
        """
        if not ignore_robots:
            return "skipped — run doctor --ignore-robots to exercise the endpoint"
        r = klook.fetch_search(client, "Hanoi cooking class", page=1, size=5)
        n = len(r["activities"])
        if n == 0:
            raise RuntimeError("0 activities for a query known to have ~29 — the "
                               "payload shape or the endpoint moved")
        rated = sum(1 for a in r["activities"] if a.rating_state == model.RATED)
        return (f"{n} activities, {rated} rated, total={r['total']}, "
                f"capped={r['capped']}")

    def airbnb_check():
        place = PLACES["hanoi"]
        r = airbnb.fetch_search(client, place.airbnb_query, size=5,
                                place_id=place.airbnb_place_id)
        n = len(r["activities"])
        if n == 0:
            raise RuntimeError("0 activities — persisted-query hash or key may have rotated")
        priced = sum(1 for a in r["activities"] if a.price_amount is not None)
        return f"{n} activities, {priced} priced, cursor={'yes' if r['next_cursor'] else 'no'}"

    def robots_check():
        gate = robots.RobotsGate(robots.default_fetcher)
        gate.check("https://www.airbnb.com/api/v3/ExperiencesSearch/x")
        try:
            gate.check("https://www.airbnb.com/s/Hanoi--Vietnam/experiences")
        except robots.Disallowed:
            return "gate live: /api/v3 allowed, /s/*/* correctly refused"
        raise RuntimeError(
            "/s/*/* was NOT refused — robots.txt changed or the gate regressed. "
            "That path was disallowed on 2026-08-26 and this tool must not fetch it.")

    def tls_check():
        """Does THIS interpreter trust anything? Measured, not assumed.

        Added because the answer differed between the interpreter every check
        ran under (/opt/homebrew/bin/python3, 193 CAs) and the one first on a
        login shell's PATH (/usr/local/bin/python3, 0 CAs). Under the second,
        every https call failed and the tool returned an honest, complete,
        entirely empty catalogue.
        """
        ctx = config.ssl_context()
        n = len(ctx.get_ca_certs())
        if n == 0:
            raise RuntimeError(config.tls_remedy())
        return f"{n} CA certs available to {sys.executable}"

    record("tls trust store", tls_check)
    record("robots gate", robots_check)
    record("klook off by default", klook_default_off_check)
    record("klook search contract", klook_endpoint_check)
    record("airbnb search contract", airbnb_check)
    record("viator key", viator_status)
    return checks
