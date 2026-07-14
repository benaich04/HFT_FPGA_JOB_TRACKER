"""Tests for adapters (mocked), failure taxonomy, database lifecycle, report, notify."""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))

from ats_adapters import (ADAPTERS, HtmlLinkScraper, Outcome,  # noqa: E402
                          PlaywrightRenderer)
from database import (DATE_FMT, canonical_url, job_identity, load_database,  # noqa: E402
                      merge_jobs, snapshot_history)
from report import generate_readme, write_run_report  # noqa: E402
from notify import build_message, notify_new_jobs  # noqa: E402
from utils import FetchResult, PoliteSession, Status, load_config  # noqa: E402

UTC = timezone.utc
CFG = load_config()
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers, self.text = {}, ""

    def json(self):
        return self._payload


class FakeSession:
    """Maps URL substrings → payloads; anything else 404s."""

    def __init__(self, routes: dict):
        self.routes = routes

    def _lookup(self, url):
        for frag, payload in self.routes.items():
            if frag in url:
                return FetchResult(FakeResp(payload), Status.OK)
        return FetchResult(None, Status.PAGE_NOT_FOUND, "HTTP 404")

    def get(self, url, **kw):
        return self._lookup(url)

    def post_json(self, url, payload):
        return self._lookup(url)


# --------------------------------------------------------------------------- #
# Adapters on mocked payloads
# --------------------------------------------------------------------------- #

def test_greenhouse_adapter_parses_and_filters_title():
    payload = {"jobs": [
        {"id": 1, "title": "FPGA Intern", "absolute_url": "https://x/1",
         "location": {"name": "Chicago"}, "content": "<p>Verilog &amp; RTL</p>"},
        {"id": 2, "title": "Crypto FPGA Intern", "absolute_url": "https://x/2",
         "location": {"name": "NYC"}, "content": ""},
    ]}
    s = FakeSession({"boards-api.greenhouse.io/v1/boards/acme": payload})
    out = ADAPTERS["greenhouse"].fetch(s, "Acme", "acme", dict(CFG["settings"]))
    assert out.ok and len(out.jobs) == 2
    assert out.jobs[0].native_id == "1" and "Verilog & RTL" in out.jobs[0].description

    out2 = ADAPTERS["greenhouse"].fetch(
        s, "Acme", "acme", {**CFG["settings"], "title_must_contain": "crypto"})
    assert [j.title for j in out2.jobs] == ["Crypto FPGA Intern"]


def test_greenhouse_404_becomes_invalid_token():
    out = ADAPTERS["greenhouse"].fetch(FakeSession({}), "Acme", "wrongtoken",
                                       dict(CFG["settings"]))
    assert out.status == Status.INVALID_TOKEN


def test_lever_adapter():
    payload = [{"id": "ab-1", "text": "Hardware Intern",
                "categories": {"location": "London", "commitment": "Intern"},
                "hostedUrl": "https://jobs.lever.co/acme/ab-1",
                "descriptionPlain": "FPGA work"}]
    out = ADAPTERS["lever"].fetch(FakeSession({"api.lever.co/v0/postings/acme": payload}),
                                  "Acme", "acme", dict(CFG["settings"]))
    assert out.ok and out.jobs[0].native_id == "ab-1"
    assert out.jobs[0].location == "London"


def test_ashby_adapter_skips_unlisted():
    payload = {"jobs": [
        {"id": "u1", "title": "FPGA Intern", "location": "New York",
         "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "isListed": True,
         "descriptionPlain": "RTL"},
        {"id": "u2", "title": "Hidden", "location": "X", "isListed": False},
    ]}
    out = ADAPTERS["ashby"].fetch(FakeSession({"api.ashbyhq.com/posting-api/job-board/acme": payload}),
                                  "Acme", "acme", dict(CFG["settings"]))
    assert out.ok and [j.title for j in out.jobs] == ["FPGA Intern"]


def test_workday_adapter_parses_and_builds_urls():
    payload = {"total": 2, "jobPostings": [
        {"title": "FPGA Engineer Intern", "locationsText": "Austin, TX",
         "externalPath": "/job/Austin/FPGA-Intern_R123", "bulletFields": ["R123"]},
        {"title": "HR Partner", "locationsText": "NYC",
         "externalPath": "/job/NYC/HR_R124", "bulletFields": ["R124"]},
    ]}
    s = FakeSession({"virtu.wd5.myworkdayjobs.com/wday/cxs/virtu/VirtuCareers/jobs": payload})
    out = ADAPTERS["workday"].fetch(s, "Virtu", "virtu.wd5/VirtuCareers",
                                    dict(CFG["settings"]))
    assert out.ok and len(out.jobs) == 2
    assert out.jobs[0].native_id == "R123"
    assert out.jobs[0].url.startswith("https://virtu.wd5.myworkdayjobs.com/VirtuCareers/")


