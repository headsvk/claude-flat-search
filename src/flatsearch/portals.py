#!/usr/bin/env python3
"""Per-portal search + detail extraction.

Every portal here was verified by measurement, not by reading its filter UI.
Three times in a row a filter that the visible UI does not expose turned out to
work as a URL parameter, so parameters are tested, never assumed:

    Rightmove   minBathrooms=2   99 -> 47      (UI does not show it)
    Zoopla      baths_min=2      below2 10->0  (needs cookie consent first)
    OpenRent    bathrooms_min=2  2073->819->91 (card never displays bathrooms)

OnTheMarket is the exception: min-bathrooms is accepted and silently ignored
(110 -> 110), so its bathroom filtering must happen client-side in core.py.
"""
from __future__ import annotations

import html as htmllib
import json
import re

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def untag(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"</(p|div|li)>", " ", s)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def money(s: str | None):
    """First GBP amount in a string, as an int."""
    if not s:
        return None
    m = re.search(r"£\s*([\d,]+)", s)
    return int(m.group(1).replace(",", "")) if m else None


def pcm_from_text(text: str):
    """Prefer an explicit pcm figure; fall back to converting a weekly one.

    Card innerText puts separators between the amount and its unit - OpenRent
    renders "£3,250 | per month", not "£3,250 per month". Without tolerating
    that, OpenRent price coverage measured 1%: 817 listings with no price, all
    of which then pass a budget filter vacuously.
    """
    sep = r"(?:\s*[|·-]\s*|\s+)"
    m = re.search(r"£\s*([\d,]+)" + sep + r"(?:pcm|per calendar month|per month|pm\b)",
                  text, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"£\s*([\d,.]+)" + sep + r"(?:pw\b|per week)", text, re.I)
    if m:
        return round(float(m.group(1).replace(",", "")) * 52 / 12)
    # Last resort: a bare amount next to an explicit monthly label anywhere.
    if re.search(r"per month|pcm", text, re.I):
        m = re.search(r"£\s*([\d,]{3,7})", text)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


SQFT = re.compile(r"([\d,]{3,6})\s*sq\.?\s*(?:ft|feet)", re.I)


def sqft(text: str | None):
    """Floor area in sq ft. Ignores implausible values - a '86 sq ft' in a card
    is a balcony or a terrace, not the flat."""
    if not text:
        return None
    best = None
    for m in SQFT.finditer(text):
        v = int(m.group(1).replace(",", ""))
        if 250 <= v <= 20000 and (best is None or v > best):
            best = v
    return best


def blank(url, title, platform, **kw):
    row = {
        "url": url, "title": (title or "")[:120], "platform": platform, "type": "unit",
        "area": None, "postcode": None, "price_pcm": None, "bills_included": "No",
        "available_from": None, "furnished": None, "bed_count": None,
        "bathrooms": None, "size_sqft": None, "flatmates": None,
        "contact": None, "notes": None,
    }
    row.update(kw)
    return row


CHALLENGE = re.compile(r"just a moment|verify you are human|unusual traffic|"
                       r"access denied|are you a robot|captcha", re.I)


# Rightmove appends a glossary after the description - council tax, parking,
# accessibility explainers. Measured at 41% of the cached text, and its wording
# ("to pay for local services like schools, libraries...") makes 75% of listings
# look as if they mention schools. Cut everything from the first marker.
BOILERPLATE = re.compile(
    r"(Read full description"
    r"|COUNCIL TAX\s+A payment made to your local authority"
    r"|PARKING\s+Details of how and where vehicles"
    r"|ACCESSIBILITY\s+(?:Details|Ask agent)"
    r"|Brochures\s+Web Details"
    r"|Disclaimer\s*-\s*Property reference"
    r"|These notes are private)", re.I)


def strip_boilerplate(text: str) -> str:
    m = BOILERPLATE.search(text)
    return text[:m.start()].strip() if m else text


def _find_heading(low: str, anchor: str, start: int) -> int:
    """Find an end anchor, preferring a match that begins a line.

    An end anchor names a *heading*, and in innerText a heading stands on its own
    line. Matching it as a bare substring lets it fire inside the prose it is
    meant to terminate: OpenRent's own boilerplate says "a flat in a great
    location", which matched the `Location` anchor and truncated the description
    to 82 chars - under `minlen`, so the listing was dropped with an EMPTY and no
    error. That silently lost 22% of OpenRent detail fetches.

    An anchor that never starts a line simply does not match. The cost of that is
    a slice that runs to the next heading that does match, or to the end of the
    text - longer than intended, but still the listing's own prose. The cost of
    the substring match was losing the listing entirely, which is worse and,
    crucially, invisible.
    """
    a = anchor.lower()
    k = low.find(a, start)
    while k >= 0:
        if k == 0 or low[k - 1] == '\n':
            return k
        k = low.find(a, k + 1)
    return -1


