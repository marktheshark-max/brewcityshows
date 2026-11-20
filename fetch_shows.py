import html
import json
import re
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

MKE_TZ = ZoneInfo("America/Chicago")
HEADERS = {
    "User-Agent": "BrewCityShowsBot/1.0 (+https://brewcityshows.com; local Milwaukee music calendar)"
}
BAND_IMAGE_CACHE = {}

NEIGHBORHOOD_MAP = {
    "fiserv forum": "Downtown",
    "turner hall": "Downtown",
    "pabst theater": "Downtown",
    "riverside theater": "Downtown",
    "miller high life": "Downtown",
    "marcus performing": "Downtown",
    "fitzgerald": "East Side",
    "vivarium": "East Side",
    "shank hall": "East Side",
    "colectivo": "East Side",
    "linneman": "Riverwest",
    "mad planet": "Riverwest",
    "falcon": "Riverwest",
    "cactus club": "Bay View",
    "garibaldi": "Bay View",
    "sugar maple": "Bay View",
    "anodyne": "Walker's Point",
    "x-ray arcade": "Cudahy",
}


def get_neighborhood(venue_name: str) -> str:
    lower = venue_name.lower()
    for key, hood in NEIGHBORHOOD_MAP.items():
        if key in lower:
            return hood
    return "Milwaukee"


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_page(url: str) -> str:
    time.sleep(1.5)  # Polite delay so we never overload venue servers
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_image_url(img_field) -> str:
    if not img_field:
        return ""
    if isinstance(img_field, str):
        return img_field.strip()
    if isinstance(img_field, list) and img_field:
        return extract_image_url(img_field[0])
    if isinstance(img_field, dict):
        return str(img_field.get("url") or img_field.get("contentUrl") or "").strip()
    return ""


def extract_band_website(ev: dict) -> str:
    performers = ev.get("performer") or ev.get("performers") or []
    if isinstance(performers, dict):
        performers = [performers]
    if not isinstance(performers, list):
        return ""

    skip_domains = (
        "ticketmaster.com",
        "axs.com",
        "etix.com",
        "eventbrite.com",
        "seetickets.us",
        "facebook.com",
        "instagram.com",
    )

    for perf in performers:
        if not isinstance(perf, dict):
            continue
        for key in ("url", "sameAs"):
            candidate = perf.get(key)
            if isinstance(candidate, list) and candidate:
                candidate = candidate[0]
            if isinstance(candidate, str) and candidate.startswith("http"):
                lower = candidate.lower()
                if not any(domain in lower for domain in skip_domains):
                    return candidate.strip()
    return ""


