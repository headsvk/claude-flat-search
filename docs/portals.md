# Portal behaviour, as measured

Reference, not configuration. Nothing here is read by any script — the config is
[examples/criteria.example.toml](../examples/criteria.example.toml).

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

## Availability dates

Whether a portal publishes one decides which listings can ever be ranked on
timing — but *publishing* and *filtering* turn out to be separate questions, and
a portal that will not tell you the date may still let you ask for it.

| Portal | states a date | filters on one | where |
|---|---|---|---|
| Rightmove | mostly | **yes**, `moveInByDate=2026-11-05` | `letAvailableDate` in the search payload, and a "Let available date" label on the detail page — value is a date, `Now`, or `Ask agent` |
| OpenRent | sometimes | **yes**, `availableBefore=2026-12-11` | prose on the card and the detail page: "Available to move in from 08 November 2026" |
| Zoopla | yes | **yes**, `available_from=1months` | a **field** on the detail page — "Available from 8 November 2026", with beds, baths and EPC, above the description; agents often repeat it in the description too |
| OnTheMarket | **usually** | none found | labelled inside "Letting details": `Availability date: 23 Sep 2026`, or `Available now` |

**This table said "not published", and it cost both portals entirely.** Until
2026-09-21 it recorded Zoopla and OnTheMarket as publishing no date, so neither
extractor looked for one, and 0 of 93 Zoopla and 0 of 74 OnTheMarket records
carried one. Both publish it. OnTheMarket labels it inside "Letting details";
Zoopla puts it in the key-facts block on the detail page, next to beds, baths
and EPC — confirmed on a live page that day.

**Mind what a number is measured over.** 64 of 79 OnTheMarket and 32 of 96
Zoopla cached pages carry availability text — but that is a measure of THIS
PIPELINE's cache, not of the portals. The cache holds what the extractors keep,
and `zoopla_detail` keeps the "About this property" and "Property description"
slices only, so Zoopla's facts block never reaches it: 32 of 96 is a floor on
our own extraction and says nothing about how often Zoopla states a date. The
parser runs over the whole page text at fetch time, so it reads the field
regardless — but do not quote that 32% as a portal statistic, which is exactly
the error that produced the "not published" row above.

OpenRent was worse than absent: its own 20-character regex stored "to move in",
"Today" and "to move in from 31 A" on 106 records, every one of which
`parse_date` read as nothing, so the field looked populated and ranked nothing.

`portals.availability()` now reads all of it — labels, prose, "now" and
"immediately", a comma before the year, a month with no day, a day with no year
— and returns only what parses, so "parking available (at extra cost)" stays
empty rather than becoming a date. Coverage after it, over the same cache:
OnTheMarket 64 of 79, Rightmove 300 of 421, OpenRent 120 of 378, Zoopla 31 of 96.

A listing that states nothing anywhere is read as available now (decided
2026-09-21) and says so in its notes — it was never demoted for the silence
either way. Older figures, measured against `state.json` on 2026-09-20 and kept
because they are what the claim above was built on: Rightmove 299 of 390 dated,
OpenRent 97 of 367, Zoopla and OnTheMarket 0. So "OpenRent states a date" was
true of the format and not of the listings, and a quarter of Rightmove's have none
either.

**OpenRent's dates are worse than that count suggests.** Of 677 listings from one
scoped search, 140 showed a date on the card — and the detail pages of a sample of
ten stated theirs as `Today`, `30 September` or `02 October`. `core.parse_date`
reads `%Y-%m-%d`, `%d %B %Y`, `%d %b %Y` and `%d/%m/%Y`, so **none of those three
forms parses**: no year, or not a date at all. An unparseable date is treated as
no date, which is the safe direction — unknown never rejects — but it means
timing ranking has effectively never applied to OpenRent, and the count above
overstates what the pipeline can actually use.

**These filters are documented here and deliberately not used.** The pipeline
never adds one, never reads one back, and warns at `check` time if a search URL
still carries one. `move_in` is filtered locally instead, in `hard_filter`,
against the date each listing states.

The reason is in the counts above. Widening Zoopla's window from three months to
twelve adds **28 listings** and stops at 6139 of 8469: no window ever reaches the
other ~2330, which is what you would expect of listings the portal holds no
availability data for, not of listings that are merely late. Rightmove behaves
the same way, ceiling ~3744 of 5374. So the filter's real effect is to drop about
a third of each portal's inventory for having nothing to say — rejecting on
missing data, server-side, where nothing downstream can flag it. That is the one
move this pipeline refuses everywhere else.

The original case for using them was that Zoopla published no date, so the filter
was the only evidence there was. That turned out to be false (see the table
above), which removed the argument along with the premise.

**The guarantee was checked, not assumed.** A filter that merely *narrows* while
quietly passing through listings it has no date for would make that marking a
lie. Ten listings returned by `availableBefore=2026-10-15` were fetched and their
detail pages read: every one stated `Today`, `30 September`, `01 October` or
`02 October` — all at or before the cutoff, none after. That is a sample of ten
against a monotonic count curve, not a proof; if a listing ever turns up marked
verified with a stated date after the window, this is the assumption that broke.

See **Search windows** below for what the filtering costs.

## Search windows

Two filters that narrow a search *before* the portal sends anything: how
recently a listing was added, and when it becomes available. Measured 2026-09-20
on the live 2 bed / 2 bath / £4500 London search, off each portal's own total —
Zoopla's `[data-testid="total-results"]`, Rightmove's `resultCount`.

