#!/usr/bin/env python3
"""Flat search - state and funnel bookkeeping.

The browser work is done by fetch.py and the A/C judgement by a model. This
script owns everything deterministic: state.json, URL dedup, the hard filters,
and - most importantly - validation of the model's A/C verdicts against the
page text it actually read. An unbacked claim collapses to `unstated`.

Subcommands:
  plan     dedupe stage-1 listings, apply hard filters, emit the stage-2 queue
  commit   validate verdicts and write records, preserving status and stamps
  report   summarise what is in state.json
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re


AIRCON_VERDICTS = {"yes", "likely", "no", "unstated"}
AIRCON_SCOPES = {"in_unit", "communal_only", "unclear"}

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def norm(s: str | None) -> str:
    """Whitespace/punctuation-insensitive form, for quote matching."""
    return _WS.sub(" ", (s or "").translate(_PUNCT)).strip().lower()


def cache_name(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16] + ".json"


def today() -> str:
    return dt.date.today().isoformat()


def parse_date(value) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None




DISTRICT_RE = re.compile('\\b([A-Z]{1,2}\\d{1,2}[A-Z]?)\\b')
TIER_NAME = {0: "High", 1: "Medium", 2: "Low"}


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# workbook
# --------------------------------------------------------------------------

# How long a listing whose detail page will not load waits before it goes into
# the tracker unread anyway. Without an end, a page that always fails would be
# retried for ever and never shown - the loss this exists to prevent, moved.
HOLD_DAYS = 7


def pending_rows(cfg, listings: list, known: set) -> list:
    """Search rows for pending listings that today's search did not return."""
    if not cfg.state_path.exists():
        return []
    pending = read_json(cfg.state_path).get("pending") or {}
    present = {str(l.get("url", "")).strip() for l in listings}
    return [dict(p.get("row") or {}, url=url, retry=True)
            for url, p in sorted(pending.items())
            if url not in present and url not in known]


def park_unread(pending: dict, url: str, listing: dict, run_date: str) -> bool:
    """Keep a listing whose detail page has not been read OUT of the tracker.

    Tracked means read. A listing tracked unread is skipped by every later
    run's fetch, because the fetch skips tracked listings, so it is never read
    at all - that is how 139 listings sat unread on 2026-09-25. Parked here it
    stays untracked, and `plan` queues it again next run.

    -> True while it should keep waiting; False once it has waited HOLD_DAYS
    and goes into the tracker unread, to be released flagged.
    """
    p = pending.get(url) or {"first_seen": run_date, "attempts": 0}
    # One attempt per run day: `finish` re-commits the same morning, and
    # counting that as a second failed fetch would overstate it.
    if p.get("last_tried") != run_date:
        p["attempts"] = int(p.get("attempts") or 0) + 1
        p["last_tried"] = run_date
    if not listing.get("retry"):
        # Seen in today's search: the freshest row, and a real sighting.
        p["last_seen"] = run_date
        p["row"] = {k: v for k, v in listing.items() if k != "retry"}
    pending[url] = p
    try:
        waited = (dt.date.fromisoformat(str(run_date)[:10])
                  - dt.date.fromisoformat(str(p["first_seen"])[:10])).days
    except ValueError:
        waited = 0
    return waited < HOLD_DAYS


def known_urls(cfg: dict) -> set:
    """Every URL already in state.json - the incremental-search stop set."""
    path = cfg.state_path
    if not path.exists():
        return set()
    data = read_json(path)
    return {str(l.get("url", "")).strip()
            for l in data.get("listings", []) if l.get("url")}


def district(listing: dict) -> str | None:
    """Postcode district (SW3, NW8, E14) - the workable proxy for 'affluent'.
    Portals always render it, unlike free-text neighbourhood names."""
    for field in ("postcode", "area"):
        m = DISTRICT_RE.search(str(listing.get(field) or "").upper())
        if m:
            return m.group(1)
    return None


