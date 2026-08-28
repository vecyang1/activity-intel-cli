# Sources — what we may read, and what we may not

Scope: **personal trip research at low volume.** A few dozen paced requests to
plan a trip. This is not a bulk harvester and must not be repurposed as one.

`activityintel/robots.py` enforces the robots.txt half of this table in code,
in front of every socket **and in front of the cache** — a cached body is still
a fetch decision, and a gate below the cache lets one override run poison every
later strict one. Path matching is `activityintel/rfc9309.py`, not
`urllib.robotparser`: the stdlib ignores a mid-path `*` on Python 3.12 and
honours it on 3.14, and nothing here chooses the interpreter.

The rest is judgement, recorded here.

**One row is reachable only through an explicit operator override.** robots.txt
is a voluntary crawl directive, and the owner of this machine decided on
2026-08-27 that it does not bind their own low-volume trip research on Klook.
That decision is encoded as `--ignore-robots`, and it is deliberately narrow on
three axes:

* **Off by default**, and announced on stderr for every host it touches.
* **Per-host, not global.** It exempts `www.klook.com` alone. Airbnb's rules
  keep being enforced while the flag is on — including its disallowed `/s/*/*`.
  An override wider than its need retires the guard that catches the next
  unrelated bug, which is not hypothetical: `/s/*/*` is the exact path an
  earlier version of this tool fetched by mistake.
* **Robots only.** It does **not** extend to bot-protection blocks — Klook's 403
  activity pages stay unread, and no code here may attempt them.

| Source | robots.txt | Reachable | Status | Why |
|---|---|---|---|---|
| **Airbnb Experiences** | `/api/v3/ExperiencesSearch/` **allowed**; `/s/*/*` **disallowed** | Yes, cookieless | **ENABLED** | Uses the allowed API path only. 219 Hanoi experiences with price, rating, reviews, duration, neighbourhood, category. |
| **Viator** | API host unrestricted | Yes (needs key) | **READY, needs owner key** | The only *sanctioned* route: Basic Access is free, self-serve, no traffic minimum. Adapter written, unit-tested, **never run against a live key**. |
| **Klook** | `Disallow: */search/*` — **matches our endpoint** | Search API yes (HTTP 200); activity pages **403** | **OFF by default — `--ignore-robots` enables** | 1,023 Hanoi listings with price, rating, review count. Free-text keyword search, unlike Airbnb. Prices always HKD. See below. |
| GetYourGuide | unreadable (403) | No | SKIP | Partner API requires **100,000 monthly visits**. |
| TripAdvisor | attractions allowed | No — DataDome 403 | SKIP | Content API returns POIs, not bookable tours; its activity inventory *is* Viator's. |
| Traveloka | `Disallow: /api/` | No — DataDome 403 | SKIP | B2B partner network only. |
| Trip.com | `Disallow: /restapi/soa2/*` | Page yes, data no | SKIP | The only endpoint returning listings is disallowed by name. |

## Klook — the split verdict

Measured 2026-08-26, re-verified 2026-08-27. Two different questions, two
different answers:

| Route | Result |
|---|---|
| `/v1/cardinfocenterservicesrv/search/platform/complete_search_v3` | robots.txt `Disallow: */search/*` under `User-Agent: *` matches the path — the `/search/` segment has characters before it, which is exactly what the leading `*` covers. |
| `/en-US/activity/<id>-<slug>/` | **HTTP 403** (Akamai). Allowed by robots, blocked by bot protection. |
| `/en-US/city/34-hanoi/` | HTTP 403, browser included. |
| `sitemap.xml`, `llms.txt` | HTTP 200, explicitly `Allow`-listed — but URLs and marketing prose only. No price, rating, review count, or language. |

So the only surface returning structured data is the one robots.txt forbids, and
every permitted surface either 403s or contains no data.

**The search endpoint is reachable under `--ignore-robots`** — a crawl directive
overridden knowingly, at low volume, by the person who owns the machine.
**The 403 is not, and must not become so.** That is active bot protection rather
than a directive, defeating it is a materially different act, and nothing in
this repo may grow code that attempts it. The practical consequence: per-activity
guided languages live on the detail page, so Klook cannot answer "is this taught
in Chinese?" — use Airbnb's `--language` for that.

**Klook runs an affiliate/partner API — that remains the route that needs no
override at all**, and `sources/klook.py` is what such an integration starts
from.

