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

# Skip pages with very little extracted content.
MIN_CONTENT_LENGTH = 300

USER_AGENT = "BankBotV2EducationalCrawler/0.1"

OUTPUT_DIR = BASE_DIR / "data" / "raw" / "web_scraped"
REGISTRY_PATH = BASE_DIR / "data" / "raw" / "source_registry.csv"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

robots_cache = {}


# -----------------------------
# URL utilities
# -----------------------------

def normalize_url(url):
    """Remove fragments and normalize trailing slashes."""
    url, _ = urldefrag(url)

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    # Skip query-string URLs for now.
    if parsed.query:
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
    """Skip PDFs, images, and other non-HTML files."""
    path = urlparse(url).path.lower()

    blocked_extensions = (
        ".pdf", ".jpg", ".jpeg", ".png", ".gif",
        ".zip", ".doc", ".docx", ".xls", ".xlsx",
        ".ppt", ".pptx", ".mp4", ".mp3",
    )

    return not path.endswith(blocked_extensions)


def is_relevant_url(url):
    """
    Restrict crawling to URLs likely to contain banking information.

    This is a first-pass filter, not a guarantee that every page
    discovered will be relevant.
    """
    path = urlparse(url).path.lower()

    relevant_keywords = (
        "account",
        "saving",
        "deposit",
        "loan",
        "card",
        "banking",
        "digital",
        "yono",
        "nri",
        "business",
        "remittance",
        "pension",
        "investment",
        "kyc",
        "fee",
        "charge",
        "rtgs",
        "neft",
        "upi",
        "forex",
        "forex",
        "agri",
        "rural",
        "insurance",
    )

    excluded_keywords = (
        "/blog/",
        "/marketing-insights",
        "/coe-chakra/",
        "/web/business/information/country",
        "quarterly-progress-reports",
    )

    if any(keyword in path for keyword in excluded_keywords):
        return False

    return any(keyword in path for keyword in relevant_keywords)


def categorize_url(url):
    """Assign a category based on the SBI URL path."""
    path = urlparse(url).path.lower()

    category_rules = [
        ("nri_banking", ("nri",)),
        ("business_banking", ("business", "sme", "corporate")),
        ("loans", ("loan",)),
        ("cards", ("card",)),
        ("digital_banking", ("digital", "yono", "upi", "internet-banking")),
        ("accounts", ("account", "saving")),
        ("deposits", ("deposit", "fd", "fixed-deposit", "rd")),
        ("fees_and_charges", ("fee", "charge")),
        ("remittances", ("remittance",)),
        ("investments", ("investment", "ppf", "mutual-fund", "securities")),
        ("eligibility", ("eligibility",)),
        ("policies", ("policy", "kyc", "continuity", "guideline")),
        ("agri_rural_banking", ("agri", "rural", "kisan")),
    ]

    for category, keywords in category_rules:
        if any(keyword in path for keyword in keywords):
            return category

    return "other"


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

def fetch_page(url):
    """
    Fetch a page once and return its title, extracted content,
    and BeautifulSoup object for link discovery.
    """
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "text/html" not in content_type:
            print(f"Skipping non-HTML content: {url}")
            return None, None, None

        html = response.text

        soup = BeautifulSoup(html, "html.parser")

        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title
            else "Untitled page"
        )

        content = trafilatura.extract(
            html,
            include_links=True,
            include_tables=True,
        )

        return title, content, soup

    except requests.RequestException as error:
        print(f"Request failed: {url} | {error}")
        return None, None, None


def extract_links(url, soup):
    """Find eligible links from an already downloaded page."""
    discovered_links = set()

    if soup is None:
        return discovered_links

    for anchor in soup.find_all("a", href=True):
        absolute_url = urljoin(url, anchor["href"])
        normalized_url = normalize_url(absolute_url)

        if not normalized_url:
            continue

        if not is_allowed_domain(normalized_url):
            continue

        if not is_html_url(normalized_url):
            continue

        if not is_relevant_url(normalized_url):
            continue

        discovered_links.add(normalized_url)

    return discovered_links


# -----------------------------
# Save documents
# -----------------------------

def save_document(url, title, content, depth):
    """Save extracted webpage as Markdown and add registry metadata."""
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    filename = f"{url_hash}.md"

    output_path = OUTPUT_DIR / filename

    category = categorize_url(url)

    safe_title = title.replace('"', "'")

    markdown = f"""---
title: "{safe_title}"
source_url: "{url}"
source_name: "SBI"
category: "{category}"
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
            category,
            url,
            "SBI",
            "webpage",
            scraped_at,
            "",
            "scraped",
        ])

    print(f"Saved [{category}]: {title}")


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

        if not is_html_url(current_url):
            continue

        if not is_relevant_url(current_url):
            continue

        if not can_crawl(current_url):
            print(f"Blocked by robots.txt: {current_url}")
            continue

        print(f"\nCrawling depth={depth}: {current_url}")

        title, content, soup = fetch_page(current_url)

        # Only save pages with enough extracted text.
        if content and len(content.strip()) >= MIN_CONTENT_LENGTH:
            save_document(
                current_url,
                title,
                content,
                depth,
            )
            saved_count += 1

        elif content:
            print(
                f"Skipping short content "
                f"({len(content.strip())} characters): {current_url}"
            )

        else:
            print(f"Skipping page with no extracted content: {current_url}")

        # Discover relevant links from the same downloaded page.
        if soup and depth < MAX_DEPTH:
            links = extract_links(current_url, soup)

            for link in links:
                if link not in visited:
                    queue.append((link, depth + 1))

        time.sleep(REQUEST_DELAY_SECONDS)

    print("\n--- CRAWL COMPLETE ---")
    print(f"Pages saved: {saved_count}")
    print(f"URLs visited: {len(visited)}")


if __name__ == "__main__":
    crawl()