"""
utils.py — Shared infrastructure for the HFT FPGA Job Tracker (V2).

New in V2:
  * A failure taxonomy (Status) so every company outcome is precisely
    classified: invalid token, robots blocked, Cloudflare blocked, timeout,
    JavaScript required, parse failure, ... instead of generic warnings.
  * FetchResult: every HTTP call returns (response, status, detail) so the
    orchestrator can make routing decisions (e.g. retry via discovery).
  * JSON POST support (needed for the Workday adapter).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
DATA_PATH = DATA_DIR / "jobs.json"
HISTORY_DIR = DATA_DIR / "history"
OVERRIDES_PATH = DATA_DIR / "discovered_ats.json"
RUN_REPORT_PATH = DATA_DIR / "run_report.json"
README_PATH = PROJECT_ROOT / "README.md"
LOG_PATH = DATA_DIR / "scraper.log"


# --------------------------------------------------------------------------- #
# Failure taxonomy — the vocabulary used everywhere in V2
# --------------------------------------------------------------------------- #

class Status:
    OK = "OK"
    NO_MATCHES = "OK_NO_MATCHES"            # scrape worked, nothing matched
    INVALID_TOKEN = "INVALID_TOKEN"          # ATS board 404s — token wrong/changed
    ROBOTS_BLOCKED = "ROBOTS_BLOCKED"        # robots.txt disallows; we comply
    CLOUDFLARE_BLOCKED = "CLOUDFLARE_BLOCKED"
    FORBIDDEN = "FORBIDDEN"                  # 403 without Cloudflare markers
    RATE_LIMITED = "RATE_LIMITED"            # 429
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    PAGE_NOT_FOUND = "PAGE_NOT_FOUND"        # careers URL itself 404s
    HTTP_ERROR = "HTTP_ERROR"
    JS_REQUIRED = "JS_REQUIRED"              # page loads but content is JS-rendered
    PLAYWRIGHT_UNAVAILABLE = "PLAYWRIGHT_UNAVAILABLE"
    API_UNAVAILABLE = "API_UNAVAILABLE"      # ATS answered but with garbage
    PARSE_FAILED = "PARSE_FAILED"
    NO_ATS_DETECTED = "NO_ATS_DETECTED"

    # Statuses that mean "the data source is authoritative and healthy".
    HEALTHY = {OK, NO_MATCHES}


FAILURE_EXPLANATIONS = {
    Status.INVALID_TOKEN: "ATS board token is wrong or has changed; discovery will retry",
    Status.ROBOTS_BLOCKED: "robots.txt disallows scraping — respected; check this company manually",
    Status.CLOUDFLARE_BLOCKED: "Cloudflare bot protection blocked the request — not bypassed; check manually",
    Status.FORBIDDEN: "server returned 403 Forbidden",
    Status.RATE_LIMITED: "server returned 429 Too Many Requests",
    Status.TIMEOUT: "request timed out",
    Status.CONNECTION_ERROR: "could not connect (DNS/network)",
    Status.PAGE_NOT_FOUND: "careers URL returned 404 — page may have moved",
    Status.JS_REQUIRED: "page content is rendered by JavaScript; needs Playwright",
    Status.PLAYWRIGHT_UNAVAILABLE: "Playwright/Chromium not installed — JS pages skipped",
    Status.API_UNAVAILABLE: "ATS endpoint answered but payload was unusable",
    Status.PARSE_FAILED: "response could not be parsed",
    Status.NO_ATS_DETECTED: "no known ATS found on the careers page",
    Status.HTTP_ERROR: "unexpected HTTP error",
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("fpga_tracker")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        logger.warning("Could not open log file at %s", LOG_PATH)
    return logger


logger = setup_logging()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    for required in ("settings", "companies", "countries", "keywords"):
        if required not in config:
            raise KeyError(f"config.yaml missing required section: '{required}'")
    # Env overrides used by tests/CI smoke runs (documented in setup guide):
    #   TRACKER_REQUEST_DELAY  — override politeness delay
    #   TRACKER_MAX_COMPANIES  — scan only the first N companies
    env_delay = os.environ.get("TRACKER_REQUEST_DELAY")
    if env_delay is not None:
        try:
            config["settings"]["request_delay_seconds"] = float(env_delay)
        except ValueError:
            pass
    env_max = os.environ.get("TRACKER_MAX_COMPANIES")
    if env_max is not None:
        try:
            config["companies"] = config["companies"][: int(env_max)]
        except ValueError:
            pass
    return config


# --------------------------------------------------------------------------- #
# HTTP with classification
# --------------------------------------------------------------------------- #

@dataclass
class FetchResult:
    """Outcome of one HTTP call: a response (maybe), a Status, and detail."""
    response: requests.Response | None
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == Status.OK and self.response is not None


def _looks_like_cloudflare(resp: requests.Response) -> bool:
    """Detect Cloudflare bot-protection responses (we report, never bypass)."""
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if "cf-ray" in headers or "cf-mitigated" in headers:
        return True
    if "cloudflare" in headers.get("server", "").lower():
        return True
    body = (resp.text or "")[:4000].lower()
    return "just a moment" in body or "_cf_chl" in body or "attention required" in body


class PoliteSession:
    """Rate-limited, retrying HTTP client that classifies every failure."""

    def __init__(self, settings: dict):
        self.delay = float(settings.get("request_delay_seconds", 2.0))
        self.timeout = float(settings.get("request_timeout_seconds", 25))
        self.respect_robots = bool(settings.get("respect_robots_txt", True))
        self.user_agent = settings.get("user_agent", "HFT-FPGA-Job-Tracker/2.0")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retry = Retry(
            total=2,
            backoff_factor=1.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._last_request_time = 0.0
        self._robots_cache: dict[str, robotparser.RobotFileParser | None] = {}

    # -- robots.txt (HTML pages only; ATS APIs are built for programmatic use)

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urlparse(url).netloc
        if host not in self._robots_cache:
            rp = robotparser.RobotFileParser()
            try:
                rp.set_url(f"{urlparse(url).scheme}://{host}/robots.txt")
                rp.read()
                self._robots_cache[host] = rp
            except Exception:  # noqa: BLE001
                self._robots_cache[host] = None
        rp = self._robots_cache[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    # -- core ----------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.monotonic()

    def _classify_response(self, resp: requests.Response) -> FetchResult:
        code = resp.status_code
        if 200 <= code < 300:
            return FetchResult(resp, Status.OK)
        if code == 404:
            return FetchResult(resp, Status.PAGE_NOT_FOUND, "HTTP 404")
        if code == 429:
            return FetchResult(resp, Status.RATE_LIMITED, "HTTP 429")
        if code in (403, 503) and _looks_like_cloudflare(resp):
            return FetchResult(resp, Status.CLOUDFLARE_BLOCKED, f"HTTP {code} + CF markers")
        if code == 403:
            return FetchResult(resp, Status.FORBIDDEN, "HTTP 403")
        return FetchResult(resp, Status.HTTP_ERROR, f"HTTP {code}")

    def _request(self, method: str, url: str, *, check_robots: bool = False,
                 json_body: dict | None = None) -> FetchResult:
        if check_robots and not self.allowed_by_robots(url):
            return FetchResult(None, Status.ROBOTS_BLOCKED, url)
        self._throttle()
        try:
            resp = self.session.request(
                method, url, timeout=self.timeout, json=json_body,
            )
            return self._classify_response(resp)
        except requests.Timeout:
            return FetchResult(None, Status.TIMEOUT, url)
        except requests.ConnectionError as exc:
            return FetchResult(None, Status.CONNECTION_ERROR, str(exc)[:160])
        except requests.RequestException as exc:
            return FetchResult(None, Status.HTTP_ERROR, str(exc)[:160])

    def get(self, url: str, *, check_robots: bool = False) -> FetchResult:
        return self._request("GET", url, check_robots=check_robots)

    def post_json(self, url: str, payload: dict) -> FetchResult:
        return self._request("POST", url, json_body=payload)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    for entity, char in (
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "),
        ("&#39;", "'"), ("&quot;", '"'), ("&rsquo;", "'"),
        ("&ndash;", "-"), ("&mdash;", "-"),
    ):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


def desc_hash(text: str) -> str:
    """Stable 16-hex-char hash of a normalized description (change detection)."""
    norm = _WS_RE.sub(" ", (text or "").lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def norm_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for identity keys."""
    text = (text or "").lower()
    text = re.sub(r"[\(\[\{].*?(req|r-|id)[\s:#-]*\d+.*?[\)\]\}]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return _WS_RE.sub(" ", text).strip()
