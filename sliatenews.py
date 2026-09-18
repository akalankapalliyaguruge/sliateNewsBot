import html
import json
import logging
import os
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.sliate.ac.lk"
BACKFILL_FROM = date(2026, 1, 1)

SECTIONS = {
    "Homepage": f"{BASE_URL}/",
    "Students": f"{BASE_URL}/news/students",
    "Common": f"{BASE_URL}/news/common",
    "Staff": f"{BASE_URL}/news/staff",
    "Events": f"{BASE_URL}/news/event",
    "Tender Notices": f"{BASE_URL}/news/tender-notices",
    "Tender Notices Legacy": f"{BASE_URL}/news/tender-notices/tender-notice",
    "Vacancies": f"{BASE_URL}/vacancies",
}

STATE_FILE = Path(__file__).with_name("sliate_seen_news.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

TIMEOUT = 20
MAX_WORKERS = 10
MAX_PAGES_PER_SECTION = 40
FULL_SCAN_EVERY = 24 * 60 * 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0 Safari/537.36 SLIATENewsBot/5.0"
    )
}

MONTHS = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)

ARTICLE_PREFIXES = ("/news/", "/vacancies/", "/sliate/")
BLOCKED = (
    "/component/users",
    "/contact",
    "/search",
    "/administrator",
    "/login",
    "/attachments/",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# COMMON HELPERS
# ---------------------------------------------------------------------------

def clean(text: str) -> str:
    return " ".join(text.split()).strip()


def normalize_url(url: str, keep_query: bool = True) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))

    if parsed.netloc.lower() not in {"sliate.ac.lk", "www.sliate.ac.lk"}:
        return ""

    path = parsed.path.rstrip("/") or "/"
    query = parsed.query if keep_query else ""

    return urlunparse(
        ("https", "www.sliate.ac.lk", path, "", query, "")
    )


def article_url(url: str) -> str:
    return normalize_url(url, keep_query=False)


LISTING_PATHS = {
    urlparse(article_url(url)).path.rstrip("/")
    for url in SECTIONS.values()
}


def is_article_url(url: str) -> bool:
    if not url:
        return False

    path = urlparse(url).path.rstrip("/")
    lower = path.lower()

    return (
        path not in LISTING_PATHS
        and path.startswith(ARTICLE_PREFIXES)
        and not any(part in lower for part in BLOCKED)
    )


def fetch_soup(url: str) -> BeautifulSoup:
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
        verify=False,  # SLIATE certificate issue
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    default = {
        "backfill_completed": False,
        "last_full_scan": 0,
        "seen_urls": [],
        "telegram_messages": {},
        "updated_at": None,
    }

    if not STATE_FILE.exists():
        return default

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key, value in default.items():
                data.setdefault(key, value)
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("State file error: %s", exc)

    return default


def save_state(state: dict) -> None:
    state["seen_urls"] = sorted(set(state.get("seen_urls", [])))
    state["updated_at"] = int(time.time())

    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# DATE / METADATA
# ---------------------------------------------------------------------------

def parse_date(value: str | None) -> date | None:
    if not value:
        return None

    value = clean(value)
    value = re.sub(
        r"\b(\d{1,2})(st|nd|rd|th)\b",
        r"\1",
        value,
        flags=re.IGNORECASE,
    )

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).date()
    except ValueError:
        pass

    for fmt in (
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
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    month_match = re.search(
        rf"\b(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})\b",
        value,
        flags=re.IGNORECASE,
    )

    if month_match:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(
                    month_match.group(1), fmt
                ).date()
            except ValueError:
                pass

    numeric_match = re.search(
        r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{4})\b",
        value,
    )

    if numeric_match:
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(
                    numeric_match.group(1), fmt
                ).date()
            except ValueError:
                pass

    return None


def format_date(value: str | None) -> str | None:
    if not value:
        return None

    try:
        return datetime.strptime(
            value, "%Y-%m-%d"
        ).strftime("%d %B %Y")
    except ValueError:
        return value


