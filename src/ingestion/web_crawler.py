import csv
import hashlib
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import trafilatura
from bs4 import BeautifulSoup
from urllib.robotparser import RobotFileParser

from src.config import BASE_DIR


# -----------------------------
# Configuration
# -----------------------------

SEED_URLS = [
    "https://sbi.bank.in/web/personal-banking/accounts/saving-account"
]

ALLOWED_DOMAINS = {
    "sbi.bank.in",
    "sbi.co.in",
}

MAX_PAGES = 20
MAX_DEPTH = 2
REQUEST_DELAY_SECONDS = 2

USER_AGENT = "BankBotV2EducationalCrawler/0.1"

OUTPUT_DIR = BASE_DIR / "data" / "raw" / "web_scraped"
REGISTRY_PATH = BASE_DIR / "data" / "raw" / "source_registry.csv"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

robots_cache = {}


# -----------------------------
# URL utilities
# -----------------------------

def normalize_url(url):
    """Remove URL fragments and normalize trailing slashes."""
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    if parsed.query:
        # Skip query-string URLs in this first version.
        return None

    normalized = parsed._replace(
        fragment="",
        path=parsed.path.rstrip("/") or "/",
    )

    return normalized.geturl()


def is_allowed_domain(url):
    """Only crawl approved SBI domains."""
    hostname = urlparse(url).hostname

    if not hostname:
        return False

    hostname = hostname.lower()

    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in ALLOWED_DOMAINS
    )


def is_html_url(url):
    """Skip files such as PDFs, images, and office documents."""
    path = urlparse(url).path.lower()

    blocked_extensions = (
        ".pdf", ".jpg", ".jpeg", ".png", ".gif",
        ".zip", ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".mp4", ".mp3"
    )

    return not path.endswith(blocked_extensions)


# -----------------------------
# Robots.txt
# -----------------------------

def get_robots_parser(url):
    """Fetch and cache robots.txt for each host."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    if robots_url in robots_cache:
        return robots_cache[robots_url]

    parser = RobotFileParser()
    parser.set_url(robots_url)

    try:
        response = session.get(robots_url, timeout=15)

        if response.status_code == 200:
            parser.parse(response.text.splitlines())
        elif response.status_code == 404:
            # No robots.txt file found.
            parser.parse([])
        else:
            # If robots.txt cannot be checked, do not crawl this host.
            parser.parse(["User-agent: *", "Disallow: /"])

    except requests.RequestException:
        parser.parse(["User-agent: *", "Disallow: /"])

    robots_cache[robots_url] = parser
    return parser


def can_crawl(url):
    parser = get_robots_parser(url)
    return parser.can_fetch(USER_AGENT, url)


# -----------------------------
# Page extraction
# -----------------------------

def extract_page(url):
    """Download a webpage and extract its title and main content."""
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "text/html" not in content_type:
            print(f"Skipping non-HTML content: {url}")
            return None, None

        html = response.text

        soup = BeautifulSoup(html, "html.parser")

        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title
            else "Untitled page"
        )

        text_content = trafilatura.extract(
            html,
            include_links=True,
            include_tables=True,
        )

        if not text_content:
            print(f"No main content extracted: {url}")
            return title, None

        return title, text_content

    except requests.RequestException as error:
        print(f"Request failed: {url} | {error}")
        return None, None


def extract_links(url):
    """Find eligible links on the current page."""
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        discovered_links = set()

        for anchor in soup.find_all("a", href=True):
            absolute_url = urljoin(url, anchor["href"])
            normalized_url = normalize_url(absolute_url)

            if not normalized_url:
                continue

            if not is_allowed_domain(normalized_url):
                continue

            if not is_html_url(normalized_url):
                continue

            discovered_links.add(normalized_url)

        return discovered_links

    except requests.RequestException as error:
        print(f"Could not extract links from {url}: {error}")
        return set()


# -----------------------------
# Save documents
# -----------------------------

def save_document(url, title, content, depth):
    """Save extracted page as Markdown and add registry metadata."""
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    filename = f"{url_hash}.md"

    output_path = OUTPUT_DIR / filename

    markdown = f"""---
title: "{title.replace('"', "'")}"
source_url: "{url}"
source_name: "SBI"
category: "unclassified"
document_type: "webpage"
scraped_at: "{scraped_at}"
crawl_depth: {depth}
---

# {title}

Source: {url}

Scraped at: {scraped_at}

---

{content}
"""

    output_path.write_text(markdown, encoding="utf-8")

    document_id = f"SBI-WEB-{url_hash}"

    registry_exists = REGISTRY_PATH.exists()

    with open(
        REGISTRY_PATH,
        "a",
        newline="",
        encoding="utf-8",
    ) as csv_file:

        writer = csv.writer(csv_file)

        if not registry_exists or REGISTRY_PATH.stat().st_size == 0:
            writer.writerow([
                "document_id",
                "title",
                "category",
                "source_url",
                "source_name",
                "document_type",
                "download_date",
                "last_updated",
                "status",
            ])

        writer.writerow([
            document_id,
            title,
            "unclassified",
            url,
            "SBI",
            "webpage",
            scraped_at,
            "",
            "scraped",
        ])

    print(f"Saved: {title}")


# -----------------------------
# Recursive crawler
# -----------------------------

def crawl():
    queue = deque()

    for seed_url in SEED_URLS:
        normalized = normalize_url(seed_url)

        if normalized:
            queue.append((normalized, 0))

    visited = set()
    saved_count = 0

    while queue and saved_count < MAX_PAGES:
        current_url, depth = queue.popleft()

        if current_url in visited:
            continue

        visited.add(current_url)

        if not is_allowed_domain(current_url):
            continue

        if not can_crawl(current_url):
            print(f"Blocked by robots.txt: {current_url}")
            continue

        print(f"\nCrawling depth={depth}: {current_url}")

        title, content = extract_page(current_url)

        if content:
            save_document(
                current_url,
                title,
                content,
                depth,
            )
            saved_count += 1

        if depth < MAX_DEPTH:
            links = extract_links(current_url)

            for link in links:
                if link not in visited:
                    queue.append((link, depth + 1))

        time.sleep(REQUEST_DELAY_SECONDS)

    print("\n--- CRAWL COMPLETE ---")
    print(f"Pages saved: {saved_count}")
    print(f"URLs visited: {len(visited)}")


if __name__ == "__main__":
    crawl()