| Zoopla — **8469** unscoped | results | | Rightmove — **5374** unscoped | results |
|---|---|---|---|---|
| `added=24_hours` | 72 | | `maxDaysSinceAdded=1` | 73 |
| `added=3_days` | 453 | | `maxDaysSinceAdded=3` | 479 |
| `added=7_days` | 1644 | | `maxDaysSinceAdded=7` | 1536 |
| `added=14_days` | 2862 | | `maxDaysSinceAdded=14` | 2565 |
| `added=30_days` | 4360 | | `maxDaysSinceAdded=30` | **0 — not a window** |
| `available_from=1months` | 5208 | | `moveInByDate=2026-10-01` | 2427 |
| `available_from=3months` | 6111 | | `moveInByDate=2026-11-05` | 3413 |
| `available_from=6months` | 6124 | | `moveInByDate=2027-06-01` | 3744 |
| `available_from=12months` | 6139 | | | |

| OnTheMarket — **15519** unscoped | results |
|---|---|
| `recently-added=24-hours` | 141 |
| `recently-added=3-days` | 1882 |
| `recently-added=7-days` | 4116 |
| `recently-added=14-days` | **0 — its ceiling is 7 days** |

They compose within a portal: `added=3_days&available_from=1months` returned
242, and `maxDaysSinceAdded=3&moveInByDate=2026-11-05` returned 297.

**OnTheMarket has no availability filter.** `available-from=`, `move-in-by=` and
`availability=` each returned the unfiltered 15519 — accepted and ignored, the
same way it treats `min-bathrooms`. Those are guessed spellings against a portal
that exposes no such control, so this is "none found", not "proven absent".

**Most of these fail closed on a value they do not recognise.**
`maxDaysSinceAdded=30`, `maxDaysSinceAdded=nonsense`, `moveInByDate=someday`,
`recently-added=14-days` and `recently-added=nonsense` each returned **zero
results**, and so does `available_from=immediately` — which is a value Zoopla's
own filter UI offers. An empty search is indistinguishable
from a block, so an unsupported value here does not degrade gracefully, it
fabricates a quiet morning. Only Zoopla's `added=` is forgiving:
`added=nonsense_value` returned the unfiltered 8469.

That is why the spellings live in `RECENT_WINDOWS` and `AVAILABILITY_WINDOWS` in
`portals.py`, where each one has a measured number beside it, and the config only
ever supplies a number of days or a date. Note the ceilings, which differ and are
not "wider is fine": Zoopla scopes to 30 days, Rightmove to 14, OnTheMarket to 7,
and one step past each is an empty result set rather than a broader one.

### Which window a run asks for

Recency scoping replaces a full walk with arithmetic. `--incremental` stops early
only on a newest-first portal and only after two ~90%-stale pages, so it is a
client-side heuristic laid over fetching the backlog anyway; a window is settled
before a single page is sent.

The size comes from `state.json`'s `updated` stamp — the gap since the last run
that committed — plus a 2-day margin, snapped **up** to a window the portal
offers. Up, because a window narrower than the gap never fetches what arrived in
between and nothing downstream can tell that from a quiet morning. A gap wider
than the widest window that portal offers is therefore **not scoped at all**:
paging the backlog is the cost of having been away, and because the ceilings
differ, a 10-day gap scopes Zoopla and Rightmove to 14 days and leaves
OnTheMarket unscoped. A
fresh clone has no stamp and uses `[run] first_run_days` (default 14).
`flat-search check` prints the window the next run would ask for; every run logs
it.

A run that searched and then refused to commit — a challenged portal — leaves
the stamp alone, so the next run widens to cover the morning that was lost.

### What the availability filter costs

It applies only when `move_in` is set, and the two portals take it differently.

**Rightmove takes the date itself**, so there is nothing to round: the filter is
set to `move_in + move_in_slack_days` exactly, and no reach is too short to use.

**Zoopla takes buckets**, so the request is snapped **down** to the widest window
ending no later than that same date. Down, because the window is what makes the
result verifiable and it must not admit a date past the one you asked for. Under
a month of reach nothing fits — the narrowest bucket still overshoots — so the
search is left unscoped and those listings stay unconfirmed.

The cost is real on both, and it is not only late listings. Even a date eight
months out returns 3744 of Rightmove's 5374, and Zoopla's widest window returns
6139 of 8469 — so asking the question at all drops roughly a quarter to a third
of the result set, presumably everything the portal cannot date. Those are
exactly the listings that would otherwise be kept and flagged. That is the trade
to weigh before putting the filter into a search URL — and it is per portal, so
it can be worth it on one and not another. Nothing adds it for you: the pipeline
reads whatever the URL carries and never writes one, so leaving it out keeps
those listings and ranks them on the dates they state.

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

**OnTheMarket's pagination was silently dead until 2026-09-20**, and this is the
shape to watch for. A search extractor returns `(rows, total)`, and `collect`
stops paging once it has seen `total` listings. `otm_search` returned the length
of the page it had just parsed, so the test was `30 >= 30` and every OnTheMarket
search ended after page one — 30 listings out of a `totalResults` of 15519, on
every run since it was written. Nothing failed: the portal was merely low, and
only *zero* was being watched for. The count it should have returned was sitting
in the same payload the listings came from.

Checked against the other three at the same time: Rightmove returns
`searchResults.resultCount` and Zoopla its `total-results` testid, both real
server counts. OpenRent returns the page length, which is correct there — it has
no page parameter, so one scrolled "page" *is* the whole result set. `&page=2`
on OnTheMarket serves different listings with `totalResults` unchanged, so the
parameter works; the fix was reading the right number, not paging differently.

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
