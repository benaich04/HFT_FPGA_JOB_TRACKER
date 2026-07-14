"""Tests for discovery.py — ATS detection, token candidates, self-heal store."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))

from discovery import (OverrideStore, alternate_careers_urls,  # noqa: E402
                       candidate_tokens, detect_ats)


def _one(html, want_ats, want_token):
    hits = detect_ats(html)
    assert (want_ats, want_token) in hits, f"{want_ats}/{want_token} not in {hits}"


def test_detect_greenhouse_embed():
    _one('<script src="https://boards.greenhouse.io/embed/job_board?for=acmetrading">',
         "greenhouse", "acmetrading")


def test_detect_greenhouse_js_embed():
    _one("<script src='https://boards.greenhouse.io/embed/job_board/js?for=acme'>",
         "greenhouse", "acme")


def test_detect_greenhouse_new_domain():
    _one('<a href="https://job-boards.greenhouse.io/citadelsec/jobs/123">',
         "greenhouse", "citadelsec")


def test_detect_lever():
    _one('<a href="https://jobs.lever.co/wintermute-trading/abc-123">',
         "lever", "wintermute-trading")


def test_detect_ashby():
    _one('<iframe src="https://jobs.ashbyhq.com/quadrature">', "ashby", "quadrature")


def test_detect_workday():
    _one('<a href="https://virtu.wd5.myworkdayjobs.com/en-US/VirtuCareers">',
         "workday", "virtu.wd5/VirtuCareers")


def test_detect_smartrecruiters():
    _one('<a href="https://careers.smartrecruiters.com/AcmeCorp1">',
         "smartrecruiters", "AcmeCorp1")


def test_detect_workable_and_false_token_filter():
    _one('<a href="https://apply.workable.com/maven-securities/">',
         "workable", "maven-securities")
    hits = detect_ats('<script src="https://apply.workable.com/api/v1/widget/accounts/acme">')
    assert ("workable", "acme") in hits
    assert ("workable", "api") not in hits


def test_detect_recruitee_and_eightfold():
    _one('<link href="https://davinci.recruitee.com/api/offers">', "recruitee", "davinci")
    _one('<script>fetch("https://apply.eightfold.ai/api/apply/v2/jobs?domain=acme.com&num=10")</script>',
         "eightfold", "acme.com")


def test_detect_browser_only_platforms_flagged():
    hits = detect_ats('<script src="https://cdn.phenompeople.com/widget.js"></script>')
    assert ("phenom", "") in hits
    hits = detect_ats('<a href="https://careers-acme.icims.com/jobs">')
    assert ("icims", "") in hits


def test_detection_order_prefers_api_backed():
    html = ('<script src="https://cdn.phenompeople.com/x.js"></script>'
            '<a href="https://jobs.lever.co/acme/1">')
    hits = detect_ats(html)
    assert hits[0] == ("lever", "acme")


def test_candidate_tokens():
    cands = candidate_tokens("Chicago Trading Company", 6)
    assert "chicagotrading" in cands and "chicago" in cands
    assert "janestreet" in candidate_tokens("Jane Street", 4)
    assert candidate_tokens("DRW")[0] == "drw"


def test_override_store_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "discovered.json"
        s = OverrideStore(path)
        s.set("Citadel", "greenhouse", "citadelamer", "discovered")
        assert OverrideStore(path).get("Citadel") == ("greenhouse", "citadelamer")
        s.invalidate("Citadel", "token 404")
        assert OverrideStore(path).get("Citadel") is None


def test_alternate_careers_urls():
    alts = alternate_careers_urls("https://www.virtu.com/old-careers-path/")
    assert "https://www.virtu.com/careers/" in alts
    assert all(a.startswith("https://www.virtu.com/") for a in alts)
