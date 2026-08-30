"""
Core scraping + extraction logic for the article scraper.

This is the same extraction logic as the original CLI script, refactored so
that NocoDB credentials and proxy credentials are passed in at call time
(via a settings dict) instead of being hardcoded constants. The GUI
(app.py) is responsible for collecting those settings from the user and
persisting them locally.
"""

from __future__ import annotations

import json
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup


CSV_FIELDS = [
    "Title",
    "Author",
    "Published date",
    "Featured image",
    "Article content",
    "Category",
    "Tags",
    "Source URL",
]

BAD_CONTENT_PATTERNS = [
    "javascript is disabled",
    "enable javascript",
    "access denied",
    "captcha",
    "verify you are human",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# SETTINGS
# ============================================================

# Baked-in defaults so the app works with zero setup. Still overridable
# and persisted via config.json (created next to this file on first save).
DEFAULT_NOCODB_BASE_URL = "https://cricexec-nocodb-prod.onrender.com"
DEFAULT_NOCODB_TABLE_ID = "meefwl8ntgjce3y"
DEFAULT_NOCODB_API_KEY = "nc_pat_EX1weQXAsK2oITb2Lz1xbBBRS3cvj5aTi4unj1cU"


@dataclass
class Settings:
    # NocoDB - pre-filled with your existing project's values
    nocodb_base_url: str = DEFAULT_NOCODB_BASE_URL
    nocodb_api_key: str = DEFAULT_NOCODB_API_KEY
    nocodb_table_id: str = DEFAULT_NOCODB_TABLE_ID

    # Proxy - just an on/off switch; the actual proxy pool lives in
    # proxies.txt and is rotated automatically (round-robin, one per URL).
    use_proxy: bool = True

    # NOTE: browser fallback, headless mode, and inter-request delay are
    # no longer user settings - they're applied automatically inside
    # scrape_urls_stream() based on how each page responds.


CONFIG_PATH = Path(__file__).parent / "config.json"

# Fields that hold secrets - used only to decide what to mask in the UI,
# never to decide what to save. Everything the user enters is saved to
# config.json in plaintext on their own machine. Recommend they gitignore
# this file (and keep server-side file permissions tight if hosted).
SECRET_FIELDS = {"nocodb_api_key"}


def load_settings() -> Settings:
    settings = Settings()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
        except Exception:
            pass

    # Backfill any blank NocoDB field with the baked-in default. This
    # covers older config.json files saved before defaults existed (or
    # ones a user accidentally cleared), so the app always comes up
    # pre-filled instead of silently reverting to empty inputs.
    if not settings.nocodb_base_url:
        settings.nocodb_base_url = DEFAULT_NOCODB_BASE_URL
    if not settings.nocodb_table_id:
        settings.nocodb_table_id = DEFAULT_NOCODB_TABLE_ID
    if not settings.nocodb_api_key:
        settings.nocodb_api_key = DEFAULT_NOCODB_API_KEY

    return settings


def save_settings(settings: Settings) -> None:
    CONFIG_PATH.write_text(
        json.dumps(settings.__dict__, indent=2),
        encoding="utf-8",
    )


# ============================================================
# PROXY POOL (loaded from proxies.txt, one "ip:port:user:pass" per line)
# ============================================================

PROXIES_PATH = Path(__file__).parent / "proxies.txt"


@dataclass
class ProxyEntry:
    host: str
    port: str
    username: str
    password: str

    @property
    def url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"

    @property
    def playwright_dict(self) -> dict:
        return {
            "server": f"http://{self.host}:{self.port}",
            "username": self.username,
            "password": self.password,
        }

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def load_proxy_pool() -> list[ProxyEntry]:
    if not PROXIES_PATH.exists():
        return []
    pool = []
    for line in PROXIES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 4:
            continue
        host, port, username, password = parts
        pool.append(ProxyEntry(host=host, port=port, username=username, password=password))
    return pool


def pick_proxy(pool: list[ProxyEntry], index: int) -> Optional[ProxyEntry]:
    """Round-robin selection: a different proxy per URL, cycling through the pool."""
    if not pool:
        return None
    return pool[index % len(pool)]


# ============================================================
# TEXT / DATE HELPERS
# ============================================================

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_date(value: Any) -> str:
    if not value:
        return ""
    value = clean_text(value)
    candidate = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        if (
            dt.hour == 0 and dt.minute == 0 and dt.second == 0
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        ):
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    formats = [
        "%B %d, %Y, %H:%M",
        "%B %d, %Y",
        "%b %d, %Y, %H:%M",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %B %Y, %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if "%H" in fmt:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


def first_nonempty(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and clean_text(value):
            return clean_text(value)
    return ""


# ============================================================
# JSON-LD
# ============================================================

def jsonld_items(soup: BeautifulSoup) -> list[Any]:
    items = []
    scripts = soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)})
    for script in scripts:
        raw = script.string or script.get_text()
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw2 = raw.strip().replace("\n", " ")
            try:
                data = json.loads(raw2)
            except Exception:
                continue
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                items.extend(data["@graph"])
            else:
                items.append(data)
    return items


