"""Tests for filters.py — scoring, vetoes, gates, locations, categories."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))

from filters import JobFilter, LocationMatcher, _contains  # noqa: E402
from utils import load_config  # noqa: E402

CFG = load_config()
F = JobFilter(CFG)
US_DESC = "Work on Verilog RTL for our ultra-low latency trading systems. PCIe, 10G Ethernet."


# --------------------------------------------------------------------------- #
# The V1 false positive — the reason V2 exists
# --------------------------------------------------------------------------- #

def test_procurement_false_positive_is_dead():
    v = F.evaluate("Procurement Specialist, IT Hardware", "Chicago, IL",
                   "Purchasing of servers, networking hardware and FPGAs for our offices")
    assert not v.keep, "V1 regression: procurement role was accepted"
    assert v.score == 5
    assert "hard veto" in v.reason


def test_hard_veto_beats_every_other_signal():
    # Even an FPGA + intern title cannot survive a business-function veto.
    v = F.evaluate("FPGA Procurement Intern", "New York, NY", "")
    assert not v.keep and v.score == 5


# --------------------------------------------------------------------------- #
# Confidence-score anchors from the V2 spec
# --------------------------------------------------------------------------- #

def test_anchor_fpga_design_intern():
    v = F.evaluate("FPGA Design Intern", "Chicago, IL", US_DESC)
    assert v.keep and v.score >= 95, v
    assert v.category == "FPGA" and v.job_type == "Internship"
    assert v.country == "United States"


def test_anchor_dsp_research_intern():
    v = F.evaluate("DSP Research Intern", "London, UK", "")
    assert v.keep and v.score >= 95, v
    assert v.category == "DSP"


def test_anchor_rtl_verification_intern():
    v = F.evaluate("RTL Verification Intern", "Austin, TX", "")
    assert v.keep and v.score >= 90, v
    assert v.category == "Verification"   # Verification wins over FPGA/RTL


def test_anchor_hardware_engineer_without_internship_signal():
    v = F.evaluate("Hardware Engineer", "New York, NY",
                   "Design FPGA systems for trading")
    assert not v.keep, v
    assert v.reason == "no internship signal"
    assert v.score <= 55            # spec anchor ~30: well under threshold


def test_senior_roles_structurally_rejected():
    for title in ("Senior FPGA Engineer", "Principal Hardware Engineer",
                  "FPGA Team Lead", "Staff ASIC Engineer"):
        v = F.evaluate(title, "Chicago, IL", "")
        assert not v.keep, title


# --------------------------------------------------------------------------- #
# Soft exclusions
# --------------------------------------------------------------------------- #

def test_soft_exclude_kills_plain_swe_intern():
    v = F.evaluate("Software Engineer Intern", "New York, NY",
                   "Modern C++ on our trading systems")
    assert not v.keep


def test_core_in_title_overrides_soft_exclude():
    v = F.evaluate("FPGA Software Engineer Intern", "New York, NY", "")
    assert v.keep and v.score >= 90, v


def test_quant_intern_rejected():
    assert not F.evaluate("Quantitative Researcher Intern", "London", "").keep


# --------------------------------------------------------------------------- #
# Role gates & types
# --------------------------------------------------------------------------- #

def test_coop_and_placement_types():
    v = F.evaluate("Hardware Co-op", "Toronto, Canada", "")
    assert v.keep and v.job_type == "Co-op", v
    v = F.evaluate("FPGA Industrial Placement", "London", "")
    assert v.keep and v.job_type == "Internship"


def test_graduate_program_gating():
    v = F.evaluate("Graduate FPGA Engineer", "London", "")
    assert v.keep and v.job_type == "Graduate Program", v

    cfg2 = copy.deepcopy(CFG)
    cfg2["settings"]["include_graduate_programs"] = False
    f2 = JobFilter(cfg2)
    assert not f2.evaluate("Graduate FPGA Engineer", "London", "").keep


def test_hardware_intern_kept_without_core_term():
    v = F.evaluate("Hardware Intern", "Amsterdam, Netherlands", "")
    assert v.keep, v            # 'Hardware Internships' is an explicit target


# --------------------------------------------------------------------------- #
# Keyword matching mechanics
# --------------------------------------------------------------------------- #

def test_mac_is_case_sensitive():
    assert _contains("ethernet mac core intern", "Ethernet MAC core Intern", "MAC")
    assert not _contains("mac support intern", "Mac Support Intern", "MAC")


def test_short_keywords_use_word_boundaries():
    assert _contains("rtl intern", "RTL Intern", "rtl")
    assert not _contains("turtle intern", "Turtle Intern", "rtl")
    assert not _contains("firmware", "firmware", "fir")   # 'fir' must not hit 'firmware'


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #

def test_location_matrix():
    m = LocationMatcher(CFG)
    cases = {
        "New York, NY": "United States",
        "New York Metro": "United States",
        "London Area": "United Kingdom",
        "Greater London, Hybrid": "United Kingdom",
        "Amsterdam Region": "Netherlands",
        "Singapore HQ": "Singapore",
        "Hong Kong SAR": "Hong Kong",
        "Sydney, Australia": "Australia",     # regression: 'US' ⊄ 'AUStralia'
        "Dubai, UAE": "United Arab Emirates",
        "Zug, Switzerland": "Switzerland",
        "Tokyo": "Japan",
        "EMEA": "United Kingdom",             # region → first member
        "APAC": "Singapore",
        "Remote": "Remote",
        "Kyiv, Ukraine": "",                  # regression: 'UK' ⊄ 'UKraine'
        "Mumbai, India": "",                  # outside target list
    }
    for loc, expect in cases.items():
        got, _ = m.match(loc)
        assert got == expect, f"{loc!r}: expected {expect!r}, got {got!r}"


def test_unknown_location_kept_with_penalty():
    v = F.evaluate("FPGA Intern", "", "")
    assert v.keep and v.country == "Unknown", v
    assert "location_unknown" in v.flags
    v2 = F.evaluate("FPGA Intern", "Chicago, IL", "")
    assert v2.score > v.score              # the -10 penalty applied


# --------------------------------------------------------------------------- #
# Categorization
# --------------------------------------------------------------------------- #

def test_category_rules_priority():
    assert F.classify_category("FPGA Verification Intern", "") == "Verification"
    assert F.classify_category("FPGA Design Intern", "") == "FPGA"
    assert F.classify_category("Signal Processing Intern", "") == "DSP"
    assert F.classify_category("ASIC Design Co-op", "") == "ASIC"
    assert F.classify_category("Hardware Intern", "work on firmware") == "Firmware"
    assert F.classify_category("Hardware Intern", "") == "Hardware"