def extract_published(soup: BeautifulSoup) -> date | None:
    for selector in (
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="date"]',
        'meta[itemprop="datePublished"]',
    ):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            parsed = parse_date(tag["content"])
            if parsed:
                return parsed

    for tag in soup.select(
        "time[datetime], [itemprop='datePublished'], "
        ".published, dd.published, .create"
    ):
        parsed = parse_date(
            tag.get("datetime")
            or tag.get("content")
            or tag.get_text(" ", strip=True)
        )
        if parsed:
            return parsed

    text = clean(soup.get_text(" ", strip=True))

    for pattern in (
        rf"Published\s*:\s*(?:[A-Za-z]+,\s*)?"
        rf"(\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}})",
        r"Published\s*:\s*(\d{1,2}[./-]\d{1,2}[./-]\d{4})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed

    return None


def extract_expiry(soup: BeautifulSoup) -> date | None:
    text = clean(soup.get_text(" ", strip=True))

    text_date = (
        rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\s+\d{{4}}"
    )
    numeric_date = r"\d{1,2}[./-]\d{1,2}[./-]\d{4}"
    any_date = rf"(?:{text_date}|{numeric_date})"

    patterns = (
        rf"(?:Application\s+)?Closing\s+Date\s*[:\-]?\s*({any_date})",
        rf"Closing\s+Date\s+of\s+(?:the\s+)?Application\s*[:\-]?\s*({any_date})",
        rf"(?:Application\s+)?Deadline\s*[:\-]?\s*({any_date})",
        rf"Expiry\s+Date\s*[:\-]?\s*({any_date})",
        rf"Expiration\s+Date\s*[:\-]?\s*({any_date})",
        rf"(?:closing\s+date|deadline).{{0,140}}?"
        rf"(?:extended\s+)?(?:up\s+to|until|to)\s+({any_date})",
        rf"(?:apply|submit|submitted|application).{{0,140}}?"
        rf"(?:on\s+or\s+before|before)\s+({any_date})",
        rf"extended\s+(?:up\s+to|until)\s+({any_date})",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed

    return None


def extract_category(soup: BeautifulSoup) -> str | None:
    for selector in (
        ".category-name",
        "dd.category-name",
        ".article-info .category-name",
        "[itemprop='articleSection']",
    ):
        for tag in soup.select(selector):
            value = re.sub(
                r"^Category\s*:\s*",
                "",
                clean(tag.get_text(" ", strip=True)),
                flags=re.IGNORECASE,
            ).strip()

            if value and len(value) <= 100:
                return value

    text = clean(soup.get_text(" ", strip=True))

    match = re.search(
        r"Category\s*:\s*(.+?)"
        r"(?=\s+Published\s*:|\s+Created\s*:|\s+Hits\s*:|"
        r"\s+Written\s+by|\s+Print\b|\s+Email\b|$)",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        value = clean(match.group(1))
        if value and len(value) <= 100:
            return value

    return None


def article_text(soup: BeautifulSoup) -> str:
    for selector in (
        "[itemprop='articleBody']",
        "article",
        ".item-page",
        ".com-content-article",
        ".item",
        "main",
    ):
        tag = soup.select_one(selector)
        if tag:
            value = clean(tag.get_text(" ", strip=True))
            if value:
                return value

    return ""


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------

def is_pagination(seed_url: str, anchor) -> bool:
    candidate = normalize_url(anchor.get("href", ""))

    if not candidate:
        return False

    seed = urlparse(normalize_url(seed_url))
    parsed = urlparse(candidate)

    if parsed.path.rstrip("/") != seed.path.rstrip("/"):
        return False

    query = parse_qs(parsed.query)

    if {"start", "limitstart", "limit", "page"}.intersection(query):
        return True

    return clean(anchor.get_text(" ", strip=True)).lower() in {
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
        "11", "12", "13", "14", "15", "next", "end", "›", "»",
    }


def discover_pages(seed_url: str) -> list[str]:
    pages = []
    queue = deque([normalize_url(seed_url)])
    visited = set()

    while queue and len(visited) < MAX_PAGES_PER_SECTION:
        page_url = queue.popleft()

        if not page_url or page_url in visited:
            continue

        visited.add(page_url)
        pages.append(page_url)

        try:
            soup = fetch_soup(page_url)
        except requests.RequestException:
            continue

        for anchor in soup.select("a[href]"):
            if is_pagination(seed_url, anchor):
                next_url = normalize_url(anchor.get("href", ""))
                if next_url and next_url not in visited:
                    queue.append(next_url)

    return pages


def listing_pages(full_scan: bool) -> list[tuple[str, str]]:
    if not full_scan:
        return list(SECTIONS.items())

    result = []

    with ThreadPoolExecutor(
        max_workers=len(SECTIONS)
    ) as executor:
        futures = {
            executor.submit(discover_pages, url): name
            for name, url in SECTIONS.items()
        }

        found = {}

        for future in as_completed(futures):
            name = futures[future]
            try:
                found[name] = future.result()
            except Exception:
                found[name] = [SECTIONS[name]]

    for name in SECTIONS:
        for page_url in found.get(name, [SECTIONS[name]]):
            result.append((name, page_url))

    return result


def candidates_from_page(
    source: str,
    page_url: str,
) -> list[dict]:
    try:
        soup = fetch_soup(page_url)
    except requests.RequestException:
        return []

    selectors = (
        "h1 a[href], h2 a[href], h3 a[href], "
        ".page-header a[href], .item-title a[href], "
        ".items-leading a[href], .items-row a[href], "
        ".items-more a[href], .blog a[href], "
        ".blog-featured a[href], .readmore a[href], "
        "a.readmore[href], article a[href], "
        "ul.latestnews a[href], .latestnews a[href], "
        "[class*='latestnews'] a[href], "
        "[class*='latest-news'] a[href]"
    )

    result = []
    seen = set()

    for anchor in soup.select(selectors):
        title = clean(anchor.get_text(" ", strip=True))
        url = article_url(anchor.get("href", ""))

        if not title or not is_article_url(url) or url in seen:
            continue

        seen.add(url)

        result.append(
            {
                "title": title,
                "url": url,
                "source": source,
            }
        )

    return result


def collect_candidates(full_scan: bool) -> list[dict]:
    pages = listing_pages(full_scan)
    page_results = {}

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                candidates_from_page,
                source,
                page_url,
            ): index
            for index, (source, page_url) in enumerate(pages)
        }

        for future in as_completed(futures):
            index = futures[future]
            try:
                page_results[index] = future.result()
            except Exception:
                page_results[index] = []

    unique = {}

    for index in range(len(pages)):
        for item in page_results.get(index, []):
            unique.setdefault(item["url"], item)

    items = list(unique.values())

    for order, item in enumerate(items):
        item["website_order"] = order

    logging.info(
        "Found %d article URL(s) (%s scan).",
        len(items),
        "full" if full_scan else "fast",
    )

    return items


# ---------------------------------------------------------------------------
# ARTICLE INSPECTION
# ---------------------------------------------------------------------------

def inspect_article(item: dict) -> dict | None:
    try:
        soup = fetch_soup(item["url"])
    except requests.RequestException:
        return None

    heading = soup.select_one(
        "article h1, .item-page h1, .page-header h1, h1"
    )

    if heading:
        title = clean(heading.get_text(" ", strip=True))
        if title:
            item["title"] = title

    published = extract_published(soup)
    expiry = extract_expiry(soup)

    item["published_date"] = (
        published.isoformat() if published else None
    )
    item["expiry_date"] = (
        expiry.isoformat() if expiry else None
    )
    item["category"] = extract_category(soup)
    item["mentions_2026"] = bool(
        re.search(
            r"\b2026\b",
            clean(f"{item['title']} {article_text(soup)[:15000]}"),
        )
    )

    return item


def inspect_new(items: list[dict]) -> list[dict]:
    if not items:
        return []

    results = {}

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                inspect_article,
                item.copy(),
            ): index
            for index, item in enumerate(items)
        }

        for future in as_completed(futures):
            index = futures[future]

            try:
                value = future.result()
                if value:
                    results[index] = value
            except Exception:
                pass

    return [
        results[index]
        for index in sorted(results)
    ]


