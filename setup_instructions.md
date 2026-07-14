# Setup Instructions — HFT FPGA Job Tracker V2

A self-healing tracker for FPGA / DSP / Signal Processing internships at ~60
HFT and quant firms. It runs on GitHub Actions every 12 hours, rewrites
`README.md` with scored, categorized listings, and keeps daily history
snapshots. **You should never need to hand-maintain ATS tokens again** — see
[How self-healing works](#how-self-healing-works).

---

## 1. Quick start (local)

```bash
pip install -r requirements.txt
playwright install chromium        # optional but recommended (JS-rendered sites)
python scraper/main.py
```

The first run takes ~10–25 minutes (60 firms × polite 2s delays × discovery
attempts). When it finishes:

- `README.md` — full report: New Today, categories, statistics
- `data/jobs.json` — the job database
- `data/run_report.json` — machine-readable per-company status (feed this to
  your downstream resume agent)
- `data/discovered_ats.json` — auto-discovered company→ATS mappings
- `data/history/YYYY-MM-DD.json` — daily snapshot

If you skip the `playwright install` step everything still works; JS-only
career sites are just reported as `PLAYWRIGHT_UNAVAILABLE` instead of scraped.

## 2. Deploy to GitHub (auto-updates every 12h)

```bash
git init
git add .
git commit -m "FPGA job tracker v2"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/HFT_FPGA_JOB_TRACKER.git
git push -u origin main
```

Then the **one required setting** — without it the bot can't commit results:

> Repo → **Settings → Actions → General → Workflow permissions →
> "Read and write permissions" → Save**

The workflow (`.github/workflows/update_jobs.yml`) triggers on your push, so
check the **Actions** tab to watch the first run. It also installs Chromium
for Playwright automatically, and runs the test suite on every push that
touches the scraper.

Finally, edit the `user_agent` line in `config.yaml` to point at your repo
URL — it's polite to identify your bot to the sites it visits.

## 3. How self-healing works

`config.yaml` treats `ats`/`token` as **hints, not truth**:

1. A learned mapping in `data/discovered_ats.json` (if present) is tried first.
2. Then the config hint. If either returns `INVALID_TOKEN`, the stale mapping
   is dropped.
3. The careers page is fetched and scanned for ATS signatures (Greenhouse,
   Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, Eightfold —
   plus detection-only: Phenom, iCIMS, SuccessFactors, Taleo/Oracle,
   BambooHR, Teamtailor). If the page itself 404s, conventional alternate
   paths (`/careers/`, `/join-us/`, …) are tried.
4. A few tokens derived from the company name are *verified* against the
   Greenhouse/Lever/Ashby public APIs (bounded by `discovery.max_guesses`).
5. Playwright renders the page — embedded boards often only appear after
   JavaScript runs — and detection is re-run on the rendered HTML; failing
   that, job links are extracted from the rendered DOM.
6. Static BeautifulSoup link extraction is the last fallback.

Anything discovered is persisted to `data/discovered_ats.json` (committed by
the workflow, so the learned mapping survives). Companies that still fail get
a precise status in the README's statistics table instead of a silent miss.

## 4. Notifications (optional)

Enable in `config.yaml` under `settings.notifications` (set `enabled: true`
and the channels you want), then add secrets under repo **Settings → Secrets
and variables → Actions**. Notifications fire **only when new internships at
or above `notifications.min_confidence` appear**.

| Channel | Config | Secret(s) needed |
|---|---|---|
| Discord | `channels.discord.enabled: true` | `DISCORD_WEBHOOK_URL` (Server Settings → Integrations → Webhooks → New) |
| Slack | `channels.slack.enabled: true` | `SLACK_WEBHOOK_URL` (api.slack.com → Incoming Webhooks) |
| Email | `channels.email.enabled: true` + fill `smtp_host/port/to_address` | `SMTP_USERNAME`, `SMTP_PASSWORD` (for Gmail: an [App Password](https://myaccount.google.com/apppasswords), not your real password) |
| GitHub Issue | `channels.github_issue.enabled: true` | none — uses the built-in `GITHUB_TOKEN` (the workflow already requests `issues: write`) |

## 5. Configuration reference (`config.yaml → settings`)

| Setting | Default | Meaning |
|---|---|---|
| `min_confidence` | `70` | Reject jobs scoring below this (0–99). Raise to ~85 for near-zero false positives; lower to ~60 to catch borderline roles. |
| `include_graduate_programs` | `true` | Also track "Graduate FPGA Engineer"-style programs. |
| `include_remote` | `true` | Keep Remote roles that specify no country. |
| `allow_unknown_location` | `true` | Keep link-scraped jobs with no visible location (flagged, −10 score). |
| `enable_playwright` | `true` | JS rendering fallback. |
| `discovery.allow_token_guessing` | `true` | Verified name-derived token guesses (≤`max_guesses`). |
| `stale_after_days` | `45` | Close unseen jobs after this long **only when the source was unhealthy**. |
| `api_missing_runs_to_close` | `2` | When a healthy ATS listing omits a job this many consecutive runs, it's closed (the ATS effectively reports it removed). |

Adding a company is now one line — a `name` and `careers_page`; discovery
does the rest. Env knobs for fast CI/smoke runs: `TRACKER_REQUEST_DELAY`,
`TRACKER_MAX_COMPANIES`.

### How the confidence score works

Hard business-function vetoes (procurement, HR, sales, operations, …) in the
title cap a job at 5 — nothing overrides them. Otherwise points come from an
internship/co-op/graduate signal (title +45, description-only +18), core
technical terms (title +40, description +25), 'hardware' in the title (+30
with a role signal), secondary terms like Vivado/AXI/PCIe (+6 each, capped),
minus seniority (−60) and software/quant-without-hardware titles (−70).
Approximate anchors: *FPGA Design Intern* ≈ 99, *DSP Research Intern* ≈ 97–99,
*RTL Verification Intern* ≈ 95, *Hardware Engineer* (no internship signal)
≈ 30–40 → rejected, *Procurement Specialist, IT Hardware* = 5 → rejected.

## 6. Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `INVALID_TOKEN` persists for a company | Discovery couldn't find the board either — open the careers page, click a posting, and put the token from the URL (`boards.greenhouse.io/TOKEN/…`, `jobs.lever.co/TOKEN/…`) into that company's config entry as a hint. |
| `ROBOTS_BLOCKED` / `CLOUDFLARE_BLOCKED` | The firm blocks automated access. The tracker **respects this by design** and lists the firm under "Check These Manually" in the README with a direct link. |
| `JS_REQUIRED` + `PLAYWRIGHT_UNAVAILABLE` | Install the browser: `playwright install chromium` (the GitHub workflow does this automatically). |
| Workflow runs but no commit appears | Step 2's write-permissions setting is missing. |
| Scheduled runs stopped after ~60 days | GitHub pauses schedules on inactive repos; the bot's own commits count as activity, so this only happens after two idle months — re-enable in the Actions tab. |
| A real FPGA internship was rejected | Check `data/scraper.log` for its score; lower `min_confidence` or add the missing term to `keywords.technical_core`. |
| A junk job got through | Add a term to `keywords.hard_veto` (title-level, absolute). |

## 7. Honest limitations

- **Firms that block bots stay blocked.** Optiver's robots.txt (and any
  Cloudflare-challenged site) is respected — those firms are surfaced in the
  README for manual checking rather than scraped through the back door. That
  is the ceiling on "100% coverage" for a tracker that follows the rules.
- **Workday/SmartRecruiters/Eightfold listings carry no descriptions** (one
  request per job would be impolite), so their scores rely on title +
  location. Titles at these firms are descriptive enough in practice.
- **Confidence scores are heuristics.** They're tuned for HFT career boards;
  always verify on the company page before your downstream agent applies.

## 8. Changes vs V1

Auto ATS discovery + self-healing token store · Playwright fallback
(API → Playwright → BeautifulSoup → classified failure log) · hard-veto
filtering (kills the "Procurement Specialist, IT Hardware" class of false
positive) · 0–99 confidence scores with threshold · auto-categorization
(FPGA/DSP/ASIC/Verification/Embedded/Firmware/Research/Hardware) ·
~60 firms (up from 30) · description-hash UPDATED detection ·
API-confirmed removal + 45-day stale window (was 7 days) · daily history
snapshots · richer README with statistics and per-company source status ·
optional Discord/Slack/Email/GitHub-Issue notifications · 51-test suite.

**Note:** per the V2 spec, the country list now includes the **United Arab
Emirates** and no longer includes Israel. Restore any country by adding it
back to `countries:` and `country_aliases:` in `config.yaml`.
