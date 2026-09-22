#!/usr/bin/env python3
"""Read a floor area off a floorplan image with OCR - no model, no tokens.

Why this exists: 77% of the listings we track state no size, and MIN_SQFT is a
hard requirement. The number is usually printed on the floorplan, which the
portals publish as an ordinary image.

The parsing is the hard part, not the OCR. Four real cases, all met in one
evening of testing:

  Zoopla     "Approximate gross internal area 83.95 sqm/ 903 sq ft"  -> 903
  Rightmove  "Approx Gross Internal Area = 74.2 sq m/799 sq ft"
             "Balcony = 1.4 sq m/ 15 sqft"
             "Total = 75.6 sq m/814 sqft"                            -> 814
  OpenRent   "Gross Internal Area: 73 Sq. metres"
             "791 Sq.feet"                                           -> 791
  (metric only)                                                      -> convert

The Rightmove case is the one that matters. Taking the first square-foot number
gives 799, which is under the 800 floor; the real total is 814, which is over it.
A naive parse silently deletes a qualifying flat. So: a stated Total wins, then a
stated gross internal area, and anything genuinely ambiguous is reported as
NOT confident rather than guessed at - the caller must not hard-reject on it.

Square feet stated on the plan always beat converting the square metres: the
OpenRent plan says 73 sq m and 791 sq ft, and 73 x 10.7639 is 785.8.

The area is not the only thing a plan states. One that prints no total at
all - room dimensions only - still prints which floor it is, and lower
ground is a hard reject in its own right. See `plan_floors`.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

SQM_TO_SQFT = 10.7639

# OCR mangles these badly: "Sq.feet", "sqm/", "Sq. metres", "sqft", "sq ft".
FT = r"(?:sq\s*\.?\s*(?:ft|feet)|sqft)"
M2 = r"(?:sq\s*\.?\s*(?:m|metres|meters|mtrs)|sqm)"
NUM = r"([\d][\d,]{0,5}(?:\.\d+)?)"

FT_RE = re.compile(NUM + r"\s*" + FT, re.I)
M2_RE = re.compile(NUM + r"\s*" + M2, re.I)
TOTAL_RE = re.compile(r"total", re.I)
GROSS_RE = re.compile(r"gross\s+internal|internal\s+area|approximate\s+area", re.I)

PLAUSIBLE = (100, 20000)


def tesseract() -> str | None:
    """Tesseract is not on PATH after a winget install; look where it lands."""
    found = shutil.which("tesseract")
    if found:
        return found
    for guess in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if pathlib.Path(guess).exists():
            return guess
    return None


def ocr(image: pathlib.Path) -> str:
    exe = tesseract()
    if not exe:
        raise RuntimeError("tesseract not installed - winget install UB-Mannheim.TesseractOCR")
    out = subprocess.run([exe, str(image), "stdout"], capture_output=True, text=True,
                         timeout=120, errors="replace")
    return out.stdout or ""


def _value(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_area(text: str) -> dict:
    """-> {sqft, basis, evidence, confident}. sqft is None when nothing is readable.

    `confident` false means: a number was found, but the plan did not say plainly
    that it was the total. Record it, never hard-reject on it.
    """
    flat = " ".join(text.split())
    result = {"sqft": None, "basis": "", "evidence": "", "confident": False}

    feet = []
    for m in FT_RE.finditer(flat):
        value = _value(m.group(1))
        if value and PLAUSIBLE[0] <= value <= PLAUSIBLE[1]:
            before = flat[max(0, m.start() - 70):m.start()]
            feet.append({"value": value, "total": bool(TOTAL_RE.search(before)),
                         "gross": bool(GROSS_RE.search(before)),
                         "evidence": " ".join(flat[max(0, m.start() - 70):m.end()].split())[-70:]})

    if feet:
        for tag in ("total", "gross"):
            named = [f for f in feet if f[tag]]
            if named:
                best = max(named, key=lambda f: f["value"])
                return {"sqft": int(round(best["value"])), "basis": tag,
                        "evidence": best["evidence"], "confident": True}
        # Nothing was labelled. One number on the plan is safe enough to trust;
        # several unlabelled ones might be per-room or per-floor, and the largest
        # could still be short of the true total - so say so instead of guessing.
        best = max(feet, key=lambda f: f["value"])
        return {"sqft": int(round(best["value"])), "basis": "stated",
                "evidence": best["evidence"], "confident": len(feet) == 1}

    metres = []
    for m in M2_RE.finditer(flat):
        value = _value(m.group(1))
        if value and 10 <= value <= 2000:
            before = flat[max(0, m.start() - 70):m.start()]
            metres.append({"value": value, "total": bool(TOTAL_RE.search(before)),
                           "gross": bool(GROSS_RE.search(before)),
                           "evidence": " ".join(flat[max(0, m.start() - 70):m.end()].split())[-70:]})
    if metres:
        for tag in ("total", "gross"):
            named = [x for x in metres if x[tag]]
            if named:
                best = max(named, key=lambda x: x["value"])
                return {"sqft": int(round(best["value"] * SQM_TO_SQFT)), "basis": tag + "/converted",
                        "evidence": best["evidence"], "confident": True}
        best = max(metres, key=lambda x: x["value"])
        return {"sqft": int(round(best["value"] * SQM_TO_SQFT)), "basis": "converted",
                "evidence": best["evidence"], "confident": len(metres) == 1}

    return result


def area_from_image(image: pathlib.Path) -> dict:
    return parse_area(ocr(image))


def looks_like_a_plan(result: dict) -> bool:
    """A photo of a kitchen OCRs to noise; a floorplan states an area.

    That is the whole detector for OpenRent, which publishes floorplans as
    ordinary listing photos with nothing marking them out - reading every image
    and keeping the one that states an area is self-validating.
    """
    return result.get("sqft") is not None


# --------------------------------------------------------------------------
# which floor the plan is of
# --------------------------------------------------------------------------
# A plan that states no total area still states which FLOOR it is, and that is
# a hard requirement of its own - lower ground and basement are an outright no.
#
# Measured 2026-09-22 on OpenRent 3048266 (Burnham Court, W2). The plan was
# found, ranked first by plan_score (0.835, next best 0.382) and OCR'd
# correctly. Its only numbers are room dimensions - "22'2 (6.76) max" - so
# parse_area found no total, area_from_candidates returned None, and the whole
# read was dropped. The last line of that discarded text was "Lower Ground
# Floor". The listing went out as a High-priority suggestion.
#
# So: the area is no longer the only thing worth keeping off a plan.

# OCR renders the feet mark as ' or the degree sign, measured in the same
# image: "22°2 (6.76) max" beside "13'1 (3.99) max".
DIMENSION = re.compile(r"\d{1,2}\s*['\u2018\u2019\u00b0]\s*\d{1,2}"
                       r"|\(\s*\d{1,2}\.\d{2}\s*\)")

ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}

FLOOR_LABEL = re.compile(
    r"\b(lower\s+ground|upper\s+ground|ground|basement|mezzanine"
    r"|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|\d{1,2}(?:st|nd|rd|th))\s+floor\b", re.I)


def plan_floors(text: str) -> list:
    """Floor labels printed on the plan, normalised, in order, deduplicated.

    A plan is a drawing OF THE UNIT, so a label on it names the unit's own
    level. That is the difference from prose, where a bare "ground floor" is as
    likely to be the concierge's - see judge.FLOOR_PAT, which will not read one.
    """
    out = []
    for m in FLOOR_LABEL.finditer(text or ""):
        label = " ".join(m.group(1).split()).lower()
        if label not in out:
            out.append(label)
    return out


def is_plan_text(text: str) -> bool:
    """Does this OCR come from a floorplan rather than a photograph?

    `looks_like_a_plan` answers the same question by whether an area was read,
    which is self-validating but only works on a plan that states one. Room
    dimensions are the thing every plan has and no photograph does: two of them,
    or one beside a floor label.
    """
    dims = len(DIMENSION.findall(text or ""))
    return dims >= 2 or (dims >= 1 and bool(plan_floors(text)))


def floor_from_plan(floors: list) -> dict:
    """-> {floor_level} / {floor_number} / {} for the labels a plan states.

    Deliberately narrow. A plan naming lower ground or basement ANYWHERE is
    read as a lower-ground flat, exactly as the same words in the description
    are - a maisonette that runs down to a lower ground floor is still half
    below ground, and judge.LOWER_GROUND has never made that distinction
    either. Anything else is only read when the plan names a single level,
    because two ordinals give no honest answer to "which floor is it on".
    """
    if any(f in ("lower ground", "basement") for f in floors):
        return {"floor_level": "lower_ground"}
    if len(floors) != 1:
        return {}
    only = floors[0]
    if only in ("ground", "upper ground"):
        return {"floor_level": "ground"}
    digits = re.match(r"(\d{1,2})", only)
    n = int(digits.group(1)) if digits else ORDINALS.get(only)
    return {"floor_number": n} if n else {}


# --------------------------------------------------------------------------
# finding the plan
# --------------------------------------------------------------------------

# Rightmove and OnTheMarket label their floorplans; Zoopla names the file inside
# a floorPlan object and the image lives on its CDN. OpenRent labels nothing at
# all - see plan_score.
RM_PLAN = r"https://media\.rightmove\.co\.uk/property-floorplan/[^\"'\\s]{4,120}"
ZOOPLA_PLAN_FILE = r"floorPlan[^}]{0,200}?filename[\\\"':\s]+([0-9a-f]{8,}\.(?:jpg|jpeg|png))"
OTM_PLAN = r"floorplans[^]]{0,400}?largeUrl[\\\"':\s]+(https://[^\"'\\s]{4,160})"
ZOOPLA_CDN = "https://lid.zoocdn.com/u/2400/1800/%s"


def plan_urls(html: str, portal: str) -> list:
    """Floorplan image URLs a portal actually names. OpenRent names none."""
    if portal == "Rightmove":
        found = re.findall(RM_PLAN, html)
        # _max_296x197 is the thumbnail; OCR needs the full-size one.
        full = [u for u in found if "_max_" not in u]
        return list(dict.fromkeys(full or found))
    if portal == "Zoopla":
        return [ZOOPLA_CDN % f for f in dict.fromkeys(re.findall(ZOOPLA_PLAN_FILE, html, re.I))]
    if portal == "OnTheMarket":
        return list(dict.fromkeys(re.findall(OTM_PLAN, html, re.I)))
    return []


def plan_score(image: pathlib.Path) -> float:
    """How much this image looks like a floorplan rather than a photograph.

    OpenRent publishes floorplans as ordinary listing photos with nothing to mark
    them out, so they have to be picked out by appearance. Measured over one full
    listing set plus three known plans from three portals: plans run 83-90% near
    white and 100% near grey; the eight photographs ran 0.1-16% white. The margin
    is wide, but this returns a score and the caller reads the best few rather
    than trusting a cutoff - a threshold that drifts would silently lose plans,
    and losing one costs a size on a hard requirement.
    """
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    try:
        im = Image.open(image).convert("RGB")
    except Exception:
        return 0.0
    im.thumbnail((240, 240))
    # Via tobytes() rather than getdata(): the latter is deprecated and removed
    # in Pillow 14, and its replacement does not exist on older versions. The
    # image is already RGB, so this is three bytes per pixel.
    raw = im.tobytes()
    if not raw:
        return 0.0
    px = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 3)]
    if not px:
        return 0.0
    white = sum(1 for r, g, b in px if r > 225 and g > 225 and b > 225)
    grey = sum(1 for r, g, b in px if max(r, g, b) - min(r, g, b) < 18)
    return (white / len(px)) * 0.7 + (grey / len(px)) * 0.3


def download(url: str, dest: pathlib.Path) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        dest.write_bytes(urllib.request.urlopen(req, timeout=40).read())
        return dest.stat().st_size > 2000
    except Exception:
        return False


def area_from_candidates(urls: list, workdir: pathlib.Path, limit: int = 3) -> dict:
    """Download candidates, read the most plan-like first, keep what the plan says.

    `limit` caps how many images get OCR'd per listing; with candidates ranked by
    plan_score the real plan is first on every sample measured so far.

    An area ends the search. A plan with no area does NOT: it still states its
    floor, and that was being thrown away - see `plan_floors`. So the first
    plan-looking text is held onto and returned when no area turns up, with
    `sqft` None exactly as before. Callers that only want a size still get one
    or nothing; the floor is extra, never a substitute.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    scored = []
    for i, u in enumerate(urls[:12]):
        dest = workdir / ("cand%02d.jpg" % i)
        if download(u, dest):
            scored.append((plan_score(dest), u, dest))
    scored.sort(reverse=True, key=lambda t: t[0])
    fallback = None
    for score, u, dest in scored[:limit]:
        text = ocr(dest)
        result = parse_area(text)
        result["text"] = text
        result["floors"] = plan_floors(text)
        result["image"] = u
        result["score"] = round(score, 3)
        if result["sqft"]:
            return result
        if fallback is None and is_plan_text(text):
            fallback = result
    return fallback or {"sqft": None, "basis": "", "evidence": "", "confident": False,
                        "image": "", "score": 0.0, "text": "", "floors": []}


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(arg, "->", area_from_image(pathlib.Path(arg)))