def hard_filter(listing: dict, cfg: dict) -> str | None:
    """Return a rejection reason, or None if the listing survives stage 1.
    Unknown values never reject - only a stated value that violates a rule does."""
    price = listing.get("price_pcm")
    if isinstance(price, (int, float)) and price > cfg.budget_pcm:
        return f"over budget (GBP {price:.0f} > {cfg.budget_pcm})"

    beds = listing.get("bed_count")
    min_beds = cfg.min_bedrooms
    if min_beds and isinstance(beds, (int, float)) and beds < min_beds:
        return f"{int(beds)} bed (min {min_beds})"
    max_beds = cfg.max_bedrooms
    if max_beds and isinstance(beds, (int, float)) and beds > max_beds:
        return f"{int(beds)}-bed property (max {max_beds})"

    baths = listing.get("bathrooms")
    min_baths = cfg.min_bathrooms
    if min_baths and isinstance(baths, (int, float)) and baths < min_baths:
        return f"{int(baths)} bath (min {min_baths})"

    # Floor area is published by ~40% of Rightmove/Zoopla listings and ~6% of
    # OnTheMarket, so this rejects only a STATED size below the floor. Requiring
    # a size would delete most of the market on missing data.
    size = listing.get("size_sqft")
    min_sqft = cfg.min_sqft
    if min_sqft and isinstance(size, (int, float)) and size < min_sqft:
        return f"{int(size)} sq ft (min {min_sqft})"

    # A date the listing STATES, past the move-in window, is a rejection like
    # any other stated value that breaks a rule - decided 2026-09-21,
    # replacing a demotion to Low. It is done here rather than at the portal
    # because a portal's own availability filter also drops every listing it
    # cannot date - roughly a third of them - and unknown never rejects.
    #
    # Most listings have no date until their detail page is read, so this
    # mostly fires at commit and shows up under "DISQUALIFIED BY THEIR DETAIL
    # PAGE", exactly like an undersized flat.
    if cfg.move_in:
        move_in = parse_date(cfg.move_in)
        avail = parse_date(listing.get("available_from"))
        if move_in and avail:
            latest = move_in + dt.timedelta(days=cfg.move_in_slack_days)
            if avail > latest:
                return "available %s, after %s" % (avail.isoformat(),
                                                   latest.isoformat())

    # Lower ground / basement is an outright no.
    if str(listing.get("floor_level", "")).strip().lower() in ("lower_ground", "basement"):
        return "lower ground / basement"

    if cfg.districts_only and not listing.get("area_trusted"):
        d = district(listing)
        # An unparseable address ("Richmond, Surrey", a development name) is an
        # UNKNOWN district, not a bad one - measured at 4 of 9 good Richmond hits.
        # Unknown never rejects; it is ranked down and flagged instead.
        if d is not None and cfg.tier_of(d) is None:
            return f"district {d} not in affluent list"
    return None


def base_tier(listing: dict, cfg: dict) -> tuple[int, list[str]]:
    notes: list[str] = []

    d = district(listing)
    tier = cfg.tier_of(d)
    if tier is None and d is None and listing.get("region_district"):
        # The search was scoped to this region, which places the listing even
        # though its address carries no postcode.
        tier = cfg.tier_of(listing["region_district"])
        if tier is not None:
            notes.append("area from %s search" % (listing.get("region_name") or "region"))
    if tier is None and d is None and (listing.get("area_trusted") or cfg.districts_only):
        # came from an area search we chose deliberately, but the address
        # carries no district - keep it, ranked low, and say why
        tier = 2
        notes.append("district unconfirmed - verify area")
    if tier is None:
        tier = 2
        notes.append("outside target areas")

    move_in = parse_date(cfg.move_in)
    if move_in:
        avail = parse_date(listing.get("available_from"))
        if avail:
            # Anything past the window was rejected by hard_filter before this
            # ran, so a date here is one that fits. Say it, so the digest can
            # show timing without anyone going back to the listing.
            notes.append("available %s" % avail.isoformat())
        else:
            # Decided 2026-09-21: a listing that states no date anywhere
            # is read as available now rather than left hanging. It costs
            # nothing in ranking - an unknown was never demoted either, because
            # unknown never rejects - so what this changes is that the digest
            # says which it is. The assumption is written into the note rather
            # than into `available_from`, so a record still cannot claim a
            # reading that never happened.
            notes.append("availability not stated (assumed available now)")
    # No MOVE_IN_DATE set means timing is flexible - say nothing about dates.

    if cfg.min_bathrooms and listing.get("bathrooms") is None:
        # OpenRent filters bathrooms server-side but its search CARD never
        # prints the count, so for a listing whose detail page has not been read
        # the search itself is the evidence - do not flag those as unconfirmed.
        # Once the detail page IS read, `bathrooms` is a stated number and this
        # branch is not reached at all.
        if listing.get("bathrooms_verified_by_search"):
            notes.append("bathrooms >= min (verified by search filter)")
        else:
            notes.append("bathroom count unconfirmed")

    pref = cfg.furnishing
    raw = str(listing.get("furnished", "")).strip().lower()
    if "or unfurnished" in raw or "unfurnished or" in raw:
        got = pref                      # landlord flexible - satisfies either preference
        notes.append("landlord flexible on furnishing")
    elif raw.startswith("part"):
        got = "part furnished"
    elif raw in ("no", "unfurnished"):
        got = "unfurnished"
    elif raw in ("yes", "furnished"):
        got = "furnished"
    else:
        got = None
    if pref in ("unfurnished", "furnished"):
        if got is None:
            notes.append("furnishing unconfirmed")
        elif got != pref:
            tier = min(2, tier + 1)
            notes.append(f"{got}, prefer {pref}")

    return tier, notes


