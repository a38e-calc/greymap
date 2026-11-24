#!/usr/bin/env python3
"""Visits the URLs queued by monitor.py and saves the cleaned text of each page.

Results go to one CSV per month in docs/data/page_text/. Each row holds the URL, the
date it was found, a status, the page title and the cleaned text, so the file can be
joined to new_urls.csv on the url column.

The fetching is deliberately gentle:
  - robots.txt is read first, and pages it disallows are skipped.
  - there is a pause between requests, a cap on pages per run, and a time limit.
  - the script says who it is in its User-Agent and does not try to look like a browser.
  - a refusal (403 and so on) is recorded and left alone, never worked around.
Anything not reached in a run stays in the queue for the next one.

Only standard Python is needed.
"""

import codecs
import csv
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from common import (
    DATA_DIR,
    ROBOTS_TOKEN,
    FetchError,
    http_get,
    load_status,
    read_pending,
    run_date,
    save_status,
    write_pending,
)

TEXT_DIR = DATA_DIR / "page_text"
COLUMNS = ["date_found", "fetched_at", "site", "url", "status", "title", "word_count", "text"]

MAX_PAGES_PER_RUN = 300         # pages visited in one run; the rest wait for the next
TIME_LIMIT_SECONDS = 30 * 60    # stop starting new pages after this long
PAUSE = 1.0                     # seconds between page requests
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
THIN_TEXT_CHARS = 200           # less text than this suggests the page needs JavaScript
MAX_ROBOTS_ATTEMPTS = 5         # runs to keep trying when robots.txt cannot be read


# ---------------------------------------------------------------------------
# Cleaning HTML into plain text
# ---------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}

# Whole elements that never hold page content worth keeping.
ALWAYS_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
               "object", "embed", "nav", "footer", "aside", "select", "button",
               "textarea", "option", "dialog"}
SKIP_ROLES = {"navigation", "contentinfo", "complementary", "search", "dialog", "alertdialog"}
# A page header or banner is dropped, unless it sits inside the main article.
SKIP_UNLESS_MAIN = {"header"}

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
PARAGRAPH_BREAK = {"p", "blockquote", "pre", "table", "ul", "ol", "figure"}
LINE_BREAK = {"div", "section", "article", "main", "tr", "dl", "dt", "dd", "details",
              "summary", "figcaption", "header", "form"}
STRUCTURAL = {"html", "body", "main", "article"}


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # entries are [tag, skipped, inside_main]
        self.title_parts = []
        self.meta_title = ""
        self.everything = []     # text from the whole page
        self.main_only = []      # text from inside <main> or <article>

    def _put(self, entry, piece):
        if entry is not None and entry[1]:
            return
        self.everything.append(piece)
        if entry is not None and entry[2]:
            self.main_only.append(piece)

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        top = self.stack[-1] if self.stack else None

        if tag == "meta":
            if a.get("property", "").lower() == "og:title" and a.get("content"):
                self.meta_title = a["content"].strip()
            return
        if tag == "br":
            self._put(top, "\n")
            return
        if tag in VOID:
            return

        role = a.get("role", "").lower()
        inside_main = bool(top and top[2]) or tag in ("main", "article") or role == "main"
        skip = bool(top and top[1])
        skip = skip or tag in ALWAYS_SKIP or role in SKIP_ROLES
        skip = skip or ((tag in SKIP_UNLESS_MAIN or role == "banner") and not inside_main)
        skip = skip or a.get("aria-hidden", "").lower() == "true" or "hidden" in a
        if tag not in STRUCTURAL:
            ident = (a.get("id", "") + " " + " ".join(a.get("class", "").split())).lower()
            skip = skip or "cookie" in ident or "consent" in ident

        entry = [tag, skip, inside_main]
        self.stack.append(entry)
        if skip:
            return
        if tag in HEADINGS:
            self._put(entry, "\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._put(entry, "\n- ")
        elif tag in PARAGRAPH_BREAK:
            self._put(entry, "\n\n")
        elif tag in LINE_BREAK:
            self._put(entry, "\n")
        elif tag in ("td", "th"):
            self._put(entry, " ")

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return
        entry = self.stack[i]
        if not entry[1]:
            if tag in HEADINGS or tag in PARAGRAPH_BREAK:
                self._put(entry, "\n\n")
            elif tag in LINE_BREAK:
                self._put(entry, "\n")
        del self.stack[i:]  # also closes anything left open inside it

    def handle_data(self, data):
        top = self.stack[-1] if self.stack else None
        if top is not None and top[0] == "title":
            self.title_parts.append(data)
            return
        if top is not None and top[1]:
            return
        self._put(top, re.sub(r"\s+", " ", data))


def _tidy(pieces):
    """Join the pieces, trim each line, drop empty runs and immediate repeats."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(pieces).split("\n")]
    out, last = [], None
    for line in lines:
        if re.fullmatch(r"[#\-|\s]*", line):  # empty, or just a bullet or hash left over
            line = ""
        if line == "":
            if out and out[-1] != "":
                out.append("")
        elif line != last:
            out.append(line)
        if line:
            last = line
    return "\n".join(out).strip()


def clean_html(html):
    """Return (title, text). Headings become # lines and list items become - lines."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip() or parser.meta_title
    main_text = _tidy(parser.main_only)
    text = main_text if len(main_text) >= THIN_TEXT_CHARS else _tidy(parser.everything)
    return title, text


def decode(body, content_type):
    """Turn the downloaded bytes into text using the page's stated character set."""
    match = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
    if not match:
        match = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.I)
        name = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    else:
        name = match.group(1)
    try:
        codecs.lookup(name)
    except LookupError:
        name = "utf-8"
    return body.decode(name, errors="replace")


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class Robots:
    def __init__(self):
        self.cache = {}

    def allowed(self, url):
        """True, False, or None when robots.txt could not be read this time."""
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.cache:
            try:
                body, _ = http_get(origin + "/robots.txt", 1_000_000, "text/plain,*/*", waits=())
                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", "replace").splitlines())
                self.cache[origin] = parser
            except FetchError as error:
                # No robots.txt (a 4xx answer) means nothing is disallowed. A server
                # fault means we cannot tell, so try again on a later run.
                is_4xx = error.code is not None and 400 <= error.code < 500
                self.cache[origin] = "allow" if is_4xx else None
        entry = self.cache[origin]
        if entry == "allow":
            return True
        if entry is None:
            return None
        return entry.can_fetch(ROBOTS_TOKEN, url)