def fetch_band_website_image(band_url: str) -> str:
    if not band_url:
        return ""
    if band_url in BAND_IMAGE_CACHE:
        return BAND_IMAGE_CACHE[band_url]

    try:
        time.sleep(1.0)
        resp = requests.get(band_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for prop in ("og:image", "twitter:image"):
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                img_url = urllib.parse.urljoin(band_url, tag["content"].strip())
                BAND_IMAGE_CACHE[band_url] = img_url
                return img_url
    except Exception:
        pass

    BAND_IMAGE_CACHE[band_url] = ""
    return ""


# ---------------------------------------------------------
# 1. X-RAY ARCADE (Squarespace JSON Feed + Offsite Shows)
# ---------------------------------------------------------
def scrape_xray_arcade() -> list:
    shows = []
    raw = fetch_page("https://xrayarcade.com/calendar?format=json")
    data = json.loads(raw)
    items = data.get("upcoming", [])

    for item in items:
        title = clean_text(item.get("title", ""))
        start_ms = item.get("startDate")
        if not title or not start_ms:
            continue

        dt = datetime.fromtimestamp(start_ms / 1000.0, tz=MKE_TZ)
        venue = "X-Ray Arcade"

        # Detect offsite shows formatted like "@ ANODYNE: Band Name"
        offsite_match = re.match(r"^@\s*([^:]+):\s*(.+)$", title)
        if offsite_match:
            venue = clean_text(offsite_match.group(1)).title()
            title = clean_text(offsite_match.group(2))

        full_url = item.get("fullUrl", "/calendar")
        ticket_url = full_url if full_url.startswith("http") else f"https://xrayarcade.com{full_url}"
        image_url = clean_text(item.get("assetUrl", ""))

        shows.append({
            "id": f"xray-{item.get('id', dt.strftime('%Y%m%d'))}",
            "artist": title,
            "venue": venue,
            "neighborhood": get_neighborhood(venue),
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%I:%M %p").lstrip("0"),
            "age": "All Ages / Check Venue",
            "price": "See Tickets",
            "ticketUrl": ticket_url,
            "image": image_url,
        })
    return shows


# ---------------------------------------------------------
# 2. UNIVERSAL JSON-LD EXTRACTOR (Cactus Club, Pabst Group, etc.)
# ---------------------------------------------------------
def find_jsonld_events(node, found: list):
    if isinstance(node, list):
        for item in node:
            find_jsonld_events(item, found)
    elif isinstance(node, dict):
        ev_type = node.get("@type", "")
        types = ev_type if isinstance(ev_type, list) else [ev_type]
        if any("Event" in str(t) for t in types):
            found.append(node)
        for val in node.values():
            if isinstance(val, (dict, list)):
                find_jsonld_events(val, found)


def scrape_jsonld_site(url: str, default_venue: str, prefix: str) -> list:
    shows = []
    page_html = fetch_page(url)
    soup = BeautifulSoup(page_html, "html.parser")
    raw_events = []

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            parsed = json.loads(script.string)
            find_jsonld_events(parsed, raw_events)
        except json.JSONDecodeError:
            continue

    for idx, ev in enumerate(raw_events):
        artist = clean_text(ev.get("name", ""))
        start_raw = ev.get("startDate", "")
        if not artist or not start_raw:
            continue

        date_str = start_raw[:10]
        time_str = "TBA"
        if "T" in start_raw:
            try:
                dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(MKE_TZ)
                date_str = dt.strftime("%Y-%m-%d")
                time_str = dt.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                pass

        loc = ev.get("location") or {}
        if isinstance(loc, list) and loc:
            loc = loc[0]
        venue = clean_text(loc.get("name", "")) if isinstance(loc, dict) else ""
        if not venue:
            venue = default_venue

        offers = ev.get("offers")
        price_str = "See Tickets"
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            p = offers.get("price")
            if p is not None and str(p) != "":
                price_str = "Free" if float(p) == 0 else f"${p}"

        ticket_url = ev.get("url") or url

        # 1. Try direct event/band image from the venue listing
        image_url = extract_image_url(ev.get("image"))
        # 2. If missing, check if a band website was identified and pull its og:image
        if not image_url:
            band_site = extract_band_website(ev)
            image_url = fetch_band_website_image(band_site)

        shows.append({
            "id": f"{prefix}-{date_str}-{idx}",
            "artist": artist,
            "venue": venue,
            "neighborhood": get_neighborhood(venue),
            "date": date_str,
            "time": time_str,
            "age": "Check Venue",
            "price": price_str,
            "ticketUrl": ticket_url,
            "image": image_url,
        })
    return shows


# ---------------------------------------------------------
# 3. SHANK HALL (HTML Schedule Parser)
# ---------------------------------------------------------
def scrape_shank_hall() -> list:
    shows = []
    url = "https://shankhall.com/tickets/"
    page_html = fetch_page(url)
    soup = BeautifulSoup(page_html, "html.parser")
    now = datetime.now(MKE_TZ)

    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }

    for idx, row in enumerate(soup.find_all(["tr", "div", "li"])):
        text = clean_text(row.get_text(" "))
        match = re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2})\b.*?\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.?\s+(.+?)\s+(\d{1,2}:\d{2}\s*[ap]m)\b",
            text,
            re.IGNORECASE,
        )
        if not match or len(text) > 300:
            continue

        mon_num = months[match.group(1).lower()[:3]]
        day_num = int(match.group(2))
        artist = clean_text(match.group(3))
        time_str = match.group(4).upper().replace("AM", " AM").replace("PM", " PM").replace("  ", " ")

        year = now.year + 1 if mon_num < now.month else now.year
        try:
            date_str = datetime(year, mon_num, day_num).strftime("%Y-%m-%d")
        except ValueError:
            continue

        price_match = re.search(r"\$\d+(?:\.\d{2})?", text)
        price_str = price_match.group(0) if price_match else "See Tickets"

        link_tag = row.find("a", href=True)
        ticket_url = link_tag["href"] if link_tag else url

        img_tag = row.find("img", src=True)
        image_url = urllib.parse.urljoin(url, img_tag["src"]) if img_tag else ""

        shows.append({
            "id": f"shank-{date_str}-{idx}",
            "artist": artist,
            "venue": "Shank Hall",
            "neighborhood": "East Side",
            "date": date_str,
            "time": time_str,
            "age": "21+",
            "price": price_str,
            "ticketUrl": ticket_url,
            "image": image_url,
        })
    return shows


# ---------------------------------------------------------
# MAIN COLLECTOR & DEDUPLICATOR
# ---------------------------------------------------------
def run_all_scrapers():
    today_str = datetime.now(MKE_TZ).strftime("%Y-%m-%d")
    collected = []

    scrapers = [
        ("X-Ray Arcade", scrape_xray_arcade),
        ("Cactus Club", lambda: scrape_jsonld_site("https://www.cactusclubmilwaukee.com/events/", "Cactus Club", "cactus")),
        ("Pabst Theater Group", lambda: scrape_jsonld_site("https://www.pabsttheatergroup.com/events", "Pabst Theater Group", "ptg")),
        ("Shank Hall", scrape_shank_hall),
    ]

    for label, fn in scrapers:
        try:
            results = fn()
            print(f"[OK] {label}: found {len(results)} events")
            collected.extend(results)
        except Exception as err:
            print(f"[WARN] {label} failed: {err}")

    seen = set()
    clean_shows = []

    for show in collected:
        if show["date"] < today_str:
            continue
        dedup_key = (
            show["date"],
            re.sub(r"\W+", "", show["venue"].lower()),
            re.sub(r"\W+", "", show["artist"].lower())[:25],
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        clean_shows.append(show)

    clean_shows.sort(key=lambda s: (s["date"], s["time"]))

    if clean_shows:
        with open("shows.json", "w", encoding="utf-8") as f:
            json.dump(clean_shows, f, indent=2)
        print(f"Successfully wrote {len(clean_shows)} upcoming Milwaukee shows to shows.json")
    else:
        print("No upcoming shows parsed; keeping existing shows.json untouched.")


if __name__ == "__main__":
    run_all_scrapers()