def apply_amenities(tier: int, listing: dict, cfg: dict) -> tuple[int, list[str]]:
    """Floor, lift and concierge. All of these are prose-only on every portal.

    Nobody advertises the ABSENCE of a lift - measured 0 of 80 listings - so an
    unstated lift is unknown, not missing, and only ever demotes with a flag.
    """
    notes: list[str] = []
    floor = str(listing.get("floor_level", "") or "").strip().lower()
    floor_no = listing.get("floor_number")
    lift = str(listing.get("lift", "") or "").strip().lower()

    if floor == "ground":
        tier = min(2, tier + 1)
        notes.append("ground floor")
    elif floor in ("top", "penthouse"):
        tier = max(0, tier - 1)
        notes.append("top floor")
    elif isinstance(floor_no, int) and floor_no >= cfg.good_floor_from:
        tier = max(0, tier - 1)
        notes.append(f"floor {floor_no}")

    lift_from = cfg.lift_required_from_floor
    if isinstance(floor_no, int) and floor_no >= lift_from:
        if lift == "yes":
            notes.append("lift confirmed")
        else:
            tier = min(2, tier + 1)
            notes.append(f"floor {floor_no} and lift {lift or 'unstated'}")
    elif lift == "yes":
        notes.append("lift")

    if str(listing.get("concierge", "") or "").lower() == "yes":
        tier = max(0, tier - 1)
        notes.append("concierge/porter")

    condition = str(listing.get("condition", "") or "").lower()
    if condition == "refurbished":
        tier = max(0, tier - 1)
        notes.append("refurbished/renovated")
    elif condition == "needs_work":
        tier = min(2, tier + 1)
        notes.append("needs refurbishment")

    size = listing.get("size_sqft")
    if cfg.min_sqft and not isinstance(size, (int, float)):
        notes.append("size not stated - verify")
    elif isinstance(size, (int, float)):
        notes.append(f"{int(size)} sq ft")
    return tier, notes


def apply_aircon(tier: int, aircon: dict, cfg: dict) -> tuple[int, list[str]]:
    """Adjust priority by the A/C verdict. in_unit is the only real hit."""
    notes: list[str] = []
    mode = cfg.aircon
    verdict = aircon.get("verdict", "unchecked")
    scope = aircon.get("scope", "unclear")
    hit = verdict in ("yes", "likely") and scope == "in_unit"

    if verdict in ("yes", "likely") and scope == "communal_only":
        notes.append("A/C is communal only, not in the unit")

    # The spelling has to match config.AIRCON_MODES exactly. It did not: this
    # read "ignore" while the only value the config accepts is "ignored", so
    # the mode was unreachable and silently behaved as "preferred" - an in-unit
    # hit still promoted a tier for someone who had asked for A/C to be ignored.
    if mode == "ignored":
        return tier, notes
    if mode == "required":
        # "unchecked" means we never opened the page - that is not evidence of
        # absence, so hold the base tier and let a later run decide.
        if verdict == "unchecked":
            notes.append("A/C not yet checked - detail page unread")
            return tier, notes
        if not hit:
            notes.append(f"A/C not confirmed in unit ({verdict})")
            return 2, notes
        return tier, notes
    # preferred
    if hit:
        tier = max(0, tier - 1)
        notes.append("A/C in unit")
    return tier, notes


# --------------------------------------------------------------------------
# verdict validation  (the anti-hallucination gate)
# --------------------------------------------------------------------------