def test_workday_bad_token_format():
    out = ADAPTERS["workday"].fetch(FakeSession({}), "X", "not-a-workday-token",
                                    dict(CFG["settings"]))
    assert out.status == Status.INVALID_TOKEN


# --------------------------------------------------------------------------- #
# Failure taxonomy
# --------------------------------------------------------------------------- #

def _resp(code, headers=None, body=b""):
    r = requests.models.Response()
    r.status_code = code
    r.headers.update(headers or {})
    r._content = body
    return r


def test_failure_classification():
    s = PoliteSession({"request_delay_seconds": 0})
    assert s._classify_response(_resp(403, {"cf-ray": "abc123"})).status == Status.CLOUDFLARE_BLOCKED
    assert s._classify_response(_resp(503, {}, b"Just a moment...")).status == Status.CLOUDFLARE_BLOCKED
    assert s._classify_response(_resp(403)).status == Status.FORBIDDEN
    assert s._classify_response(_resp(404)).status == Status.PAGE_NOT_FOUND
    assert s._classify_response(_resp(429)).status == Status.RATE_LIMITED
    assert s._classify_response(_resp(500)).status == Status.HTTP_ERROR
    assert s._classify_response(_resp(200)).status == Status.OK


def test_playwright_gracefully_reports_unavailable_when_missing():
    r = PlaywrightRenderer({"enable_playwright": True})
    html, status = r.render("https://example.com")
    # In CI with chromium installed this may render; without it, it must
    # degrade to the taxonomy status rather than raising.
    assert status in (Status.OK, Status.PLAYWRIGHT_UNAVAILABLE,
                      Status.TIMEOUT, Status.PARSE_FAILED)


# --------------------------------------------------------------------------- #
# Identity & dedup
# --------------------------------------------------------------------------- #

def test_identity_prefers_native_id():
    a = {"company": "Jump Trading", "ats": "greenhouse", "native_id": "123",
         "title": "FPGA Intern", "url": "https://x/1"}
    b = {"company": "Jump Trading", "ats": "greenhouse", "native_id": "123",
         "title": "FPGA Intern (Summer)", "url": "https://x/other?utm_source=z"}
    assert job_identity(a) == job_identity(b)


def test_canonical_url_strips_tracking_keeps_gh_jid():
    u = canonical_url("https://citadel.com/careers/details/?gh_jid=99&utm_source=li&ref=x")
    assert "gh_jid=99" in u and "utm" not in u and "ref=" not in u
    assert canonical_url("https://X.com/a/") == canonical_url("https://x.com/a")


def test_identity_normalizes_req_ids_and_case():
    a = {"company": "Acme", "title": "FPGA Intern (Req 12345)", "location": "NYC"}
    b = {"company": "ACME", "title": "fpga intern", "location": "nyc"}
    assert job_identity(a) == job_identity(b)


# --------------------------------------------------------------------------- #
# Merge lifecycle: add → desc-update → API-confirmed close → reopen → stale
# --------------------------------------------------------------------------- #

def _job(desc="Verilog RTL work"):
    return {"company": "Acme", "title": "FPGA Intern", "location": "Chicago, IL",
            "country": "United States", "type": "Internship", "category": "FPGA",
            "score": 95, "url": "https://x/1", "description": desc,
            "ats": "greenhouse", "native_id": "7", "flags": []}


def test_merge_lifecycle():
    db = {"schema": 2, "jobs": {}, "last_run": None}
    t0 = NOW

    s = merge_jobs(db, [_job()], {"Acme": True}, t0, CFG)
    assert s["added"] == 1 and len(db["jobs"]) == 1

    # description change → UPDATED
    s = merge_jobs(db, [_job("Now with PCIe Gen5")], {"Acme": True},
                   t0 + timedelta(hours=12), CFG)
    assert s["added"] == 0 and s["desc_updated"] == 1
    jid = next(iter(db["jobs"]))
    assert db["jobs"][jid]["updated_at"]

    # healthy API scrape missing the job: 1st miss keeps, 2nd closes
    s = merge_jobs(db, [], {"Acme": True}, t0 + timedelta(days=1), CFG)
    assert s["closed_api"] == 0 and db["jobs"][jid]["status"] == "active"
    s = merge_jobs(db, [], {"Acme": True}, t0 + timedelta(days=1, hours=12), CFG)
    assert s["closed_api"] == 1 and db["jobs"][jid]["status"] == "closed"

    # job reappears → reopened
    s = merge_jobs(db, [_job("Now with PCIe Gen5")], {"Acme": True},
                   t0 + timedelta(days=2), CFG)
    assert s["reopened"] == 1 and db["jobs"][jid]["status"] == "active"

    # UNHEALTHY source: survives 44 days, closes at 46
    s = merge_jobs(db, [], {"Acme": False}, t0 + timedelta(days=46), CFG)
    assert s["closed_stale"] == 0    # last_seen was day 2 → 44 days ago
    s = merge_jobs(db, [], {"Acme": False}, t0 + timedelta(days=48), CFG)
    assert s["closed_stale"] == 1 and db["jobs"][jid]["closed_reason"].startswith("unseen")


