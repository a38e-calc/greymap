#!/usr/bin/env python3
"""Daily sitemap watcher, version with page text.

Reads the sitemap addresses listed in sitemaps.txt, collects every page URL (<loc>)
in each one, and compares the result with the copy saved last time.

New and removed URLs are appended to one running file, docs/data/new_urls.csv.
New URLs are also queued in state/pending_urls.tsv so that pagetext.py can visit
them and save the cleaned text of each page.

Only standard Python is needed.
"""

import csv
import gzip
import io
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

from common import (
    DATA_DIR,
    ROOT,
    STATE_DIR,
    FetchError,
    http_get,
    load_status,
    read_pending,
    run_date,
    save_status,
    write_pending,
)

LIST_FILE = ROOT / "sitemaps.txt"
SNAP_DIR = ROOT / "snapshots"
DIFF_FILE = DATA_DIR / "new_urls.csv"

MAX_BYTES = 100 * 1024 * 1024  # refuse anything bigger than this
MAX_DEPTH = 3                  # how many levels of sitemap index to follow
PAUSE = 0.5                    # seconds between requests inside a sitemap index
MAX_QUEUE_PER_SITE = 500       # more new URLs than this from one sitemap in one run
                               # looks like a site rebuild, so no page text is fetched


def read_list():
    """Sitemap addresses from sitemaps.txt. Blank lines and # comments are ignored."""
    urls = []
    for line in LIST_FILE.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"(^|\s+)#.*$", "", line).strip()
        if line and line not in urls:
            urls.append(line)
    return urls


def site_name(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def slugify(url):
    p = urlparse(url)
    text = site_name(url) + p.path + (("?" + p.query) if p.query else "")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def fetch(url):
    """Download a sitemap, unzipping .gz content automatically."""
    data, _ = http_get(url, MAX_BYTES, "application/xml,text/xml,*/*")
    if data[:2] == b"\x1f\x8b":  # gzip marker
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            data = g.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise FetchError(f"could not download {url} (unzipped file is larger than the size limit)")
    return data


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def collect(url, seen, depth=0):
    """Return the set of page URLs in a sitemap, following sitemap indexes."""
    if url in seen:
        return set()
    seen.add(url)
    if depth > MAX_DEPTH:
        raise RuntimeError(f"sitemap index nested too deeply at {url}")

    data = fetch(url)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise RuntimeError(f"{url} is not valid XML ({error})") from error

    # Each <url> or <sitemap> element has one direct <loc> child. Looking only at
    # direct children skips things like image:loc inside image entries.
    locs = []
    for entry in root:
        for el in entry:
            if local_name(el.tag) == "loc" and el.text and el.text.strip():
                locs.append(el.text.strip())

    kind = local_name(root.tag)
    if kind == "urlset":
        return set(locs)
    if kind == "sitemapindex":
        pages = set()
        for child in locs:
            time.sleep(PAUSE)
            pages |= collect(child, seen, depth + 1)
        return pages
    raise RuntimeError(f"{url} has an unexpected top-level element <{kind}>")


def append_diff_rows(rows):
    """Add this run's rows to the end of the single running file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not DIFF_FILE.exists() or DIFF_FILE.stat().st_size == 0
    with DIFF_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        if is_new:
            writer.writerow(["date", "site", "change", "url"])
        writer.writerows(rows)


def queue_for_text(rows):
    """Queue newly added URLs for pagetext.py. Returns notices for the web page."""
    pending = read_pending()
    known = {item[2] for item in pending}

    added_by_site = {}
    for date, site, change, url in rows:
        if change == "added":
            added_by_site.setdefault(site, []).append((date, url))

    notices = []
    for site, items in added_by_site.items():
        if len(items) > MAX_QUEUE_PER_SITE:
            notices.append(
                f"{site}: {len(items)} new URLs in one run, so page text was not fetched "
                f"(limit is {MAX_QUEUE_PER_SITE}). The URLs are still in new_urls.csv."
            )
            continue
        for date, url in items:
            usable = url.lower().startswith(("http://", "https://")) and not re.search(r"\s", url)
            if usable and url not in known:
                pending.append((date, site, url, 0))
                known.add(url)

    write_pending(pending)  # also makes sure the file exists
    return notices


def main():
    today = run_date()
    SNAP_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rows, errors = [], []
    sitemaps = read_list()
    for sitemap in sitemaps:
        snapshot = SNAP_DIR / f"{slugify(sitemap)}.txt"
        site = site_name(sitemap)
        try:
            current = collect(sitemap, set())
            if not current:
                raise RuntimeError("no URLs found, so leaving the saved copy alone")
        except Exception as error:  # noqa: BLE001
            message = f"{sitemap}: {error}"
            errors.append(message)
            print(f"::warning::{message}")
            continue  # keep the last saved copy untouched

        if snapshot.exists():
            previous = set(snapshot.read_text(encoding="utf-8").splitlines()) - {""}
            added = sorted(current - previous)
            removed = sorted(previous - current)
            rows += [(today, site, "added", u) for u in added]
            rows += [(today, site, "removed", u) for u in removed]
            print(f"{sitemap}: {len(current)} URLs, {len(added)} new, {len(removed)} removed")
        else:
            print(f"{sitemap}: first run, saved {len(current)} URLs as the starting point")

        snapshot.write_text("\n".join(sorted(current)) + "\n", encoding="utf-8")

    if rows:
        append_diff_rows(rows)
    notices = queue_for_text(rows)

    status = load_status()
    status.update(
        last_checked=today,
        last_checked_at=datetime.now(timezone.utc).isoformat(timespec="minutes"),
        errors=errors,
        notices=notices,
    )
    save_status(status)

    # One unreadable sitemap is reported (warning in the run log, banner on the web
    # page) without failing the whole run. The run only fails if every sitemap failed.
    return 1 if errors and len(errors) == len(sitemaps) else 0


if __name__ == "__main__":
    sys.exit(main())
