#!/usr/bin/env python3
"""Readiness audit - is the search actually ready for a full catch-up run?

A catch-up means ~660 detail fetches. Before spending that, check that every
portal really works end to end, per portal, with evidence. Anything that reads
FAIL or THIN here would waste a large chunk of that run.

    flat-search audit
"""
from __future__ import annotations

import collections
import pathlib

from . import core
from . import judge


def pct(n, d):
    return "%3d%%" % (100 * n // d) if d else "  -%"


def report(cfg, stage1_path) -> bool:
    """-> True when every sampled portal looks healthy.

    Returns rather than exits: the caller decides what a failed gate means.
    """
    stage1 = core.read_json(pathlib.Path(stage1_path))
    rows = stage1["listings"]
    cache = cfg.cache_dir
    cached = {}
    for f in cache.glob("*.json"):
        page = core.read_json(f)
        if page.get("url"):
            cached[page["url"]] = page

    print("=" * 74)
    print("STAGE 1 - field coverage per portal (what the search page gives us)")
    print("=" * 74)
    by_portal = collections.defaultdict(list)
    for r in rows:
        by_portal[r["platform"]].append(r)
    print("%-13s %6s %6s %6s %6s %6s %6s" %
          ("portal", "n", "price", "beds", "baths", "size", "postcd"))
    for p, rs in sorted(by_portal.items()):
        n = len(rs)
        print("%-13s %6d %6s %6s %6s %6s %6s" % (
            p, n,
            pct(sum(1 for r in rs if r.get("price_pcm")), n),
            pct(sum(1 for r in rs if r.get("bed_count")), n),
            pct(sum(1 for r in rs if r.get("bathrooms")
                    or r.get("bathrooms_verified_by_search")), n),
            pct(sum(1 for r in rs if r.get("size_sqft")), n),
            pct(sum(1 for r in rs if core.district(r)), n)))

    print()
    print("=" * 74)
    print("STAGE 2 - detail extraction per portal (the part a catch-up spends on)")
    print("=" * 74)
    if not cached:
        # READY, not a failure. Nothing has been fetched yet because nothing
        # has been fetched yet - which is the state of every fresh clone, and
        # exactly what the details fetch that comes next is for. Returning None
        # here made `flat-search run` abort on its own first run with "an
        # extractor looks broken", sending people after a bug that is not there.
        print("  no cache yet - nothing to certify; the details fetch comes next")
        return True
    seen = collections.defaultdict(lambda: {"n": 0, "chars": [], "thin": 0,
                                            "desc": 0, "amen": 0})
    # Resolve a cached page's portal from everything we have ever tracked, not
    # just today's search. The cache outlives any one stage1: on a fresh search
    # most cached listings are not in it, and mapping only against `rows` filed
    # them all under "?" - which made the per-portal health numbers meaningless
    # exactly when they are being relied on.
    url_portal = {r["url"]: r["platform"] for r in rows}
    state_file = cfg.state_path
    if state_file.exists():
        for rec in core.read_json(state_file).get("listings", []):
            url_portal.setdefault(str(rec.get("url", "")).strip(),
                                  rec.get("platform", "?"))
    for url, j in cached.items():
        p = url_portal.get(url, "?")
        s = seen[p]
        text = j.get("text", "")
        s["n"] += 1
        s["chars"].append(len(text))
        s["thin"] += len(text) < 300
        s["desc"] += "Description:" in text
        s["amen"] += bool(judge.amenities(text))
    # A short advert is not a failed extraction. Measured 2026-09-19: 14 of 652
    # cached pages sit under 300 chars and every one of them is a real, coherent
    # listing - private OpenRent landlords write two lines, and one Rightmove
    # agent literally wrote "No description...". Demanding thin == 0 made this
    # gate permanently red, and a gate that is always red gets ignored, which is
    # worse than having none. What actually distinguishes a broken extractor is
    # the description going MISSING, which `has desc` below measures directly.
    THIN_RATE_FAIL = 0.10
    print("%-13s %5s %8s %7s %8s %9s  %s" %
          ("portal", "n", "median", "thin", "has desc", "amenities", "verdict"))
    ready = True
    for p, s in sorted(seen.items()):
        ch = sorted(s["chars"])
        med = ch[len(ch) // 2]
        thin_rate = s["thin"] / s["n"] if s["n"] else 0.0
        ok = med >= 400 and thin_rate <= THIN_RATE_FAIL and s["desc"] == s["n"]
        ready &= ok
        print("%-13s %5d %8d %6d%s %8s %9s  %s" %
              (p, s["n"], med, s["thin"],
               "!" if thin_rate > THIN_RATE_FAIL else " ",
               pct(s["desc"], s["n"]), pct(s["amen"], s["n"]),
               "OK" if ok else "*** FAIL ***"))

    # A portal is uncertified only if NOTHING of it has ever been cached. Today's
    # new listings not being cached yet is the normal state straight after a
    # search - that is what the details fetch is for, and failing on it would
    # make the gate red on every fresh run.
    never_seen = [p for p in by_portal if p not in seen]
    if never_seen:
        ready = False
        print("\n  NEVER SAMPLED (cannot certify): %s" % ", ".join(sorted(never_seen)))
        print("  A catch-up would fetch these blind.")

    print()
    print("=" * 74)
    print("VERDICT: %s" % ("READY for a catch-up run" if ready else
                           "NOT READY - fix the above first"))
    print("=" * 74)
    return ready
