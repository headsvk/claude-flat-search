# flat-search

A London rental search that runs itself every morning and hands you a short list of
only what is new, ranked, with the things the portals will not let you filter on
already read out of the listing text.

It exists because the filters on Rightmove, Zoopla, OnTheMarket and OpenRent cannot
express what most people actually want. You can filter on bedrooms and price. You
cannot filter on *floor area over 800 sq ft*, *not a basement*, *has a lift if it is
above the third floor*, or *air conditioning in the flat rather than in the residents'
gym*. Those live in the prose, so this reads the prose.

Four portals, one browser each, run concurrently. Roughly 650 listings tracked on a
normal week; a morning run turns up a handful of genuinely new ones.

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

Two design choices carry most of the weight.

A third choice follows from the first two. **The output collapses duplicate ads but
never duplicate flats.** One flat is routinely several rows — cross-listed on two
portals, or a new-build block marketing every unit it has under one address. Ads that
agree on price, address and bed/bath count and contradict each other on nothing they
state become one row carrying every link; flats that merely share a building stay
separate rows, marked with how many the block has going. Measured on one live set:
840 ads for 778 flats, and four ads at the same price in the same block sitting on
two different floors — which is why agreement on the *stated* fields is required
before anything merges.

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

204 tests, no dependencies beyond the standard library. `pytest` will collect them
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
including the later ones, where deleting `judge.run`'s `return` fails seven tests and
merging two ads that state different floors fails two.

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

## Credit

This started life as a fork of
[mikepapadim/london-property-hunt-public](https://github.com/mikepapadim/london-property-hunt-public),
which searches for rooms in shared flats. The whole-flat rewrite kept none of its code —
that repo is a prose skill-prompt with no Python in it — but it is where the idea came
from, and credit is cheap.

## Licence

MIT. See [LICENSE](LICENSE).