`doctor` asserts both halves and cannot be satisfied by passing the flag:
`klook off by default` builds its own default-policy gate, so
`doctor --ignore-robots` still verifies that the source is refused under normal
policy *and* that the override still reaches it.

### Measured Klook quirks that produce wrong answers if assumed

| Behaviour | Consequence |
|---|---|
| Never returns "no results" — a nonsense query gives ~15 confident Taipei listings | Every union must be scoped by place; `Place.scope_of` does it and reports the drop count |
| `total` caps at exactly 1000 for any broad query; page 21 returns `cards: []` while still reporting 1000 | `total` is a display ceiling, not a count. Reach comes from partitioning keywords, not paging deeper |
| `size` silently clamps to 50 | Asking 100 and believing you walked 100-item pages doubles your imagined coverage |
| Prices are **always HKD**. `k_currency`, `currency`, `X-Klook-Currency`, `kepler_currency`/`currency` cookies and a different `k_lang` were all measured and all ignored (2026-08-27) | Cross-source ranking needs `model.to_usd`; the native price is never overwritten |
| `k_lang=zh_CN` and `k_lang=en_US` return byte-identical card sets | It is not a language filter. Do not offer it as a way to find Chinese-guided tours |
| Language tags are sparse — only `nature_language_en`, on 19 of 50 Hanoi cards | An empty `languages` means *the payload did not say*, never "English only" |
| The tag service degrades silently (1 distinct tagKey on one call, 15 on the next, all HTTP 200) | `klook.tag_health` is the canary; a degraded run is flagged in `coverage` |
| `aggr_condition.filter_list` offers Price range / Others / Location only | There is no server-side language filter to reach for |
| **The search mixes verticals.** 441 of 1,072 Hanoi rows were hotel rooms; `data.category` said "Hotels" and `deep_link` pointed at `/hotels/detail/`, but the structural signal is `card_name` = `web_search_hotel_activity_01` (and numeric `data.vertical_type` = 102) | An activity catalogue must filter on the vertical, never the localized category label. `klook.split_verticals` keeps `ttd`/`fnd`, drops `hotel`/`carrental` **with counts**, and keeps an unknown vertical while naming it |
| A `ttd` card can carry `vertical_type` 104 and a `deep_link` to `/airport-transfers/?...` rather than `/activity/` | The URL path is not a reliable discriminator — a private airport transfer is a real bookable service Klook files under things-to-do. Key on the vertical |

## The distinction this file exists to hold

**robots.txt is not a licence.** A path being allowed there says only that the
site's crawl file does not object. Terms of Service are a separate document:
Airbnb's restrict automated collection, and Viator's affiliate API is
provisioned for partners who send booking traffic back. Access and permission
are different questions, and this tool answers only the first one in code.

That is why the volume boundary at the top of this file is part of the design
and not a footnote.

## Re-deriving Airbnb's pinned values

The public frontend API key and the persisted-query hash are baked into
Airbnb's own JS and rotate on deploy. `airbnb.explain_rotation()` prints the
current pins and the recovery steps when a request fails. Both are public
values from page source, not secrets — but note that the page carrying them
(`/s/*/*`) is robots-disallowed, so re-derive from an allowed page or from the
`ExperiencesSearchRoute` bundle on `a0.muscache.com`.

## Adding a source

1. Check robots.txt **first**, with `urllib.robotparser`, not by eye. If it is
   disallowed, the source is off by default — expose it through the existing
   `available(ignore_robots=...)` shape rather than a new flag or constant.
2. Confirm it is reachable with plain curl before writing a parser.
3. Write the adapter under `activityintel/sources/` — **it must not open a
   socket**; the transport is the only thing that does.
4. Capture a real response into `tests/fixtures/` unedited. Never hand-write one.
5. Add a `doctor` check that can actually fail.
6. If the source reports prices in a fixed currency, add it to
   `config.FX_TO_USD` with an honest `as_of`, or accept `price_usd: null`.
7. **Establish what verticals the source sells, and which of them are
   activities.** Every marketplace sells more than one kind of thing, and the
   one that is not an activity will be priced per night, rated 5.00, and
   indistinguishable from inventory in the ranking. Find the source's own
   structural signal for it — not a display label, which is localized and
   reworded — and give unknown values a third state so a vertical added next
   year is neither silently included nor silently dropped.
8. Add its data terms to `NOTICE` in the same change (AGENTS.md rule 9).
9. Update this table.
