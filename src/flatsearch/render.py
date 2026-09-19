#!/usr/bin/env python3
"""Turn state.json into something worth reading, and fold in your own calls.

Three files, and the split is the point:

  state.json     machine state - every field, written by the pipeline, never read
                 by a person and never read into an agent's context.
  decisions.md   Your calls. Hand-edited. The ONLY file here a person writes.
  shortlist.md   generated view, regenerated every run and therefore disposable.

The spreadsheet this replaces was doing the first and third jobs at once, which is
why it had 25 columns and why a morning run failed outright whenever it was left
open in Excel.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

RANK = {"High": 0, "Medium": 1, "Low": 2}
DECISION_RE = re.compile(r"^\s*-\s*\[(?P<status>[^\]]*)\]\s*(?P<url>https?://\S+)\s*(?:—|-|:)?\s*(?P<note>.*)$")


def load_state(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_decisions(path: pathlib.Path) -> dict:
    """`- [viewed] https://... — note` per line. Anything else in the file is prose.

    Kept deliberately forgiving: it is a file a person types into at speed, so an
    unparseable line is skipped rather than treated as an error.
    """
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = DECISION_RE.match(line)
        if m:
            out[m.group("url").strip()] = {"status": m.group("status").strip().upper() or "SEEN",
                                           "note": m.group("note").strip()}
    return out


def money(value) -> str:
    try:
        return "£%s" % format(int(value), ",d")
    except (TypeError, ValueError):
        return "?"


def cell(value, dash: str = "–") -> str:
    if value in (None, "", "Unknown"):
        return dash
    return str(value).replace("|", "/")


def size_cell(listing: dict) -> str:
    stated = listing.get("size_sqft")
    if stated:
        return str(int(stated))
    plan = listing.get("size_sqft_plan")
    if plan:
        # Flag, never a hard fact: this came off a floorplan by OCR.
        return "~%d*" % int(plan)
    return "–"


def flags(listing: dict) -> str:
    out = []
    if listing.get("aircon") == "yes":
        out.append("**A/C**")
    if listing.get("concierge"):
        out.append("concierge")
    if listing.get("lift"):
        out.append("lift")
    cond = str(listing.get("condition") or "")
    if "refurb" in cond.lower():
        out.append(cond)
    return ", ".join(out) or "–"


def table(rows: list) -> list:
    out = ["| £pcm | area | sqft | fl | beds/bath | notable | listing |",
           "|---|---|---|---|---|---|---|"]
    for l in rows:
        out.append("| %s | %s | %s | %s | %s/%s | %s | [%s](%s) |" % (
            money(l.get("price_pcm")),
            cell(l.get("postcode") or l.get("area")),
            size_cell(l),
            cell(l.get("floor"), "–"),
            cell(l.get("bedrooms"), "?"), cell(l.get("bathrooms"), "?"),
            flags(l),
            str(l.get("platform") or "link"), l.get("url")))
    return out


def order(rows: list) -> list:
    return sorted(rows, key=lambda l: (RANK.get(str(l.get("priority")), 3),
                                       0 if l.get("aircon") == "yes" else 1,
                                       l.get("price_pcm") or 10 ** 9))


def is_live(l: dict) -> bool:
    return str(l.get("status", "")).upper() not in ("REJECTED", "DISMISSED", "GONE")


HOLD_DAYS = 7


def overdue(listing: dict, day: str) -> bool:
    """Has this listing waited too long for a detail page?

    The escape hatch on holding listings back. A listing whose detail fetch
    keeps failing would otherwise be held for ever and silently never reported -
    which is the exact failure mode the hold is meant to prevent, just moved.
    After HOLD_DAYS it goes out with whatever is known, flagged as unread.
    """
    found = parse_day(listing.get("found_on"))
    now = parse_day(day)
    if not found or not now:
        return False
    return (now - found).days >= HOLD_DAYS


def parse_day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def render_daily(state: dict, decisions: dict, day: str) -> tuple:
    """The listings you have not been shown yet - the file you actually read.

    A listing appears in exactly one daily file, ever: `reported_on` is stamped
    when it is written out. Older files stay on disk as a browsable record of
    what turned up when, which is also the only place a flat that has since been
    let still appears.
    """
    listings = state.get("listings", [])
    apply_decisions(listings, decisions)

    # Hold back a listing whose detail page has not been read yet. STAGE2_CAP
    # bounds how many details one run fetches, so a big morning commits far more
    # listings than it reads - and a listing appears in exactly ONE daily file,
    # ever. Reporting an unread one spends its single appearance on a stub with
    # no size, floor, lift or A/C, and the filled-in version is never shown.
    # Measured 2026-09-19: 142 of 179 went out like that. They wait instead.
    fresh = [l for l in listings if not l.get("reported_on")]
    held = [l for l in fresh
            if is_live(l) and not l.get("aircon_checked") and not overdue(l, day)]
    fresh = [l for l in fresh if l not in held]

    # Re-running a day must reproduce that day, not blank it: everything already
    # stamped with this date belongs in this file too. Without this, generating
    # twice in one morning silently emptied the file.
    todays = fresh + [l for l in listings if l.get("reported_on") == day]
    live = order([l for l in todays if is_live(l)])
    dead = [l for l in todays if not is_live(l)]

    ac = [l for l in live if l.get("aircon") == "yes"]
    out = ["# New listings — %s" % day, ""]
    if not live and not dead:
        out += ["Nothing new today.", ""]
        if held:
            out += ["%d listing(s) found today are waiting on a detail page and "
                    "will appear once it is read." % len(held), ""]
        return '\n'.join(out), [], held, 0

    out += ["**%d new** · %d with A/C in unit · %d ruled out before you saw them"
            % (len(live), len(ac), len(dead)), "",
            "Sizes marked `*` were read off a floorplan by OCR, not stated by the agent.",
            "Record any call in `decisions.md`. Earlier days are in this folder.", ""]
    if held:
        out += ["_%d more found today are waiting on a detail page. They will appear "
                "in a later file, complete, rather than as a stub here._" % len(held), ""]

    for tier in ("High", "Medium", "Low"):
        rows = [l for l in live if str(l.get("priority")) == tier]
        if rows:
            out += ["## %s — %d" % (tier, len(rows)), ""] + table(rows) + [""]

    if dead:
        out += ["## Ruled out on sight — %d" % len(dead), "",
                "Caught by the hard filters, listed so the filtering stays visible.", "",
                "| why | £pcm | area | listing |", "|---|---|---|---|"]
        for l in dead:
            why = ""
            for part in str(l.get("notes") or "").split(";"):
                if "rejected on detail:" in part:
                    why = part.split("rejected on detail:")[1].strip()
            out.append("| %s | %s | %s | [%s](%s) |" % (
                why or str(l.get("status")).lower(), money(l.get("price_pcm")),
                cell(l.get("postcode") or l.get("area")),
                str(l.get("platform") or "link"), l.get("url")))
        out.append("")
    return '\n'.join(out), fresh, held, len(live) + len(dead)


def write_index(folder: pathlib.Path) -> None:
    days = sorted(folder.glob("20*.md"), reverse=True)
    lines = ["# Daily listings", "",
             "One file per run, holding only what was new that morning.", ""]
    for f in days:
        head = f.read_text(encoding="utf-8").splitlines()
        count = next((l for l in head if l.startswith("**")), "").split("·")[0].strip().strip("*")
        lines.append("- [%s](%s)%s" % (f.stem, f.name, " — " + count if count else " — nothing new"))
    (folder / "index.md").write_text('\n'.join(lines) + '\n', encoding="utf-8")


def apply_decisions(listings: list, decisions: dict) -> None:
    for l in listings:
        d = decisions.get(str(l.get("url", "")).strip())
        if d:
            l["status"] = d["status"]
            l["decision_note"] = d["note"]


def render(state: dict, decisions: dict, limit_low: int = 60) -> str:
    listings = state.get("listings", [])
    apply_decisions(listings, decisions)
    live = order([l for l in listings if is_live(l)])
    dead = [l for l in listings if not is_live(l)]

    today = dt.date.today().isoformat()
    ac = sum(1 for l in live if l.get("aircon") == "yes")
    out = ["# Flat shortlist", "",
           "%s · **%d live** · %d with A/C in unit · %d ruled out"
           % (today, len(live), ac, len(dead)), "",
           "Sizes marked `*` were read off a floorplan by OCR, not stated by the agent.",
           "To record a call, add a line to `decisions.md` — this file is regenerated every run.",
           ""]

    for tier in ("High", "Medium", "Low"):
        rows = [l for l in live if str(l.get("priority")) == tier]
        if not rows:
            continue
        shown = rows if tier != "Low" else rows[:limit_low]
        out += ["## %s — %d" % (tier, len(rows)), "",
                "| £pcm | area | sqft | fl | beds/bath | notable | listing |",
                "|---|---|---|---|---|---|---|"]
        for l in shown:
            out.append("| %s | %s | %s | %s | %s/%s | %s | [%s](%s) |" % (
                money(l.get("price_pcm")),
                cell(l.get("postcode") or l.get("area")),
                size_cell(l),
                cell(l.get("floor"), "–"),
                cell(l.get("bedrooms"), "?"), cell(l.get("bathrooms"), "?"),
                flags(l),
                str(l.get("platform") or "link"), l.get("url")))
        if len(shown) < len(rows):
            out.append("")
            out.append("_%d more %s listings in `state.json`._" % (len(rows) - len(shown), tier))
        out.append("")

    if dead:
        out += ["## Ruled out — %d" % len(dead), "",
                "| why | £pcm | area | listing |", "|---|---|---|---|"]
        for l in sorted(dead, key=lambda l: str(l.get("notes") or "")):
            why = ""
            for part in str(l.get("notes") or "").split(";"):
                if "rejected on detail:" in part:
                    why = part.split("rejected on detail:")[1].strip()
            out.append("| %s | %s | %s | [%s](%s) |" % (
                why or l.get("decision_note") or str(l.get("status")).lower(),
                money(l.get("price_pcm")),
                cell(l.get("postcode") or l.get("area")),
                str(l.get("platform") or "link"), l.get("url")))
        out.append("")
    return "\n".join(out)


def run(cfg, day: str | None = None) -> None:
    """Write today's daily file and regenerate the shortlist."""
    day = day or dt.date.today().isoformat()
    state_path = cfg.state_path
    state = load_state(state_path)
    decisions = load_decisions(cfg.decisions_path)

    folder = cfg.daily_dir
    folder.mkdir(parents=True, exist_ok=True)
    text, fresh, held, shown = render_daily(state, decisions, day)
    (folder / ("%s.md" % day)).write_text(text, encoding="utf-8")
    # Stamp only after the file is safely written, so a crash cannot lose
    # listings into a day file that was never created.
    for l in fresh:
        l["reported_on"] = day
    if fresh:
        state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False),
                              encoding="utf-8")
    write_index(folder)
    print("%s: %d listed%s%s" % (
        folder / ("%s.md" % day), shown,
        "  (%d newly stamped)" % len(fresh) if len(fresh) != shown else "",
        "  (%d held for their detail page)" % len(held) if held else ""))

    cfg.shortlist_path.write_text(render(state, decisions), encoding="utf-8")
    print("%s: %d listings, %d decisions folded in"
          % (cfg.shortlist_path, len(state.get("listings", [])), len(decisions)))