# ---------------------------------------------------------------------------
# Output file
# ---------------------------------------------------------------------------

class MonthlyFile:
    """Appends rows to the month's CSV, creating it (with a header) on the first row."""

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.writer = None

    def write(self, row):
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists() or self.path.stat().st_size == 0
            self.handle = self.path.open("a", newline="", encoding="utf-8")
            self.writer = csv.writer(self.handle, lineterminator="\n")
            if is_new:
                self.writer.writerow(COLUMNS)
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        if self.handle is not None:
            self.handle.close()


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------

def visit(url):
    """Download and clean one page. Returns (status, title, text)."""
    try:
        body, content_type = http_get(
            url, MAX_PAGE_BYTES, "text/html,application/xhtml+xml,*/*;q=0.5", waits=(5, 20)
        )
    except FetchError as error:
        if error.code:
            return f"http_{error.code}", "", ""
        return f"error ({error.detail[:80]})", "", ""

    if content_type and "html" not in content_type.lower():
        return f"not_html ({content_type.split(';')[0].strip()})", "", ""
    try:
        title, text = clean_html(decode(body, content_type))
    except Exception as error:  # noqa: BLE001 - badly broken HTML should not stop the run
        return f"error (could not read the HTML: {str(error)[:60]})", "", ""

    status = "ok"
    if len(text) > MAX_TEXT_CHARS:
        text, status = text[:MAX_TEXT_CHARS], "ok_truncated"
    elif len(text) < THIN_TEXT_CHARS:
        status = "ok_thin"  # very little text; the page may need JavaScript to show its content
    return status, title, text


def main():
    today = run_date()
    out = MonthlyFile(TEXT_DIR / f"{today[:7]}.csv")
    pending = read_pending()
    write_pending(pending)  # makes sure the queue file exists

    robots = Robots()
    started = time.monotonic()
    visited = 0
    postponed = []

    try:
        for i, (date_found, site, url, attempts) in enumerate(pending):
            if visited >= MAX_PAGES_PER_RUN or time.monotonic() - started > TIME_LIMIT_SECONDS:
                postponed.extend(pending[i:])
                break

            permission = robots.allowed(url)
            if permission is None:
                if attempts + 1 < MAX_ROBOTS_ATTEMPTS:
                    postponed.append((date_found, site, url, attempts + 1))
                    write_pending(postponed + pending[i + 1:])
                    continue
                status, title, text = "robots_unreadable", "", ""
            elif permission is False:
                status, title, text = "blocked_by_robots", "", ""
            else:
                status, title, text = visit(url)
                visited += 1
                time.sleep(PAUSE)

            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            out.write([date_found, fetched_at, site, url, status, title, len(text.split()), text])
            write_pending(postponed + pending[i + 1:])  # progress is saved after every page
            print(f"{status:<18} {url}")
    finally:
        out.close()

    write_pending(postponed)

    status_data = load_status()
    status_data.update(
        pending=len(postponed),
        pages_visited_last_run=visited,
        text_files=sorted(p.name for p in TEXT_DIR.glob("*.csv")),
    )
    save_status(status_data)
    print(f"Visited {visited} pages; {len(postponed)} still waiting in the queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
