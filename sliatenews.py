import html
import json
import logging
import os
import re
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.sliate.ac.lk"
HOMEPAGE_URL = f"{BASE_URL}/"

CATEGORY_PAGES = {
    "Students": f"{BASE_URL}/news/students",
    "Common": f"{BASE_URL}/news/common",
    "Staff": f"{BASE_URL}/news/staff",
    "Events": f"{BASE_URL}/news/event",
    "Tender Notices": f"{BASE_URL}/news/tender-notices/tender-notice",
    "Vacancies": f"{BASE_URL}/vacancies",
}

# Only send posts actually published on or after this date.
BACKFILL_FROM_DATE = date(2026, 1, 1)

CHECK_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT = 30
MAX_LISTING_PAGES_PER_SECTION = 25

STATE_FILE = Path(__file__).with_name("sliate_seen_news.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0 Safari/537.36 SLIATENewsBot/2.0"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)


def normalize_url(url: str) -> str:
    absolute = urljoin(BASE_URL, url)
    parsed = urlparse(absolute)

    if parsed.netloc.lower() not in {"sliate.ac.lk", "www.sliate.ac.lk"}:
        return ""

    path = parsed.path.rstrip("/") or "/"

    return urlunparse(
        ("https", "www.sliate.ac.lk", path, "", parsed.query, "")
    )


def canonical_article_url(url: str) -> str:
    """Remove query/fragment from article URLs used as the unique ID."""
    normalized = normalize_url(url)
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", "www.sliate.ac.lk", path, "", "", ""))


def clean_text(text: str) -> str:
    return " ".join(text.split()).strip()


def fetch_soup(url: str) -> BeautifulSoup:
    # SLIATE's site has recently presented an expired HTTPS certificate.
    # Verification is disabled ONLY for reads from SLIATE.
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        verify=False,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def default_state() -> dict:
    return {
        "seen_urls": [],
        "telegram_messages": {},
        "updated_at": None,
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return default_state()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        # Backward compatibility with the original state file.
        if isinstance(data, dict):
            data.setdefault("seen_urls", [])
            data.setdefault("telegram_messages", {})
            data.setdefault("updated_at", None)
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not read state file: %s", exc)

    return default_state()


def save_state(state: dict) -> None:
    state["seen_urls"] = sorted(set(state.get("seen_urls", [])))
    state["updated_at"] = int(time.time())

    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def parse_date_string(value: str) -> date | None:
    value = clean_text(value)

    formats = (
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    )

    # ISO values often contain milliseconds or Z.
    iso = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    match = re.search(
        rf"\b(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})\b",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        try:
            return datetime.strptime(match.group(1), "%d %B %Y").date()
        except ValueError:
            pass

    return None


def extract_published_date(soup: BeautifulSoup) -> date | None:
    # Structured metadata first.
    meta_selectors = (
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="date"]',
        'meta[itemprop="datePublished"]',
    )

    for selector in meta_selectors:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            parsed = parse_date_string(tag["content"])
            if parsed:
                return parsed

    for tag in soup.select("time[datetime], [itemprop='datePublished']"):
        raw = tag.get("datetime") or tag.get("content") or tag.get_text(" ", strip=True)
        parsed = parse_date_string(raw)
        if parsed:
            return parsed

    # SLIATE Joomla pages commonly show:
    # "Details Category: Students Published: 31 August 2026"
    page_text = soup.get_text(" ", strip=True)

    match = re.search(
        rf"Published\s*:\s*(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})",
        page_text,
        flags=re.IGNORECASE,
    )
    if match:
        return parse_date_string(match.group(1))

    # Less specific fallback around common Joomla publishing-date classes.
    for selector in (
        ".published",
        ".create",
        ".createdby",
        ".article-info",
        ".article-info-term",
        "dd.published",
    ):
        for tag in soup.select(selector):
            parsed = parse_date_string(tag.get_text(" ", strip=True))
            if parsed:
                return parsed

    return None


def is_pagination_link(current_seed: str, href: str) -> bool:
    candidate = normalize_url(href)
    if not candidate:
        return False

    seed = urlparse(normalize_url(current_seed))
    parsed = urlparse(candidate)

    if parsed.path.rstrip("/") != seed.path.rstrip("/"):
        return False

    query = parse_qs(parsed.query)
    pagination_keys = {
        "start",
        "limitstart",
        "limit",
        "page",
    }

    return bool(pagination_keys.intersection(query))


def discover_listing_pages(seed_url: str) -> list[str]:
    """Follow Joomla pagination links for one section."""
    discovered = []
    queue = deque([normalize_url(seed_url)])
    visited = set()

    while queue and len(visited) < MAX_LISTING_PAGES_PER_SECTION:
        page_url = queue.popleft()

        if not page_url or page_url in visited:
            continue

        visited.add(page_url)
        discovered.append(page_url)

        try:
            soup = fetch_soup(page_url)
        except requests.RequestException as exc:
            logging.warning("Listing page failed %s: %s", page_url, exc)
            continue

        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "")
            if is_pagination_link(seed_url, href):
                next_url = normalize_url(href)
                if next_url and next_url not in visited:
                    queue.append(next_url)

    return discovered