def type_matches(item: dict, wanted: set[str]) -> bool:
    item_type = item.get("@type")
    if isinstance(item_type, list):
        return any(str(x).lower() in wanted for x in item_type)
    return str(item_type).lower() in wanted


def find_article_jsonld(items: list[Any]) -> dict:
    wanted = {
        "article", "newsarticle", "blogposting", "report",
        "liveblogposting", "analysisnewsarticle", "satirearticle",
        "scholarlyarticle",
    }
    for item in items:
        if isinstance(item, dict) and type_matches(item, wanted):
            return item
    for item in items:
        if isinstance(item, dict):
            keys = {str(k).lower() for k in item.keys()}
            if {"headline", "datepublished"} & keys:
                return item
    return {}


# ============================================================
# FIELD EXTRACTORS
# ============================================================

def extract_author(article_ld: dict, soup: BeautifulSoup) -> str:
    author = article_ld.get("author")

    def author_name(value: Any) -> str:
        if isinstance(value, dict):
            return first_nonempty(value.get("name"), value.get("url"))
        if isinstance(value, list):
            names = [author_name(x) for x in value]
            return ", ".join(x for x in names if x)
        return clean_text(value)

    result = author_name(author)
    if result:
        return result

    for attr, value in [
        ("name", "author"),
        ("property", "article:author"),
        ("property", "og:article:author"),
    ]:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return clean_text(tag["content"])

    selectors = [
        '[rel="author"]',
        '[class*="author" i]',
        '[class*="byline" i]',
        '[data-testid*="author" i]',
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            text = re.sub(r"^(by|author)\s*[:\-]?\s*", "", text, flags=re.I)
            if 1 <= len(text) <= 120:
                return text
    return ""


def extract_image(article_ld: dict, soup: BeautifulSoup, page_url: str) -> str:
    image = article_ld.get("image")
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    elif isinstance(image, list):
        for item in image:
            if isinstance(item, dict):
                image = item.get("url") or item.get("contentUrl")
                if image:
                    break
            elif isinstance(item, str):
                image = item
                break

    if image:
        return urljoin(page_url, clean_text(image))

    for attr, value in [
        ("property", "og:image"),
        ("name", "twitter:image"),
        ("property", "twitter:image"),
        ("itemprop", "image"),
    ]:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return urljoin(page_url, clean_text(tag["content"]))

    for container in soup.select("article, main"):
        for img in container.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                return urljoin(page_url, src)
    return ""


def extract_title(article_ld: dict, soup: BeautifulSoup) -> str:
    title = first_nonempty(article_ld.get("headline"), article_ld.get("name"))
    if title:
        return title
    for attr, value in [("property", "og:title"), ("name", "twitter:title")]:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))
    if soup.title:
        return clean_text(soup.title.get_text(" ", strip=True))
    return ""


def extract_date(article_ld: dict, soup: BeautifulSoup) -> str:
    date = first_nonempty(
        article_ld.get("datePublished"),
        article_ld.get("dateCreated"),
        article_ld.get("dateModified"),
    )
    if date:
        return normalize_date(date)

    for attr, value in [
        ("property", "article:published_time"),
        ("property", "article:modified_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publish-date"),
        ("itemprop", "datePublished"),
        ("itemprop", "dateCreated"),
    ]:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            return normalize_date(tag["content"])

    for selector in [
        "time[datetime]", "time",
        "[itemprop='datePublished']", "[itemprop='dateCreated']",
    ]:
        tag = soup.select_one(selector)
        if tag:
            value = tag.get("datetime") or tag.get("content") or tag.get_text(" ", strip=True)
            if value:
                return normalize_date(value)
    return ""