def backfill_ok(item: dict) -> bool:
    published = item.get("published_date")

    if published:
        try:
            return (
                datetime.strptime(
                    published,
                    "%Y-%m-%d",
                ).date()
                >= BACKFILL_FROM
            )
        except ValueError:
            return False

    return bool(item.get("mentions_2026"))


def telegram_order(items: list[dict]) -> list[dict]:
    """
    Follow the SLIATE website publication order.

    SLIATE listing pages normally show newest posts first.
    Telegram shows messages in the order they are sent, so for a backfill
    we reverse that website order and send oldest -> newest.

    This does NOT depend on Published metadata, so undated posts stay in
    their correct website position too.
    """
    return sorted(
        items,
        key=lambda item: item.get("website_order", 999999),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def telegram_message(item: dict) -> str:
    title = html.escape(item["title"])
    url = html.escape(item["url"], quote=True)

    details = []

    published = format_date(item.get("published_date"))
    if published:
        details.append(f"Published: {html.escape(published)}")

    expiry = format_date(item.get("expiry_date"))
    if expiry:
        details.append(f"Expired: {html.escape(expiry)}")

    category = item.get("category")
    if category:
        details.append(f"Category: {html.escape(category)}")

    message = (
        "<b>SLIATE NEWS</b>\n\n"
        f"<b>{title}</b>\n\n"
    )

    if details:
        message += "\n".join(details) + "\n\n"

    return (
        message
        + f'<a href="{url}">Read full news on SLIATE</a>\n\n'
        + "Sri Lanka Institute of Advanced Technological Education"
    )


def send_telegram(item: dict) -> int | None:
    api = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            api,
            data={
                "chat_id": CHANNEL_ID,
                "text": telegram_message(item),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            return None

        logging.info("Sent: %s", item["title"])

        return int(data["result"]["message_id"])

    except (requests.RequestException, ValueError, KeyError) as exc:
        logging.error("Telegram error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# MAIN RUN
# ---------------------------------------------------------------------------

def needs_full_scan(
    state: dict,
    first_run: bool,
) -> bool:
    if first_run:
        return True

    return (
        time.time() - int(state.get("last_full_scan", 0) or 0)
        >= FULL_SCAN_EVERY
    )


def run_check() -> None:
    state = load_state()
    seen = set(state["seen_urls"])

    first_run = not state["backfill_completed"]
    full_scan = needs_full_scan(state, first_run)

    logging.info(
        "%s",
        (
            "Initial full backfill"
            if first_run
            else (
                "Daily full safety scan"
                if full_scan
                else "Fast new-post scan"
            )
        ),
    )

    discovered = collect_candidates(full_scan)

    # Speed improvement:
    # Never re-open old article pages in normal runs.
    unseen = [
        item
        for item in discovered
        if item["url"] not in seen
    ]

    if not unseen:
        logging.info("No new SLIATE news.")

        if full_scan and not first_run:
            state["last_full_scan"] = int(time.time())
            save_state(state)

        return

    logging.info(
        "Inspecting only %d unseen article(s).",
        len(unseen),
    )

    inspected = inspect_new(unseen)

    if first_run:
        send_items = [
            item
            for item in inspected
            if backfill_ok(item)
        ]

        eligible_urls = {
            item["url"]
            for item in send_items
        }

        # Baseline old/current ineligible content.
        seen.update(
            item["url"]
            for item in inspected
            if item["url"] not in eligible_urls
        )

    else:
        # Future: every newly discovered URL is valid.
        send_items = inspected

    messages = state["telegram_messages"]

    for item in telegram_order(send_items):
        message_id = send_telegram(item)

        if message_id is None:
            continue

        seen.add(item["url"])

        messages[item["url"]] = {
            "message_id": message_id,
            "title": item.get("title"),
            "published_date": item.get("published_date"),
            "expiry_date": item.get("expiry_date"),
            "category": item.get("category"),
        }

        state["seen_urls"] = sorted(seen)
        save_state(state)

        time.sleep(1)

    if first_run:
        state["backfill_completed"] = True

    if full_scan:
        state["last_full_scan"] = int(time.time())

    state["seen_urls"] = sorted(seen)
    save_state(state)

    logging.info("Done.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")

    if not CHANNEL_ID:
        raise SystemExit("TELEGRAM_CHANNEL_ID is not set.")

    logging.info("SLIATE News Bot started.")
    run_check()


if __name__ == "__main__":
    main()
