"""
ats_adapters.py — Adapters for every Applicant Tracking System we can read.

Each adapter implements:
    fetch(session, company_name, token_or_url, cfg) -> Outcome

Outcome.status uses the shared taxonomy in utils.Status, and Outcome.jobs is
a list of RawJob. Adapters NEVER raise — every failure is classified.

Fallback order per company (implemented in main.py):
    ATS API  →  Playwright (rendered page)  →  static BeautifulSoup  →  log

Adapters with official/public JSON endpoints (preferred, most reliable):
    greenhouse, lever, ashby, workday, smartrecruiters, workable,
    recruitee, eightfold, janestreet
Systems we can *detect* but not read via a public API (phenom, icims,
successfactors, taleo/oracle, bamboohr, teamtailor) are routed to the
Playwright/HTML path by discovery.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import FetchResult, PoliteSession, Status, logger, strip_html, truncate


@dataclass
class RawJob:
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    ats: str = ""
    native_id: str = ""          # the ATS's own job id — best dedup key
    extra: dict = field(default_factory=dict)


@dataclass
class Outcome:
    status: str
    jobs: list = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in Status.HEALTHY


def _json_or_none(fr: FetchResult):
    if not fr.ok:
        return None
    try:
        return fr.response.json()
    except ValueError:
        return None


def _fail_from(fr: FetchResult, *, token_endpoint: bool = False) -> Outcome:
    """Translate a FetchResult failure into an Outcome."""
    status = fr.status
    # A 404 on an ATS board endpoint means the token is wrong/changed.
    if token_endpoint and status == Status.PAGE_NOT_FOUND:
        status = Status.INVALID_TOKEN
    return Outcome(status, detail=fr.detail)


# ============================================================================ #
# Greenhouse — https://developers.greenhouse.io/job-board.html
# ============================================================================ #

class GreenhouseAdapter:
    name = "greenhouse"
    API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        url = self.API.format(token=token)
        if cfg.get("fetch_descriptions", True):
            url += "?content=true"
        fr = session.get(url)
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "jobs" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'jobs' key")

        max_desc = int(cfg.get("max_description_length", 300))
        must = (cfg.get("title_must_contain") or "").lower()
        jobs = []
        for item in payload.get("jobs", []):
            try:
                title = (item.get("title") or "").strip()
                if not title or (must and must not in title.lower()):
                    continue
                location = (item.get("location") or {}).get("name", "") or ""
                offices = "; ".join(o.get("name", "") for o in item.get("offices", []) if o)
                if offices and offices.lower() not in location.lower():
                    location = f"{location}; {offices}".strip("; ")
                jobs.append(RawJob(
                    company=company, title=title, location=location,
                    url=item.get("absolute_url", "") or "",
                    description=truncate(strip_html(item.get("content", "")), max_desc),
                    ats=self.name, native_id=str(item.get("id", "")),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/greenhouse: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "jobs" in payload


# ============================================================================ #
# Lever — https://github.com/lever/postings-api
# ============================================================================ #

class LeverAdapter:
    name = "lever"
    API = "https://api.lever.co/v0/postings/{token}?mode=json"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, list):
            return Outcome(Status.API_UNAVAILABLE, detail="expected a JSON list")

        max_desc = int(cfg.get("max_description_length", 300))
        jobs = []
        for item in payload:
            try:
                cats = item.get("categories") or {}
                loc = cats.get("location", "") or ""
                all_locs = item.get("workplaceType", "")
                if isinstance(item.get("categories", {}).get("allLocations"), list):
                    loc = "; ".join(item["categories"]["allLocations"]) or loc
                jobs.append(RawJob(
                    company=company, title=(item.get("text") or "").strip(),
                    location=loc or all_locs,
                    url=item.get("hostedUrl", "") or "",
                    description=truncate(strip_html(
                        item.get("descriptionPlain", "") or item.get("description", "")
                    ), max_desc),
                    ats=self.name, native_id=str(item.get("id", "")),
                    extra={"commitment": cats.get("commitment", "")},
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/lever: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        return isinstance(_json_or_none(fr), list)


# ============================================================================ #
# Ashby — https://developers.ashbyhq.com/docs/public-job-posting-api
# ============================================================================ #

class AshbyAdapter:
    name = "ashby"
    API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "jobs" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'jobs' key")

        max_desc = int(cfg.get("max_description_length", 300))
        jobs = []
        for item in payload.get("jobs", []):
            try:
                if item.get("isListed") is False:
                    continue
                locs = [item.get("location") or ""]
                for sec in item.get("secondaryLocations", []) or []:
                    locs.append(sec.get("location", ""))
                desc = item.get("descriptionPlain") or strip_html(item.get("descriptionHtml", ""))
                jobs.append(RawJob(
                    company=company, title=(item.get("title") or "").strip(),
                    location="; ".join(l for l in locs if l),
                    url=item.get("jobUrl", "") or item.get("applyUrl", "") or "",
                    description=truncate(desc, max_desc),
                    ats=self.name, native_id=str(item.get("id", "")),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/ashby: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "jobs" in payload


# ============================================================================ #
# Workday — public CXS search endpoint used by every myworkdayjobs.com site
# ============================================================================ #

class WorkdayAdapter:
    """
    token format: "tenant.wdN/SiteName", e.g. "virtu.wd5/VirtuCareers".
    Endpoint: POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    Descriptions are not fetched (would need one request per job).
    """
    name = "workday"
    PAGE = 20
    MAX_JOBS = 400

    def _parts(self, token: str):
        try:
            host_part, site = token.split("/", 1)
            tenant, wd = host_part.split(".", 1)
            return tenant, wd, site
        except ValueError:
            return None

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        parts = self._parts(token)
        if not parts:
            return Outcome(Status.INVALID_TOKEN, detail=f"bad workday token {token!r}")
        tenant, wd, site = parts
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"

        jobs, offset, total = [], 0, None
        while offset < self.MAX_JOBS:
            fr = session.post_json(api, {"appliedFacets": {}, "limit": self.PAGE,
                                         "offset": offset, "searchText": ""})
            if not fr.ok:
                return _fail_from(fr, token_endpoint=True) if not jobs else \
                    Outcome(Status.OK, jobs, detail="pagination truncated")
            payload = _json_or_none(fr)
            if not isinstance(payload, dict) or "jobPostings" not in payload:
                return Outcome(Status.API_UNAVAILABLE, detail="no 'jobPostings'") if not jobs \
                    else Outcome(Status.OK, jobs)
            total = payload.get("total", 0)
            for item in payload.get("jobPostings", []):
                try:
                    path = item.get("externalPath", "")
                    jobs.append(RawJob(
                        company=company, title=(item.get("title") or "").strip(),
                        location=item.get("locationsText", "") or "",
                        url=urljoin(base + f"/{site}/", path.lstrip("/")) if path else base,
                        ats=self.name,
                        native_id=str((item.get("bulletFields") or [""])[0] or path),
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s/workday: bad entry: %s", company, exc)
            offset += self.PAGE
            if offset >= (total or 0):
                break
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        parts = self._parts(token)
        if not parts:
            return False
        tenant, wd, site = parts
        fr = session.post_json(
            f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "jobPostings" in payload


# ============================================================================ #
# SmartRecruiters — https://developers.smartrecruiters.com (public postings API)
# ============================================================================ #

class SmartRecruitersAdapter:
    name = "smartrecruiters"
    API = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "content" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'content' key")
        jobs = []
        for item in payload.get("content", []):
            try:
                loc = item.get("location") or {}
                loc_str = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                loc.get("country")) if x)
                if loc.get("remote"):
                    loc_str = (loc_str + "; Remote").strip("; ")
                jid = item.get("id", "")
                jobs.append(RawJob(
                    company=company, title=(item.get("name") or "").strip(),
                    location=loc_str,
                    url=f"https://jobs.smartrecruiters.com/{token}/{jid}",
                    ats=self.name, native_id=str(jid),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/smartrecruiters: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "content" in payload


# ============================================================================ #
# Workable — public widget API
# ============================================================================ #

class WorkableAdapter:
    name = "workable"
    API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "jobs" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'jobs' key")
        max_desc = int(cfg.get("max_description_length", 300))
        jobs = []
        for item in payload.get("jobs", []):
            try:
                loc = ", ".join(x for x in (item.get("city"), item.get("state"),
                                            item.get("country")) if x)
                jobs.append(RawJob(
                    company=company, title=(item.get("title") or "").strip(),
                    location=loc, url=item.get("url", "") or "",
                    description=truncate(strip_html(item.get("description", "")), max_desc),
                    ats=self.name, native_id=str(item.get("shortcode", "")),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/workable: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "jobs" in payload


# ============================================================================ #
# Recruitee — public offers API (popular with Dutch firms)
# ============================================================================ #

class RecruiteeAdapter:
    name = "recruitee"
    API = "https://{token}.recruitee.com/api/offers/"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "offers" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'offers' key")
        max_desc = int(cfg.get("max_description_length", 300))
        jobs = []
        for item in payload.get("offers", []):
            try:
                jobs.append(RawJob(
                    company=company, title=(item.get("title") or "").strip(),
                    location=item.get("location", "") or item.get("city", "") or "",
                    url=item.get("careers_url", "") or "",
                    description=truncate(strip_html(item.get("description", "")), max_desc),
                    ats=self.name, native_id=str(item.get("id", "")),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/recruitee: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "offers" in payload


# ============================================================================ #
# Eightfold — public jobs API used by apply.eightfold.ai career sites
# ============================================================================ #

class EightfoldAdapter:
    name = "eightfold"
    API = "https://apply.eightfold.ai/api/apply/v2/jobs?domain={token}&num=200&start=0"

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        fr = session.get(self.API.format(token=token))
        if not fr.ok:
            return _fail_from(fr, token_endpoint=True)
        payload = _json_or_none(fr)
        if not isinstance(payload, dict) or "positions" not in payload:
            return Outcome(Status.API_UNAVAILABLE, detail="no 'positions' key")
        jobs = []
        for item in payload.get("positions", []):
            try:
                jobs.append(RawJob(
                    company=company, title=(item.get("name") or "").strip(),
                    location=item.get("location", "") or
                             "; ".join(item.get("locations", []) or []),
                    url=item.get("canonicalPositionUrl", "") or "",
                    ats=self.name, native_id=str(item.get("id", "")),
                ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s/eightfold: bad entry: %s", company, exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)

    def verify(self, session: PoliteSession, token: str) -> bool:
        fr = session.get(self.API.format(token=token))
        payload = _json_or_none(fr)
        return isinstance(payload, dict) and "positions" in payload


# ============================================================================ #
# Jane Street — their own JSON feed (defensive parsing, field names drift)
# ============================================================================ #

class JaneStreetAdapter:
    name = "janestreet"
    JOB_URL = "https://www.janestreet.com/join-jane-street/position/{id}/"

    @staticmethod
    def _first(item: dict, *keys: str) -> str:
        for key in keys:
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, list) and v:
                return ", ".join(str(x) for x in v)
        return ""

    def fetch(self, session: PoliteSession, company: str, token: str, cfg: dict) -> Outcome:
        url = token if token.startswith("http") else "https://www.janestreet.com/jobs/main.json"
        fr = session.get(url)
        if not fr.ok:
            return _fail_from(fr)
        payload = _json_or_none(fr)
        if payload is None:
            return Outcome(Status.API_UNAVAILABLE, detail="non-JSON feed")
        items = payload if isinstance(payload, list) else payload.get("jobs", [])
        max_desc = int(cfg.get("max_description_length", 300))
        jobs = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                title = self._first(item, "position", "title", "name")
                jid = str(item.get("id") or item.get("job_id") or "")
                link = self._first(item, "absolute_url", "url", "apply_url") or \
                    (self.JOB_URL.format(id=jid) if jid else "")
                if title:
                    jobs.append(RawJob(
                        company=company, title=title,
                        location=self._first(item, "city", "location", "locations", "office"),
                        url=link,
                        description=truncate(strip_html(
                            self._first(item, "overview", "description", "summary")), max_desc),
                        ats=self.name, native_id=jid,
                    ))
            except Exception as exc:  # noqa: BLE001
                logger.debug("janestreet: bad entry: %s", exc)
        return Outcome(Status.OK if jobs else Status.NO_MATCHES, jobs)


# ============================================================================ #
# Playwright — headless rendering for JavaScript-only career sites
# ============================================================================ #

class PlaywrightRenderer:
    """
    Lazily-imported wrapper around Playwright. If the package or the Chromium
    binary is missing, every call degrades to PLAYWRIGHT_UNAVAILABLE and the
    orchestrator falls back to static HTML — the tracker never hard-fails.
    """

    def __init__(self, settings: dict):
        self.enabled = bool(settings.get("enable_playwright", True))
        self.timeout_ms = int(settings.get("playwright_timeout_seconds", 35)) * 1000
        self.user_agent = settings.get("user_agent", "HFT-FPGA-Job-Tracker/2.0")
        self._checked = False
        self._available = False

    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._checked:
            return self._available
        self._checked = True
        try:
            import playwright.sync_api  # noqa: F401
            self._available = True
        except ImportError:
            logger.warning("Playwright not installed — JS-rendered sites will "
                           "fall back to static HTML (pip install playwright && "
                           "playwright install chromium)")
            self._available = False
        return self._available

    def render(self, url: str) -> tuple[str | None, str]:
        """Return (html, status). Never raises."""
        if not self.available():
            return None, Status.PLAYWRIGHT_UNAVAILABLE
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=self.user_agent)
                    page.goto(url, wait_until="networkidle", timeout=self.timeout_ms)
                    # Nudge lazy-loaded lists.
                    for _ in range(3):
                        page.mouse.wheel(0, 4000)
                        page.wait_for_timeout(700)
                    html = page.content()
                finally:
                    browser.close()
            return html, Status.OK
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "Executable doesn't exist" in msg or "browser" in msg.lower():
                logger.warning("Playwright installed but Chromium missing — run "
                               "`playwright install chromium`")
                self._available = False   # don't retry a doomed launch per company
                return None, Status.PLAYWRIGHT_UNAVAILABLE
            if "Timeout" in msg:
                return None, Status.TIMEOUT
            logger.debug("Playwright render failed for %s: %s", url, msg[:200])
            return None, Status.PARSE_FAILED


# ============================================================================ #
# Generic HTML link extraction (used on both static and rendered HTML)
# ============================================================================ #

class HtmlLinkScraper:
    """
    Last-resort extraction: collect anchors whose text looks like a job title
    containing a role term (intern/co-op/graduate) or a technical keyword.
    Locations are often unavailable here — such jobs are flagged
    'location_unknown' and handled by the filter's allow_unknown_location
    setting rather than silently dropped (coverage-first).
    """

    JS_MARKERS = ("__next_data__", "window.__", "id=\"root\"", "id=\"app\"",
                  "data-reactroot", "ng-app", "vue")

    def __init__(self, settings: dict, role_terms: list[str], tech_terms: list[str]):
        self.role_terms = [t.lower() for t in role_terms]
        self.tech_terms = [t.lower() for t in tech_terms]

    def looks_js_only(self, html: str, anchors_found: int) -> bool:
        if anchors_found >= 5:
            return False
        low = html.lower()
        return any(m in low for m in self.JS_MARKERS)

    def extract(self, company: str, html: str, base_url: str) -> list[RawJob]:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: BS parse failed: %s", company, exc)
            return []
        jobs, seen = [], set()
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())
            if not text or len(text) > 140:
                continue
            tl = text.lower()
            if not (any(t in tl for t in self.role_terms)
                    or any(t in tl for t in self.tech_terms)):
                continue
            link = urljoin(base_url, a["href"])
            if link in seen or urlparse(link).scheme not in ("http", "https"):
                continue
            seen.add(link)
            location = ""
            parent = a.find_parent(["li", "tr", "article", "div"])
            if parent is not None:
                ctx = " ".join(parent.get_text(" ", strip=True).split())
                location = truncate(ctx.replace(text, "").strip(" -|•·,"), 90)
            jobs.append(RawJob(company=company, title=text, location=location,
                               url=link, ats="html"))
        return jobs


# ============================================================================ #
# Registry
# ============================================================================ #

ADAPTERS = {
    a.name: a for a in (
        GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), WorkdayAdapter(),
        SmartRecruitersAdapter(), WorkableAdapter(), RecruiteeAdapter(),
        EightfoldAdapter(), JaneStreetAdapter(),
    )
}

# ATS platforms we can detect on a page but can only scrape via the browser.
BROWSER_ONLY_ATS = {"phenom", "icims", "successfactors", "taleo",
                    "oraclecloud", "bamboohr", "teamtailor"}