def clean_article_html(soup: BeautifulSoup) -> None:
    selectors = [
        "script", "style", "noscript", "template", "svg", "canvas",
        "nav", "header", "footer", "aside", "form", "button", "input", "iframe",
        ".advertisement", ".ads", ".ad",
        "[class*='advert' i]", "[id*='advert' i]",
        "[class*='social' i]", "[class*='share' i]",
        "[class*='comment' i]", "[id*='comment' i]",
        "[class*='related' i]", "[class*='recommended' i]",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            node.decompose()


def fallback_article_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    clean_article_html(soup)
    candidates = []
    selectors = [
        "article", "main", "[itemprop='articleBody']",
        "[class*='article-body' i]", "[class*='article-content' i]",
        "[class*='article__body' i]", "[class*='story-body' i]",
        "[class*='post-content' i]", "[class*='entry-content' i]",
        "[class*='content-body' i]",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            paragraphs = []
            for p in node.find_all(["p", "h2", "h3", "blockquote", "li"]):
                text = clean_text(p.get_text(" ", strip=True))
                if text:
                    paragraphs.append(text)
            if paragraphs:
                candidates.append("\n\n".join(paragraphs))

    if not candidates:
        paragraphs = []
        for p in soup.find_all(["p", "h2", "h3", "blockquote"]):
            text = clean_text(p.get_text(" ", strip=True))
            if text:
                paragraphs.append(text)
        candidates.append("\n\n".join(paragraphs))

    return max(candidates, key=len, default="")


def is_bad_content(text: str) -> bool:
    low = text.lower()
    return any(pattern in low for pattern in BAD_CONTENT_PATTERNS)


def extract_article(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld_items = jsonld_items(soup)
    article_ld = find_article_jsonld(ld_items)

    title = extract_title(article_ld, soup)
    author = extract_author(article_ld, soup)
    published = extract_date(article_ld, soup)
    image = extract_image(article_ld, soup, url)

    content = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=False,
        include_links=False,
        favor_precision=True,
    ) or ""
    content = clean_text(content)

    if len(content) < 250 or is_bad_content(content):
        fallback = fallback_article_text(html)
        if len(fallback) > len(content):
            content = fallback

    return {
        "Title": clean_text(title),
        "Author": clean_text(author),
        "Published date": published,
        "Featured image": image,
        "Article content": content,
        "Category": "no_category",
        "Tags": "no_tags",
        "Source URL": url,
    }


# ============================================================
# FETCHING
# ============================================================

def check_proxy_pool(settings: Settings) -> tuple[bool, str]:
    """Tests every proxy in the pool and reports how many are working."""
    pool = load_proxy_pool()
    if not pool:
        return False, "No proxies found in proxies.txt."
    if not settings.use_proxy:
        return True, f"{len(pool)} proxies loaded (currently disabled)."

    ok_count = 0
    errors = []
    for proxy in pool:
        try:
            response = httpx.get("https://ipv4.webshare.io/", proxy=proxy.url, timeout=15.0)
            response.raise_for_status()
            ok_count += 1
        except Exception as exc:
            errors.append(f"{proxy}: {exc}")

    if ok_count == len(pool):
        return True, f"All {ok_count}/{len(pool)} proxies OK."
    if ok_count > 0:
        return True, f"{ok_count}/{len(pool)} proxies OK. Failing: " + "; ".join(errors[:3])
    return False, f"All proxies failed. First error: {errors[0] if errors else 'unknown'}"


def check_nocodb(settings: Settings) -> tuple[bool, str]:
    if not settings.nocodb_api_key or not settings.nocodb_table_id or not settings.nocodb_base_url:
        return False, "Missing NocoDB base URL, API key, or table ID."
    base_url = settings.nocodb_base_url.rstrip("/")
    endpoint = f"{base_url}/api/v2/tables/{settings.nocodb_table_id}/records"
    headers = {"xc-token": settings.nocodb_api_key, "Accept": "application/json"}
    try:
        response = httpx.get(endpoint, headers=headers, params={"limit": 1}, timeout=15.0)
        if response.status_code >= 400:
            return False, f"NocoDB error {response.status_code}: {response.text[:200]}"
        return True, "NocoDB connection OK."
    except Exception as exc:
        return False, f"NocoDB connection FAILED: {exc}"


def fetch_http(url: str, proxy: Optional[ProxyEntry], timeout: float = 30.0) -> tuple[str, str]:
    client_kwargs = dict(headers=HEADERS, follow_redirects=True, timeout=timeout, http2=True)
    if proxy:
        client_kwargs["proxy"] = proxy.url
    with httpx.Client(**client_kwargs) as client:
        response = client.get(url)
        response.raise_for_status()
        return str(response.url), response.text


def fetch_playwright(url: str, proxy: Optional[ProxyEntry], timeout_ms: int = 30000) -> tuple[str, str]:
    # Headless is always on automatically - this runs on a server with no display.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy.playwright_dict

        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1366, "height": 768},
        )
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        for selector in [
            "button:has-text('Accept')",
            "button:has-text('Accept All')",
            "button:has-text('I Agree')",
            "button:has-text('Agree')",
            "#onetrust-accept-btn-handler",
        ]:
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1000):
                    button.click(timeout=1000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        page.wait_for_timeout(3000)
        final_url = page.url
        html = page.content()
        browser.close()
        return final_url, html


def send_to_nocodb(article: dict, settings: Settings) -> None:
    if not settings.nocodb_api_key:
        raise RuntimeError("NocoDB API key is empty.")
    if not settings.nocodb_table_id:
        raise RuntimeError("NocoDB table ID is empty.")

    base_url = settings.nocodb_base_url.rstrip("/")
    endpoint = f"{base_url}/api/v2/tables/{settings.nocodb_table_id}/records"
    payload = {field: article[field] for field in CSV_FIELDS}
    headers = {
        "xc-token": settings.nocodb_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    response = httpx.post(endpoint, headers=headers, json=payload, timeout=30.0)
    response.raise_for_status()


# ============================================================
# URL LIST PARSING
# ============================================================

def parse_urls(raw_text: str) -> list[str]:
    urls = []
    for line in raw_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


# ============================================================
# SCRAPE LOOP (generator, yields one result dict per URL as it completes)
# ============================================================

def scrape_urls_stream(urls: list[str], settings: Settings, upload_to_nocodb: bool = True) -> Iterator[dict]:
    """
    Yields a dict per URL processed:
    {
        "index": int, "total": int, "url": str,
        "article": dict,           # extracted fields, CSV_FIELDS keys
        "method": "http" | "playwright" | "failed",
        "proxy": str,               # proxy used for this URL, or "none"
        "nocodb_status": "uploaded" | "skipped" | "failed" | "disabled",
        "nocodb_error": str | None,
        "log": list[str],          # human-readable log lines for this URL
    }

    Browser fallback, headless mode, and inter-request delay are all
    automatic: Playwright kicks in only when the plain HTTP fetch fails or
    returns weak content, and a small randomized delay is inserted between
    requests to avoid hammering sites/proxies.
    """
    import time as _time

    proxy_pool = load_proxy_pool() if settings.use_proxy else []
    total = len(urls)

    for index, url in enumerate(urls, start=1):
        proxy = pick_proxy(proxy_pool, index - 1)
        log = [f"[{index}/{total}] {url}" + (f" (proxy: {proxy})" if proxy else "")]
        article = None
        method = "failed"

        try:
            final_url, html = fetch_http(url, proxy)
            article = extract_article(final_url, html)
            method = "http"
        except Exception as exc:
            log.append(f"HTTP fetch failed: {exc}")

        # Automatic fallback: only reach for a real browser when the plain
        # fetch failed outright or came back thin (likely JS-rendered/blocked).
        needs_fallback = article is None or len(article["Article content"]) < 500

        if needs_fallback:
            reason = "HTTP fetch failed" if article is None else "weak extraction"
            log.append(f"{reason}; trying Playwright automatically...")
            try:
                browser_url, browser_html = fetch_playwright(url, proxy)
                browser_article = extract_article(browser_url, browser_html)
                if article is None or len(browser_article["Article content"]) > len(article["Article content"]):
                    article = browser_article
                    method = "playwright"
            except Exception as exc:
                log.append(f"Playwright failed: {exc}")

        if article is None:
            article = {
                "Title": "", "Author": "", "Published date": "",
                "Featured image": "", "Article content": "",
                "Category": "no_category", "Tags": "no_tags",
                "Source URL": url,
            }

        log.append(
            f"title={bool(article['Title'])}, author={bool(article['Author'])}, "
            f"date={bool(article['Published date'])}, image={bool(article['Featured image'])}, "
            f"content_chars={len(article['Article content'])}"
        )

        nocodb_status = "disabled"
        nocodb_error = None
        has_required = bool(article["Title"]) and bool(article["Article content"]) and bool(article["Source URL"])

        if upload_to_nocodb:
            if not has_required:
                nocodb_status = "skipped"
                log.append("NocoDB: skipped (incomplete extraction)")
            else:
                try:
                    send_to_nocodb(article, settings)
                    nocodb_status = "uploaded"
                    log.append("NocoDB: uploaded successfully")
                except Exception as exc:
                    nocodb_status = "failed"
                    nocodb_error = str(exc)
                    log.append(f"NocoDB upload failed: {exc}")

        yield {
            "index": index,
            "total": total,
            "url": url,
            "article": article,
            "method": method,
            "proxy": str(proxy) if proxy else "none",
            "nocodb_status": nocodb_status,
            "nocodb_error": nocodb_error,
            "log": log,
        }

        # Automatic, randomized throttle between requests - not user-configurable.
        if index < total:
            _time.sleep(random.uniform(1.0, 2.5))