# Floor-area keys in each portal's embedded JSON, tolerant of the escaped
# form used when the JSON sits inside a JS string literal.
OTM_SIZE_RE = '\\\\?"minimumAreaSqFt\\\\?"\\s*:\\s*([\\d.,]+)'
# Zoopla states it directly as a string, and as "" when unknown - which
# simply fails to match, so the nested floorArea object stays as a fallback.
ZOOPLA_SIZE_RE = '\\\\?"sizeSqFeet\\\\?"\\s*:\\s*\\\\?"?([\\d.,]+)'
ZOOPLA_SIZE_RE2 = '\\\\?"floorArea\\\\?"\\s*:\\s*\\{[^}]*?\\\\?"value\\\\?"\\s*:\\s*([\\d.,]+)'

def size_from_json(html: str, *patterns) -> "int | None":
    """Read a floor area out of the page's own embedded JSON.

    Deliberately not a loose scan of the visible text for "N sq ft": that also
    matches a garden, a terrace, or a price-per-square-foot line, and a wrong
    size does not merely mislabel a listing - MIN_SQFT is a hard filter, so it
    would delete one. A named JSON key is unambiguous. Class names are not an
    option either: these are generated (`text-link ml-1`) and churn.

    Handles backslash-escaped JSON too, because some of it is embedded inside a
    JS string literal.
    """
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        try:
            value = int(float(m.group(1).replace(",", "")))
        except (TypeError, ValueError):
            continue
        # A real flat is not 12 sq ft and not 100,000.
        if 100 <= value <= 20000:
            return value
    return None


def slice_between(text: str, starts, ends, minlen: int = 80):
    """Take the visible text between the first start anchor and the next end anchor.

    These portals use generated class names that churn, and CSS selectors written
    from a search page turned out to match nothing on a detail page - 18 of 18
    non-Rightmove fetches came back empty. Visible headings are far more durable.
    """
    low = text.lower()
    for s in starts:
        i = low.find(s.lower())
        if i < 0:
            continue
        i += len(s)
        j = len(text)
        for e in ends:
            k = _find_heading(low, e, i)
            if k >= 0:
                j = min(j, k)
        chunk = re.sub(r"[ \t]*\n[ \t]*", " ", text[i:j]).strip(" |\n\t:")
        chunk = re.sub(r"\s{2,}", " ", chunk)
        if len(chunk) >= minlen:
            return chunk
    return ""


# --------------------------------------------------------------------------
# Rightmove  -  __NEXT_DATA__ in the search page
# --------------------------------------------------------------------------

