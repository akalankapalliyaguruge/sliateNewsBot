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
    "Tender Notices": f"{BASE_URL}/news/tender-notices",
    "Tender Notices Legacy": f"{BASE_URL}/news/tender-notices/tender-notice",
    "Vacancies": f"{BASE_URL}/vacancies",
}

# Only send posts actually published on or after this date.
BACKFILL_FROM_DATE = date(2026, 1, 1)

CHECK_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT = 30
MAX_LISTING_PAGES_PER_SECTION = 40

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
        "backfill_completed": False,
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
            data.setdefault("backfill_completed", False)
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
    if not value:
        return None

    value = clean_text(value)
    value = re.sub(
        r"\b(\d{1,2})(st|nd|rd|th)\b",
        r"\1",
        value,
        flags=re.IGNORECASE,
    )

    formats = (
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%A, %d %B %Y %H:%M",
        "%A, %d %B %Y",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    )

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

    month_match = re.search(
        rf"\b(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})\b",
        value,
        flags=re.IGNORECASE,
    )
    if month_match:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(month_match.group(1), fmt).date()
            except ValueError:
                continue

    numeric_match = re.search(
        r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b",
        value,
    )
    if numeric_match:
        candidate = numeric_match.group(1)
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue

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
        rf"Published\s*:\s*(?:[A-Za-z]+,\s*)?"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",
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


def extract_expiry_date(soup: BeautifulSoup) -> date | None:
    """Find closing/deadline/expiry dates when the article provides one."""
    page_text = clean_text(soup.get_text(" ", strip=True))

    text_date = rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}}"
    numeric_date = r"\d{1,2}[./-]\d{1,2}[./-]\d{4}"
    any_date = rf"(?:{text_date}|{numeric_date})"

    patterns = (
        rf"(?:Application\s+)?Closing\s+Date\s*[:\-]?\s*({any_date})",
        rf"Closing\s+Date\s+of\s+(?:the\s+)?Application\s*[:\-]?\s*({any_date})",
        rf"(?:Application\s+)?Deadline\s*[:\-]?\s*({any_date})",
        rf"Expiry\s+Date\s*[:\-]?\s*({any_date})",
        rf"Expiration\s+Date\s*[:\-]?\s*({any_date})",
        rf"(?:closing\s+date|deadline).{{0,120}}?(?:extended\s+)?(?:up\s+to|until|to)\s+({any_date})",
        rf"(?:apply|submit|submitted|application).{{0,120}}?(?:on\s+or\s+before|before)\s+({any_date})",
        rf"extended\s+(?:up\s+to|until)\s+({any_date})",
    )

    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            parsed = parse_date_string(match.group(1))
            if parsed:
                return parsed

    return None


def extract_category(soup: BeautifulSoup) -> str | None:
    """Return category only if the article page itself exposes it."""
    for selector in (
        ".category-name",
        "dd.category-name",
        ".article-info .category-name",
        "[itemprop='articleSection']",
    ):
        for tag in soup.select(selector):
            value = clean_text(tag.get_text(" ", strip=True))
            value = re.sub(
                r"^Category\s*:\s*",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
            if value:
                return value

    page_text = clean_text(soup.get_text(" ", strip=True))
    match = re.search(
        r"Category\s*:\s*(.+?)(?=\s+Published\s*:|\s+Created\s*:|\s+Hits\s*:|\s+Print\b|\s+Email\b|$)",
        page_text,
        flags=re.IGNORECASE,
    )
    if match:
        value = clean_text(match.group(1))
        if value and len(value) <= 100:
            return value

    return None


def article_content_text(soup: BeautifulSoup) -> str:
    """Get the main article body without relying on sidebar/latest-news text."""
    selectors = (
        "[itemprop='articleBody']",
        "article",
        ".item-page",
        ".com-content-article",
        ".item",
        "main",
    )

    for selector in selectors:
        tag = soup.select_one(selector)
        if tag:
            value = clean_text(tag.get_text(" ", strip=True))
            if value:
                return value

    return ""


def page_mentions_2026(title: str, soup: BeautifulSoup) -> bool:
    """
    Initial-backfill fallback for a genuine 2026 notice that has no
    Published metadata. Only inspect the article body/title, not sidebars.
    """
    body = article_content_text(soup)
    sample = clean_text(f"{title} {body[:15000]}")
    return bool(re.search(r"\b2026\b", sample))


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

    listing_paths = {
        urlparse(canonical_article_url(HOMEPAGE_URL)).path.rstrip("/"),
        *[
            urlparse(canonical_article_url(v)).path.rstrip("/")
            for v in CATEGORY_PAGES.values()
        ],
    }

    def add_anchor(anchor) -> None:
        title = clean_text(anchor.get_text(" ", strip=True))
        url = canonical_article_url(anchor.get("href", ""))

        if not title or not url:
            return

        path = urlparse(url).path.lower()

        if any(x in path for x in (
            "/component/users",
            "/contact",
            "/search",
            "/administrator",
        )):
            return

        if urlparse(url).path.rstrip("/") in listing_paths:
            return

        # News articles used by SLIATE are normally under these routes.
        if not path.startswith(("/news/", "/vacancies/", "/sliate/")):
            return

        if url in seen_here:
            return

        seen_here.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "source_category": category,
            }
        )

    # Joomla news/blog/article links: titles, cards, read-more and more-articles.
    for anchor in soup.select(
        "h1 a[href], h2 a[href], h3 a[href], "
        ".page-header a[href], .item-title a[href], "
        ".items-leading a[href], .items-row a[href], "
        ".items-more a[href], .blog a[href], .blog-featured a[href], "
        ".readmore a[href], a.readmore[href], article a[href]"
    ):
        add_anchor(anchor)

    # SLIATE's Latest News modules contain important undated /sliate/... posts.
    for selector in (
        "ul.latestnews a[href]",
        ".latestnews a[href]",
        "[class*='latestnews'] a[href]",
        "[class*='latest-news'] a[href]",
    ):
        for anchor in soup.select(selector):
            add_anchor(anchor)

    # Fallback for a heading literally named Latest News.
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if clean_text(heading.get_text(" ", strip=True)).lower() != "latest news":
            continue
        container = heading.find_next(["ul", "div"])
        if container:
            for anchor in container.select("a[href]"):
                add_anchor(anchor)

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


