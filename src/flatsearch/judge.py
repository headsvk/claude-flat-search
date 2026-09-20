#!/usr/bin/env python3
"""Stage-2 A/C pre-filter - deterministic, no model.

Measured on 47 real listings in this segment: ZERO mention cooling in any form.
So putting a model on every listing is paying for a judgement that is almost
always "the text is silent" - which Python can establish exactly.

The split:

  * NO cooling term anywhere  -> `unstated`, decided here. Sound, because a
    verdict of `unstated` is a claim about the ABSENCE of vocabulary, and the
    term list below is the vocabulary. Note this never emits `no` - a silent
    listing is not a listing that says there is none.

  * ANY cooling term present  -> written to needs_review.json and left for a
    model. The hard call is `scope`: air conditioning in the flat, or in the
    residents' gym? That needs reading comprehension and is what the whole
    two-stage design exists for. It just turns out to be rare.

This is deliberately conservative: the pre-filter may only downgrade work to a
human/model, never upgrade. It cannot emit `yes` on its own.

    flat-search judge
"""
from __future__ import annotations

import hashlib
import pathlib
import re

from . import core

# Kept broad on purpose: a term the list misses becomes a wrong `unstated`.
TERMS = [
    "air condition", "air-condition", "air con", "aircon", "a/c", "a.c.",
    "comfort cooling", "climate control", "climate-control",
    "vrf", "vrv", "split system", "split-system",
    "daikin", "mitsubishi electric", "panasonic", "fujitsu",
    "heating and cooling", "cooling system", "cooled",
]

# Words that look relevant but are NOT air conditioning. Flagged in the report
# so a near-miss is visible rather than silently swallowed.
DECOYS = ["ceiling fan", "mvhr", "mechanical ventilation", "well ventilated",
          "underfloor heating", "double glazing", "air source heat pump"]



# --------------------------------------------------------------------------
# amenities - floor / lift / concierge / size
# --------------------------------------------------------------------------

WORD_FLOOR = {"ground": 0, "first": 1, "second": 2, "third": 3, "fourth": 4,
              "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
              "tenth": 10}

# Only phrasings that describe THE PROPERTY. A bare "ground floor" often refers
# to the building ("concierge on the ground floor") - the same trap as A/C in
# the residents' gym, so it is not enough on its own.
FLOOR_PAT = re.compile(
    r"(?:situated|located|set|positioned)?\s*on\s+the\s+(\w+)\s+floor"
    r"|(\w+)[- ]floor\s+(?:apartment|flat|maisonette|property)"
    r"|(\d+)(?:st|nd|rd|th)\s+floor", re.I)
LOWER_GROUND = re.compile(r"lower\s+ground|basement|garden\s+level", re.I)
TOP_FLOOR = re.compile(r"top[- ]floor|penthouse", re.I)
LIFT_YES = re.compile(r"\blifts?\b|\belevator\b|passenger lift", re.I)
LIFT_NO = re.compile(r"\bno lift|without a lift|\bwalk[- ]?up\b|\bno elevator", re.I)
CONCIERGE = re.compile(r"concierge|porter(?:age)?\b|door\s?man", re.I)
SQFT = re.compile(r"([\d,]{3,6})\s*sq\.?\s*(?:ft|feet)", re.I)

# "Refurbished" is a promotion signal, but the same words describe a WRECK when
# they point forwards: "in need of refurbishment", "scope for renovation". Those
# mean the flat needs the work done, which is the opposite of what we want, so
# the negative pattern is checked first and wins.
REFURB_NEG = re.compile(
    r"(?:in need of|needs?|requires?|scope for|potential for|opportunity (?:for|to)"
    r"|ripe for|to be|awaiting|due to be|will be|prior to)\s+"
    r"(?:complete\s+|full\s+|some\s+)?(?:refurbish\w*|renovat\w*|modernis\w*|updating)"
    r"|(?:un|non[- ])modernised|in need of (?:some )?(?:work|updating|tlc)", re.I)
REFURB_POS = re.compile(
    r"(?:newly|recently|fully|completely|extensively|beautifully|tastefully|"
    r"just|high[- ]spec(?:ification)?|professionally)\s+"
    r"(?:refurbish\w*|renovat\w*|modernis\w*|redecorat\w*)"
    r"|(?:has|have|been)\s+(?:recently\s+|fully\s+)?(?:refurbish\w*|renovat\w*|modernis\w*)"
    r"|\brefurbish(?:ed|ment)\b|\brenovated\b|\bbrand[- ]new\b", re.I)


