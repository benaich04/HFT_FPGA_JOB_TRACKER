"""
discovery.py — Automatic ATS detection, board-token discovery, self-healing.

The V1 failure mode this module eliminates: hardcoded Greenhouse tokens that
silently rot (10 of 30 companies in the first real run). V2 policy:

  * config.yaml only *hints* at an ATS/token. Hints that fail with
    INVALID_TOKEN are ignored and rediscovered.
  * Discovery fetches the company's careers page (static first, rendered via
    Playwright second) and scans it for ATS signatures — greenhouse embed
    scripts, lever/ashby/workday/smartrecruiters/... URLs.
  * As a bounded last resort, a few token guesses derived from the company
    name are *verified* against the ATS public API (max `max_guesses`,
    GET requests against endpoints built for programmatic access).
  * Every successful discovery is persisted to data/discovered_ats.json,
    which takes precedence on future runs — the system heals itself and
    the mapping survives in git history.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ats_adapters import ADAPTERS
from utils import OVERRIDES_PATH, PoliteSession, logger

# --------------------------------------------------------------------------- #
# Detection signatures: regex -> (ats_name, token_group_builder)
# Order = adapter preference (API-backed systems first).
# --------------------------------------------------------------------------- #

_SIGNATURES: list[tuple[str, re.Pattern, callable]] = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?(?:[^\"'<>]*?)for=)?([A-Za-z0-9]+)"),
        lambda m: m.group(1)),
    ("greenhouse", re.compile(
        r"greenhouse\.io/embed/job_board/js\?[^\"'<>]*?for=([A-Za-z0-9]+)"),
        lambda m: m.group(1)),
    ("greenhouse", re.compile(
        r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9]+)"),
        lambda m: m.group(1)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9-]+)"), lambda m: m.group(1)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9-]+)"), lambda m: m.group(1)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9%\- ]+)"), lambda m: m.group(1)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9%\- ]+)"),
        lambda m: m.group(1)),
    ("workday", re.compile(
        r"https?://([a-z0-9]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)"),
        lambda m: f"{m.group(1)}.{m.group(2)}/{m.group(3)}"),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)"),
        lambda m: m.group(1)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)"),
        lambda m: m.group(1)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/[^\"'<>]*?accounts/)?([a-z0-9-]+)"),
        lambda m: m.group(1)),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com"), lambda m: m.group(1)),
    ("eightfold", re.compile(r"apply\.eightfold\.ai[^\"'<>]*?domain=([a-z0-9.\-]+)"),
        lambda m: m.group(1)),
]

# Detected-but-browser-only systems (no public read API) — informs routing/logs.
_BROWSER_ONLY_MARKERS = {
    "phenom": re.compile(r"phenom(?:people)?\.com|widget\.phenom"),
    "icims": re.compile(r"[a-z0-9-]+\.icims\.com"),
    "successfactors": re.compile(r"successfactors\.(?:com|eu)"),
    "taleo": re.compile(r"taleo\.net"),
    "oraclecloud": re.compile(r"oraclecloud\.com/hcmUI|fa\.oraclecloud"),
    "bamboohr": re.compile(r"[a-z0-9-]+\.bamboohr\.com"),
    "teamtailor": re.compile(r"[a-z0-9-]+\.teamtailor\.com"),
}

_WORKABLE_FALSE_TOKENS = {"api", "v1", "v3", "accounts", "widget"}


def detect_ats(html: str) -> list[tuple[str, str]]:
    """
    Scan HTML for ATS signatures. Returns ordered, de-duplicated
    [(ats_name, token), ...] with API-backed systems first, then
    [(browser_only_name, ""), ...] markers.
    """
    if not html:
        return []
    found: list[tuple[str, str]] = []
    seen = set()
    for ats, pattern, extract in _SIGNATURES:
        for m in pattern.finditer(html):
            token = extract(m).strip().strip("/")
            if ats == "workable" and token in _WORKABLE_FALSE_TOKENS:
                continue
            key = (ats, token.lower())
            if token and key not in seen:
                seen.add(key)
                found.append((ats, token))
    for name, pattern in _BROWSER_ONLY_MARKERS.items():
        if pattern.search(html) and (name, "") not in found:
            found.append((name, ""))
    return found


# --------------------------------------------------------------------------- #
# Token guessing (bounded, verified, opt-in)
# --------------------------------------------------------------------------- #

_SUFFIX_WORDS = {"capital", "trading", "securities", "technologies", "technology",
                 "group", "partners", "management", "asset", "financial",
                 "international", "global", "company", "fund", "derivatives",
                 "llc", "ltd", "lp", "inc", "the"}


def candidate_tokens(company_name: str, max_candidates: int = 4) -> list[str]:
    """Derive a few plausible board tokens from a company name."""
    words = re.sub(r"[^a-z0-9 ]+", " ", company_name.lower()).split()
    core = [w for w in words if w not in _SUFFIX_WORDS] or words
    cands = [
        "".join(words),            # chicagotradingcompany
        "".join(words[:-1]),       # chicagotrading  (drop last suffix word)
        "".join(core),             # chicago / janestreet
        "-".join(words),           # chicago-trading-company
        "-".join(core),            # jane-street
        core[0] if core else "",   # jane
    ]
    out, seen = [], set()
    for c in cands:
        if c and len(c) >= 3 and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:max_candidates]


def guess_and_verify(session: PoliteSession, company_name: str,
                     max_guesses: int) -> tuple[str, str] | None:
    """Try candidate tokens against Greenhouse/Lever/Ashby public APIs."""
    for token in candidate_tokens(company_name, max_guesses):
        for ats in ("greenhouse", "lever", "ashby"):
            adapter = ADAPTERS[ats]
            try:
                if adapter.verify(session, token):
                    logger.info("Discovery: guessed & verified %s token '%s' for %s",
                                ats, token, company_name)
                    return ats, token
            except Exception:  # noqa: BLE001
                continue
    return None


# --------------------------------------------------------------------------- #
# Self-healing overrides store
# --------------------------------------------------------------------------- #

class OverrideStore:
    """
    data/discovered_ats.json — the tracker's learned company→ATS mapping.
    Takes precedence over config hints; invalidated automatically when a
    stored token starts returning INVALID_TOKEN.
    """

    def __init__(self, path=OVERRIDES_PATH):
        self.path = path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                self.data: dict = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, company: str) -> tuple[str, str] | None:
        entry = self.data.get(company)
        if entry and entry.get("ats") and entry.get("token"):
            return entry["ats"], entry["token"]
        return None

    def set(self, company: str, ats: str, token: str, evidence: str) -> None:
        prev = self.data.get(company, {})
        if prev.get("ats") == ats and prev.get("token") == token:
            return
        self.data[company] = {
            "ats": ats, "token": token, "evidence": evidence,
            "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        logger.info("Self-heal: %s → %s board '%s' (%s)", company, ats, token, evidence)
        self._save()

    def invalidate(self, company: str, reason: str) -> None:
        if company in self.data:
            logger.warning("Self-heal: dropping stale mapping for %s (%s)", company, reason)
            del self.data[company]
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("Could not persist ATS overrides: %s", exc)


# --------------------------------------------------------------------------- #
# Careers-page fallback paths (self-heal for moved URLs, e.g. Virtu/Squarepoint)
# --------------------------------------------------------------------------- #

def alternate_careers_urls(careers_page: str) -> list[str]:
    """If the configured careers URL 404s, try a few conventional paths."""
    m = re.match(r"(https?://[^/]+)", careers_page or "")
    if not m:
        return []
    root = m.group(1)
    return [f"{root}/careers/", f"{root}/careers", f"{root}/join-us/",
            f"{root}/jobs/", f"{root}/careers/open-positions/"]