def scrape_all_articles() -> list[dict]:
    candidates = collect_article_candidates()
    inspected = []

    logging.info(
        "Inspecting metadata for %d candidate article(s)...",
        len(candidates),
    )

    for index, item in enumerate(candidates, start=1):
        try:
            soup = fetch_soup(item["url"])
        except requests.RequestException as exc:
            logging.warning("Article failed %s: %s", item["url"], exc)
            continue

        # Prefer the article's own heading when available.
        heading = soup.select_one("article h1, .item-page h1, .page-header h1, h1")
        if heading:
            title = clean_text(heading.get_text(" ", strip=True))
            if title:
                item["title"] = title

        published = extract_published_date(soup)
        expiry = extract_expiry_date(soup)

        item["published_date"] = published.isoformat() if published else None
        item["expiry_date"] = expiry.isoformat() if expiry else None
        item["category"] = extract_category(soup)
        item["mentions_2026"] = page_mentions_2026(item["title"], soup)
        inspected.append(item)

        if index % 10 == 0:
            logging.info(
                "Inspected %d/%d article(s)",
                index,
                len(candidates),
            )

    return inspected


def backfill_eligible(item: dict) -> bool:
    published = item.get("published_date")

    if published:
        try:
            return (
                datetime.strptime(published, "%Y-%m-%d").date()
                >= BACKFILL_FROM_DATE
            )
        except ValueError:
            return False

    # Some current SLIATE notices have no Published field. For the initial
    # 2026 rebuild, include them only when the page clearly refers to 2026.
    return bool(item.get("mentions_2026"))


def sort_items(items: list[dict]) -> list[dict]:
    # Dated posts oldest -> newest. Undated 2026 notices come afterwards.
    return sorted(
        items,
        key=lambda item: (
            item.get("published_date") is None,
            item.get("published_date") or "9999-12-31",
            item.get("title", "").lower(),
        ),
    )


def send_to_telegram(item: dict) -> int | None:
    telegram_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    title = html.escape(item["title"])
    article_url = html.escape(item["url"], quote=True)

    details = []

    published = item.get("published_date")
    if published:
        try:
            published_text = datetime.strptime(
                published, "%Y-%m-%d"
            ).strftime("%d %B %Y")
        except ValueError:
            published_text = published
        details.append(f"Published: {html.escape(published_text)}")

    expiry = item.get("expiry_date")
    if expiry:
        try:
            expiry_text = datetime.strptime(
                expiry, "%Y-%m-%d"
            ).strftime("%d %B %Y")
        except ValueError:
            expiry_text = expiry
        details.append(f"Expired: {html.escape(expiry_text)}")

    category = item.get("category")
    if category:
        details.append(f"Category: {html.escape(category)}")

    message = (
        "<b>SLIATE NEWS</b>\n\n"
        f"<b>{title}</b>\n\n"
    )

    if details:
        message += "\n".join(details) + "\n\n"

    message += (
        f'<a href="{article_url}">Read full news on SLIATE</a>\n\n'
        "Sri Lanka Institute of Advanced Technological Education"
    )

    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        # Keep preview disabled so SLIATE's author metadata (dilaxshi) is hidden.
        "disable_web_page_preview": True,
    }

    try:
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
    state = load_state()
    seen = set(state.get("seen_urls", []))
    first_backfill = not state.get("backfill_completed", False)

    if first_backfill:
        logging.info(
            "Initial backfill: sending SLIATE posts from %s onward.",
            BACKFILL_FROM_DATE.isoformat(),
        )
    else:
        logging.info(
            "Normal mode: every newly discovered SLIATE news article URL will be sent."
        )

    items = scrape_all_articles()

    if not items:
        logging.warning(
            "No SLIATE article items detected. State was not changed."
        )
        return

    if first_backfill:
        # Baseline all currently visible pre-2026 / non-2026 archive items as
        # already known, so the next scheduled run does not suddenly send old
        # archive content.
        for item in items:
            if not backfill_eligible(item):
                seen.add(item["url"])

        new_items = [
            item for item in items
            if backfill_eligible(item) and item["url"] not in seen
        ]
    else:
        # After the initial rebuild, URL novelty is the rule. Published date,
        # expiry date and category are optional and never block a new post.
        new_items = [
            item for item in items
            if item["url"] not in seen
        ]

    new_items = sort_items(new_items)

    logging.info(
        "Found %d unsent qualifying article(s).",
        len(new_items),
    )

    telegram_messages = state.setdefault("telegram_messages", {})

    for item in new_items:
        message_id = send_to_telegram(item)

        if message_id is None:
            continue

        seen.add(item["url"])
        telegram_messages[item["url"]] = {
            "message_id": message_id,
            "published_date": item.get("published_date"),
            "expiry_date": item.get("expiry_date"),
            "category": item.get("category"),
            "title": item.get("title"),
        }
        state["seen_urls"] = sorted(seen)
        save_state(state)
        time.sleep(1.2)

    if first_backfill:
        state["backfill_completed"] = True
        state["seen_urls"] = sorted(seen)
        save_state(state)
        logging.info(
            "2026 backfill completed. Future runs will send every new SLIATE news URL."
        )


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
