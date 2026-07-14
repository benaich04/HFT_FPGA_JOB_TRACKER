"""
notify.py — Optional notifications, fired ONLY when new internships appear.

Channels: Discord webhook, Slack webhook, SMTP email, GitHub Issue.
All are off by default and configured in config.yaml → settings.notifications.
Secrets come from environment variables (in GitHub Actions: repo Secrets).
Notification failures are logged and never crash a run.
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from email.mime.text import MIMEText

from utils import logger


def _job_line(j: dict) -> str:
    loc = j.get("location") or j.get("country") or "—"
    return (f"[{j.get('score', 0)}%] {j.get('company')} — {j.get('title')} "
            f"({loc}) {j.get('url', '')}".strip())


def build_message(new_jobs: list[dict]) -> str:
    head = f"🔥 {len(new_jobs)} new FPGA/DSP internship(s) found:\n\n"
    return head + "\n".join(f"• {_job_line(j)}" for j in new_jobs)


def _post_json(url: str, payload: dict, timeout: int = 15) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "HFT-FPGA-Job-Tracker/2.0"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=timeout).read()  # noqa: S310


def _discord(webhook: str, message: str) -> None:
    # Discord caps content at 2000 chars — chunk politely.
    for i in range(0, len(message), 1900):
        _post_json(webhook, {"content": message[i:i + 1900]})


def _slack(webhook: str, message: str) -> None:
    _post_json(webhook, {"text": message})


def _email(cfg: dict, subject: str, body: str) -> None:
    host = cfg.get("smtp_host", "")
    port = int(cfg.get("smtp_port", 587))
    user = os.environ.get(cfg.get("username_env", "SMTP_USERNAME"), "")
    pw = os.environ.get(cfg.get("password_env", "SMTP_PASSWORD"), "")
    sender = cfg.get("from_address") or user
    to = cfg.get("to_address", "")
    if not (host and user and pw and to):
        logger.warning("Email notification skipped: SMTP settings/env incomplete")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    with smtplib.SMTP(host, port, timeout=25) as s:
        s.starttls()
        s.login(user, pw)
        s.sendmail(sender, [to], msg.as_string())


def _github_issue(title: str, body: str) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (token and repo):
        logger.warning("GitHub Issue notification skipped: GITHUB_TOKEN/"
                       "GITHUB_REPOSITORY not set (needs `issues: write`)")
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": title, "body": body,
                         "labels": ["new-internship"]}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "HFT-FPGA-Job-Tracker/2.0"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()  # noqa: S310


def notify_new_jobs(config: dict, new_jobs: list[dict]) -> None:
    ncfg = (config.get("settings") or {}).get("notifications") or {}
    if not ncfg.get("enabled") or not new_jobs:
        return
    threshold = int(ncfg.get("min_confidence", 0))
    jobs = [j for j in new_jobs if int(j.get("score", 0)) >= threshold]
    if not jobs:
        return
    message = build_message(jobs)
    subject = f"[FPGA Tracker] {len(jobs)} new internship(s)"
    channels = ncfg.get("channels") or {}

    def _try(name: str, fn) -> None:
        try:
            fn()
            logger.info("Notification sent via %s (%d jobs)", name, len(jobs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Notification via %s failed: %s", name, str(exc)[:200])

    dc = channels.get("discord") or {}
    if dc.get("enabled"):
        hook = os.environ.get(dc.get("webhook_env", "DISCORD_WEBHOOK_URL"), "")
        if hook:
            _try("discord", lambda: _discord(hook, message))
        else:
            logger.warning("Discord enabled but %s not set",
                           dc.get("webhook_env", "DISCORD_WEBHOOK_URL"))

    sl = channels.get("slack") or {}
    if sl.get("enabled"):
        hook = os.environ.get(sl.get("webhook_env", "SLACK_WEBHOOK_URL"), "")
        if hook:
            _try("slack", lambda: _slack(hook, message))
        else:
            logger.warning("Slack enabled but %s not set",
                           sl.get("webhook_env", "SLACK_WEBHOOK_URL"))

    em = channels.get("email") or {}
    if em.get("enabled"):
        _try("email", lambda: _email(em, subject, message))

    gi = channels.get("github_issue") or {}
    if gi.get("enabled"):
        _try("github_issue", lambda: _github_issue(subject, message))
