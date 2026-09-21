"""The one entry point: `flat-search <command>`.

Before this existed the pipeline was seven scripts that had to be run in a
precise order, and the consequences of getting it wrong were not symmetrical.
Committing after a failed search marks listings as seen that were never shown,
and they never appear in a daily file again. That invariant lived in a prose
runbook, which is the wrong place for a rule whose violation destroys data.

`flat-search run` does the whole pipeline and enforces it. The individual
commands remain, because being able to re-run one stage against the files the
last one wrote is genuinely useful when something breaks.
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys

from . import audit as audit_mod
from . import config as config_mod
from . import core
from . import fetch as fetch_mod
from . import judge as judge_mod
from . import render as render_mod


# Raised here and in core, caught in one place: a refusal from either reads
# as a refusal, not as a traceback.
Abort = core.Abort


def _load(args) -> config_mod.Config:
    try:
        return config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        raise Abort(str(exc)) from None


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def stage_search(cfg, args) -> pathlib.Path:
    out = cfg.runs_dir / "stage1.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    ok = asyncio.run(fetch_mod.run_search(cfg, out, incremental=args.incremental))
    if not ok:
        raise Abort(
            "SEARCH FAILED - not committing.\n"
            "A challenged search and a quiet morning look identical, so this run\n"
            "stops here rather than marking unseen listings as seen. Listings\n"
            "committed now would never appear in a daily file again.\n"
            "Re-run the affected portal alone with --host, or try later.")
    return out


def stage_audit(cfg, stage1: pathlib.Path) -> None:
    if not audit_mod.report(cfg, stage1):
        raise Abort(
            "AUDIT SAYS NOT READY - stopping before the details fetch.\n"
            "An extractor looks broken. It will not crash; it will quietly\n"
            "produce a tracker in which every listing says 'no A/C mentioned'.")


def stage_details(cfg, stage1: pathlib.Path, args) -> None:
    queue = cfg.runs_dir / "queue.json"
    core.plan(cfg, stage1, queue, cap=args.cap, refresh=args.refresh)
    asyncio.run(fetch_mod.run_details(cfg, queue, stage1, host=args.host))


def stage_judge(cfg, stage1: pathlib.Path) -> int:
    verdicts = cfg.runs_dir / "verdicts.json"
    review = cfg.runs_dir / "needs_review.json"
    return judge_mod.run(cfg, verdicts, review, stage1)


def stage_commit(cfg, stage1: pathlib.Path, args) -> None:
    core.commit(cfg, stage1, cfg.runs_dir / "verdicts.json", update=args.refresh)


def stage_render(cfg) -> None:
    render_mod.run(cfg)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    cfg = _load(args)
    # Before anything touches state: the report at the end has to be able to
    # say what THIS run changed, not just what the totals are. `finish` reuses
    # the same baseline, so the delta spans the whole morning.
    core.write_baseline(cfg)
    stage1 = stage_search(cfg, args)
    stage_audit(cfg, stage1)
    stage_details(cfg, stage1, args)
    pending = stage_judge(cfg, stage1)
    # `judge` returns how many listings a model still has to read. Nothing may
    # commit while that is non-zero: an uncommitted listing can wait, but a
    # listing committed `unchecked` is stamped into a daily file it only ever
    # gets one of, and the judged version is never shown.
    #
    # The type check is not paranoia. `judge.run` once fell off the end
    # returning None, None is falsy, and the effect was not an error but a run
    # that committed 43 unjudged listings and looked perfectly healthy doing
    # it. A missing count has to be loud, because a quiet one commits.
    if not isinstance(pending, int):
        raise Abort(
            "JUDGE DID NOT REPORT A COUNT - not committing.\n"
            "It returned %r, so this run cannot tell 'nothing to judge' from\n"
            "'judgement was skipped'. Committing on that guess would stamp\n"
            "unjudged listings into today's file, and each listing gets one.\n"
            "Fix judge.run to return the number still needing a model."
            % (pending,))
    if pending:
        print()
        print("=" * 72)
        print("%d listing(s) mention cooling and need a model's judgement." % pending)
        print("  %s" % (cfg.runs_dir / "needs_review.json"))
        print()
        print("Judge them (in_unit vs communal_only, with a VERBATIM quote), write")
        print("  %s" % (cfg.runs_dir / "verdicts_haiku.json"))
        print("then finish with:  flat-search finish")
        print("=" * 72)
        return 0
    stage_commit(cfg, stage1, args)
    stage_render(cfg)
    core.report(cfg)
    return 0


def cmd_finish(args) -> int:
    """Second half of `run`, after a model has judged the flagged listings."""
    cfg = _load(args)
    stage1 = cfg.runs_dir / "stage1.json"
    if not stage1.exists():
        raise Abort("no %s - run `flat-search run` first" % stage1)
    merged = judge_mod.merge_verdicts(cfg)
    print("verdicts merged: %d" % merged)
    stage_commit(cfg, stage1, args)
    stage_render(cfg)
    core.report(cfg)
    return 0


def cmd_search(args) -> int:
    cfg = _load(args)
    # `run` is the way in, and it takes the baseline itself. This is for the
    # mornings that do not go that way: a portal re-run with `--host`, or
    # stages driven by hand. Without it `report` has nothing to difference
    # against, and on 2026-09-21 that silence ended in a digest that worked
    # the deltas out by hand and called 18 old rejections new.
    #
    # It fills a gap; it never moves a baseline already taken today, because
    # the repair case would otherwise reset the morning it is repairing.
    core.ensure_baseline(cfg)
    stage_search(cfg, args)
    return 0


def cmd_audit(args) -> int:
    cfg = _load(args)
    stage_audit(cfg, cfg.runs_dir / "stage1.json")
    return 0


def cmd_details(args) -> int:
    cfg = _load(args)
    stage_details(cfg, cfg.runs_dir / "stage1.json", args)
    return 0


def cmd_judge(args) -> int:
    cfg = _load(args)
    stage_judge(cfg, cfg.runs_dir / "stage1.json")
    return 0


def cmd_commit(args) -> int:
    cfg = _load(args)
    stage_commit(cfg, cfg.runs_dir / "stage1.json", args)
    return 0


def cmd_render(args) -> int:
    stage_render(_load(args))
    return 0


def cmd_report(args) -> int:
    core.report(_load(args))
    return 0


def cmd_check(args) -> int:
    """Validate the config and say what it will do, without touching a portal."""
    cfg = _load(args)
    print("config      : %s" % cfg.path)
    print("data dir    : %s" % cfg.data_dir)
    print("budget      : GBP %d pcm" % cfg.budget_pcm)
    print("bedrooms    : min %d%s" % (cfg.min_bedrooms,
                                      "" if not cfg.max_bedrooms else ", max %d" % cfg.max_bedrooms))
    print("bathrooms   : min %d" % cfg.min_bathrooms)
    print("size        : min %d sq ft (stated sizes only)" % cfg.min_sqft)
    print("districts   : %d prime, %d affluent, %d fringe%s" % (
        len(cfg.prime_districts), len(cfg.affluent_districts),
        len(cfg.fringe_districts),
        " (others REJECTED)" if cfg.districts_only else " (others ranked low)"))
    # Printed because it is the one threshold with no visible effect when it is
    # wrong: nothing downstream fails on an unreadable date, the availability
    # check is simply skipped. Loading now rejects one, so this line is here to
    # show what was understood - and to say which portals can actually answer it.
    if cfg.move_in:
        print("move-in     : %s (+%d days slack)" % (cfg.move_in, cfg.move_in_slack_days))
        print("              Filtered HERE, against the date each listing states:")
        print("              a listing stating a later one is REJECTED, one stating")
        print("              none is kept and read as available now.")
        # Nothing adds a portal-side availability filter any more, but a URL
        # pasted from a portal can still carry one, and it costs listings
        # silently: the portal drops everything it cannot date before this sees
        # it. Worth reading before a run, not deducing from a thin digest.
        carried = sorted({u.split("/")[2] for u in cfg.searches
                          if re.search(r"[?&](moveInByDate|available_from|"
                                       r"availableBefore)=", u)})
        if carried:
            print("              WARNING: these searches still carry the PORTAL's own")
            print("              availability filter: %s" % ", ".join(carried))
            print("              It drops every listing the portal cannot date - about")
            print("              a third of them - before we can read the page. Take it")
            print("              out of the URL and let the move-in date above do it.")
    else:
        print("move-in     : any (no date set - timing is not ranked on)")
    print("aircon      : %s" % cfg.aircon)
    print("cap         : %d detail pages per run" % cfg.stage2_cap)

    # Say what the NEXT run would ask each portal for, not just what the config
    # says, because the recency window is computed from the gap since the last
    # run and a stale state file is exactly when you want to see it spelled out.
    gap = fetch_mod.days_since_last_run(cfg)
    print("window      : %s" % (
        "server-side windowing is OFF (first_run_days = 0)" if not cfg.first_run_days
        else "no previous run - a first run looks back %d days" % cfg.first_run_days
        if gap is None else
        "%d day(s) since the last run, +%d margin"
        % (gap, fetch_mod.WINDOW_MARGIN_DAYS)))
    print("searches    : %d URL(s)" % len(cfg.searches))
    for u, note in fetch_mod.scoped_searches(cfg):
        print("   %s" % u)
        if note:
            print("      -> %s" % note)
    drift = fetch_mod.config_url_drift(cfg)
    print()
    if drift:
        print("WARNING - these searches are TIGHTER than your config, so they")
        print("never fetch listings your thresholds allow:")
        for d in drift:
            print("   ! %s" % d)
    else:
        print("searches and thresholds agree.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flat-search",
        description="A rental search that reads the listing prose for what the "
                    "portal filters cannot express.")
    p.add_argument("--config", default=config_mod.DEFAULT_CONFIG_NAME,
                   help="path to criteria.toml (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn, incremental=False, refresh=False, host=None, cap=None)
        return s

    r = add("run", cmd_run, "the whole pipeline, in the only safe order")
    r.add_argument("--incremental", action="store_true",
                   help="stop each newest-first portal once it serves listings "
                        "already tracked")
    r.add_argument("--refresh", action="store_true",
                   help="re-queue already-tracked listings; also how you clear a "
                        "backlog, since only uncached ones are fetched")
    r.add_argument("--cap", type=int, help="override stage2_cap for this run")

    f = add("finish", cmd_finish, "commit and render after a model judged the flagged listings")
    f.add_argument("--refresh", action="store_true", help="pairs with run --refresh")

    s = add("search", cmd_search, "stage 1 only: page the portal search results")
    s.add_argument("--incremental", action="store_true")

    add("audit", cmd_audit, "per-portal extraction health, against what is cached")

    d = add("details", cmd_details, "stage 2 only: plan a queue and fetch detail pages")
    d.add_argument("--refresh", action="store_true")
    d.add_argument("--cap", type=int)
    d.add_argument("--host", help="fetch only this portal, e.g. openrent")

    add("judge", cmd_judge, "decide A/C; flag the ones needing a model")
    c = add("commit", cmd_commit, "validate verdicts and write state")
    c.add_argument("--refresh", action="store_true", help="update existing rows")
    add("render", cmd_render, "write today's daily file and the shortlist")
    add("report", cmd_report, "summarise what is tracked")
    add("check", cmd_check, "validate the config; touches no portal")
    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except Abort as exc:
        print("\n%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
