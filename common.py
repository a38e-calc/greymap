"""Shared settings and helpers for monitor.py and pagetext.py."""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PENDING = STATE_DIR / "pending_urls.tsv"
DATA_DIR = ROOT / "docs" / "data"
STATUS = DATA_DIR / "status.json"

# Says honestly what is asking. On GitHub it includes the repository address, which
# lets a site owner recognise the requests and allow them if they wish.
_REPO = os.environ.get("GITHUB_REPOSITORY")
USER_AGENT = (
    "Mozilla/5.0 (compatible; sitemap-watch/2.0"
    + (f"; +https://github.com/{_REPO}" if _REPO else "")
    + ")"
)
ROBOTS_TOKEN = "sitemap-watch"   # the name robots.txt rules are matched against
RETRY_WAITS = (5, 20, 60)        # seconds to wait before each retry


class FetchError(Exception):
    """A download that failed. `code` is the HTTP status when there was one."""

    def __init__(self, message, code=None, detail=""):
        super().__init__(message)
        self.code = code
        self.detail = detail or message


def run_date():
    """The date to record. The workflow sets SITEMAP_DATE for scheduled runs so a
    run that starts late is still filed under the day it was scheduled for."""
    return os.environ.get("SITEMAP_DATE") or datetime.now(timezone.utc).date().isoformat()


def http_get(url, max_bytes, accept="*/*", waits=RETRY_WAITS):
    """Download a URL and return (body, content_type).

    Only http and https addresses are allowed. Temporary faults (server errors,
    timeouts, dropped connections, rate limits) are retried after a pause. A refusal
    such as 403 or 404 is not retried, because asking again will not change the answer.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise FetchError(f"could not download {url} (only http and https addresses are allowed)")

    last_error, code = None, None
    for attempt in range(len(waits) + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read(max_bytes + 1)
                content_type = response.headers.get("Content-Type", "")
            if len(body) > max_bytes:
                raise FetchError(f"could not download {url} (larger than the size limit)")
            return body, content_type
        except FetchError:
            raise
        except urllib.error.HTTPError as error:
            last_error, code = error, error.code
            if error.code < 500 and error.code not in (408, 429):
                break  # a refusal, so do not keep asking
        except Exception as error:  # noqa: BLE001 - report whatever went wrong
            last_error, code = error, None
        if attempt < len(waits):
            time.sleep(waits[attempt])
    raise FetchError(f"could not download {url} ({last_error})", code, str(last_error))


# ---- the queue of new URLs waiting to have their page text fetched ----

def read_pending():
    """Queue items as (date_found, site, url, attempts)."""
    if not PENDING.exists():
        return []
    items = []
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            attempts = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            items.append((parts[0], parts[1], parts[2], attempts))
    return items


def write_pending(items):
    STATE_DIR.mkdir(exist_ok=True)
    text = "".join("\t".join(str(part) for part in item) + "\n" for item in items)
    temp = PENDING.with_suffix(".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(PENDING)


# ---- small status file that the web page reads ----

def load_status():
    if STATUS.exists():
        return json.loads(STATUS.read_text(encoding="utf-8"))
    return {}


def save_status(status):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