def validate_verdict(url: str, raw: dict | None, cache_dir: pathlib.Path) -> dict:
    """Every positive or negative A/C claim must quote text that really appears
    on the page we cached. Unbacked claims collapse to 'unstated'."""
    out = {"verdict": "unchecked", "scope": "", "evidence": "", "checked": "", "flags": []}
    if raw is None:
        return out

    cache_file = cache_dir / cache_name(url)
    if not cache_file.exists():
        out["flags"].append("no cached page text - verdict discarded")
        return out

    try:
        page = read_json(cache_file)
    except json.JSONDecodeError:
        out["flags"].append("cache file unreadable - verdict discarded")
        return out

    page_text = norm(page.get("text", ""))
    out["checked"] = str(page.get("fetched_at", today()))[:10]

    verdict = str(raw.get("verdict", "")).strip().lower()
    scope = str(raw.get("scope", "")).strip().lower()
    evidence = (raw.get("evidence") or "").strip()

    if verdict not in AIRCON_VERDICTS:
        out["verdict"] = "unstated"
        out["flags"].append("unknown verdict '" + verdict + "' -> unstated")
        return out

    if verdict == "unstated":
        out["verdict"] = "unstated"
        return out

    if not evidence:
        out["verdict"] = "unstated"
        out["flags"].append(verdict + " claimed with no quote -> unstated")
        return out

    if norm(evidence) not in page_text:
        out["verdict"] = "unstated"
        out["evidence"] = ""
        out["flags"].append(verdict + " quote not found in page text -> unstated")
        return out

    out["verdict"] = verdict
    out["evidence"] = evidence
    if verdict in ("yes", "likely"):
        if scope in AIRCON_SCOPES:
            out["scope"] = scope
        else:
            out["scope"] = "unclear"
            out["flags"].append("unknown scope '" + scope + "' -> unclear")
    return out


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def plan(cfg, stage1_path, out_path, refresh=False):
    """Queue every listing whose detail page this run should read.

    There is no cap. There was one - 150 pages - and past it listings were
    tracked without ever being read, then skipped by every later run because
    they were tracked. When the daily intake rose to ~200 (2026-09-22, the
    portal availability filter came out) that parked 50-70 listings a day where
    nothing would go back for them. A long morning is the cost of having no
    cap; a silently lost listing was the cost of having one.

    Listings whose page did not load on an earlier run are in state `pending`,
    not tracked, and are re-queued here from the search row they were found
    with - including after they have aged out of today's search window.
    """
    stage1_path, out_path = pathlib.Path(stage1_path), pathlib.Path(out_path)
    stage1 = read_json(stage1_path)
    listings = stage1.get("listings", stage1 if isinstance(stage1, list) else [])
    known = known_urls(cfg)
    retried = pending_rows(cfg, listings, known)
    if retried and isinstance(stage1, dict):
        # Into stage1 itself, not just the queue: fetch enriches the row it
        # finds there and commit reads rows from there, so a listing only in
        # the queue would be fetched and then never recorded.
        stage1.setdefault("listings", listings).extend(retried)
        write_json(stage1_path, stage1)
    cache_dir = cfg.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    queue, dupes, rejected, seen = [], [], [], set()
    refreshed = []
    for listing in listings:
        url = str(listing.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if url in known:
            if not refresh:
                dupes.append(url)
                continue
            refreshed.append(url)
        reason = hard_filter(listing, cfg)
        if reason:
            rejected.append({"url": url, "title": listing.get("title"), "reason": reason})
            continue
        tier, _ = base_tier(listing, cfg)
        cache_file = cache_dir / cache_name(url)
        queue.append({
            "url": url,
            "title": listing.get("title"),
            "platform": listing.get("platform"),
            "tier": tier,
            "cache_path": str(cache_file),
            "already_cached": cache_file.exists(),
            "tracked": url in known,
        })

    queue.sort(key=lambda q: (q["already_cached"], q["tier"]))
    to_fetch = [q for q in queue if not q["already_cached"]]

    payload = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "fetch": to_fetch,
        "cached": [q for q in queue if q["already_cached"]],
        "skipped_duplicates": len(dupes),
        "rejected": rejected,
    }
    write_json(out_path, payload)

    print("stage 1 in          : " + str(len(seen)) + " unique listings")
    print("already tracked     : " + str(len(dupes)) + " (skipped)")
    if refresh:
        print("re-queued (refresh) : " + str(len(refreshed)) +
              " already-tracked listing(s) - commit --refresh rewrites them in "
              "full; without it the fresh values merge and nothing is blanked")
    print("hard-filtered out   : " + str(len(rejected)))
    for r in rejected[:8]:
        print("   - " + r["reason"] + ": " + str(r["title"] or r["url"])[:60])
    if retried:
        print("retrying            : " + str(len(retried)) +
              " listing(s) whose detail page did not load on an earlier run")
    print("already cached      : " + str(len(payload["cached"])) + " (no refetch needed)")
    print("TO FETCH            : " + str(len(to_fetch)))
    print("\nqueue -> " + str(out_path))
    return payload


def load_state(cfg: dict) -> dict:
    path = cfg.state_path
    if not path.exists():
        return {"updated": None, "listings": []}
    return read_json(path)