def test_flaky_scrape_never_wipes_jobs():
    db = {"schema": 2, "jobs": {}, "last_run": None}
    merge_jobs(db, [_job()], {"Acme": True}, NOW, CFG)
    for d in range(1, 10):   # nine consecutive failed scrapes
        merge_jobs(db, [], {"Acme": False}, NOW + timedelta(days=d), CFG)
    assert all(j["status"] == "active" for j in db["jobs"].values())


# --------------------------------------------------------------------------- #
# History, README, run report
# --------------------------------------------------------------------------- #

def _mini_db():
    db = {"schema": 2, "jobs": {}, "last_run": None}
    merge_jobs(db, [_job()], {"Acme": True}, NOW, CFG)
    return db


def test_history_snapshot():
    with tempfile.TemporaryDirectory() as td:
        path = snapshot_history(_mini_db(), {"added": 1}, NOW, Path(td))
        snap = json.loads(Path(path).read_text())
        assert snap["date"] == "2026-07-08"
        assert snap["stats"]["active_total"] == 1
        assert snap["stats"]["by_category"]["FPGA"] == 1
        assert snap["active_jobs"][0]["company"] == "Acme"


def test_readme_contains_all_required_sections():
    reports = [
        {"name": "Acme", "status": Status.OK, "detail": "", "method": "api:greenhouse",
         "careers_page": "https://acme.com/careers", "jobs_raw": 5, "jobs_kept": 1,
         "seconds": 1.0, "attempts": []},
        {"name": "Optiver", "status": Status.ROBOTS_BLOCKED, "detail": "",
         "method": "", "careers_page": "https://optiver.com/careers", "jobs_raw": 0,
         "jobs_kept": 0, "seconds": 0.5, "attempts": []},
        {"name": "BadToken Co", "status": Status.INVALID_TOKEN, "detail": "gh 'x' 404",
         "method": "", "careers_page": "", "jobs_raw": 0, "jobs_kept": 0,
         "seconds": 0.5, "attempts": []},
    ]
    stats = {"added": 1, "updated": 0, "desc_updated": 0, "closed_api": 0,
             "closed_stale": 0, "reopened": 0, "raw_postings": 5}
    md = generate_readme(_mini_db(), CFG, NOW, stats, reports, runtime_seconds=42)
    for needle in ("New Today", "New This Week", "### FPGA", "### DSP", "### ASIC",
                   "### Verification", "### Embedded", "### Firmware", "### Research",
                   "### Hardware", "Internships", "Co-op", "Graduate Programs",
                   "Statistics", "Companies scanned", "Coverage", "Runtime",
                   "Last Updated", "Check These Manually", "Optiver",
                   "ROBOTS_BLOCKED", "INVALID_TOKEN"):
        assert needle in md, f"README missing section/marker: {needle}"
    assert "FPGA Intern" in md and "95%" in md


def test_run_report_written():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "run_report.json"
        write_run_report([{"name": "Acme", "status": Status.OK}], {"added": 0},
                         NOW, 3.2, p)
        data = json.loads(p.read_text())
        assert data["companies"][0]["name"] == "Acme"
        assert data["runtime_seconds"] == 3.2


# --------------------------------------------------------------------------- #
# Notify & HTML link scraper
# --------------------------------------------------------------------------- #

def test_notify_message_and_disabled_noop():
    msg = build_message([_job()])
    assert "FPGA Intern" in msg and "Acme" in msg and "95%" in msg
    notify_new_jobs(CFG, [_job()])   # notifications disabled in config → no-op


def test_html_link_scraper():
    html = """
    <html><body>
      <ul>
        <li><a href="/jobs/1">FPGA Engineering Intern</a> Chicago, IL</li>
        <li><a href="/jobs/2">Marketing Manager</a> NYC</li>
        <li><a href="/about">About us</a></li>
      </ul>
    </body></html>"""
    scraper = HtmlLinkScraper({}, ["intern"], ["fpga"])
    jobs = scraper.extract("Acme", html, "https://acme.com/careers")
    titles = [j.title for j in jobs]
    assert "FPGA Engineering Intern" in titles
    assert "About us" not in titles
    assert jobs[0].url == "https://acme.com/jobs/1"
    assert "Chicago" in jobs[0].location


def test_js_only_heuristic():
    scraper = HtmlLinkScraper({}, ["intern"], ["fpga"])
    assert scraper.looks_js_only('<div id="root"></div><script>window.__APP__={}</script>', 0)
    assert not scraper.looks_js_only("<a>FPGA Intern</a>" * 10, 10)
