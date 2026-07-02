import logging
import random

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

STRIP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form", "noscript", "svg", "iframe"]
CONTENT_TAGS = ["h1", "h2", "h3", "p"]
MAX_RETRIES = 3
REQUEST_TIMEOUT = 10.0


def _build_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _normalize_url(domain: str) -> str:
    domain = domain.strip()
    if not domain.startswith(("http://", "https://")):
        return f"https://{domain}"
    return domain


def fetch_html(url: str) -> str | None:
    url = _normalize_url(url)
    last_exception: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
                response = client.get(url, headers=_build_headers())
                response.raise_for_status()
                return response.text
        except httpx.TimeoutException as exc:
            last_exception = exc
            logger.warning("Timeout fetching %s (attempt %d/%d)", url, attempt, MAX_RETRIES)
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %s error fetching %s", exc.response.status_code, url)
            return None
        except httpx.RequestError as exc:
            last_exception = exc
            logger.warning("Network error fetching %s (attempt %d/%d): %s", url, attempt, MAX_RETRIES, exc)

    logger.error("Failed to fetch %s after %d attempts: %s", url, MAX_RETRIES, last_exception)
    return None


def extract_clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    text_chunks = []
    for tag in soup.find_all(CONTENT_TAGS):
        text = tag.get_text(strip=True)
        if text:
            text_chunks.append(text)

    return "\n".join(text_chunks)


def scrape_company_website(domain: str) -> str | None:
    html = fetch_html(domain)
    if not html:
        logger.error("No HTML retrieved for domain: %s", domain)
        return None

    clean_text = extract_clean_text(html)
    if not clean_text:
        logger.warning("No extractable text found for domain: %s", domain)
        return None

    return clean_text