def save_state(cfg: dict, state: dict) -> None:
    """Write via a temp file and swap, so an interrupted run cannot truncate it."""
    state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    path = cfg.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# Fields the pipeline owns and rewrites on every commit. Everything else on a
# record - status, found_on, reported_on, anything you set - is left alone.
DERIVED = ("title", "platform", "area", "postcode", "price_pcm", "bills_included",
           "available_from", "furnished", "bedrooms", "bathrooms", "size_sqft",
           "size_sqft_plan", "floor", "lift", "concierge", "condition", "contact",
           "aircon", "aircon_scope", "aircon_evidence", "aircon_checked",
           "notes", "priority")

# The four A/C fields are one fact in four columns and must always be written
# together, from whichever verdict won. Merging them field by field produced a
# record reading `unstated` while still carrying the quote from the `yes` it
# replaced - a contradiction that no report would ever surface. `carry_verdict`
# already decides this group as a whole, so the per-field guard below skips it.
AIRCON_FIELDS = ("aircon", "aircon_scope", "aircon_evidence", "aircon_checked")

# Values that mean "this run learned nothing", as opposed to a fact. `unstated`
# is a finding - the page was read and says nothing about cooling - and must
# overwrite. `unchecked` is the absence of a reading and must not.
NO_INFORMATION = (None, "", "Unknown", "unchecked")


def uninformative(value) -> bool:
    return value in NO_INFORMATION or (isinstance(value, str) and not value.strip())


def carry_verdict(ac: dict, existing: dict | None) -> dict:
    """Keep a stored A/C verdict when this run has nothing to say about it.

    `commit` walks stage 1, not the fetch queue, so a listing tracked weeks ago
    is re-committed every morning it is still advertised - but only listings in
    the queue get a verdict. With no cache entry `validate_verdict` returns
    `unchecked`, and writing that over a stored verdict destroyed the quote the
    whole validation gate exists to protect: a listing confirmed `yes` with
    evidence came back `unchecked` with none, and being unchecked is invisible.

    Carrying it forward keeps the verdict AND its evidence, and - because this
    runs before tiering - keeps `priority` and `notes` consistent with it.
    """
    if not existing or ac.get("verdict") != "unchecked":
        return ac
    stored = str(existing.get("aircon") or "")
    if uninformative(stored) or stored not in AIRCON_VERDICTS:
        return ac
    return {"verdict": stored,
            "scope": existing.get("aircon_scope") or "",
            "evidence": existing.get("aircon_evidence") or "",
            "checked": existing.get("aircon_checked") or "",
            "flags": []}


UNJUDGED = """NOT COMMITTING - %d listing(s) a model was asked to read are still unread.
%s
They are in %s. Judge them with a Haiku subagent (in_unit vs communal_only,
with a VERBATIM quote) into verdicts_haiku.json, then `flat-search finish`,
which merges both files and commits.

A listing committed now is written `unchecked` into today's daily file, and
each listing gets exactly one."""


class Abort(Exception):
    """A refusal the user needs to read, not a traceback."""


def gate_unjudged(cfg, verdicts_path) -> None:
    """Refuse to commit while a listing a model was asked to read is unread.

    This refusal used to live in `flat-search run` alone, which is the one
    path that cannot reach commit without passing it. `flat-search commit`
    had no gate at all, and the morning of 2026-09-21 was driven stage by
    stage - correctly, as it happened, but nothing was enforcing it.

    What it costs to be wrong is not symmetric. An uncommitted listing waits;
    a listing committed `unchecked` is stamped into a daily file it only ever
    gets one of, and the judged version can never be shown.

    `judge` rewrites needs_review.json every time it runs, so a leftover file
    means judgement was skipped entirely, and that refuses too.
    """
    review_path = cfg.runs_dir / "needs_review.json"
    if not review_path.exists():
        return
    try:
        review = read_json(review_path).get("needs_model_judgement", [])
    except (ValueError, OSError):
        return
    pending = [str(e.get("url", "")).strip() for e in review if e.get("url")]
    if not pending:
        return

    judged = set()
    path = pathlib.Path(verdicts_path) if verdicts_path else None
    if path and path.exists():
        raw = read_json(path)
        for v in raw.get("verdicts", raw if isinstance(raw, list) else []):
            url = str(v.get("url", "")).strip()
            if url:
                judged.add(url)
    missing = [u for u in pending if u not in judged]
    if not missing:
        return

    listed = "".join("  %s\n" % u for u in missing[:5])
    if len(missing) > 5:
        listed += "  ... and %d more\n" % (len(missing) - 5)
    raise Abort(UNJUDGED % (len(missing), listed, review_path))


