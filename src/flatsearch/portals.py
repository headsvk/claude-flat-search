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

import datetime as dt
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


# Measured 2026-09-22 on a live Zoopla challenge page. The words that identify
# it are split across the document: "Just a moment..." is the TITLE, and the
# only thing the body says is "Performing security verification". Matching the
# body alone - which is what the fetch layer did - therefore missed every one
# of them, and a challenge that is not recognised is indistinguishable from a
# page with nothing on it.
CHALLENGE = re.compile(r"just a moment|verify you are human|unusual traffic|"
                       r"access denied|are you a robot|captcha|"
                       r"security verification|checking your browser|"
                       r"enable javascript and cookies to continue", re.I)


def challenged(html: str, body: str) -> bool:
    """Is this a bot check rather than a page? Title AND body, because the two
    halves of the evidence are not in the same place."""
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return bool(CHALLENGE.search(body[:4000] or "")
                or (title and CHALLENGE.search(title.group(1)[:200])))


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
    """-> (rows, total). `total` is None when the page could not be read - a
    challenge, a changed payload - and 0 only when Rightmove itself stated
    zero results. `collect` relies on that difference; see STATES_ZERO."""
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], None
    try:
        sr = json.loads(m.group(1))["props"]["pageProps"]["searchResults"]
        props = sr["properties"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return [], None
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
    count = sr.get("resultCount")
    count = "" if count is None else str(count).replace(",", "")
    return out, int(count) if count.isdigit() else None


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


# Both portals render the availability date and the furnishing as a FACT LABEL
# inside their own payload rather than as a typed field - measured on live
# pages, 2026-09-21. Neither has an `availableFrom` key of any kind; what they
# have is:
#
#   Zoopla        \"tagsV2\":[{\"label\":\"Available from 8 November 2026\"},
#                             {\"label\":\"Unfurnished\"}]        (RSC flight
#                 payload in self.__next_f.push, so doubly escaped)
#   OnTheMarket   "lettingDetails":{"items":["Availability date: 1 Nov 2026",
#                                            "Furnished"]}          (__NEXT_DATA__)
#
# Reading these rather than the page text matters for the same reason
# `size_from_json` exists: the containers hold facts and nothing else, so a
# parser pointed at them cannot pick up "Underground parking available (at
# extra cost)" or the word "unfurnished" from the middle of an advert. The page
# text remains the fallback, because a payload shape can change overnight and a
# silently empty extractor is this project's characteristic bug.
#
# NOT `startDateValues` on OnTheMarket, which looks right and is not: it is the
# option list for the enquiry form's "when do you want to move" dropdown, the
# same five values on every listing.
ZOOPLA_TAGS_RE = r'\\?"tagsV2\\?"\s*:\s*\[(.*?)\]'
ZOOPLA_TAG_LABEL_RE = r'\\?"label\\?"\s*:\s*\\?"(.*?)\\?"'
OTM_LETTING_RE = r'\\?"lettingDetails\\?"\s*:\s*\{\s*\\?"items\\?"\s*:\s*\[(.*?)\]'
OTM_ITEM_RE = r'\\?"(.*?)\\?"'


def fact_labels(html: str, block_re: str, item_re: str) -> list:
    """The labels inside one of those containers, in order."""
    m = re.search(block_re, html, re.I | re.S)
    if not m:
        return []
    out = []
    for raw in re.findall(item_re, m.group(1), re.S):
        label = htmllib.unescape(raw.replace('\\"', '"').replace('\\\\', '\\')).strip()
        if label:
            out.append(label)
    return out


FURNISHING = {"furnished": "Furnished", "unfurnished": "Unfurnished",
              "part furnished": "Part furnished", "partly furnished": "Part furnished"}


def furnishing_from(labels) -> "str | None":
    """Furnishing as the PORTAL states it, not as the advert mentions it.

    The text fallback matches the first occurrence anywhere on the page, and an
    advert reading "offered part unfurnished" is one of several where the tag
    and the prose disagree. The tag is the portal's own summary field.
    """
    for label in labels:
        got = FURNISHING.get(label.strip().lower())
        if got:
            return got
    return None


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
    tags = fact_labels(html, ZOOPLA_TAGS_RE, ZOOPLA_TAG_LABEL_RE)
    furnished = furnishing_from(tags)
    if not furnished:
        m = re.search(r"(Furnished|Unfurnished|Part furnished)", text, re.I)
        furnished = m.group(1).capitalize() if m else None
    if furnished:
        info["furnished"] = furnished
    size = size_from_json(html, ZOOPLA_SIZE_RE, ZOOPLA_SIZE_RE2)
    if size:
        info["size_sqft"] = size
        parts.append("Size: %d sq ft" % size)
    # `tagsV2` first: it holds facts only, so it cannot pick up "parking
    # available (at extra cost)". The page text is the fallback for the day the
    # payload changes shape - a silently empty extractor is worse than a loose
    # one.
    avail = availability(" | ".join(tags)) or availability(text)
    if avail:
        info["available_from"] = avail
        parts.append("Availability: " + avail)
    return parts, info


# --------------------------------------------------------------------------
# OnTheMarket  -  redux state. min-bathrooms is ignored, so filter client-side.
# --------------------------------------------------------------------------

def otm_search(html: str, page):
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], 0
    try:
        res = json.loads(m.group(1))["props"]["initialReduxState"]["results"]
        lst = res["list"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return [], 0
    # `totalResults` is the server's count for THIS query; `list` is one page of
    # 30 (`currentQuery.frame-size`). Returning the page length as the total
    # made `collect` stop after page one every single time - len(seen) >= total
    # is 30 >= 30 - so this portal had been reading 30 of 15519 since it was
    # written, and looked healthy doing it: 56 listings a morning is low, not
    # zero, and only zero was being watched for.
    total = res.get("totalResults")
    total = total if isinstance(total, int) else 0
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
    return out, total or len(out)


def otm_detail(html: str, text: str = ""):
    parts, info = [], {}
    body = slice_between(text, ["Features and description"],
                         ["Marketed by", "Visit agent website", "Area insights",
                          "About this agent", "Mortgage", "Similar properties"],
                         minlen=60)
    if body:
        parts.append("Description: " + body[:6000])
    # `lettingDetails.items` is the portal's own fact list - ["Availability
    # date: 1 Nov 2026", "Furnished"] - and holds nothing else. The text slice
    # is the fallback for the day the payload changes shape.
    items = fact_labels(html, OTM_LETTING_RE, OTM_ITEM_RE)
    lett = " | ".join(items) or slice_between(
        text, ["Letting details"], ["Features and description"], minlen=4)
    if lett:
        parts.append("Letting details: " + lett)
    furnished = furnishing_from(items)
    if not furnished:
        m = re.search(r"\b(Furnished|Unfurnished|Part furnished)\b", lett or "", re.I)
        furnished = m.group(1).capitalize() if m else None
    if furnished:
        info["furnished"] = furnished
    # OnTheMarket publishes no size on its search cards - measured 0% coverage -
    # but the detail page carries it in JSON. Without this the 800 sq ft floor
    # never applied to this portal at all.
    size = size_from_json(html, OTM_SIZE_RE)
    if size:
        info["size_sqft"] = size
        parts.append("Size: %d sq ft" % size)
    # "Letting details: Availability date: 23 Sep 2026" is the labelled one and
    # wins; the description's prose is the fallback for the listings without it.
    avail = availability(lett or "") or availability(text)
    if avail:
        info["available_from"] = avail
        parts.append("Availability: " + avail)
    return parts, info


# --------------------------------------------------------------------------
# OpenRent  -  DOM cards. bathrooms_min filters server-side but the card never
# prints a bathroom count, so mark it verified-by-search instead of inventing
# one. The DETAIL page does print it - openrent_detail reads it and wins.
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
        avail_card = availability(t)
        title = re.search(r"(\d+\s+Bed\s+[^|]+)", t)
        out.append(blank(url, (title.group(1) if title else t)[:120], "OpenRent",
                         area=(addr.group(1).strip() if addr else None),
                         postcode=(addr.group(1).strip() if addr else None),
                         price_pcm=pcm_from_text(t),
                         bed_count=int(beds.group(1)) if beds else None,
                         furnished=(furn.group(1) if furn else None),
                         available_from=avail_card,
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
    # The SEARCH CARD never prints a bathroom count - that is what
    # `bathrooms_verified_by_search` stands in for - but the DETAIL page states
    # it outright, in a three-line block under the title:
    #
    #     2 Bed Flat, Hopgood Tower, SE3
    #     2 bedrooms
    #     2 bathrooms
    #     4 tenants max.
    #
    # Nothing read it, so all 399 tracked OpenRent listings carried bathrooms
    # None and rendered as "2/?" while the page said 2. Anchored to a whole
    # line: the description's own prose ("2 bathrooms (1 en-suite), modern
    # kitchen") is a claim about the flat, not the portal's field, and the
    # anchor is what keeps the two apart.
    baths = re.search(r"^[ \t]*(\d+)[ \t]+bathrooms?[ \t]*$", text, re.I | re.M)
    if baths:
        info["bathrooms"] = int(baths.group(1))
        # Only alongside a description: `parts` empty means the page came back
        # blank and has to be retried, and an evidence line on its own would
        # look like a successful read of a page that rendered nothing.
        if parts:
            parts.append("Bathrooms: %d" % info["bathrooms"])
    # This used to be its own regex over a 20-character window, which captured
    # "to move in", "Today" and "to move in from 31 A" - 106 records carrying a
    # value that looked like data and that parse_date read as nothing at all.
    avail = availability(text)
    if avail:
        info["available_from"] = avail
    if re.search(r"offered unfurnished|unfurnished", text, re.I):
        info["furnished"] = "Unfurnished"
    elif re.search(r"\bfurnished\b", text, re.I):
        info["furnished"] = "Furnished"
    return parts, info


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------
# docs/portals.md said Zoopla and OnTheMarket publish no availability date, so
# neither extractor looked for one and every listing on both carried
# "availability unconfirmed": 0 of 93 Zoopla and 0 of 74 OnTheMarket records
# held a date. Measured against the cache on 2026-09-21, the pages say
# otherwise - 32 of 96 cached Zoopla pages and 64 of 79 OnTheMarket ones state
# availability somewhere in their text. What is true is narrower: neither
# portal puts it in a FIELD. OnTheMarket labels it inside "Letting details",
# Zoopla writes it into the agent's prose, and a parser has to read English.
#
# Shapes measured across those pages:
#
#   Availability date: 23 Sep 2026          OnTheMarket, labelled
#   Letting details: Available now          OnTheMarket, labelled
#   Available to move in from 15 October 2026   Zoopla, OpenRent, OnTheMarket
#   Available from 28 October, 2026         a comma before the year
#   available from 27th October             an ordinal, and NO year
#   Available to move in from November 2026  a month, and no day
#   Available now / Available immediately    every portal
#   Let available date: 26/09/2026 | Now | Ask agent    Rightmove
#
# Only a value that PARSES is returned, so the many near-misses in listing
# prose - "Underground parking available (at extra cost)", "phone bookings
# available 9am-9pm" - fall through as None rather than becoming a date.
AVAIL_RE = re.compile(
    r"(?:Availability date|Let available date"
    r"|Available(?:\s+to\s+move\s+in)?(?:\s+from)?)\s*:?\s*([^.|\n]{2,40})",
    re.I)

# The label's value runs on into whatever follows it - "23 Sep 2026 Furnished",
# "26/09/2026 Deposit" - so the date is found INSIDE the captured text rather
# than by parsing the whole of it. Longest shapes first: "8 November 2026" must
# not be read as the bare "November 2026" that follows it.
DATE_IN_RE = re.compile(
    r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+20\d{2}"
    r"|\d{1,2}/\d{1,2}/20\d{2}"
    r"|20\d{2}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}"
    r"|[A-Za-z]{3,9}\s+20\d{2})", re.I)
IMMEDIATE_RE = re.compile(r"^(?:now|immediately|immediate|today)\b", re.I)
ORDINAL_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)", re.I)


def availability(text: str, today=None) -> str | None:
    """-> ISO date this listing states it is available from, or None.

    "now" and "immediately" are a stated date, not a missing one: the page was
    read and it answered. That is the distinction the whole pipeline turns on
    everywhere else, and availability is no different.
    """
    today = today or dt.date.today()
    for m in AVAIL_RE.finditer(text or ""):
        got = _one_date(m.group(1).strip(), today)
        if got:
            return got.isoformat()
    return None


def availability_value(value, today=None) -> str | None:
    """Normalise ONE already-isolated label value -> ISO date, or None.

    For a portal that hands over the value on its own: Rightmove's "Let
    available date", which is "26/09/2026", "Now" or "Ask agent". "Ask agent"
    is not a date and must read as None, not as a string that looks like data
    and parses as nothing - 106 OpenRent records held exactly that.
    """
    got = _one_date(str(value or "").strip(), today or dt.date.today())
    return got.isoformat() if got else None


def _one_date(value: str, today):
    value = value.strip().strip("-").strip()
    if not value:
        return None
    if IMMEDIATE_RE.match(value):
        return today
    m = DATE_IN_RE.search(value)
    if not m:
        return None
    v = re.sub(r"\s+", " ", ORDINAL_RE.sub(r"\1", m.group(1)).replace(",", " ")).strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    for fmt in ("%B %Y", "%b %Y"):
        # "available from November 2026" - the month is the claim, so take the
        # first of it rather than throwing the only date on the page away.
        try:
            return dt.datetime.strptime(v, fmt).date().replace(day=1)
        except ValueError:
            pass
    # A day and a month with no year at all ("27th October"). Agents write this
    # about the coming weeks, so read it as the next such date, not as one
    # eleven months past.
    # The year is supplied rather than left out: strptime on a bare day and
    # month is deprecated in 3.14 and changes behaviour in 3.15, and "29
    # February" then raises on the wrong year instead of rolling to the right
    # one. This tries this year before next, and takes the first that is not
    # already well past.
    for fmt in ("%d %B %Y", "%d %b %Y"):
        for year in (today.year, today.year + 1):
            try:
                cand = dt.datetime.strptime("%s %d" % (v, year), fmt).date()
            except ValueError:
                continue
            if (today - cand).days <= 30:
                return cand
    return None


# --------------------------------------------------------------------------
# server-side windows
# --------------------------------------------------------------------------
# Two things a portal can narrow for us before it sends anything: how recently
# a listing was added, and when it becomes available. Both are worth having -
# the first because paging a backlog we already track is pure waste, the second
# because a portal that will FILTER on availability tells us the date even when
# it refuses to PRINT it, exactly like OpenRent's bathrooms on the search card.
#
# Measured 2026-09-20 on the live 2-bed / <=4500 London search, off each
# portal's own total - Zoopla's [data-testid="total-results"], Rightmove's
# `resultCount`, OnTheMarket's `totalResults`. Read off the counter, never the UI:
#
#   ZOOPLA          8469 unscoped      RIGHTMOVE       5374 unscoped
#   added=24_hours    72               maxDaysSinceAdded=1     73
#   added=3_days     453               maxDaysSinceAdded=3    479
#   added=7_days    1644               maxDaysSinceAdded=7   1536
#   added=14_days   2862               maxDaysSinceAdded=14  2565
#   added=30_days   4360               maxDaysSinceAdded=30     0  <-- NOT a window
#   available_from=1months   5208      moveInByDate=2026-10-01   2427
#   available_from=3months   6111      moveInByDate=2026-11-05   3413
#   available_from=6months   6124      moveInByDate=2027-06-01   3744
#   available_from=12months  6139
#
#   ONTHEMARKET    15519 unscoped
#   recently-added=24-hours   141
#   recently-added=3-days    1882
#   recently-added=7-days    4116
#   recently-added=14-days      0     <-- NOT a window; its ceiling is 7 days
#
# They compose within a portal: added=3_days & available_from=1months returned
# 242, and maxDaysSinceAdded=3 & moveInByDate=2026-11-05 returned 297 - each
# below either of its parts alone.
#
# **Most of these fail CLOSED on a value they do not recognise**, which is why
# values are only ever taken from these maps and never built from a config
# string. `maxDaysSinceAdded=30`, `maxDaysSinceAdded=nonsense`,
# `moveInByDate=someday`, `recently-added=14-days` and `recently-added=nonsense`
# all returned ZERO results - so does `available_from=immediately`, which is a
# value the Zoopla filter UI itself offers. An empty search is indistinguishable
# from a block, so an unsupported value does not degrade, it fabricates a quiet
# morning. Only Zoopla's `added=` is forgiving: `added=nonsense_value` returned
# the unfiltered 8469.
#
# OnTheMarket has no availability filter: `available-from=` and `move-in-by=`
# both returned the unfiltered 15519 - accepted and ignored, the same way it
# treats `min-bathrooms`. Those are guessed spellings and the portal exposes no
# such control, so this is "none found", not "proven absent". OpenRent is
# UNTESTED for both, which is not the same as "no". A portal absent from these
# tables is simply never scoped.

RECENT_WINDOWS = {
    "zoopla.co.uk": ("added", {1: "24_hours", 3: "3_days", 7: "7_days",
                               14: "14_days", 30: "30_days"}),
    "rightmove.co.uk": ("maxDaysSinceAdded", {1: "1", 3: "3", 7: "7", 14: "14"}),
    "onthemarket.com": ("recently-added", {1: "24-hours", 3: "3-days", 7: "7-days"}),
}

def window_for(url: str, table: dict):
    """-> (param, {days: value}) for this URL's portal, or None."""
    for host, spec in table.items():
        if host in url:
            return spec
    return None


def snap_up(days: int, offered) -> int | None:
    """Smallest offered window that still covers `days`.

    Recency snaps UP: a window NARROWER than the gap since the last run stops
    fetching listings that arrived inside it, and they are gone silently -
    nothing downstream can tell a listing that was filtered out at the portal
    from one that was never posted. Wider merely costs a page.
    """
    for d in sorted(offered):
        if days <= d:
            return d
    return None


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


# Portals whose search extractor returns total=None for a page it could not
# read, so that a total of 0 can only mean the portal said "0 results" on a
# well-formed page. The others return 0 for both, and for them an empty first
# page must stay a failure. Rightmove's is the only one verified: its
# `resultCount` sits beside the results in the same payload, and a challenge
# page carries no payload at all.
STATES_ZERO = {"rightmove.co.uk"}


def portal_for(url: str):
    for host, name, s, d, page_param, step, sorted_newest in PORTALS:
        if host in url:
            return {"host": host, "name": name, "search": s, "detail": d,
                    "page_param": page_param, "step": step,
                    "sorted_newest": sorted_newest,
                    "states_zero": host in STATES_ZERO}
    return None
