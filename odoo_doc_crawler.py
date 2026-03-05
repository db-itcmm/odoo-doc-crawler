#!/usr/bin/env python3
"""
Odoo 19.0 Documentation Crawler
================================
Crawls the official Odoo 19.0 documentation and converts pages to clean Markdown files.
Organized by section for optimal use in a Claude Project.

Usage:
    python3 odoo_doc_crawler.py [--sections all|user|developer|contributing] [--output ./odoo_docs]

Requirements:
    pip install requests beautifulsoup4 markdownify --break-system-packages

Author: Script generated for ITC MULTIMEDIA - David
"""

import os
import re
import sys
import time
import json
import hashlib
import argparse
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md
except ImportError:
    print("Installing required packages...")
    os.system("pip install requests beautifulsoup4 markdownify --break-system-packages")
    import requests
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md

# ─── Configuration ──────────────────────────────────────────────────────────────

BASE_URL = "https://www.odoo.com/documentation/19.0/"
USER_AGENT = "Mozilla/5.0 (compatible; OdooDocCrawler/1.0)"
REQUEST_DELAY = 0.5  # seconds between requests (be polite)
MAX_RETRIES = 3
TIMEOUT = 30
MAX_WORKERS = 4  # parallel downloads (keep low to avoid rate limiting)

# ─── Logging ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("odoo_crawler")

# ─── URL Discovery ──────────────────────────────────────────────────────────────

