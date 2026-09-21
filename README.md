# UK flat search, read by Claude

A rental search that runs itself every morning and hands you a short list of only
what is new, ranked, with the things the portals will not let you filter on already
read out of the listing text.

It exists because the filters on Rightmove, Zoopla, OnTheMarket and OpenRent cannot
express what most people actually want. You can filter on bedrooms and price. You
cannot filter on *floor area over 800 sq ft*, *not a basement*, *has a lift if it is
above the third floor*, or *air conditioning in the flat rather than in the residents'
gym*. Those live in the prose, so this reads the prose.

Four portals, one browser each, run concurrently. How many listings that comes to is
entirely a function of your criteria — a wide net over a city runs to several hundred
tracked at a time, a narrow one to a few dozen. Either way a morning run turns up a
handful of genuinely new ones, which is the number that matters.

All four portals are national, and nothing in the filtering knows what a London
postcode is, so this works anywhere in the UK. The worked example is London because
that is where it was built and measured; [Outside London](#outside-london) is the
two things you change.

> **You do not have to set this up by hand.** Everything below — installing it,
> writing the config, working out the search URLs, running it on a schedule — is
> work you can hand to a coding agent. See [Let an agent set it up](#let-an-agent-set-it-up).

## How it works

    search  ->  audit  ->  plan  ->  details  ->  judge  ->  commit  ->  render

| Stage | What it does |
|---|---|
| `search` | Pages each portal's search results in a real browser. Cheap fields only. |
| `audit` | Per-portal field coverage. Refuses to certify a portal it has not sampled. |
| `plan` | Dedupes against what is already tracked, applies the hard filters, emits a fetch queue. |
| `details` | Fetches each listing page and extracts the description, amenities and floor area. |
| `judge` | Decides air conditioning. Deterministic for the silent majority; a model for the rest, once each. |
| `commit` | Validates every claim against the cached page, then writes state. |
| `render` | Writes today's "what's new" file and regenerates the full shortlist, duplicate ads collapsed. |

Two design choices carry most of the weight, and a third follows from them.

**Unknown never rejects.** An unstated size, district, bathroom count or lift is
recorded and flagged, never treated as absence. About 60% of listings state no floor
area at all and 0 of 80 sampled mention the *absence* of a lift, so a filter that
treats missing data as failing data deletes most of the market. `MIN_SQFT` rejects a
*stated* size below the floor and nothing else.

**Every claim has to quote the page.** The air-conditioning judgement is the one step
that needs a language model — telling "air conditioning throughout the apartment" from
"air conditioning in the residents' gym" is reading comprehension, not pattern
matching. So a model may only say `yes` while quoting the listing verbatim, and
`commit` re-checks that quote against the cached page text before it is believed. An
unbacked claim collapses to `unstated`. A wrong claim that looks like evidence is
worse than no claim.

The model is also kept off the easy 95%: a listing whose text contains no cooling
vocabulary at all is decided in Python, because `unstated` is a claim about the
absence of vocabulary and a word list settles it exactly.

A third choice follows from the first two. **The output collapses duplicate ads but
never duplicate flats.** One flat is routinely several rows — cross-listed on two
portals, or a new-build block marketing every unit it has under one address. Ads that
agree on price, address and bed/bath count and contradict each other on nothing they
state become one row carrying every link; flats that merely share a building stay
separate rows, marked with how many the block has going. Measured on one live set:
840 ads for 778 flats, and four ads at the same price in the same block sitting on
two different floors — which is why agreement on the *stated* fields is required
before anything merges.

---

*Everything above is the idea. Everything below is operating it — and you can read it
or delegate it.*

## Let an agent set it up

The setup is fiddly in a way that is tedious rather than difficult: a Python toolchain,
a Chromium binary, an OCR binary in a place that is not on `PATH`, a TOML file of
thresholds, and four search URLs whose query parameters have to agree with those
thresholds. None of it needs judgement. All of it is exactly what a coding agent is
good at, and this repo is built so one can do it end to end.

Open [Claude Code](https://claude.com/claude-code) in a clone of this repo and say:

```
Read README.md and docs/runbook.md, then set this up for me.
I want: <how many bedrooms, how many bathrooms, budget per month,
which city and which areas, minimum size, move-in date>.
Install everything, write criteria.toml, work out the search URLs
for all four portals, and run `flat-search check` until it is clean.
```

It has what it needs to finish: `check` validates the config, names every unknown key,
and warns when a search URL is tighter than your thresholds — so the agent has a real
gate to work against rather than having to guess whether it got it right. The test
suite is the other gate; `uv run python -m unittest discover -s tests` either passes
or does not.

Two things to keep for yourself. **The criteria are a judgement call** — which areas,
what you will pay, what you will not live without — and an agent guessing them is how
you end up with a tracker full of the wrong flats. Say them out loud in the prompt.
And **read the [Scope and etiquette](#scope-and-etiquette) section yourself** before
pointing it at anything, because that is a decision about how you treat someone else's
servers, not a configuration value.

The rest of this file is the same instructions written out, for reading directly or
for the agent to follow.

## Getting it running

Python 3.11 or newer. With [uv](https://docs.astral.sh/uv/):

    uv sync --extra ocr
    uv run playwright install chromium

`uv.lock` is committed, so that resolves to the exact versions this was tested
against. Drop `--extra ocr` if you do not want floorplan reading.

The second command is separate on purpose and no package manager can skip it: it
downloads a Chromium **binary**, not a Python package. The run checks for it up
front rather than failing a third of the way in.

The browser is not optional. The portals challenge datacenter IPs and plain HTTP —
Zoopla 403s every browserless request — so this is built to run on a desktop on a
home connection, not on a server.

Floorplan OCR is optional and needs the Tesseract **binary** on top of the `ocr`
extra: [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) on Windows,
`tesseract-ocr` from your package manager on Linux, `brew install tesseract` on macOS.
It need not be on `PATH`. Without it a listing that states no size is flagged rather
than sized — and the run says so at startup, because a silent OCR failure looks
exactly like a run in which no listing happened to publish a size.

Then write your criteria:

    cp examples/criteria.example.toml criteria.toml
    uv run flat-search check

`criteria.toml` is the single source of truth and is gitignored. `check` validates it,
prints what it will do, and warns if a search URL is tighter than your thresholds —
without touching a portal. An unknown key is an error, not a silent default.

## Running it

    uv run flat-search run

That is the whole pipeline, in the only safe order. Individual stages exist too
(`search`, `audit`, `details`, `judge`, `commit`, `render`, `report`) for when you
need to re-run one against the files the last one wrote.

**`run` refuses to commit after a failed search.** A challenged search and a genuinely
quiet morning are indistinguishable — both produce nothing. Committing stamps listings
as seen, so a portal blocked this morning would have its listings marked seen without
ever being shown, and they would never appear in a daily file again. That rule used to
live in a runbook; it lives in the code now.

When listings mention cooling, `run` stops and asks a model to judge them — in-unit
versus the residents' gym is reading comprehension, not pattern matching. Write the
verdicts where it tells you, then:

    uv run flat-search finish

**`run` refuses to commit while a listing is still unjudged**, for the same reason it
refuses after a failed search: committing stamps a listing into a daily file it only
ever gets one of, so a listing committed `unchecked` can never be shown judged. That
refusal is also why `judge` must return a count and `run` aborts if it does not —
falling off the end returning `None` made the check silently always-false, and one
morning's run committed 43 unjudged listings and looked perfectly healthy doing it.

`finish` records which page each verdict was read off, so the next run carries it
forward instead of paying a model to read the same listing again. A listing that
mentions cooling is otherwise a candidate every single morning: on one run, 43 were
flagged and 4 were new. A page rewritten under a stored verdict is re-flagged rather
than carried, because `commit` would otherwise collapse the stale quote to `unstated`
and the listing would never be read again.

### A run only asks for the days it missed

Where a portal can scope a search by listing age, it does. The window is the gap
since the last committed run plus a small margin — a daily run asks for 3 days
rather than paging 8469 Zoopla results, 5374 Rightmove ones and 15519 on
OnTheMarket to find the handful posted overnight — and it is chosen before a
single page is fetched, unlike `--incremental`, which can only notice staleness
after fetching it.

Skipping a morning widens the window rather than losing it. Being away longer
than the widest window a portal offers drops the scoping for that portal and
pages its backlog, because a window narrower than the gap would lose the
difference with nothing to show it happened. The ceilings differ — 30 days on
Zoopla, 14 on Rightmove, 7 on OnTheMarket — so a 10-day gap scopes two of them
and leaves the third walking everything. OpenRent has no such filter at all,
which is a pity, because it is also the one portal with no newest-first sort, so
neither this nor `--incremental` can spare it. A fresh clone has no last run and
uses `[run] first_run_days`, 14 by default. `flat-search check` prints the window
the next run would ask for, and every run logs it.

**Availability is the one filter this pipeline will not ask a portal for.**
Three of the four offer one, and what it removes is not late flats but undated
ones: of 15 listings dropped by Zoopla's filter, 1 stated a date on its page,
against 15 of 15 in the control — it dropped a flat whose page says "available
now" and kept one stating 2027-06-10. On the unscoped London corpus that is
about a third of the inventory (6139 of 8469, and widening the window from three
months to twelve adds 28 listings); on a narrow recency-scoped search it is far
less, measured at 213 against 203. Rejecting a listing because a portal has
no data about it is exactly what `min_sqft` and the district list refuse to do,
and doing it server-side is worse, because nothing downstream can flag what
never arrived.

So `move_in` filters here instead, against the date the listing itself states:
state a date past the window and you are rejected like an undersized flat; state
none and you are kept and read as available now. That became practical once all
four portals' dates were readable — Rightmove and OnTheMarket label theirs,
Zoopla renders it as a key fact, OpenRent writes it into the advert. If a search
URL still carries `moveInByDate=`, `available_from=` or `availableBefore=`,
`flat-search check` says so and tells you to take it out.

**None of these parameter values are guesses, and they could not safely be.**
Three of the four fail *closed* on a value they do not recognise — `maxDaysSinceAdded=30`,
`moveInByDate=someday` and Zoopla's own UI value `available_from=immediately`
each return zero results, which this pipeline is built to read as a possible
block. Every value in the tables has a measured count beside it in
[docs/portals.md](docs/portals.md).

## Outside London

Nothing in the filtering is London-specific. Postcode districts are parsed from the
general UK outward-code format, so `M3`, `WA14` and `EH10` behave exactly as `SW3`
does, and the stem fallback that maps `SW1X` to `SW1` maps `EC1A` to `EC1` the same
way. All four portals are national. Two things change:

**The search URLs**, which encode the location. Build each one in the portal's own UI
for the city you want, then paste it into `[searches]` and run `flat-search check` —
it will tell you if a URL is tighter than your thresholds.

**The district lists**, which are what `prime`, `affluent` and `fringe` mean to you.
These are outward codes, not names: `m3`, `m20`, `wa14` rather than Deansgate,
Didsbury, Altrincham. An unlisted district is never rejected on that basis unless you
set `only = true`; it is ranked low and flagged.

One London-only convenience is worth knowing about because its absence is silent.
`REGION_DISTRICT` in [fetch.py](src/flatsearch/fetch.py) maps five Rightmove region
IDs to the districts they cover, so a listing advertised as "Richmond, Surrey" with no
postcode in its address still gets placed. Elsewhere there is no such mapping, and
those listings are kept, ranked low and flagged `district unconfirmed - verify area`
rather than dropped. Add your own entries if a region you search often advertises
without postcodes; the run works without them.

## Layout

    criteria.toml            your thresholds and search URLs (gitignored)
    examples/                the config template to copy
    src/flatsearch/          the package
      config.py              typed config: load, validate, derive paths
      cli.py                 the one entry point
      fetch.py               browser work: search pages and detail pages
      portals.py             per-portal extraction
      floorplan.py           floorplan OCR
      judge.py               A/C: deterministic pre-filter, model for the rest
      core.py                state, filters, tiering, commit
      render.py              daily file and shortlist
    tests/                   the test suite
    docs/portals.md          measured portal behaviour; no script reads it
    docs/runbook.md          driving the pipeline unattended
    data/                    everything a run writes (gitignored)
      state.json  cache/  daily/  runs/  shortlist.md  decisions.md

Code and data are separate: `data/` is the only thing a run writes, so the repo stays
source-only and the whole search is one folder to back up. `decisions.md` in there is
the one file you hand-edit.

A listing appears in exactly one daily file, ever. That file is the thing you read; the
shortlist is for looking something up.

## Tests

    uv run python -m unittest discover -s tests -v

228 tests, no dependencies beyond the standard library. `pytest` will collect them
too if you prefer it. The OCR tests draw their own floorplans with Pillow rather than
committing image fixtures, so the repo carries no third-party content and no binaries;
the end-to-end OCR cases skip themselves where Tesseract is absent.

Every case in there is a bug this project actually shipped. That is deliberate, because
the characteristic failure here is silent: a broken extractor does not crash, it
produces a perfectly healthy-looking tracker in which every listing says "no A/C
mentioned". One end anchor matching mid-line — OpenRent's stock copy says "a flat in a
great location", which matched the `Location` heading anchor — truncated descriptions
below the minimum length and dropped 22% of that portal's fetches with no error at all.
The suite is verified by mutation: reintroducing each of those bugs turns it red —
including the later ones, where deleting `judge.run`'s `return` fails seven tests,
merging two ads that state different floors fails two, and letting `commit` write an
`unchecked` verdict over a confirmed one fails two.

`fetch.py` also refuses to start a run it cannot finish. A missing `playwright` or an
uninstalled chromium aborts with the fix command; a missing Tesseract only warns, but
loudly, because silent OCR means every listing is filed "size not stated" and the run
still looks healthy.

Two traps worth knowing if you extend it. An end anchor must match at the start of a
line, and before adding one, ask whether it is also an ordinary English word. And after
fixing an extractor, purge the cache it wrote — the cache is the skip condition, so bad
entries are never refetched.

## Running it unattended

[docs/runbook.md](docs/runbook.md) is the runbook: every step, every refusal, and
the traps behind them. It is written for an LLM agent, but it is worth reading
yourself even if you never automate anything, because it carries what a command
list cannot — the ordering, and the rules about when *not* to proceed.

This is the other half an agent can do for you — ask it to set up the scheduled task
as well and it will follow the same runbook. What follows is what it should end up
with, and what you would write by hand.

To run it on a schedule with Claude Code, create a scheduled task that **points at
this file rather than copying it**:

    mkdir -p ~/.claude/scheduled-tasks/flat-search

Create `~/.claude/scheduled-tasks/flat-search/SKILL.md` containing your own context
and a pointer to the runbook — not a copy of it:

```markdown
---
name: flat-search
description: Scheduled rental scan, with A/C judgement and a daily digest.
---

Working directory: /path/to/your/clone

Read `docs/runbook.md` in that folder and follow it. That is the full procedure.
This file holds only what is specific to me.

    cd /path/to/your/clone
    uv run flat-search run

Then, anything personal: how you want the digest written, which areas you care
about most, what you have already ruled out.
```

Then schedule it — in Claude Code, ask for a scheduled task pointing at that skill,
or use whatever scheduler you prefer to run `uv run flat-search run` in that folder.

**Keep the procedure in one place.** The obvious thing is to paste the runbook into
the skill file, and it is a trap: the two copies drift, and the one the automation
actually reads is the one you forget to update. A pointer costs one line and cannot
go stale. Two things are worth knowing if you use Claude Code's scheduled tasks:

- Creating a task **overwrites** that `SKILL.md` with the prompt you pass it, so
  write the file after creating the task, or back it up first.
- A task does not inherit your configured model; without an explicit model key it
  runs on the default.

## Scope and etiquette

This is a personal tool, published because the portal quirks it encodes were expensive
to work out and are not written down anywhere else. It paces itself (one page at a time
per portal, with a pause between pages) and is meant to run once a day on one person's
desktop. Do not point it at a portal faster than a person would browse, and check the
terms of service of anything you point it at. The scraped pages it caches are the
portals' content, not yours — `cache/` and the rendered views are gitignored for that
reason as much as for privacy.

It does not contact agents, submit enquiries, or fill in anything. It reads listings and
ranks them; talking to a human being about a flat is still your job.

## Licence

MIT. See [LICENSE](LICENSE).
