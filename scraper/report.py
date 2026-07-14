"""
report.py — README.md generation (V2) and machine-readable run report.

README sections (per V2 spec): New Today · New This Week · By Category
(FPGA/DSP/ASIC/Verification/Embedded/Firmware/Research/Hardware) · By Type
(Internships/Co-op/Graduate Programs) · Statistics (companies scanned,
succeeded, failed with reasons, coverage %, runtime, last updated) ·
Manual-check list for robots/Cloudflare/JS-blocked companies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from utils import FAILURE_EXPLANATIONS, RUN_REPORT_PATH, Status

UTC = timezone.utc
DATE_FMT = "%Y-%m-%d %H:%M UTC"

CATEGORY_ORDER = ["FPGA", "DSP", "ASIC", "Verification", "Embedded",
                  "Firmware", "Research", "Hardware"]
TYPE_ORDER = ["Internship", "Co-op", "Graduate Program"]


def _esc(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _first_seen(job: dict) -> datetime:
    try:
        return datetime.strptime(job["first_seen"], DATE_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return datetime(1970, 1, 1, tzinfo=UTC)


def _table(jobs: list[dict], now: datetime) -> list[str]:
    lines = [
        "| Score | Company | Position | Location | Type | Category | Found | Link |",
        "|------:|---------|----------|----------|------|----------|-------|------|",
    ]
    day_ago = now - timedelta(hours=24)
    for j in jobs:
        badge = ""
        if _first_seen(j) >= day_ago:
            badge = " 🆕"
        elif j.get("updated_at") and j.get("updated_at", "") >= (now - timedelta(hours=24)).strftime(DATE_FMT):
            badge = " ✏️"
        link = f"[Apply]({j['url']})" if j.get("url") else "—"
        lines.append(
            f"| {j.get('score', 0)}% | {_esc(j.get('company', ''))} "
            f"| {_esc(j.get('title', ''))}{badge} | {_esc(j.get('location', '')) or '—'} "
            f"| {j.get('type', '')} | {j.get('category', '')} "
            f"| {(j.get('first_seen', '') or '').split(' ')[0]} | {link} |"
        )
    return lines


def _job_block(j: dict) -> list[str]:
    out = [
        f"**Role:** {_esc(j.get('title', ''))} &nbsp;·&nbsp; **Confidence:** {j.get('score', 0)}%",
        f"**Company:** {_esc(j.get('company', ''))}",
        f"**Location:** {_esc(j.get('location', '')) or '—'} ({j.get('country', '')})",
        f"**Type:** {j.get('type', '')} &nbsp;·&nbsp; **Category:** {j.get('category', '')}",
        f"**Found:** {j.get('first_seen', '')}",
    ]
    if j.get("description"):
        out.append(f"**About:** {_esc(j['description'])}")
    if j.get("url"):
        out.append(f"**Link:** [Apply here]({j['url']})")
    out += ["", "---", ""]
    return out


def generate_readme(db: dict, config: dict, now: datetime,
                    run_stats: dict, company_reports: list[dict],
                    runtime_seconds: float) -> str:
    active = sorted(
        [j for j in db["jobs"].values() if j.get("status") == "active"],
        key=lambda j: (-int(j.get("score", 0)), j.get("company", ""), j.get("title", "")),
    )
    day_ago, week_ago = now - timedelta(hours=24), now - timedelta(days=7)
    new_today = [j for j in active if _first_seen(j) >= day_ago]
    new_week = [j for j in active if _first_seen(j) >= week_ago]

    ok_reports = [r for r in company_reports if r["status"] in Status.HEALTHY]
    failed_reports = [r for r in company_reports if r["status"] not in Status.HEALTHY]
    manual = [r for r in failed_reports if r["status"] in
              (Status.ROBOTS_BLOCKED, Status.CLOUDFLARE_BLOCKED, Status.FORBIDDEN,
               Status.JS_REQUIRED, Status.PLAYWRIGHT_UNAVAILABLE)]
    coverage = 100.0 * len(ok_reports) / max(len(company_reports), 1)

    L: list[str] = []
    L += [
        "# FPGA HFT Opportunities Tracker",
        "",
        "Automated tracker for **FPGA / DSP / Signal Processing / hardware "
        "internships, co-ops, and graduate programs** at major HFT and quant "
        "firms. Career pages and ATS APIs are re-checked every 12 hours; "
        "sources are auto-discovered and self-healing.",
        "",
        f"**Last Updated:** {now.strftime(DATE_FMT)}",
        "",
        f"**Active positions:** {len(active)} &nbsp;·&nbsp; "
        f"**New today:** {len(new_today)} &nbsp;·&nbsp; "
        f"**New this week:** {len(new_week)} &nbsp;·&nbsp; "
        f"**Source coverage:** {coverage:.0f}% "
        f"({len(ok_reports)}/{len(company_reports)} companies)",
        "",
    ]

    # ---------------- New Today ----------------
    L += ["## 🔥 New Today", ""]
    if new_today:
        for j in new_today:
            L += _job_block(j)
    else:
        L += ["_No new positions in the last 24 hours._", ""]

    # ---------------- New This Week ----------------
    L += ["## 🆕 New This Week", ""]
    older_new = [j for j in new_week if j not in new_today]
    if older_new:
        L += _table(older_new, now) + [""]
    elif not new_today:
        L += ["_No new positions in the last 7 days._", ""]
    else:
        L += ["_All of this week's finds are listed under New Today._", ""]

    # ---------------- By Category ----------------
    L += ["## 🗂 Positions by Category", ""]
    for cat in CATEGORY_ORDER:
        subset = [j for j in active if j.get("category") == cat]
        L += [f"### {cat} ({len(subset)})", ""]
        L += (_table(subset, now) + [""]) if subset else ["_None currently._", ""]

    # ---------------- By Type ----------------
    L += ["## 🎓 Positions by Type", ""]
    for t in TYPE_ORDER:
        subset = [j for j in active if j.get("type") == t]
        label = {"Internship": "Internships", "Co-op": "Co-op",
                 "Graduate Program": "Graduate Programs"}[t]
        L += [f"### {label} ({len(subset)})", ""]
        L += (_table(subset, now) + [""]) if subset else ["_None currently._", ""]

    # ---------------- Statistics ----------------
    L += [
        "## 📊 Statistics",
        "",
        f"- **Companies scanned:** {len(company_reports)}",
        f"- **Companies succeeded:** {len(ok_reports)}",
        f"- **Companies failed:** {len(failed_reports)}",
        f"- **Coverage:** {coverage:.0f}%",
        f"- **Raw postings scanned:** {run_stats.get('raw_postings', 0)}",
        f"- **This run:** +{run_stats.get('added', 0)} new · "
        f"{run_stats.get('desc_updated', 0)} descriptions updated · "
        f"{run_stats.get('updated', 0)} fields updated · "
        f"{run_stats.get('closed_api', 0) + run_stats.get('closed_stale', 0)} closed · "
        f"{run_stats.get('reopened', 0)} reopened",
        f"- **Runtime:** {runtime_seconds:.0f}s",
        f"- **Last Updated:** {now.strftime(DATE_FMT)}",
        "",
    ]
    if failed_reports:
        L += ["<details><summary><b>Per-company source status "
              f"({len(failed_reports)} not healthy)</b></summary>", "",
              "| Company | Status | Detail | Method tried |",
              "|---------|--------|--------|--------------|"]
        for r in sorted(failed_reports, key=lambda r: r["name"]):
            expl = FAILURE_EXPLANATIONS.get(r["status"], "")
            L.append(f"| {_esc(r['name'])} | `{r['status']}` "
                     f"| {_esc(r.get('detail') or expl)} | {_esc(r.get('method', ''))} |")
        L += ["", "</details>", ""]

    # ---------------- Manual check ----------------
    if manual:
        L += ["## ⚠️ Check These Manually", "",
              "These firms block automated access (robots.txt / bot protection) "
              "or require JavaScript rendering that wasn't available this run. "
              "The tracker **respects those restrictions** rather than bypassing "
              "them — review their pages directly:", ""]
        for r in sorted(manual, key=lambda r: r["name"]):
            link = r.get("careers_page", "")
            L.append(f"- **{r['name']}** — `{r['status']}`"
                     + (f" → [careers page]({link})" if link else ""))
        L += [""]

    L += [
        "## About",
        "",
        "Sources are official company career pages and public ATS job-board "
        "APIs only. robots.txt is respected; bot protection is never bypassed; "
        "requests are rate-limited. Configuration lives in "
        "[`config.yaml`](config.yaml); setup in "
        "[`setup_instructions.md`](setup_instructions.md). Historical daily "
        "snapshots: [`data/history/`](data/history/).",
        "",
        "_Confidence scores and classifications are heuristic — verify details "
        "on the company's page before applying._",
        "",
    ]
    return "\n".join(L)


def write_run_report(company_reports: list[dict], run_stats: dict,
                     now: datetime, runtime_seconds: float,
                     path=RUN_REPORT_PATH) -> None:
    payload = {
        "generated_at": now.strftime(DATE_FMT),
        "runtime_seconds": round(runtime_seconds, 1),
        "stats": run_stats,
        "companies": company_reports,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