def amenities(text: str) -> dict:
    low = text.lower()
    out = {}

    if LOWER_GROUND.search(low):
        out["floor_level"] = "lower_ground"
    elif TOP_FLOOR.search(low):
        out["floor_level"] = "top"
    else:
        for m in FLOOR_PAT.finditer(low):
            tok = next((g for g in m.groups() if g), None)
            if tok is None:
                continue
            n = WORD_FLOOR.get(tok) if not tok.isdigit() else int(tok)
            if n is None:
                continue
            if n == 0:
                out["floor_level"] = "ground"
            else:
                out["floor_number"] = n
            break

    if LIFT_NO.search(low):
        out["lift"] = "no"
    elif LIFT_YES.search(low):
        out["lift"] = "yes"

    if CONCIERGE.search(low):
        out["concierge"] = "yes"

    # Negative wins: a listing that says both is describing work still to do.
    if REFURB_NEG.search(low):
        out["condition"] = "needs_work"
    elif REFURB_POS.search(low):
        out["condition"] = "refurbished"

    best = None
    for m in SQFT.finditer(low):
        v = int(m.group(1).replace(",", ""))
        if 250 <= v <= 20000 and (best is None or v > best):
            best = v
    if best:
        out["size_sqft"] = best
    return out


def scan(text: str):
    low = text.lower()
    hits = [t for t in TERMS if t in low]
    decoys = [d for d in DECOYS if d in low]
    return hits, decoys


# The review payload used to carry each listing's whole page text - 76.6 KB for
# 32 entries. The judgement only ever needs the prose around the matched term,
# and `commit` re-validates the quote against the full cached text on disk, so
# windowing is safe PROVIDED every window is a contiguous verbatim slice. The
# gap marker has to be unmistakable: a quote spanning two windows would not be
# found in the page and would silently collapse to `unstated`.
WINDOW = 250
GAP = "\n\n[... snip - do not quote across this ...]\n\n"


def windows(text: str, hits: list, pad: int = WINDOW) -> str:
    """Contiguous +/-pad slices round every occurrence of every matched term."""
    low = text.lower()
    spans = []
    for term in hits:
        start = 0
        while True:
            i = low.find(term, start)
            if i < 0:
                break
            spans.append((max(0, i - pad), min(len(text), i + len(term) + pad)))
            start = i + 1
    if not spans:
        return text
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return GAP.join(text[a:b] for a, b in merged)


# --------------------------------------------------------------------------
# carrying verdicts forward
# --------------------------------------------------------------------------

def text_sha(text: str) -> str:
    """Fingerprint of the page a verdict was made against.

    Normalised first, so a whitespace change on the portal does not look like
    a rewritten description and send a settled listing back to the model.
    """
    return hashlib.sha256(core.norm(text).encode("utf-8")).hexdigest()[:16]


def load_judged(cfg) -> tuple[dict, list]:
    """Model verdicts from earlier runs that the current page still supports.

    `run` scans the whole cache, so a listing that mentions cooling is a
    candidate every single morning for as long as it is tracked. Measured
    2026-09-20: 43 flagged, 4 of them genuinely new - the other 39 already had
    a verdict, and diffing them out was a manual step done by hand in the
    session. It is done here instead.

    A carried-forward verdict is only safe while the page it quotes is
    unchanged. `commit` already collapses a claim whose quote has vanished,
    but that is a SILENT downgrade: the listing lands on `unstated` and is
    never read again. Comparing the fingerprint re-flags it for judgement
    instead, which is the only outcome that recovers.

    -> ({url: verdict}, [urls whose page changed under a stored verdict])
    """
    path = cfg.runs_dir / "verdicts_haiku.json"
    if not path.exists():
        return {}, []
    try:
        stored = core.read_json(path).get("verdicts", [])
    except (ValueError, OSError):
        return {}, []

    judged, stale = {}, []
    for v in stored:
        url = str(v.get("url", "")).strip()
        if not url:
            continue
        # Only the handful of pages that actually carry a verdict, addressed
        # directly. Walking the whole cache to find them reads every listing
        # twice for the sake of a few dozen.
        cache_file = cfg.cache_dir / core.cache_name(url)
        if not cache_file.exists():
            continue
        try:
            text = core.read_json(cache_file).get("text", "")
        except (ValueError, OSError):
            continue
        sha = str(v.get("text_sha", "") or "")
        if sha:
            fresh = sha == text_sha(text)
        else:
            # Written before verdicts carried a fingerprint. Fall back to the
            # quote itself, which is the same test `commit` applies; a verdict
            # with nothing to quote has nothing that can go stale.
            evidence = (v.get("aircon") or {}).get("evidence") or ""
            fresh = not evidence.strip() or core.norm(evidence) in core.norm(text)
        if fresh:
            carried = {"url": url, "aircon": v.get("aircon") or {}}
            judged[url] = carried
        else:
            stale.append(url)
    return judged, stale