def commit(cfg, stage1_path, verdicts_path=None, update=False):
    """Write stage-1 listings into state.json.

    `update` (the `--refresh` flag) decides what happens to a listing already
    tracked. It used to be accepted and never read, so both paths did the same
    thing - the destructive one:

      update=False  merge. Fresh values are written, but a field this run knows
                    nothing about keeps what is already on the record. This is
                    the normal morning run, where most tracked listings were
                    never re-fetched and there is nothing new to say about them.
      update=True   authoritative rewrite. You deliberately re-fetched these
                    pages, so the new read wins outright, including clearing a
                    field the listing no longer states.
    """
    gate_unjudged(cfg, verdicts_path)
    stage1 = read_json(pathlib.Path(stage1_path))
    listings = stage1.get("listings", stage1 if isinstance(stage1, list) else [])
    verdicts = {}
    if verdicts_path and pathlib.Path(verdicts_path).exists():
        raw = read_json(pathlib.Path(verdicts_path))
        for v in raw.get("verdicts", raw if isinstance(raw, list) else []):
            url = str(v.get("url", "")).strip()
            if url:
                verdicts[url] = v.get("aircon", v)

    state = load_state(cfg)
    by_url = {str(l.get("url", "")).strip(): l for l in state["listings"]}
    pending = state.setdefault("pending", {})
    cache_dir = cfg.cache_dir
    run_date = stage1.get("run_date") or today()

    added = updated = 0
    parked, released = 0, []
    rejected_late, flags, ac_counts, seen = [], [], {}, set()

    for listing in listings:
        url = str(listing.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        existing = by_url.get(url)
        if existing:
            # Tracked is read; a pending entry for it is stale.
            pending.pop(url, None)

        reason = hard_filter(listing, cfg)
        if reason:
            pending.pop(url, None)
            # A listing already tracked can fail later, once its detail page has
            # been read - a basement flat, or a stated size under the floor. Say
            # so on the record rather than leaving it looking untriaged. One not
            # tracked yet is simply never tracked.
            if existing:
                note = "rejected on detail: " + reason
                if str(existing.get("status", "")).upper() in ("", "NEW"):
                    existing["status"] = "REJECTED"
                for key in ("size_sqft", "size_sqft_plan", "floor", "lift", "concierge"):
                    if listing.get(key):
                        existing[key] = listing[key]
                notes = [n for n in str(existing.get("notes") or "").split("; ") if n]
                if listing.get("size_sqft"):
                    notes = [n for n in notes if "size not stated" not in n]
                if note not in notes:
                    notes.append(note)
                existing["notes"] = "; ".join(notes)
                rejected_late.append(str(listing.get("title") or url)[:44] + ": " + reason)
            continue

        unread_note = None
        if not existing and not (cache_dir / cache_name(url)).exists():
            if park_unread(pending, url, listing, run_date):
                parked += 1
                continue
            unread_note = ("detail page never loaded (%d attempts) - unread"
                           % pending[url]["attempts"])
            released.append(str(listing.get("title") or url)[:60])

        ac = validate_verdict(url, verdicts.get(url), cache_dir)
        if not update:
            ac = carry_verdict(ac, existing)
        for f in ac["flags"]:
            flags.append(str(listing.get("title") or url)[:48] + ": " + f)
        ac_counts[ac["verdict"]] = ac_counts.get(ac["verdict"], 0) + 1

        tier, notes = base_tier(listing, cfg)
        tier, am_notes = apply_amenities(tier, listing, cfg)
        notes += am_notes
        tier, ac_notes = apply_aircon(tier, ac, cfg)
        notes += ac_notes
        if listing.get("notes"):
            notes.insert(0, str(listing["notes"]))
        if unread_note:
            notes.append(unread_note)
        if listing.get("size_sqft_plan") and not listing.get("size_sqft"):
            notes.append("size %d sq ft read off the floorplan%s - verify"
                         % (listing["size_sqft_plan"],
                            "" if listing.get("size_plan_confident") else " (AMBIGUOUS)"))

        record = {
            "url": url,
            "title": listing.get("title"),
            "platform": listing.get("platform"),
            "area": listing.get("area"),
            "postcode": listing.get("postcode"),
            "price_pcm": listing.get("price_pcm"),
            "bills_included": listing.get("bills_included", "Unknown"),
            "available_from": listing.get("available_from"),
            "furnished": listing.get("furnished", "Unknown"),
            "bedrooms": listing.get("bed_count"),
            "bathrooms": listing.get("bathrooms"),
            "size_sqft": listing.get("size_sqft"),
            "size_sqft_plan": listing.get("size_sqft_plan"),
            "floor": listing.get("floor_level") or listing.get("floor_number"),
            "lift": listing.get("lift"),
            "concierge": listing.get("concierge"),
            "condition": listing.get("condition"),
            "contact": listing.get("contact"),
            "aircon": ac["verdict"],
            "aircon_scope": ac["scope"],
            "aircon_evidence": ac["evidence"],
            "aircon_checked": ac["checked"],
            "notes": "; ".join(n for n in notes if n),
            "priority": TIER_NAME[tier],
        }

        if existing:
            for key in DERIVED:
                value = record.get(key)
                # A blank from a run that never read this listing is not a
                # correction, and must not erase what an earlier run measured.
                if not update and key not in AIRCON_FIELDS \
                        and uninformative(value) \
                        and not uninformative(existing.get(key)):
                    continue
                existing[key] = value
            existing["last_seen"] = run_date
            updated += 1
        else:
            # A listing that waited in `pending` keeps the day it was first
            # found, and - if it came back only as a retry, not from today's
            # search - the day it was last actually seen.
            pend = pending.pop(url, None) or {}
            record["status"] = "NEW"
            record["found_on"] = pend.get("first_seen") or run_date
            record["last_seen"] = (pend.get("last_seen") or run_date
                                   if listing.get("retry") else run_date)
            record["reported_on"] = None
            state["listings"].append(record)
            by_url[url] = record
            added += 1

    save_state(cfg, state)
    print("state: " + str(cfg.state_path))
    print("added %d, updated %d, tracked total %d" % (added, updated, len(state["listings"])))
    if parked:
        print("NOT TRACKED YET - %d listing(s) whose detail page has not loaded;"
              " retried next run" % parked)
    if released:
        print("RELEASED UNREAD - %d listing(s) waited %d days for a detail page:"
              % (len(released), HOLD_DAYS))
        for r in released[:12]:
            print("   ? " + r)
    ac_line = ", ".join(k + "=" + str(v) for k, v in sorted(ac_counts.items()))
    print("A/C verdicts: " + (ac_line or "none"))
    if rejected_late:
        print("")
        print("DISQUALIFIED BY THEIR DETAIL PAGE - %d marked REJECTED:" % len(rejected_late))
        for r in rejected_late[:12]:
            print("   x " + r)
        if len(rejected_late) > 12:
            print("   ... and %d more" % (len(rejected_late) - 12))
    if flags:
        print("")
        print("VALIDATION - %d verdict(s) downgraded:" % len(flags))
        for f in flags:
            print("   ! " + f)


DEAD_STATUSES = ("REJECTED", "DISMISSED", "GONE")


def is_live(listing: dict) -> bool:
    return str(listing.get("status", "")).upper() not in DEAD_STATUSES


def aircon_in_unit(listing: dict) -> bool:
    """The same hit `apply_aircon` ranks on, read off a stored record.

    Everything that showed or counted A/C tested the verdict alone, so a
    `communal_only` yes - cooling in the residents' gym - was bolded **A/C**
    and counted "in unit" while being ranked, correctly, as though it were not.
    The first one landed 2026-09-25 and put 2 in a headline that had 1.
    """
    return (listing.get("aircon") in ("yes", "likely")
            and listing.get("aircon_scope") == "in_unit")


def aircon_label(listing: dict) -> str:
    """The A/C tally key: a yes that is not in the unit says which kind it is."""
    verdict = listing.get("aircon") or "(blank)"
    if verdict in ("yes", "likely") and not aircon_in_unit(listing):
        return "%s-%s" % (verdict, listing.get("aircon_scope") or "unclear")
    return verdict


def tally(rows: list, field) -> dict:
    out: dict = {}
    for l in rows:
        key = (field(l) if callable(field) else l.get(field)) or "(blank)"
        out[key] = out.get(key, 0) + 1
    return out


def _counts(tallied: dict) -> str:
    return "  ".join("%s=%d" % kv for kv in
                     sorted(tallied.items(), key=lambda x: str(x[0])))


def snapshot(cfg) -> dict:
    """The counts as they stand right now.

    Taken before a run so the report afterwards can say what THIS run changed.
    Without it every number is a running total, and "22 disqualified by their
    detail page" cannot be told from 22 that were already there - which is
    exactly the question left open after the 2026-09-20 run.
    """
    state = load_state(cfg)
    listings = state.get("listings", [])
    live = [l for l in listings if is_live(l)]
    dead = [l for l in listings if not is_live(l)]
    return {
        "taken_at": dt.datetime.now().isoformat(timespec="seconds"),
        "tracked": len(listings),
        "live": len(live),
        "ruled_out": len(dead),
        "aircon_live": tally(live, "aircon"),
        "aircon_in_unit": sum(1 for l in live if aircon_in_unit(l)),
        "unreported": sum(1 for l in live if not l.get("reported_on")),
    }


def write_baseline(cfg) -> dict:
    snap = snapshot(cfg)
    write_json(cfg.runs_dir / "baseline.json", snap)
    return snap


def ensure_baseline(cfg) -> dict:
    """Fill in a missing baseline without moving one already taken today.

    `search` is also how a partly failed morning is repaired - the search
    re-run after a failure - and a repair must not reset where the morning
    started, or the delta covers the repair instead of the run.
    """
    base = read_baseline(cfg)
    if base and str(base.get("taken_at", ""))[:10] == dt.date.today().isoformat():
        return base
    return write_baseline(cfg)


def read_baseline(cfg) -> dict | None:
    path = cfg.runs_dir / "baseline.json"
    if not path.exists():
        return None
    try:
        return read_json(path)
    except (ValueError, OSError):
        return None


def _delta(label: str, before, after) -> str | None:
    diff = (after or 0) - (before or 0)
    if not diff:
        return None
    return "%+d %s" % (diff, label)


def report(cfg):
    state = load_state(cfg)
    listings = state["listings"]
    live = [l for l in listings if is_live(l)]
    dead = [l for l in listings if not is_live(l)]
    print("tracked %d  |  live %d  ·  ruled out %d"
          % (len(listings), len(live), len(dead)))

    base = read_baseline(cfg)
    if base:
        parts = [p for p in (
            _delta("tracked", base.get("tracked"), len(listings)),
            _delta("live", base.get("live"), len(live)),
            _delta("ruled out", base.get("ruled_out"), len(dead)),
            # A baseline written before `aircon_in_unit` existed only has the
            # raw tally; its yes count is the closest thing it recorded.
            _delta("A/C in unit",
                   base.get("aircon_in_unit",
                            (base.get("aircon_live") or {}).get("yes")),
                   sum(1 for l in live if aircon_in_unit(l))),
            _delta("awaiting a daily file", base.get("unreported"),
                   sum(1 for l in live if not l.get("reported_on"))),
        ) if p]
        taken = str(base.get("taken_at", ""))
        # A baseline left over from an earlier day differences today's totals
        # against the wrong morning, and the line reads exactly like a good
        # one. Say which morning it is measuring from.
        stale = taken[:10] != dt.date.today().isoformat()
        print("  since %s%s: %s"
              % (taken[:16].replace("T", " "),
                 "  (STALE - that baseline is not from today)" if stale else "",
                 ", ".join(parts) or "nothing changed"))
    else:
        # Silence here reads as "nothing changed", and it is not: it means the
        # totals below are running totals with nothing to compare them to. The
        # digest that follows will otherwise difference them by hand, which is
        # how 2026-09-21 reported 18 old rejections as new ones.
        print("  since: NO BASELINE for this run - every count below is a "
              "running total.")
        print("         Do not difference them by hand. Start the morning with "
              "`flat-search run`,")
        print("         or `flat-search search`, either of which takes one.")

    # Split live from ruled-out. A single combined tally is what made the
    # 2026-09-20 run's "aircon unchecked=58" unreadable: every one of those
    # belonged to a listing the hard filter had already thrown out before its
    # detail page was ever fetched, but the line could not say so, and the
    # reason had to be dug out of state.json by hand.
    for field, key in (("priority", "priority"), ("aircon", aircon_label)):
        if live:
            print("  %-9s live      %s" % (field, _counts(tally(live, key))))
        if dead:
            print("  %-9s ruled out %s" % (field if not live else "",
                                           _counts(tally(dead, key))))
    if listings:
        print("  %-9s %s" % ("status", _counts(tally(listings, "status"))))

    unchecked_live = tally(live, "aircon").get("unchecked", 0)
    unchecked_dead = tally(dead, "aircon").get("unchecked", 0)
    if unchecked_live:
        print("  %d live listing(s) have an unread detail page - A/C still unknown"
              % unchecked_live)
    elif unchecked_dead:
        print("  every unchecked A/C verdict (%d) belongs to a listing already ruled "
              "out - nothing to fetch" % unchecked_dead)
    unseen = sum(1 for l in live if not l.get("reported_on"))
    if unseen:
        print("  %d live listing(s) not yet written to a daily file" % unseen)
    waiting = len(state.get("pending") or {})
    if waiting:
        print("  %d listing(s) not tracked yet - detail page not loaded, retried "
              "every run" % waiting)
