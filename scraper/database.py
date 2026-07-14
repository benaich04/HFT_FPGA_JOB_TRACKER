"""
database.py — Job store, normalized deduplication, lifecycle, history.

V2 changes:
  * Identity prefers the ATS's own job id (survives title/URL edits), then a
    canonicalized URL, then normalized company+title+location.
  * Description hashing: a changed description marks the job UPDATED.
  * Removal policy: a job is closed immediately-ish (2 consecutive misses)
    only when its company's *API* scrape was healthy — the ATS effectively
    reports it removed. Otherwise it survives `stale_after_days` (default 45,
    per requirement ≥30) so flaky scrapes never wipe listings.
  * Closed jobs stay in jobs.json with status='closed' (hiring-trend data);
    daily snapshots land in data/history/YYYY-MM-DD.json.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse, urlencode, urlunparse

from utils import DATA_PATH, HISTORY_DIR, desc_hash, logger, norm_text

UTC = timezone.utc
DATE_FMT = "%Y-%m-%d %H:%M UTC"


# --------------------------------------------------------------------------- #
# Identity / normalization
# --------------------------------------------------------------------------- #

_KEEP_QUERY_KEYS = {"gh_jid", "lever-origin"}  # gh_jid IS the id on embedded boards


def canonical_url(url: str) -> str:
    """Strip tracking params & fragments, keep identity-bearing ones."""
    if not url:
        return ""
    p = urlparse(url)
    q = {k: v for k, v in parse_qs(p.query).items() if k in _KEEP_QUERY_KEYS}
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"),
                       "", urlencode(q, doseq=True), ""))


def job_identity(job: dict) -> str:
    """Stable id: ats:native_id > canonical url > normalized text triple."""
    if job.get("ats") and job.get("native_id"):
        base = f"{job['ats']}:{job['native_id']}"
    elif job.get("url"):
        base = canonical_url(job["url"])
    else:
        base = f"{norm_text(job.get('title', ''))}|{norm_text(job.get('location', ''))}"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{norm_text(job.get('company', ''))}-{base}".lower())
    return slug.strip("-")[:180]


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #

def load_database(path=DATA_PATH) -> dict:
    if not path.exists():
        return {"schema": 2, "jobs": {}, "last_run": None}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            db = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Job database unreadable (%s) — starting fresh", exc)
        return {"schema": 2, "jobs": {}, "last_run": None}
    db.setdefault("jobs", {})
    # v1 → v2 migration: add new fields in place.
    for job in db["jobs"].values():
        job.setdefault("status", "active")
        job.setdefault("score", 0)
        job.setdefault("category", "Hardware")
        job.setdefault("desc_hash", desc_hash(job.get("description", "")))
        job.setdefault("missing_healthy_runs", 0)
        job.setdefault("flags", [])
    db["schema"] = 2
    return db


def save_database(db: dict, path=DATA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.replace(path)
    active = sum(1 for j in db["jobs"].values() if j.get("status") == "active")
    logger.info("Database saved: %d active / %d total jobs", active, len(db["jobs"]))


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

_UPDATE_FIELDS = ("title", "location", "type", "url", "country",
                  "category", "score", "ats", "native_id")


def merge_jobs(db: dict, scraped: list[dict], company_health: dict[str, bool],
               now: datetime, cfg: dict) -> dict:
    """
    Merge scraped jobs (already filtered) into db['jobs'] in place.
    company_health[company] is True when that company's scrape this run was
    an authoritative API listing (Status healthy via an ATS adapter).
    Returns a stats dict.
    """
    settings = cfg["settings"]
    stale_days = int(settings.get("stale_after_days", 45))
    misses_to_close = int(settings.get("api_missing_runs_to_close", 2))
    now_iso = now.strftime(DATE_FMT)

    jobs = db["jobs"]
    seen: set[str] = set()
    stats = {"added": 0, "updated": 0, "desc_updated": 0,
             "closed_api": 0, "closed_stale": 0, "reopened": 0}

    for job in scraped:
        jid = job_identity(job)
        job["id"] = jid
        seen.add(jid)
        job["desc_hash"] = desc_hash(job.get("description", ""))

        if jid not in jobs:
            job.update(status="active", first_seen=now_iso, last_seen=now_iso,
                       missing_healthy_runs=0)
            job.setdefault("flags", [])
            jobs[jid] = job
            stats["added"] += 1
            logger.info("NEW  %3d%% [%s] %s — %s (%s)", job.get("score", 0),
                        job.get("category", "?"), job["company"], job["title"],
                        job.get("location", "?"))
            continue

        existing = jobs[jid]
        if existing.get("status") == "closed":
            existing["status"] = "active"
            existing.pop("closed_at", None)
            stats["reopened"] += 1
            logger.info("REOPENED: %s — %s", job["company"], job["title"])

        changed = False
        for f in _UPDATE_FIELDS:
            if job.get(f) not in (None, "", 0) and job[f] != existing.get(f):
                existing[f] = job[f]
                changed = True
        if job["desc_hash"] != existing.get("desc_hash"):
            existing["description"] = job.get("description", "")
            existing["desc_hash"] = job["desc_hash"]
            existing["updated_at"] = now_iso
            stats["desc_updated"] += 1
            logger.info("UPDATED (description): %s — %s", job["company"], job["title"])
        elif changed:
            existing["updated_at"] = now_iso
            stats["updated"] += 1
        existing["last_seen"] = now_iso
        existing["missing_healthy_runs"] = 0

    # ---- lifecycle for jobs not seen this run ----
    cutoff = now - timedelta(days=stale_days)
    for jid, job in jobs.items():
        if jid in seen or job.get("status") == "closed":
            continue
        healthy = company_health.get(job.get("company", ""), False)
        if healthy:
            job["missing_healthy_runs"] = job.get("missing_healthy_runs", 0) + 1
            if job["missing_healthy_runs"] >= misses_to_close:
                job["status"] = "closed"
                job["closed_at"] = now_iso
                job["closed_reason"] = "removed from ATS listing"
                stats["closed_api"] += 1
                logger.info("CLOSED (ATS removed): %s — %s", job["company"], job["title"])
        else:
            try:
                last_seen = datetime.strptime(job["last_seen"], DATE_FMT).replace(tzinfo=UTC)
            except (KeyError, ValueError):
                job["last_seen"] = now_iso
                continue
            if last_seen < cutoff:
                job["status"] = "closed"
                job["closed_at"] = now_iso
                job["closed_reason"] = f"unseen for {stale_days}+ days"
                stats["closed_stale"] += 1
                logger.info("CLOSED (stale %dd): %s — %s", stale_days,
                            job["company"], job["title"])
    return stats


# --------------------------------------------------------------------------- #
# History snapshots
# --------------------------------------------------------------------------- #

def snapshot_history(db: dict, run_stats: dict, now: datetime,
                     history_dir=HISTORY_DIR) -> str:
    """Write data/history/YYYY-MM-DD.json (same-day runs overwrite)."""
    history_dir.mkdir(parents=True, exist_ok=True)
    active = [j for j in db["jobs"].values() if j.get("status") == "active"]
    snap = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.strftime(DATE_FMT),
        "stats": {
            "active_total": len(active),
            "by_company": _count_by(active, "company"),
            "by_category": _count_by(active, "category"),
            "by_type": _count_by(active, "type"),
            "by_country": _count_by(active, "country"),
            **run_stats,
        },
        "active_jobs": [
            {k: j.get(k) for k in ("id", "company", "title", "location", "country",
                                   "type", "category", "score", "url", "first_seen")}
            for j in sorted(active, key=lambda x: (x.get("company", ""), x.get("title", "")))
        ],
    }
    path = history_dir / f"{snap['date']}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, ensure_ascii=False)
    logger.info("History snapshot written: %s (%d active jobs)", path.name, len(active))
    return str(path)


def _count_by(jobs: list[dict], key: str) -> dict:
    out: dict = {}
    for j in jobs:
        out[j.get(key) or "Unknown"] = out.get(j.get(key) or "Unknown", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