def rightmove_search(html: str, page):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], 0
    try:
        sr = json.loads(m.group(1))["props"]["pageProps"]["searchResults"]
        props = sr["properties"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return [], 0
    out = []
    for p in props:
        addr = (p.get("displayAddress") or "").replace("\n", " ").strip()
        url = (p.get("propertyUrl") or "").split("#")[0]
        if url.startswith("/"):
            url = "https://www.rightmove.co.uk" + url
        price = p.get("price") or {}
        amt = price.get("amount")
        pcm = (round(amt * 52 / 12) if price.get("frequency") == "weekly" else amt) if amt else None
        out.append(blank(url, p.get("propertyTypeFullDescription") or addr, "Rightmove",
                         area=addr, postcode=addr, price_pcm=pcm,
                         available_from=(p.get("letAvailableDate") or "")[:10] or None,
                         bed_count=p.get("bedrooms"), bathrooms=p.get("bathrooms"),
                         size_sqft=sqft(p.get("displaySize")),
                         contact=(p.get("customer") or {}).get("branchDisplayName")))
    return out, int(str(sr.get("resultCount", "0")).replace(",", "") or 0)


def rightmove_detail(html: str, text: str = ""):
    parts, info = [], {}
    kf = re.search(r'data-testid="keyFeatures".*?</ul>', html, re.S)
    if kf:
        parts.append("Key features: " + " / ".join(
            untag(i) for i in re.findall(r"<li[^>]*>(.*?)</li>", kf.group(0), re.S)))
    d = re.search(r"<h2[^>]*>Description</h2>(.*?)(?:<h2|</section)", html, re.S)
    if d:
        parts.append("Description: " + strip_boilerplate(untag(d.group(1))))
    return parts, info


# --------------------------------------------------------------------------
# Zoopla  -  DOM cards. Its JSON-LD is breadcrumbs only these days.
# --------------------------------------------------------------------------

ZOOPLA_JS = """() => [...document.querySelectorAll('[id^="listing_"]')].map(e => {
    const a = e.querySelector('a[href*="/details/"]');
    return {id: e.id, href: a ? a.href.split('?')[0] : null,
            txt: e.innerText.replace(/\\n+/g, ' | ')};
})"""


async def zoopla_search(html: str, page):
    cards = await page.evaluate(ZOOPLA_JS)
    # The visible "N results found" text goes stale and reads 0 while 28 cards
    # sit on the page - it misled several measurements. This testid is the real
    # total and is what bounds pagination.
    total = 0
    try:
        el = await page.query_selector('[data-testid="total-results"]')
        if el:
            m = re.search(r"([\d,]+)", (await el.inner_text()) or "")
            if m:
                total = int(m.group(1).replace(",", ""))
    except Exception:
        pass
    out = []
    for c in cards:
        if not c.get("href"):
            continue
        t = c["txt"]
        beds = re.search(r"(\d+)\s+bed", t)
        baths = re.search(r"(\d+)\s+bath", t)
        addr = re.search(r"\|\s*([^|]*?[A-Z]{1,2}\d{1,2}[A-Z]?)\s*\|", t)
        title = re.sub(r"^(Property of the week|Highlight|Featured)\s*\|\s*", "", t)
        out.append(blank(c["href"], title[:120], "Zoopla",
                         area=(addr.group(1).strip() if addr else None),
                         postcode=(addr.group(1).strip() if addr else None),
                         price_pcm=pcm_from_text(t),
                         bed_count=int(beds.group(1)) if beds else None,
                         bathrooms=int(baths.group(1)) if baths else None,
                         size_sqft=sqft(t)))
    return out, total or len(out)


def zoopla_detail(html: str, text: str = ""):
    parts, info = [], {}
    feats = slice_between(text, ["About this property"],
                          ["Local area information", "full description", "Could you afford",
                           "Property descriptions and related", "Points of interest"])
    if feats:
        parts.append("Key features: " + feats)
    desc = slice_between(text, ["Property description", "Description"],
                         ["Local area information", "Points of interest",
                          "Property descriptions and related", "Could you afford"], minlen=120)
    if desc:
        parts.append("Description: " + desc[:6000])
    m = re.search(r"(Furnished|Unfurnished|Part furnished)", text, re.I)
    if m:
        info["furnished"] = m.group(1)
    size = size_from_json(html, ZOOPLA_SIZE_RE, ZOOPLA_SIZE_RE2)
    if size:
        info["size_sqft"] = size
        parts.append("Size: %d sq ft" % size)
    return parts, info


# --------------------------------------------------------------------------
# OnTheMarket  -  redux state. min-bathrooms is ignored, so filter client-side.
# --------------------------------------------------------------------------

def otm_search(html: str, page):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], 0
    try:
        lst = json.loads(m.group(1))["props"]["initialReduxState"]["results"]["list"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return [], 0
    out = []
    for p in lst:
        url = p.get("details-url") or ""
        if url.startswith("/"):
            url = "https://www.onthemarket.com" + url
        addr = p.get("address")
        out.append(blank(url, p.get("property-title") or addr, "OnTheMarket",
                         area=addr, postcode=addr,
                         price_pcm=pcm_from_text(p.get("price") or ""),
                         bed_count=p.get("bedrooms"), bathrooms=p.get("bathrooms"),
                         contact=((p.get("agent") or {}).get("name")
                                  if isinstance(p.get("agent"), dict) else None),
                         notes=" / ".join(p.get("features") or [])[:200] or None))
    return out, len(out)


def otm_detail(html: str, text: str = ""):
    parts, info = [], {}
    body = slice_between(text, ["Features and description"],
                         ["Marketed by", "Visit agent website", "Area insights",
                          "About this agent", "Mortgage", "Similar properties"],
                         minlen=60)
    if body:
        parts.append("Description: " + body[:6000])
    lett = slice_between(text, ["Letting details"], ["Features and description"], minlen=4)
    if lett:
        parts.append("Letting details: " + lett)
        m = re.search(r"(Furnished|Unfurnished|Part furnished)", lett, re.I)
        if m:
            info["furnished"] = m.group(1)
    # OnTheMarket publishes no size on its search cards - measured 0% coverage -
    # but the detail page carries it in JSON. Without this the 800 sq ft floor
    # never applied to this portal at all.
    size = size_from_json(html, OTM_SIZE_RE)
    if size:
        info["size_sqft"] = size
        parts.append("Size: %d sq ft" % size)
    return parts, info


# --------------------------------------------------------------------------
# OpenRent  -  DOM cards. bathrooms_min filters server-side but the card never
# prints a bathroom count, so mark it verified-by-search instead of inventing one.
# --------------------------------------------------------------------------

OPENRENT_JS = """() => [...document.querySelectorAll('a.pli.search-property-card')].map(e => ({
    href: e.getAttribute('href'), txt: e.innerText.replace(/\\n+/g, ' | ')}))"""


async def openrent_search(html: str, page):
    # OpenRent ignores &page= and lazy-loads on scroll: measured 20 -> 819.
    prev = -1
    for _ in range(40):
        cards = await page.evaluate(OPENRENT_JS)
        if len(cards) == prev:
            break
        prev = len(cards)
        await page.mouse.wheel(0, 25000)
        await page.wait_for_timeout(1500)
    cards = await page.evaluate(OPENRENT_JS)
    out = []
    for c in cards:
        href = c.get("href") or ""
        if not href:
            continue
        url = "https://www.openrent.co.uk" + href if href.startswith("/") else href
        t = c["txt"]
        beds = re.search(r"(\d+)\s+Bed", t)
        addr = re.search(r"\d+\s+Bed\s+[A-Za-z ]+,\s*([^|]+)", t)
        furn = re.search(r"\b(Unfurnished|Furnished|Part furnished)\b", t, re.I)
        avail = re.search(r"Available\s+([A-Za-z0-9 ]{3,20})", t)
        title = re.search(r"(\d+\s+Bed\s+[^|]+)", t)
        out.append(blank(url, (title.group(1) if title else t)[:120], "OpenRent",
                         area=(addr.group(1).strip() if addr else None),
                         postcode=(addr.group(1).strip() if addr else None),
                         price_pcm=pcm_from_text(t),
                         bed_count=int(beds.group(1)) if beds else None,
                         furnished=(furn.group(1) if furn else None),
                         available_from=(avail.group(1).strip() if avail else None),
                         size_sqft=sqft(t)))
        out[-1]["bathrooms_verified_by_search"] = True
    return out, len(out)


def openrent_detail(html: str, text: str = ""):
    """OpenRent prints the description with NO heading: it runs from just after
    the 'N tenants max.' line to 'Price & Bills'."""
    parts, info = [], {}
    body = slice_between(text,
                         ["tenants max.", "bathrooms", "bathroom"],
                         # No "Location" here: OpenRent has no such heading, and
                         # a listing whose description opens with "LOCATION -"
                         # then cuts itself to nothing.
                         ["Price & Bills", "Tenant Preference", "Availability",
                          "Features"], minlen=100)
    if body:
        parts.append("Description: " + body[:6000])
    m = re.search(r"Rent PCM\s*£?([\d,]+)", text, re.I)
    if m:
        info["price_pcm"] = int(m.group(1).replace(",", ""))
    m = re.search(r"Available From\s*([A-Za-z0-9 /]{3,20})", text, re.I)
    if m:
        info["available_from"] = m.group(1).strip()
    if re.search(r"offered unfurnished|unfurnished", text, re.I):
        info["furnished"] = "Unfurnished"
    elif re.search(r"furnished", text, re.I):
        info["furnished"] = "Furnished"
    return parts, info


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

# sorted_newest: only a newest-first search may stop early on already-seen
# listings. Verified from each portal's own sort control, not assumed.
PORTALS = [
    ("rightmove.co.uk",  "Rightmove",   rightmove_search, rightmove_detail, "&index=%d", 24, True),
    ("zoopla.co.uk",     "Zoopla",      zoopla_search,    zoopla_detail,    "&pn=%d",     1, True),
    ("onthemarket.com",  "OnTheMarket", otm_search,       otm_detail,       "&page=%d",   1, True),
    ("openrent.co.uk",   "OpenRent",    openrent_search,  openrent_detail,  "&page=%d",   1, False),
]


def portal_for(url: str):
    for host, name, s, d, page_param, step, sorted_newest in PORTALS:
        if host in url:
            return {"host": host, "name": name, "search": s, "detail": d,
                    "page_param": page_param, "step": step,
                    "sorted_newest": sorted_newest}
    return None