def get_all_doc_urls(base_url: str) -> dict:
    """
    Fetch the main TOC page and extract all documentation URLs, organized by section.
    """
    log.info(f"Fetching main TOC from {base_url}")
    resp = requests.get(base_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    urls = {
        "user": [],
        "developer": [],
        "contributing": [],
        "setup": [],
    }

    # Find all links in the documentation
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        # Skip external links, anchors, and non-doc links
        if href.startswith("http") and "odoo.com/documentation/19.0" not in href:
            continue
        if href.startswith("#") or href.startswith("mailto:"):
            continue

        full_url = urljoin(base_url, href)

        # Only keep documentation pages
        if "/documentation/19.0/" not in full_url:
            continue
        if not full_url.endswith(".html"):
            continue
        # Skip translations (keep only English or root)
        parsed = urlparse(full_url)
        path = parsed.path
        # Categorize
        if "/developer/" in path:
            urls["developer"].append(full_url)
        elif "/contributing/" in path:
            urls["contributing"].append(full_url)
        elif "/administration/" in path:
            urls["setup"].append(full_url)
        elif "/applications/" in path:
            urls["user"].append(full_url)
        else:
            urls["user"].append(full_url)  # default to user

    # Deduplicate
    for key in urls:
        urls[key] = sorted(set(urls[key]))

    total = sum(len(v) for v in urls.values())
    log.info(f"Discovered {total} unique URLs:")
    for section, section_urls in urls.items():
        log.info(f"  {section}: {len(section_urls)} pages")

    return urls


def discover_deep_urls(base_url: str, section_urls: dict) -> dict:
    """
    For each discovered page, also crawl it to find sub-pages not in the main TOC.
    This catches pages only linked from sub-sections.
    """
    all_urls = {}
    for section, urls in section_urls.items():
        all_urls[section] = set(urls)

    # Crawl developer and user index pages to find more links
    index_pages = [
        (base_url + "developer.html", "developer"),
        (base_url + "developer/reference.html", "developer"),
        (base_url + "developer/tutorials.html", "developer"),
        (base_url + "developer/howtos.html", "developer"),
        (base_url + "contributing.html", "contributing"),
        (base_url + "administration.html", "setup"),
    ]

    for index_url, section in index_pages:
        try:
            log.info(f"Scanning index page: {index_url}")
            resp = requests.get(index_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            if resp.status_code != 200:
                log.warning(f"  Skipped (HTTP {resp.status_code})")
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                full_url = urljoin(index_url, href)
                if "/documentation/19.0/" in full_url and full_url.endswith(".html"):
                    all_urls.setdefault(section, set()).add(full_url)
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning(f"  Error scanning {index_url}: {e}")

    # Convert back to sorted lists
    result = {}
    for section, urls in all_urls.items():
        result[section] = sorted(urls)

    total = sum(len(v) for v in result.values())
    log.info(f"After deep scan: {total} total unique URLs")
    return result


# ─── Page Conversion ────────────────────────────────────────────────────────────

def fetch_and_convert(url: str) -> tuple:
    """
    Fetch a single documentation page and convert it to clean Markdown.
    Returns (url, markdown_content, title) or (url, None, None) on failure.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            if resp.status_code == 404:
                log.warning(f"  404: {url}")
                return (url, None, None)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract title
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
                # Remove the "¶" anchor symbol
                title = title.replace("¶", "").strip()

            # Find main content area (Odoo uses article or main content div)
            content = (
                soup.find("article", class_="doc-body") or
                soup.find("div", class_="doc-body") or
                soup.find("article") or
                soup.find("main") or
                soup.find("div", {"role": "main"}) or
                soup.find("div", class_="document")
            )

            if not content:
                # Fallback: use body
                content = soup.find("body")

            if not content:
                log.warning(f"  No content found: {url}")
                return (url, None, None)

            # Remove navigation, sidebar, footer, breadcrumbs
            for tag in content.find_all(["nav", "footer", "aside"]):
                tag.decompose()
            for cls in ["sidebar", "breadcrumb", "toc-backref", "headerlink",
                        "o_page_nav", "o_page_toc", "d-print-none"]:
                for tag in content.find_all(class_=cls):
                    tag.decompose()
            # Remove "Edit on GitHub" links
            for a_tag in content.find_all("a", string=re.compile(r"Edit on GitHub", re.I)):
                a_tag.decompose()
            # Remove script/style tags
            for tag in content.find_all(["script", "style"]):
                tag.decompose()

            # Convert to Markdown
            markdown = md(
                str(content),
                heading_style="ATX",
                bullets="-",
                code_language="python",
                strip=["img"],  # Strip images (they'd be broken links)
                convert=["table", "pre", "code", "p", "h1", "h2", "h3", "h4", "h5", "h6",
                         "ul", "ol", "li", "a", "strong", "em", "blockquote", "div", "span",
                         "dl", "dt", "dd"]
            )

            # Clean up the markdown
            markdown = clean_markdown(markdown)

            # Add metadata header
            source_path = urlparse(url).path.replace("/documentation/19.0/", "")
            header = f"---\ntitle: \"{title}\"\nsource: {url}\npath: {source_path}\n---\n\n"
            markdown = header + markdown

            return (url, markdown, title)

        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                log.warning(f"  Retry {attempt+1}/{MAX_RETRIES} for {url}: {e}")
                time.sleep(REQUEST_DELAY * (attempt + 1))
            else:
                log.error(f"  Failed after {MAX_RETRIES} attempts: {url}: {e}")
                return (url, None, None)

    return (url, None, None)


def clean_markdown(text: str) -> str:
    """Clean up converted markdown: remove excessive whitespace, fix formatting."""
    # Remove excessive blank lines (more than 2 consecutive)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    # Remove trailing whitespace on each line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    # Fix broken markdown links
    text = re.sub(r"\[([^\]]*)\]\(\s+([^)]*)\)", r"[\1](\2)", text)
    # Remove zero-width spaces and other invisible chars
    text = text.replace("\u200b", "").replace("\u00a0", " ")
    # Normalize line endings
    text = text.replace("\r\n", "\n")
    return text.strip() + "\n"


# ─── File Output ────────────────────────────────────────────────────────────────

def url_to_filepath(url: str, section: str, output_dir: str) -> str:
    """
    Convert a URL to a local file path.
    Example: .../applications/finance/accounting.html → user/finance/accounting.md
    """
    parsed = urlparse(url)
    path = parsed.path.replace("/documentation/19.0/", "")
    path = path.replace(".html", ".md")

    # Remove "applications/" prefix for user docs (redundant with section folder)
    if section == "user" and path.startswith("applications/"):
        path = path[len("applications/"):]

    # Build final path
    filepath = os.path.join(output_dir, section, path)
    return filepath


def save_markdown(filepath: str, content: str):
    """Save markdown content to file, creating directories as needed."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


# ─── Main Crawler ───────────────────────────────────────────────────────────────

def crawl(sections: list, output_dir: str):
    """Main crawl function."""
    output_dir = os.path.abspath(output_dir)
    log.info(f"Output directory: {output_dir}")
    log.info(f"Sections to crawl: {', '.join(sections)}")

    # Step 1: Discover URLs
    section_urls = get_all_doc_urls(BASE_URL)

    # Step 2: Deep scan for additional URLs
    section_urls = discover_deep_urls(BASE_URL, section_urls)

    # Step 3: Filter sections
    if "all" not in sections:
        section_urls = {k: v for k, v in section_urls.items() if k in sections}

    total_urls = sum(len(v) for v in section_urls.values())
    log.info(f"\nStarting crawl of {total_urls} pages...")

    # Step 4: Crawl and convert
    stats = {"success": 0, "failed": 0, "skipped": 0}
    all_pages = []

    for section, urls in section_urls.items():
        for url in urls:
            all_pages.append((section, url))

    processed = 0
    for section, url in all_pages:
        processed += 1
        filepath = url_to_filepath(url, section, output_dir)

        # Skip if already downloaded (resume support)
        if os.path.exists(filepath):
            stats["skipped"] += 1
            continue

        log.info(f"[{processed}/{total_urls}] {url}")
        url, content, title = fetch_and_convert(url)

        if content:
            save_markdown(filepath, content)
            stats["success"] += 1
        else:
            stats["failed"] += 1

        time.sleep(REQUEST_DELAY)

    # Step 5: Generate index
    generate_index(output_dir, section_urls)

    # Step 6: Summary
    log.info(f"\n{'='*60}")
    log.info(f"CRAWL COMPLETE")
    log.info(f"{'='*60}")
    log.info(f"Success:  {stats['success']}")
    log.info(f"Failed:   {stats['failed']}")
    log.info(f"Skipped:  {stats['skipped']} (already existed)")
    log.info(f"Output:   {output_dir}")
    log.info(f"{'='*60}")

    # Step 7: Show file size summary
    total_size = 0
    file_count = 0
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if f.endswith(".md"):
                total_size += os.path.getsize(os.path.join(root, f))
                file_count += 1

    log.info(f"\nTotal: {file_count} Markdown files, {total_size / 1024 / 1024:.1f} MB")
    log.info(f"\nNext steps:")
    log.info(f"  1. Create a Claude Project at claude.ai")
    log.info(f"  2. Upload the files from {output_dir} to Project Knowledge")
    log.info(f"  3. Tip: Upload by section if total size exceeds limits")
    log.info(f"  4. Start asking Odoo questions with full context!")


def generate_index(output_dir: str, section_urls: dict):
    """Generate an index.md file listing all crawled pages."""
    lines = ["# Odoo 19.0 Documentation Index\n"]
    lines.append(f"*Crawled on {time.strftime('%Y-%m-%d %H:%M:%S')}*\n\n")

    for section, urls in sorted(section_urls.items()):
        lines.append(f"## {section.title()} ({len(urls)} pages)\n\n")
        for url in urls:
            path = urlparse(url).path.replace("/documentation/19.0/", "")
            name = path.replace(".html", "").replace("/", " > ").replace("_", " ").title()
            lines.append(f"- [{name}]({url})\n")
        lines.append("\n")

    index_path = os.path.join(output_dir, "INDEX.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    log.info(f"Index saved to {index_path}")


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Crawl Odoo 19.0 documentation and convert to Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Crawl everything
  python3 odoo_doc_crawler.py --sections all

  # Crawl only user docs and developer reference
  python3 odoo_doc_crawler.py --sections user developer

  # Crawl to custom directory
  python3 odoo_doc_crawler.py --output ~/odoo19_docs --sections all

Sections available:
  user         - Application user documentation (Accounting, CRM, etc.)
  developer    - Developer reference (ORM, Views, JS framework, etc.)
  contributing - Contributing guidelines
  setup        - Administration & setup guides
  all          - Everything
        """
    )
    parser.add_argument(
        "--sections", nargs="+", default=["all"],
        choices=["all", "user", "developer", "contributing", "setup"],
        help="Sections to crawl (default: all)"
    )
    parser.add_argument(
        "--output", default="./odoo_19_docs",
        help="Output directory (default: ./odoo_19_docs)"
    )
    args = parser.parse_args()
    crawl(args.sections, args.output)


if __name__ == "__main__":
    main()