def run(cfg, out_path, review_path, stage1_path=None) -> int:
    """-> how many listings still need a model's judgement.

    That return value is the whole point of the stage: `flat-search run` uses
    it to decide whether to stop and ask, or to go on and commit. It was
    missing for a while - the function fell off the end returning None - and
    the effect was not a crash but a run that committed 43 unjudged listings
    as `unchecked` and wrote them into a daily file, which a listing only ever
    gets one of. Keep the return.
    """
    out_path, review_path = pathlib.Path(out_path), pathlib.Path(review_path)
    cache = cfg.cache_dir
    files = sorted(cache.glob("*.json"))
    if not files:
        raise RuntimeError("cache is empty - fetch detail pages first")

    stage1_path = pathlib.Path(stage1_path) if stage1_path else None
    stage1 = core.read_json(stage1_path) if stage1_path and stage1_path.exists() else None
    by_url = {l["url"]: l for l in stage1["listings"]} if stage1 else {}
    amen_counts = {}
    judged, stale = load_judged(cfg)

    verdicts, review, decoy_hits, thin = [], [], [], []
    carried = 0
    for f in files:
        j = core.read_json(f)
        text, url = j.get("text", ""), j.get("url", "")
        row = by_url.get(url)
        if row is not None:
            for k, v in amenities(text).items():
                if not row.get(k):
                    row[k] = v
                amen_counts[k] = amen_counts.get(k, 0) + 1
        if len(text) < 300:
            thin.append((url, len(text)))
        hits, decoys = scan(text)
        if decoys:
            decoy_hits.append((url, decoys))
        if not hits:
            verdicts.append({"url": url,
                             "aircon": {"verdict": "unstated", "scope": "", "evidence": ""}})
        elif url in judged:
            # Already read by a model, against this same page. Fold the stored
            # verdict straight into verdicts.json so a run with nothing new can
            # commit it without waiting for `finish`.
            verdicts.append(judged[url])
            carried += 1
        else:
            review.append({"url": url, "terms": hits, "cache_file": str(f),
                           "text": windows(text, hits), "text_chars": len(text),
                           "text_sha": text_sha(text),
                           "rejudge": url in stale})

    core.write_json(out_path, {"verdicts": verdicts})
    core.write_json(review_path, {"needs_model_judgement": review})

    if stage1_path and stage1 is not None:
        core.write_json(stage1_path, stage1)
        print("amenities extracted: %s" % (dict(sorted(amen_counts.items())) or "none"))
    print("cached listings   : %d" % len(files))
    print("silent -> unstated: %d  (decided here, no model)" % (len(verdicts) - carried))
    print("already judged    : %d  (verdict carried forward, page unchanged)" % carried)
    if stale:
        print("re-judge          : %d  (page changed under a stored verdict)" % len(stale))
    print("NEEDS JUDGEMENT   : %d  -> %s" % (len(review), review_path))
    for r in review:
        print("   ! %s  terms=%s%s" % (r["url"].rsplit("/", 1)[-1], r["terms"],
                                       "  [re-judge]" if r.get("rejudge") else ""))
    if decoy_hits:
        print("\nnear-misses (NOT A/C, listed so they are visible):")
        for url, d in decoy_hits[:10]:
            print("   ~ %s  %s" % (url.rsplit("/", 1)[-1], d))
    if thin:
        print("\nWARNING - suspiciously short cached text (%d):" % len(thin))
        for url, n in thin[:10]:
            print("   ? %s  %d chars" % (url.rsplit("/", 1)[-1], n))
    print("\nIf NEEDS JUDGEMENT is 0, verdicts.json is complete: run core.py commit.")
    print("Otherwise judge those entries for scope (in_unit vs communal_only)")
    print("with a verbatim quote, append them to verdicts.json, then commit.")
    return len(review)



def merge_verdicts(cfg) -> int:
    """Fold model verdicts into verdicts.json and return the total.

    `commit` takes ONE verdicts file and writes any listing absent from it as
    `unchecked`, so committing the deterministic file and the model's file in
    sequence would mark everything missing from the second as unchecked. They
    have to be merged first. This was a manual python -c one-liner in the
    runbook, which is precisely the kind of step that eventually gets it wrong.
    """
    import io
    import json

    base = cfg.runs_dir / "verdicts.json"
    extra = cfg.runs_dir / "verdicts_haiku.json"
    merged = {}
    for path in (base, extra):
        if not path.exists():
            continue
        # Always explicit UTF-8: the Windows default codepage raises on
        # listing text.
        data = json.load(io.open(path, encoding="utf-8"))
        for v in data.get("verdicts", []):
            url = str(v.get("url", "")).strip()
            if url:
                merged[url] = v
    core.write_json(base, {"verdicts": list(merged.values())})
    stamp_judged(cfg, extra)
    return len(merged)


def stamp_judged(cfg, path) -> int:
    """Record which page each model verdict was read off, in that file.

    Without this the verdict is just a claim with no date on it, and the next
    run cannot tell a settled listing from one whose description has since
    been rewritten. The fingerprint is what lets `load_judged` carry a verdict
    forward instead of paying for it again, and what makes a changed page
    re-flag rather than quietly collapse to `unstated` at commit.
    """
    path = pathlib.Path(path)
    if not path.exists():
        return 0
    try:
        data = core.read_json(path)
    except (ValueError, OSError):
        return 0
    stamped = 0
    for v in data.get("verdicts", []):
        url = str(v.get("url", "")).strip()
        if not url:
            continue
        cache_file = cfg.cache_dir / core.cache_name(url)
        if not cache_file.exists():
            continue
        try:
            text = core.read_json(cache_file).get("text", "")
        except (ValueError, OSError):
            continue
        v["text_sha"] = text_sha(text)
        stamped += 1
    core.write_json(path, data)
    return stamped
