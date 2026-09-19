# Portal behaviour, as measured

Reference, not configuration. Nothing here is read by any script — the config is
[criteria.example.md](criteria.example.md).

Everything below was established by running searches and counting results. **None of
it was taken from a portal's filter UI**, because several of these do not do what
the UI implies. If you change a search URL, test the change and count the results
before trusting it.

## Bathroom filters

| Portal | parameter | measured effect |
|---|---|---|
| Rightmove | `minBathrooms=2` | 99 → 47 |
| Zoopla | `baths_min=2` | below-2 count 10 → 0; `=3` narrows 28 → 18 |
| OpenRent | `bathrooms_min=2` | 2073 → 819; `=3` → 91 |
| OnTheMarket | `min-bathrooms=2` | **accepted and ignored**, 110 → 110 |

OnTheMarket's bathroom filtering therefore has to happen locally in `core.py`, which
is why its search URL carries no bathroom parameter — passing one would imply a
guarantee that does not exist.

OpenRent never prints a bathroom count on the card, but its server filter does work.
Those rows carry `bathrooms_verified_by_search` rather than an invented number.

## Sort order

Only a newest-first search may stop early on already-seen listings.

| Portal | newest-first parameter | verified |
|---|---|---|
| Rightmove | `sortType=6` | yes |
| Zoopla | `results_sort=newest_listings` | yes, read from its own `results_sort` control |
| OnTheMarket | `sort-field=update_date` | yes; mean listing age 5.8d → 1.1d |
| OpenRent | none found | **no** — treated as unsorted |

`fetch.py search --incremental` therefore stops early on the first three only.
OpenRent is always paged in full: stopping on "we have seen these" is unsound when
the order is relevance rather than recency.

OnTheMarket interleaves promoted listings, so its early stop needs the
two-consecutive-stale-pages rule rather than reacting to a single page.

## Pagination

| Portal | parameter | step |
|---|---|---|
| Rightmove | `&index=` | 24 |
| Zoopla | `&pn=` | 1 |
| OnTheMarket | `&page=` | 1 |
| OpenRent | infinite scroll, no page parameter | — |

**Zoopla's page parameter is UNVERIFIED.** Its sort order was confirmed; its
pagination never was, so a Zoopla search may only ever be reading page one. It
returned 223 listings on 2026-09-19, which suggests it does paginate, but that has
not been proven against a known total. Worth settling before trusting a quiet
Zoopla morning.

## Coverage of the fields that matter

Floor area, stated on the search card:

| Portal | coverage |
|---|---|
| Rightmove | 40% |
| Zoopla | 46% |
| OnTheMarket | 6% |
| OpenRent | 5% |

This is why `MIN_SQFT` rejects only a *stated* size. Requiring one would delete most
of the market on missing data rather than on a real mismatch.

Floor area is read from the page's own embedded JSON (`minimumAreaSqFt` on
OnTheMarket, `sizeSqFeet` on Zoopla — a string, and `""` when unknown, which simply
fails to match so unknown stays unknown). It is deliberately **not** scraped from
visible text: "N sq ft" there also matches a garden, a terrace, or a
price-per-square-foot line, and since `MIN_SQFT` is a hard filter a wrong size
*deletes* a listing rather than mislabelling it.

Of listings that do publish a size, roughly a third fall below 800 sq ft — so the
threshold is a real filter, not a formality.

## Region ids

Rightmove's `REGION^87490` is the London **postal** area, not Greater London.
Measured across ~75 listings it returned zero TW, KT, BR, HA or IG addresses —
Richmond, Kingston, Bromley, Harrow and Chigwell absent entirely. Outer areas each
need their own id:

| Area | id | Covers |
|---|---|---|
| London | `REGION^87490` | E, EC, N, NW, SE, SW, W, WC |
| Richmond | `REGION^1127` | TW9, TW10 |
| Twickenham | `REGION^1368` | TW1, TW2, TW11 |
| Kingston upon Thames | `REGION^746` | KT1, KT2, KT6 |
| Chigwell | `REGION^317` | IG7, IG8 |
| High Barnet | `OUTCODE^855` | EN5 |

An outer search is often a richer seam than all-London: 9 of 25 on page 1 of
Richmond met 2 bed / 2 bath / £4500, against 4 of 25 for all-London, because the
whole area qualifies.

Listings from these searches often carry no postcode at all ("Richmond, Surrey").
An unknown district is never rejected — it is ranked fringe and flagged.

## Extraction

Detail pages are read by **visible-heading text anchors**, not CSS selectors.
Generated class names churn (`text-link ml-1`), and selectors written against a
search page matched nothing on a detail page — 18 of 18 non-Rightmove fetches came
back empty.

**An end anchor must match at the start of a line.** It names a heading, and in
rendered text a heading stands alone on its line. Matched as a bare substring it
fires inside the prose it is meant to terminate: OpenRent's stock copy says "a flat
in a great **location**", which matched the `Location` anchor, truncated the
description below the minimum length, and dropped the listing with no error at all.
That silently lost 22% of OpenRent detail fetches. If you add an end anchor, ask
whether it is also an ordinary English word.

**Never reuse a browser page across navigations.** Zoopla serves the first listing
in full and blanks afterwards — an ordering artefact indistinguishable from a
broken filter. `details` retries an empty result once in a fresh session, which
recovers them. Watch for the same first-ok-then-blank shape on any portal.

Zoopla also 403s every browserless request, and renders zero cards behind its cookie
consent wall before escalating to a challenge on repeat visits. Consent is accepted
before anything is read.

**Rightmove quotes prime listings per week.** `£1,000 pw` is £4,333 pcm. Always
convert.

## After changing an extractor

Purge the cache it wrote. The cache is the skip condition, so bad entries are never
refetched — one listing sat at 135 characters and re-extracted at 2,098 once its
stale cache entry was removed.
