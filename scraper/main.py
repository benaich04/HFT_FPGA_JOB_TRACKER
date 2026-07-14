"""
main.py — Orchestrator (V2).

Per-company strategy (requirement: API → Playwright → BeautifulSoup → log):

    1. Known source (self-heal override > config hint) → ATS API adapter.
       INVALID_TOKEN drops the stale mapping and continues down the chain.
    2. Discovery on the static careers page (404 → conventional alternate
       paths) → detected ATS API adapter. Successes are persisted.
    3. Bounded token guessing verified against Greenhouse/Lever/Ashby.
    4. Playwright renders the careers page; the rendered HTML is re-scanned
       for ATS signatures (an embedded board often only appears after JS),
       otherwise job links are extracted from the rendered DOM.
    5. Static BeautifulSoup link extraction as the final fallback.
    6. A precisely classified failure (INVALID_TOKEN / ROBOTS_BLOCKED /
       CLOUDFLARE_BLOCKED / JS_REQUIRED / TIMEOUT / ...) is recorded and
       surfaced in the README — never a silent miss, never a bypass.

Run: python scraper/main.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ats_adapters import ADAPTERS, BROWSER_ONLY_ATS, HtmlLinkScraper, PlaywrightRenderer  # noqa: E402
from database import load_database, merge_jobs, save_database, snapshot_history  # noqa: E402
from discovery import (OverrideStore, alternate_careers_urls, detect_ats,  # noqa: E402
                       guess_and_verify)
from filters import JobFilter  # noqa: E402
from notify import notify_new_jobs  # noqa: E402
from report import generate_readme, write_run_report  # noqa: E402
from utils import (README_PATH, PoliteSession, Status, load_config, logger)  # noqa: E402

UTC = timezone.utc


@dataclass
class CompanyReport:
    name: str
    status: str = Status.NO_ATS_DETECTED
    detail: str = ""
    method: str = ""          # which layer produced the result
    careers_page: str = ""
    jobs_raw: int = 0
    jobs_kept: int = 0
    seconds: float = 0.0
    attempts: list = field(default_factory=list)


class Tracker:
    def __init__(self, config: dict):
        self.cfg = config
        self.settings = config["settings"]
        self.session = PoliteSession(self.settings)
        self.overrides = OverrideStore()
        self.renderer = PlaywrightRenderer(self.settings)
        kw = config["keywords"]
        role_terms = (kw.get("role_intern_terms", []) + kw.get("role_coop_terms", [])
                      + kw.get("role_graduate_terms", []))
        self.link_scraper = HtmlLinkScraper(
            self.settings, role_terms,
            kw.get("technical_core", []) + ["hardware"])
        self.job_filter = JobFilter(config)

    # ------------------------------------------------------------------ #
    # Fetch layers
    # ------------------------------------------------------------------ #

    def _run_adapter(self, report: CompanyReport, company: dict,
                     ats: str, token: str, origin: str):
        adapter = ADAPTERS.get(ats)
        if adapter is None:
            report.attempts.append(f"{origin}:{ats}=no-adapter")
            return None
        outcome = adapter.fetch(self.session, company["name"], token,
                                {**self.settings, **company})
        report.attempts.append(f"{origin}:{ats}/{token}={outcome.status}")
        if outcome.ok:
            report.status, report.method = outcome.status, f"api:{ats}"
            report.detail = outcome.detail
            if origin != "hint":
                self.overrides.set(company["name"], ats, token, origin)
            return outcome.jobs
        if outcome.status == Status.INVALID_TOKEN:
            if origin == "override":
                self.overrides.invalidate(company["name"], "token stopped working")
            report.status, report.detail = outcome.status, f"{ats} '{token}' not found"
        elif report.status in (Status.NO_ATS_DETECTED, Status.INVALID_TOKEN):
            report.status, report.detail = outcome.status, outcome.detail
        return None

    def _fetch_careers_html(self, report: CompanyReport, url: str) -> str | None:
        fr = self.session.get(url, check_robots=True)
        if fr.ok:
            return fr.response.text
        report.attempts.append(f"careers:{fr.status}")
        if fr.status == Status.PAGE_NOT_FOUND:
            for alt in alternate_careers_urls(url):
                if alt.rstrip("/") == url.rstrip("/"):
                    continue
                fr2 = self.session.get(alt, check_robots=True)
                report.attempts.append(f"alt:{alt.split('//')[-1]}={fr2.status}")
                if fr2.ok:
                    report.careers_page = alt
                    logger.info("%s: careers page moved — using %s", report.name, alt)
                    return fr2.response.text
        # Record the strongest signal we have for the eventual failure status.
        if report.status == Status.NO_ATS_DETECTED:
            report.status, report.detail = fr.status, fr.detail
        return None

    def _try_detections(self, report: CompanyReport, company: dict,
                        html: str, origin: str):
        detections = detect_ats(html)
        browser_only = [name for name, tok in detections if name in BROWSER_ONLY_ATS]
        for ats, token in detections:
            if ats in BROWSER_ONLY_ATS or not token:
                continue
            jobs = self._run_adapter(report, company, ats, token, origin)
            if jobs is not None:
                return jobs
        if browser_only and report.status == Status.NO_ATS_DETECTED:
            report.status = Status.JS_REQUIRED
            report.detail = f"{browser_only[0]} platform (browser-only ATS)"
        return None

    # ------------------------------------------------------------------ #
    # Per-company chain
    # ------------------------------------------------------------------ #

    def scrape_company(self, company: dict):
        name = company["name"]
        report = CompanyReport(name=name, careers_page=company.get("careers_page", ""))
        started = time.monotonic()
        jobs = None
        try:
            # 1) Known source: override > hint
            override = self.overrides.get(name)
            if override:
                jobs = self._run_adapter(report, company, *override, "override")
            if jobs is None and company.get("ats") and company.get("token"):
                jobs = self._run_adapter(report, company, company["ats"],
                                         str(company["token"]), "hint")

            # 2) Static discovery
            html = None
            careers = company.get("careers_page", "")
            if jobs is None and careers:
                html = self._fetch_careers_html(report, careers)
                if html:
                    jobs = self._try_detections(report, company, html, "discovered")

            # 3) Verified token guessing
            disc_cfg = self.settings.get("discovery") or {}
            if jobs is None and disc_cfg.get("allow_token_guessing", True):
                guess = guess_and_verify(self.session, name,
                                         int(disc_cfg.get("max_guesses", 4)))
                if guess:
                    jobs = self._run_adapter(report, company, *guess, "guessed")
                else:
                    report.attempts.append("guessing=exhausted")

            # 4) Playwright render — never on robots-disallowed pages.
            #    (Explicit robots check: an earlier API failure may have
            #    overwritten report.status, so status alone can't be trusted.)
            if jobs is None and careers:
                target = report.careers_page or careers
                if not self.session.allowed_by_robots(target):
                    if report.status == Status.NO_ATS_DETECTED:
                        report.status = Status.ROBOTS_BLOCKED
                        report.detail = "robots.txt disallows careers page"
                    report.attempts.append("playwright=skipped(robots)")
                else:
                    rendered, rstatus = self.renderer.render(target)
                    report.attempts.append(f"playwright={rstatus}")
                    if rendered:
                        jobs = self._try_detections(report, company, rendered, "rendered")
                        if jobs is None:
                            extracted = self.link_scraper.extract(name, rendered, target)
                            if extracted:
                                jobs = extracted
                                report.status, report.method = Status.OK, "playwright:links"
                    elif rstatus == Status.PLAYWRIGHT_UNAVAILABLE and \
                            report.status in (Status.NO_ATS_DETECTED, Status.JS_REQUIRED):
                        report.status = Status.PLAYWRIGHT_UNAVAILABLE

            # 5) Static BeautifulSoup links
            if jobs is None and html:
                extracted = self.link_scraper.extract(name, html,
                                                      report.careers_page or careers)
                if extracted:
                    jobs = extracted
                    report.status, report.method = Status.OK, "html:links"
                elif self.link_scraper.looks_js_only(html, 0) and \
                        report.status == Status.NO_ATS_DETECTED:
                    report.status = Status.JS_REQUIRED
                    report.detail = "page appears JavaScript-rendered"

        except Exception as exc:  # noqa: BLE001 — one company never kills the run
            report.status, report.detail = Status.PARSE_FAILED, str(exc)[:200]
            logger.error("%s: unexpected error: %s", name, exc)

        report.seconds = round(time.monotonic() - started, 1)
        report.jobs_raw = len(jobs or [])
        return jobs or [], report


def run() -> int:
    started = time.monotonic()
    now = datetime.now(UTC)
    logger.info("=" * 70)
    logger.info("HFT FPGA Job Tracker V2 — run started %s", now.strftime("%Y-%m-%d %H:%M UTC"))

    try:
        config = load_config()
    except (FileNotFoundError, KeyError) as exc:
        logger.error("Config error: %s", exc)
        return 1

    tracker = Tracker(config)
    companies = config["companies"]
    logger.info("Scanning %d companies · min confidence %d%% · Playwright %s",
                len(companies), tracker.job_filter.min_confidence,
                "on" if tracker.renderer.available() else "off/unavailable")

    matched: list[dict] = []
    reports: list[CompanyReport] = []
    company_health: dict[str, bool] = {}
    raw_total = 0

    for company in companies:
        raw_jobs, report = tracker.scrape_company(company)
        raw_total += len(raw_jobs)

        kept = 0
        for rj in raw_jobs:
            v = tracker.job_filter.evaluate(rj.title, rj.location, rj.description)
            if not v.keep:
                continue
            kept += 1
            matched.append({
                "company": rj.company, "title": rj.title, "location": rj.location,
                "country": v.country, "type": v.job_type, "category": v.category,
                "score": v.score, "url": rj.url, "description": rj.description,
                "ats": rj.ats, "native_id": rj.native_id, "flags": v.flags,
            })
        report.jobs_kept = kept
        reports.append(report)
        # Authoritative listing ⇒ safe to treat missing jobs as removed.
        company_health[company["name"]] = (
            report.method.startswith("api:") and report.status in Status.HEALTHY
        )
        icon = "✓" if report.status in Status.HEALTHY else "✗"
        logger.info("%s %-28s %-20s raw=%-4d kept=%-3d via %s %s",
                    icon, company["name"], report.status, report.jobs_raw,
                    kept, report.method or "-",
                    f"({report.detail})" if report.detail else "")

    # ---- persist ----
    db = load_database()
    stats = merge_jobs(db, matched, company_health, now, config)
    stats["raw_postings"] = raw_total
    save_database(db)
    snapshot_history(db, stats, now)

    runtime = time.monotonic() - started
    report_dicts = [asdict(r) for r in reports]
    try:
        README_PATH.write_text(
            generate_readme(db, config, now, stats, report_dicts, runtime),
            encoding="utf-8")
        logger.info("README.md updated")
    except OSError as exc:
        logger.error("Failed to write README: %s", exc)
        return 1
    write_run_report(report_dicts, stats, now, runtime)

    new_jobs = [j for j in db["jobs"].values()
                if j.get("first_seen") == now.strftime("%Y-%m-%d %H:%M UTC")]
    notify_new_jobs(config, new_jobs)

    healthy = sum(1 for r in reports if r.status in Status.HEALTHY)
    logger.info("Done in %.0fs — %d/%d sources healthy, %d raw, +%d new, "
                "%d desc-updated, %d closed",
                runtime, healthy, len(reports), raw_total, stats["added"],
                stats["desc_updated"], stats["closed_api"] + stats["closed_stale"])
    return 0


if __name__ == "__main__":
    sys.exit(run())