def extract_article_links(page_url: str, category: str) -> list[dict]:
    try:
        soup = fetch_soup(page_url)
    except requests.RequestException as exc:
        logging.warning("%s listing failed: %s", category, exc)
        return []

    results = []
    seen_here = set()

    # Main Joomla article-title links.
    for anchor in soup.select(
        "h1 a[href], h2 a[href], h3 a[href], "
        ".page-header a[href], .item-title a[href]"
    ):
        title = clean_text(anchor.get_text(" ", strip=True))
        url = canonical_article_url(anchor.get("href", ""))

        if not title or not url:
            continue

        path = urlparse(url).path.lower()

        blocked = (
            "/component/users",
            "/contact",
            "/search",
        )
        if any(x in path for x in blocked):
            continue

        # Don't treat section/listing pages themselves as articles.
        listing_paths = {
            urlparse(canonical_article_url(HOMEPAGE_URL)).path.rstrip("/"),
            *[
                urlparse(canonical_article_url(v)).path.rstrip("/")
                for v in CATEGORY_PAGES.values()
            ],
        }
        if urlparse(url).path.rstrip("/") in listing_paths:
            continue

        if url in seen_here:
            continue

        seen_here.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "category": category,
            }
        )

    return results


def collect_article_candidates() -> list[dict]:
    candidates: dict[str, dict] = {}

    seeds = list(CATEGORY_PAGES.items()) + [("Common", HOMEPAGE_URL)]

    for category, seed_url in seeds:
        pages = discover_listing_pages(seed_url)
        logging.info("%s: scanning %d listing page(s)", category, len(pages))

        for page_url in pages:
            for item in extract_article_links(page_url, category):
                candidates.setdefault(item["url"], item)

    return list(candidates.values())


def scrape_2026_and_newer() -> list[dict]:
    candidates = collect_article_candidates()
    accepted = []

    logging.info("Checking published dates for %d candidate article(s)...", len(candidates))

    for index, item in enumerate(candidates, start=1):
        try:
            soup = fetch_soup(item["url"])
        except requests.RequestException as exc:
            logging.warning("Article failed %s: %s", item["url"], exc)
            continue

        published = extract_published_date(soup)

        if not published:
            logging.warning(
                "Skipped because Published date could not be determined: %s",
                item["title"],
            )
            continue

        if published < BACKFILL_FROM_DATE:
            continue

        item["published_date"] = published.isoformat()
        accepted.append(item)

        if index % 10 == 0:
            logging.info(
                "Date checked %d/%d article(s)",
                index,
                len(candidates),
            )

    # First backfill should look natural in Telegram.
    accepted.sort(
        key=lambda x: (
            x["published_date"],
            x["title"].lower(),
        )
    )

    return accepted


def send_to_telegram(item: dict) -> int | None:
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    title = html.escape(item["title"])
    category = html.escape(item["category"])
    article_url = html.escape(item["url"], quote=True)

    try:
        published_text = datetime.strptime(
            item["published_date"], "%Y-%m-%d"
        ).strftime("%d %B %Y")
    except (KeyError, ValueError):
        published_text = item.get("published_date", "Unknown")

    message = (
        " <b>SLIATE NEWS</b>\n\n"
        f"<b>{title}</b>\n\n"
        f" Published: {html.escape(published_text)}\n"
        f" Category: {category}\n\n"
        f' <a href="{article_url}">Read full news on SLIATE</a>\n\n'
        " Sri Lanka Institute of Advanced Technological Education"
    )

    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        # Telegram remains fully SSL-verified.
        response = session.post(
            telegram_url,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram API error: %s", result)
            return None

        message_id = result["result"]["message_id"]
        logging.info("Sent to Telegram: %s", item["title"])
        return int(message_id)

    except (requests.RequestException, ValueError, KeyError) as exc:
        logging.error("Telegram send failed: %s", exc)
        return None


def run_check() -> None:
    logging.info(
        "Checking SLIATE for posts published from %s onward...",
        BACKFILL_FROM_DATE.isoformat(),
    )

    items = scrape_2026_and_newer()

    if not items:
        logging.warning(
            "No qualifying SLIATE posts detected. State was not changed."
        )
        return

    state = load_state()
    seen = set(state.get("seen_urls", []))
    telegram_messages = state.setdefault("telegram_messages", {})

    new_items = [item for item in items if item["url"] not in seen]

    if not new_items:
        logging.info("No new qualifying SLIATE news.")
        return

    logging.info("Found %d unsent qualifying article(s).", len(new_items))

    for item in new_items:
        message_id = send_to_telegram(item)

        if message_id is not None:
            seen.add(item["url"])
            telegram_messages[item["url"]] = {
                "message_id": message_id,
                "published_date": item.get("published_date"),
                "title": item.get("title"),
            }
            state["seen_urls"] = sorted(seen)
            save_state(state)

        time.sleep(1.2)


def validate_config() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "ERROR: TELEGRAM_BOT_TOKEN is not set."
        )

    if not CHANNEL_ID:
        raise SystemExit(
            "ERROR: TELEGRAM_CHANNEL_ID is not set. "
            "Example: @my_sliate_news_channel"
        )


def main() -> None:
    validate_config()

    logging.info("SLIATE News Bot started.")

    try:
        run_check()
    except Exception:
        logging.exception("Unexpected error while checking SLIATE.")
        raise


if __name__ == "__main__":
    main()
