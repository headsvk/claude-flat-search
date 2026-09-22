#!/usr/bin/env python3
"""Fetch layer - local headless Chromium via Playwright, all four portals.

One browser per portal run, cookie consent accepted before reading anything,
jittered pacing, and a hard cache so a listing page is never fetched twice.

    flat-search search
    flat-search details

Two hard-won rules are encoded here:

* **Accept cookie consent first.** Zoopla renders zero cards behind its consent
  wall and then escalates to a Cloudflare challenge on repeat visits. Consent
  handled, the same URL returns a full page and the bathroom filter grades
  correctly. This was the root cause of three wrong conclusions.

* **Never reuse a page across search URLs.** Navigating several searches through
  one page object returns a full first page and empty ones after - an ordering
  artifact that looks exactly like a broken filter. Each search URL gets a fresh
  context.

A challenge page and a genuinely empty result set look identical, so an empty
search is reported as an ERROR, never as a quiet success.

Setup:  uv sync && uv run playwright install chromium
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import random
import re
import shutil
import sys

from . import core
from . import floorplan
from . import judge
from . import portals

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

MAX_PAGES = 8

CONSENT = ["#onetrust-accept-btn-handler",
           "button:has-text('Accept all')", "button:has-text('Accept All')",
           "button:has-text('Accept cookies')", "button:has-text('Agree')",
           "button:has-text('Accept')"]

# Region-scoped searches place a listing whose address omits the postcode.
REGION_DISTRICT = {
    "REGION%5E1127": ("TW9", "Richmond"),
    "REGION%5E1368": ("TW1", "Twickenham"),
    "REGION%5E746": ("KT2", "Kingston upon Thames"),
    "REGION%5E317": ("IG7", "Chigwell"),
    "OUTCODE%5E855": ("EN5", "High Barnet"),
}


class Challenged(RuntimeError):
    """A bot check came back instead of the page.

    Its own type because the two callers want opposite things from it: a
    challenged SEARCH is a hard error - an empty result set and a block look
    identical, and the quiet one is the dangerous one - while a challenged
    DETAIL page is worth retrying in a fresh session, which is measurably
    enough to get past it.
    """


class Session:
    """A browser whose cookie consent has been dealt with."""

    def __init__(self, min_gap=2.0, max_gap=5.0):
        self.min_gap, self.max_gap = min_gap, max_gap
        self._consented = False
        self._first = True

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._b = await self._pw.chromium.launch(headless=True)
        self._ctx = await self._b.new_context(user_agent=UA, locale="en-GB")
        self.page = await self._ctx.new_page()
        return self

    async def __aexit__(self, *exc):
        await self._b.close()
        await self._pw.stop()

    async def _consent(self):
        for sel in CONSENT:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await self.page.wait_for_timeout(2500)
                    self._consented = True
                    return sel
            except Exception:
                pass
        return None

    async def recycle(self):
        """Throw this browser context away and open a clean one.

        Measured 2026-09-22 against Zoopla, three listings per variant:

            one page, 3s gap      ok, CHALLENGED, CHALLENGED
            one page, 20s gap     ok, CHALLENGED, CHALLENGED
            fresh CONTEXT, 3s     ok, ok, ok

        So the bot check is not about the rate - twenty seconds behaves exactly
        like three - it is about the context. A clean one clears it, and that is
        the cheap half of what the retry pass was already doing by launching a
        whole second browser.
        """
        try:
            await self._ctx.close()
        except Exception:
            pass
        self._ctx = await self._b.new_context(user_agent=UA, locale="en-GB")
        self.page = await self._ctx.new_page()
        self._consented = False

    async def get(self, url: str, settle=4000) -> str:
        if self._first:
            self._first = False
        else:
            await asyncio.sleep(random.uniform(self.min_gap, self.max_gap))
        await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        if not self._consented:
            await self._consent()
        await self.page.wait_for_timeout(settle)
        html = await self.page.content()
        try:
            body = await self.page.inner_text("body")
        except Exception:
            body = ""
        if portals.challenged(html, body):
            raise Challenged("CHALLENGED by %s" % url.split("/")[2])
        self.text = body
        return html

    async def expand(self):
        """Descriptions are collapsed behind a control on most portals; a
        truncated read is indistinguishable from a listing that says nothing.

        Two guards, both learned the hard way:
        * the control's WHOLE label must match - a loose "See more" matched
          OnTheMarket's "See more properties like this" and navigated to a
          search page, which then extracted as an empty listing;
        * if the URL changes anyway, keep the pre-expand text.
        """
        before_url = self.page.url
        before_text = getattr(self, "text", "") or ""
        labels = ("Read full description", "Show full description",
                  "Read more", "Show more", "More details")
        for label in labels:
            try:
                for tag in ("button", "a"):
                    el = await self.page.query_selector("%s:text-is('%s')" % (tag, label))
                    if el and await el.is_visible():
                        await el.click()
                        await self.page.wait_for_timeout(1200)
                        break
            except Exception:
                pass
        for _ in range(3):
            try:
                await self.page.mouse.wheel(0, 6000)
                await self.page.wait_for_timeout(700)
            except Exception:
                break
        try:
            after = await self.page.inner_text("body")
        except Exception:
            return before_text
        if self.page.url != before_url:
            # a click navigated away - that text belongs to another page
            try:
                await self.page.goto(before_url, timeout=45000,
                                     wait_until="domcontentloaded")
                await self.page.wait_for_timeout(2500)
                after = await self.page.inner_text("body")
            except Exception:
                after = before_text
        self.text = after if len(after) >= len(before_text) * 0.6 else before_text
        return self.text


# A run's recency window is the gap since the last one PLUS this, because the
# gap is measured from when the last run committed and listings keep arriving
# while it runs. Two days is cheap; the failure it prevents is silent.
WINDOW_MARGIN_DAYS = 2


def days_since_last_run(cfg) -> int | None:
    """Whole days since state was last written, or None if it never was.

    `updated` is stamped by every save_state, so it marks the last run that
    actually committed something - which is the right mark. A run that searched
    and then refused to commit (a challenged portal) leaves the stamp alone, so
    the next run widens its window to cover the morning that was lost.
    """
    state_path = cfg.state_path
    if not state_path.exists():
        return None
    stamp = str(core.read_json(state_path).get("updated") or "").strip()
    if not stamp:
        return None
    try:
        then = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return max(0, (dt.datetime.now() - then).days)


def add_param(url: str, param: str, value: str) -> str:
    """Append one query parameter, whichever way the URL is written.

    A URL built on a portal's own filter page usually carries a query string
    already - every one in the config today does - but not always: OnTheMarket
    and OpenRent both have path-only forms, and `url + "&added=3_days"` on one
    of those puts an ampersand in a path. Neither outcome is loud. A portal
    that ignores the parameter pages its whole backlog, which reads as a busy
    morning; one that 404s returns nothing, which reads as a quiet one.
    """
    return "%s%s%s=%s" % (url, "&" if "?" in url else "?", param, value)


def scope_search_url(url: str, cfg, gap_days: int | None) -> tuple[str, str | None]:
    """Narrow one search URL at the portal. -> (url, note or None).

    Doing this server-side is strictly cheaper than the client-side stop rule
    it complements: `--incremental` only stops early on newest-first portals,
    and only after two nearly-stale pages, so it is a heuristic laid over a
    full walk. A window is arithmetic done before anything is sent.

    A parameter already written into the config URL is left exactly as it is -
    it is a deliberate choice by whoever wrote it, and quietly appending a
    second value for the same filter is how you get an unreadable URL that
    neither of you chose.
    """
    notes = []

    recent = portals.window_for(url, portals.RECENT_WINDOWS)
    if recent and cfg.first_run_days:
        param, offered = recent
        if re.search(r"[?&]" + re.escape(param) + r"=", url):
            notes.append("%s= already in the URL, left alone" % param)
        else:
            want = (cfg.first_run_days if gap_days is None
                    else gap_days + WINDOW_MARGIN_DAYS)
            days = portals.snap_up(want, offered)
            if days is None:
                # Been away longer than the portal will scope. Page the lot -
                # a window narrower than the gap loses the difference silently.
                notes.append("%d-day gap is wider than any %s= window - not scoped"
                             % (want, param))
            else:
                url = add_param(url, param, offered[days])
                notes.append("%s=%s (%s)" % (param, offered[days],
                                             "first run" if gap_days is None
                                             else "%d days since the last run" % gap_days))

    # Availability is NOT added here, and that is deliberate. A move-in
    # deadline is something you state on the portal, in the same filter panel
    # as the price and the bed count, and the URL you paste into [searches] is
    # that statement. This module used to append one of its own from
    # `move_in`, which meant editing a date in criteria.toml silently changed
    # what four portals returned - the runbook had to carry a warning that a
    # sharp drop in counts the next morning was not a broken extractor.
    #
    # The line between the two is what the run knows that the URL cannot say:
    # the recency window above is measured from the last run and has to be
    # computed fresh every morning. A move-in date is not - and a move-in date
    # is filtered HERE, in hard_filter, against the date each listing states,
    # so nothing is asked of the portal at all.
    return url, "; ".join(notes) or None


def scoped_searches(cfg) -> list:
    """Every search URL, narrowed where the portal supports it, with a log."""
    gap = days_since_last_run(cfg)
    out = []
    for url in cfg.searches:
        scoped, note = scope_search_url(url, cfg, gap)
        out.append((scoped, note))
    return out


async def collect(url: str, seen: set, known: set | None = None,
                  incremental: bool = False) -> tuple[list, str]:
    """One search URL, its own browser. Returns (new listings, status)."""
    pt = portals.portal_for(url)
    if not pt:
        return [], "no portal handler"
    ident = re.search(r"locationIdentifier=([A-Z]+%5E\d+)", url)
    fallback = REGION_DISTRICT.get(ident.group(1)) if ident else None

    found, local_seen, stale = [], set(), 0
    for page_no in range(MAX_PAGES):
        target = url if page_no == 0 else url + (pt["page_param"] % (page_no * pt["step"]
                                                                     if pt["step"] > 1
                                                                     else page_no + 1))
        # A FRESH browser per page. Reusing one page object across paginated
        # URLs returns a full first page and empty ones after - measured, and
        # it is indistinguishable from "the portal has no more results".
        # Zoopla looked capped at 28 listings for exactly this reason; with a
        # fresh context, pn=2 and pn=3 overlap page 1 by 0%.
        async with Session() as s:
            html = await s.get(target)
            res = pt["search"](html, s.page)
            rows, total = (await res) if asyncio.iscoroutine(res) else res
        if not rows:
            return found, ("EMPTY on page %d" % page_no) if page_no == 0 else "ok"

        page_urls = {r["url"] for r in rows if r.get("url")}
        # Pagination advance is judged PER SEARCH, not against the global dedup
        # set. Searches overlap (all-London contains Richmond), so a page that is
        # entirely new-to-the-portal but already collected by another search must
        # not be read as "no more pages" - that silently truncated whole searches.
        advancing = bool(page_urls - local_seen)
        local_seen |= page_urls

        fresh = [r for r in rows if r.get("url") and r["url"] not in seen]
        for r in fresh:
            seen.add(r["url"])
            if fallback:
                r["region_district"], r["region_name"] = fallback
                r["area_trusted"] = True
        found += fresh

        if not advancing:                   # same page served again - truly done
            break
        if total and len(local_seen) >= total:
            break
        if pt["step"] > 1 and total and (page_no + 1) * pt["step"] >= total:
            break

        # Incremental runs stop once the portal is serving listings we already
        # track. Only sound when the search is sorted newest-first, and only
        # after TWO consecutive stale pages, so one unlucky page cannot end it.
        if incremental and known and pt.get("sorted_newest"):
            already = len(page_urls & known) / max(len(page_urls), 1)
            stale = stale + 1 if already >= 0.9 else 0
            if stale >= 2:
                return found, "stopped early: 2 pages >=90%% already tracked"
    return found, "ok"


async def run_search(cfg, out_path, incremental: bool = False) -> bool:
    """-> True when every portal came back clean.

    Returns a bool rather than exiting, because the CALLER has to decide what a
    failed search means - and the only safe answer is "do not commit". An empty
    search and a blocked one are indistinguishable from here.
    """
    await preflight(cfg)
    out_path = pathlib.Path(out_path)
    if not cfg.searches:
        raise RuntimeError("no [searches] URLs in the config")
    urls = []
    for url, note in scoped_searches(cfg):
        urls.append(url)
        if note:
            print("  window %-16s %s" % (url.split("/")[2], note), flush=True)
    known = set()
    if incremental:
        known = core.known_urls(cfg)
        print("incremental: %d listings already tracked" % len(known))

    by_host: dict = {}
    for url in urls:
        by_host.setdefault(url.split("/")[2], []).append(url)

    async def run_host(host, host_urls):
        """One browser per portal, its URLs in order - unchanged behaviour."""
        seen, rows_all, problems, log = set(), [], [], []
        for url in host_urls:
            try:
                rows, status = await collect(url, seen, known, incremental)
            except Exception as exc:
                problems.append("%s  %s" % (host, str(exc)[:90]))
                log.append("  !! %-22s %s" % (host, str(exc)[:90]))
                continue
            if status != "ok":
                problems.append("%s  %s" % (host, status))
            rows_all += rows
            log.append("  %-22s +%-4d %s" % (host, len(rows), status))
            await asyncio.sleep(random.uniform(3, 6))
        print("  done %-20s %d listings" % (host, len(rows_all)), flush=True)
        return rows_all, problems, log

    print("searching %d portals concurrently" % len(by_host), flush=True)
    results = await asyncio.gather(*(run_host(h, u) for h, u in by_host.items()))

    listings, problems, merged = [], [], set()
    for rows, host_problems, log in results:
        problems += host_problems
        print('\n'.join(log))
        for row in rows:
            url = row.get("url")
            if url and url not in merged:
                merged.add(url)
                listings.append(row)

    core.write_json(out_path, {"run_date": core.today(), "listings": listings})
    by = {}
    for l in listings:
        by[l["platform"]] = by.get(l["platform"], 0) + 1
    print("\n%d unique listings  %s" % (len(listings), by))
    print("-> %s" % out_path)
    if problems:
        print("\nPROBLEMS (an empty search is indistinguishable from a block):")
        for p in problems:
            print("   ! %s" % p)
        return False
    return True



# OpenRent names no floorplan: it is just another listing photo, and the URLs are
# built client side, so they have to come from the rendered DOM.
OPENRENT_IMAGES = """() => {
  const out = new Set();
  document.querySelectorAll('img').forEach(i => {
    [i.src, i.dataset.src].forEach(v => { if (v) out.add(v); });
    if (i.srcset) i.srcset.split(',').forEach(p => out.add(p.trim().split(' ')[0]));
  });
  return [...out];
}"""


async def floorplan_size(session, html, portal_name, cfg, url):
    """Read the size off the floorplan, for listings that state none.

    A FALLBACK ONLY: a stated size always wins. What comes back is recorded as
    `size_sqft_plan`, never as `size_sqft`, so it cannot reach the hard filter -
    a misread plan must not delete a flat, so this is flag-only by design.
    """
    if portal_name == "OpenRent":
        try:
            images = await session.page.evaluate(OPENRENT_IMAGES)
        except Exception:
            return None
        candidates = [u for u in images
                      if "imagescdn.openrent" in u and "staticMap" not in u]
    else:
        candidates = floorplan.plan_urls(html, portal_name)
    if not candidates:
        return None
    workdir = cfg.runs_dir / "plans" / re.sub(r"[^A-Za-z0-9]+", "_", url)[-60:]
    try:
        result = floorplan.area_from_candidates(candidates, workdir)
    except Exception as exc:
        print("  plan-fail %-34s %s" % (url[-34:], str(exc)[:40]))
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    # Not just a size any more: a plan with no total area still names its
    # floor, and that read was being dropped with the rest of the OCR.
    return result if (result.get("sqft") or result.get("floors")) else None


async def enrich_detail(session, pt, url, html, text, parts, info, row, cfg):
    """Everything read AFTER the portal's own detail extractor: Rightmove's
    labelled fields, and the floorplan.

    This exists as one function because it used to be written out inline in the
    main pass and silently omitted from the retry pass. Zoopla serves a stub for
    every listing after the first in a session - measured 496,259 chars for the
    first and 28,781 for the rest, with the whole embedded payload gone - so the
    stub extracts to nothing, trips the empty-page guard, and is retried in a
    fresh session. That means effectively EVERY Zoopla listing arrives through
    the retry path. The description came back, so the listings looked healthy;
    the floorplan read simply never ran. 0 of 126 Zoopla pages carried one.
    """
    if pt["name"] == "Rightmove":
        flat = portals.untag(html)
        for label, key in (("Furnish type", "furnished"),
                           ("Let available date", "available_from")):
            m = re.search(re.escape(label) + r"\s*:?\s*([A-Za-z0-9 /,-]{2,30})", flat)
            if m:
                v = clean_value(m.group(1))
                if v:
                    parts.append("%s: %s" % (label, v))
                    # "Now" is a date; "Ask agent" is not one.
                    info[key] = (portals.availability_value(v) or v
                                 if key == "available_from" else v)

    row = row or {}
    # The plan is read when the listing states no size, and ALSO when it states
    # no floor. It was only ever the size before, which left 230 live listings
    # with a stated size and an unknown floor that no plan was ever read for -
    # and lower ground is a hard reject, so an unread plan there is a listing
    # recommended on a missing fact. The floor known from the prose counts:
    # judge.amenities is pure text and costs nothing to ask here.
    prose = judge.amenities(text or "")
    knows_size = bool(info.get("size_sqft") or row.get("size_sqft"))
    knows_floor = bool(info.get("floor_level") or info.get("floor_number")
                       or row.get("floor_level") or row.get("floor_number")
                       or prose.get("floor_level") or prose.get("floor_number"))
    if knows_size and knows_floor:
        return
    plan = await floorplan_size(session, html, pt["name"], cfg, url)
    if plan and plan.get("sqft") and not knows_size:
        info["size_sqft_plan"] = plan["sqft"]
        info["size_plan_basis"] = plan["basis"]
        info["size_plan_confident"] = plan["confident"]
        parts.append(
            "Floorplan area: %d sq ft (%s, %s) - read by OCR from %s"
            % (plan["sqft"], plan["basis"],
               "clear" if plan["confident"] else "AMBIGUOUS - verify",
               plan.get("image", "")))
        parts.append("Floorplan text: " + plan.get("evidence", ""))
    # A plan states which floor it is whether or not it states an area, and
    # lower ground is a hard reject - so the floor is kept even when the size
    # read came back empty, which is the case that used to discard the whole
    # OCR. Unlike the size, this DOES reach the hard filter: a misread digit
    # turns 814 into 314, but the phrase "Lower Ground Floor" does not misread
    # into a different storey.
    if plan and plan.get("floors") and not knows_floor:
        parts.append("Floorplan floors: %s - read by OCR from %s"
                     % (", ".join(plan["floors"]), plan.get("image", "")))
        for k, v in floorplan.floor_from_plan(plan["floors"]).items():
            info.setdefault(k, v)


async def run_details(cfg, queue_path, stage1_path, host=None):
    await preflight(cfg)
    queue = core.read_json(pathlib.Path(queue_path))
    items = queue.get("fetch", [])
    stage1_path = pathlib.Path(stage1_path)
    stage1 = core.read_json(stage1_path)
    by_url = {l["url"]: l for l in stage1.get("listings", [])}
    done = failed = 0

    groups: dict[str, list] = {}
    for it in items:
        if not pathlib.Path(it["cache_path"]).exists():
            groups.setdefault(it["url"].split("/")[2], []).append(it)
    if host:
        groups = {h: g for h, g in groups.items() if host.lower() in h.lower()}
        if not groups:
            print("no queued listings match --host %s" % host)
            print("queued hosts: " + ", ".join(sorted(
                {it["url"].split("/")[2] for it in items})))
            return
    if not groups:
        print("nothing to fetch - cache is warm")
        return

    async def run_host(host, group):
        # These tally the whole run across all four workers.
        nonlocal done, failed
        pt = portals.portal_for(group[0]["url"])
        host_done = host_failed = host_challenged = 0
        retry: list = []
        print('\n' + "--- %s: %d to fetch ---" % (host, len(group)))
        async with Session() as s:
            for it in group:
                try:
                    try:
                        html = await s.get(it["url"], settle=3000)
                    except Challenged:
                        # A clean context clears the check; waiting does not.
                        # One attempt at that before handing the listing to the
                        # retry pass, which pays for a whole new browser.
                        host_challenged += 1
                        print("  challenged %-30s -> clean context, retrying"
                              % it["url"][-30:])
                        await s.recycle()
                        html = await s.get(it["url"], settle=3000)
                    text = await s.expand()
                    parts, info = pt["detail"](html, text)
                    await enrich_detail(s, pt, it["url"], html, text,
                                        parts, info, by_url.get(it["url"]), cfg)
                    text = "\n".join(parts)
                    if not text.strip():
                        # A page that rendered nothing. Until 2026-09-22 this also
                        # silently absorbed every Zoopla bot challenge, because the
                        # challenge was not recognised and extracted to nothing -
                        # see Challenged. Retry once in a fresh session before
                        # believing the listing is contentless.
                        retry.append(it)
                        continue
                    core.write_json(pathlib.Path(it["cache_path"]),
                                    {"url": it["url"], "fetched_at": core.today(),
                                     "source": pt["name"].lower() + "-detail", "text": text})
                    row = by_url.get(it["url"])
                    if row:
                        for k, v in info.items():
                            if not row.get(k):
                                row[k] = v
                    done += 1
                    host_done += 1
                    print("  ok %-46s %5d chars" % (it["url"][-46:], len(text)))
                except Challenged:
                    # Challenged again on a clean context. Not a failure - the
                    # retry pass gets a whole new browser and that has always
                    # been enough - but counted and printed either way, because
                    # a portal quietly serving bot checks for half a run is
                    # exactly what must not pass for a healthy fetch.
                    print("  challenged %-30s -> retry in a fresh session"
                          % it["url"][-30:])
                    retry.append(it)
                    continue
                except Exception as e:
                    print("  FAIL %-42s %s" % (it["url"][-42:], str(e)[:60]))
                    failed += 1
                    host_failed += 1
        for it in retry:
            recovered = False
            try:
                async with Session() as s2:
                    html = await s2.get(it["url"], settle=5000)
                    text = await s2.expand()
                    parts, info = pt["detail"](html, text)
                    await enrich_detail(s2, pt, it["url"], html, text,
                                        parts, info, by_url.get(it["url"]), cfg)
                    text = chr(10).join(parts)
                    if text.strip():
                        core.write_json(pathlib.Path(it["cache_path"]),
                                        {"url": it["url"], "fetched_at": core.today(),
                                         "source": pt["name"].lower() + "-detail",
                                         "text": text})
                        row = by_url.get(it["url"])
                        if row:
                            for k, v in info.items():
                                if not row.get(k):
                                    row[k] = v
                        recovered = True
            except Exception as e:
                print("  FAIL(retry) %-36s %s" % (it["url"][-36:], str(e)[:50]))
            if recovered:
                done += 1
                host_done += 1
                print("  ok(retry) %-40s %5d chars" % (it["url"][-40:], len(text)))
            else:
                failed += 1
                host_failed += 1
                print("  EMPTY %s" % it["url"][-46:])

        # Per-host, not just per-run: a portal whose extractor has regressed
        # shows up here as a high miss rate instead of being averaged away
        # across the other three.
        total = host_done + host_failed
        rate = (100.0 * host_failed / total) if total else 0.0
        print("--- %s: ok %d, missed %d (%.0f%%)%s %s" % (
            host, host_done, host_failed, rate,
            ", %d bot-challenged" % host_challenged if host_challenged else "",
            "*** CHECK THE EXTRACTOR ***" if rate >= 10 and total >= 10 else ""))
    await asyncio.gather(*(run_host(h, g) for h, g in groups.items()))
    core.write_json(stage1_path, stage1)
    print('\n' + "cached %d, failed %d -> %s" % (done, failed, stage1_path))


STOP_LABELS = re.compile(
    r"\s+(?:Council Tax|Deposit|Let available date|Furnish type|Tenancy info|"
    r"PROPERTY TYPE|BEDROOMS|BATHROOMS|SIZE|Key features|Description)\b", re.I)


def clean_value(raw: str) -> str:
    return STOP_LABELS.split(raw.strip(), 1)[0].strip(" :,-")


# Each portal spells the same three filters differently. Value semantics:
# a price is a CEILING, beds and baths are FLOORS.
URL_FILTERS = {
    "maxPrice": ("budget_pcm", "ceiling"),
    "price_max": ("budget_pcm", "ceiling"),
    "max-price": ("budget_pcm", "ceiling"),
    "prices_max": ("budget_pcm", "ceiling"),
    "minBedrooms": ("min_bedrooms", "floor"),
    "beds_min": ("min_bedrooms", "floor"),
    "min-bedrooms": ("min_bedrooms", "floor"),
    "bedrooms_min": ("min_bedrooms", "floor"),
    "minBathrooms": ("min_bathrooms", "floor"),
    "baths_min": ("min_bathrooms", "floor"),
    "min-bathrooms": ("min_bathrooms", "floor"),
    "bathrooms_min": ("min_bathrooms", "floor"),
}


def config_url_drift(cfg) -> list:
    """Thresholds live in TWO places: the config keys the hard filter reads, and
    the query params baked into each search URL. Nothing keeps them in step.

    Only one direction of drift matters, and it is invisible. A URL that is
    LOOSER than the config is merely wasteful - the extra listings are fetched
    and then rejected locally. A URL that is TIGHTER never fetches them at all,
    so they cannot appear in any digest and the morning looks quiet. Raising
    UNIT_BUDGET to 5000 while the URLs still say maxPrice=4500 silently deletes
    the whole band you just opened up.

    Reported, never fatal: a deliberately tighter URL is a legitimate choice.
    """
    out = []
    for url in cfg.searches:
        host = url.split("/")[2]
        for param, (key, kind) in URL_FILTERS.items():
            m = re.search(r"[?&]" + re.escape(param) + r"=(\d+)", url)
            if not m:
                continue
            want = getattr(cfg, key, None)
            if not isinstance(want, int) or want <= 0:
                continue
            got = int(m.group(1))
            if kind == "ceiling" and got < want:
                out.append("%s: %s=%d is BELOW %s=%d - listings between %d and "
                           "%d are never fetched" % (host, param, got, key, want, got, want))
            elif kind == "floor" and got > want:
                out.append("%s: %s=%d is ABOVE %s=%d - listings with %d are "
                           "never fetched" % (host, param, got, key, want, want))
    # Six Rightmove URLs carrying the same params produce six identical lines.
    return list(dict.fromkeys(out))


async def preflight(cfg: dict | None = None) -> None:
    """Prove we can actually fetch BEFORE spending an hour finding out we cannot.

    A missing REQUIRED dependency is the easy case - it fails loudly either way.
    The dangerous one is a missing OPTIONAL dependency, because it degrades in
    silence: with no Tesseract, floorplan OCR reads nothing, every listing is
    filed "size not stated", and the run looks completely healthy. That is this
    project's characteristic bug wearing a different hat, so the absence is
    announced once, up front, where it cannot be mistaken for a finding.

    Chromium is launched rather than merely located: `pip install playwright`
    without `playwright install chromium` imports perfectly and then fails on
    the first navigation, a third of the way into a run.
    """
    problems = []
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("FATAL: playwright is not installed.\n"
                 "  uv sync\n"
                 "  uv run playwright install chromium")

    try:
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=True)
            await browser.close()
        finally:
            await pw.stop()
    except Exception as exc:
        problems.append("chromium will not launch: %s" % str(exc)[:160])

    if problems:
        sys.exit("FATAL: cannot fetch.\n  " + "\n  ".join(problems) +
                 "\n  python -m playwright install chromium")

    # Optional - degrade, but say so out loud.
    notes = []
    if not floorplan.tesseract():
        notes.append("Tesseract not found - floorplan OCR DISABLED. Listings that "
                     "state no size will be flagged 'size not stated', NOT sized.")
    try:
        import PIL  # noqa: F401
    except ImportError:
        notes.append("Pillow not installed - floorplan detection DISABLED "
                     "(OpenRent plans are found by appearance).")
    if cfg is not None:
        for d in config_url_drift(cfg):
            notes.append("SEARCH URL IS TIGHTER THAN YOUR CONFIG - " + d)
    for n in notes:
        print("  ! %s" % n, flush=True)
