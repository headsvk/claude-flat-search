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
import sys

from . import audit as audit_mod
from . import config as config_mod
from . import core
from . import fetch as fetch_mod
from . import judge as judge_mod
from . import render as render_mod


class Abort(Exception):
    """A refusal the user needs to read, not a traceback."""


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
    stage1 = stage_search(cfg, args)
    stage_audit(cfg, stage1)
    stage_details(cfg, stage1, args)
    pending = stage_judge(cfg, stage1)
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
    print("aircon      : %s" % cfg.aircon)
    print("cap         : %d detail pages per run" % cfg.stage2_cap)
    print("searches    : %d URL(s)" % len(cfg.searches))
    for u in cfg.searches:
        print("   %s" % u)
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
