"""
filters.py — Relevance engine (V2): vetoes, gates, confidence score, category.

Design principles (from the V1 postmortem):

  * HARD VETO beats everything. "Procurement Specialist, IT Hardware" slipped
    through V1 because a technical keyword ('hardware') overrode the title
    exclusions. In V2, business-function terms in the title cap the score at
    5 regardless of any other signal.
  * INTERNSHIP-ONLY. A job earns role points only from intern/co-op/placement
    /graduate-program signals. Without them, the maximum achievable score
    sits well below the default threshold, so experienced roles are excluded
    structurally, not by an ever-growing blocklist.
  * CONFIDENCE SCORE (0–99), thresholded via settings.min_confidence.
    Approximate anchors:
        FPGA Design Intern            ~99
        DSP Research Intern           ~97
        RTL Verification Intern       ~95
        Graduate Hardware Engineer    ~75  (if graduate programs enabled)
        Hardware Engineer (no intern) ~30  → rejected
        Procurement Specialist, IT HW   5  → rejected
  * SOFT EXCLUSIONS (software/quant/data/web...) subtract heavily unless a
    core hardware term appears in the title — so 'FPGA Software Engineer
    Intern' survives while 'Software Engineer Intern' does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from utils import logger

# Terms that must match case-sensitively (avoid 'MAC' matching 'Mac laptops').
_CASE_SENSITIVE = {"MAC"}


def _contains(haystack_lower: str, haystack_raw: str, needle: str) -> bool:
    """Keyword match: word boundaries for short tokens, case rules for MAC."""
    if needle in _CASE_SENSITIVE:
        return re.search(rf"\b{re.escape(needle)}\b", haystack_raw) is not None
    n = needle.lower().strip()
    if len(n) <= 4 and n.replace("-", "").isalnum():
        return re.search(rf"\b{re.escape(n)}\b", haystack_lower) is not None
    return n in haystack_lower


def _any(hay_l: str, hay_raw: str, needles: list[str]) -> list[str]:
    return [n for n in needles if _contains(hay_l, hay_raw, n)]


@dataclass
class Verdict:
    keep: bool
    score: int
    reason: str = ""
    country: str = ""
    job_type: str = ""       # Internship | Co-op | Graduate Program
    category: str = ""       # FPGA | DSP | ASIC | Verification | Embedded | Firmware | Research | Hardware
    flags: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Location matching
# --------------------------------------------------------------------------- #

_LOC_NOISE = re.compile(
    r"\b(greater|metro(politan)?|area|region|hq|office|onsite|on-site|hybrid|"
    r"downtown|city of)\b", re.I)


class LocationMatcher:
    def __init__(self, config: dict):
        self.countries: list[str] = config.get("countries", [])
        self.aliases = {
            c: [a.lower() for a in al]
            for c, al in (config.get("country_aliases") or {}).items()
        }
        self.regions = {
            r.lower(): members
            for r, members in (config.get("region_groups") or {}).items()
        }
        s = config["settings"]
        self.include_remote = bool(s.get("include_remote", True))

    def match(self, location: str) -> tuple[str, list[str]]:
        """
        Returns (country_or_empty, flags). 'Remote' is a pseudo-country when
        no real country can be extracted and include_remote is on. Empty
        location returns ("", ["location_unknown"]) — the caller decides.
        """
        raw = (location or "").strip()
        if not raw:
            return "", ["location_unknown"]
        low = _LOC_NOISE.sub(" ", raw.lower())

        # Specific country/city aliases first. Short codes need word
        # boundaries: 'us' must never match inside 'AUStralia', nor 'uk'
        # inside 'UKraine' — a silent-mislabel class of bug.
        for country in self.countries:
            for alias in self.aliases.get(country, []) + [country.lower()]:
                if len(alias) <= 3:
                    if re.search(rf"\b{re.escape(alias)}\b", low):
                        return country, []
                elif alias in low:
                    return country, []

        # Coarse region labels (EMEA/APAC/...) as fallback — mapped to the
        # first member country so the job is kept rather than dropped.
        for region, members in self.regions.items():
            if re.search(rf"\b{re.escape(region)}\b", low):
                for member in members:
                    if member in self.countries:
                        return member, [f"region:{region}"]

        if self.include_remote and re.search(r"\bremote\b|\bwork from home\b", low):
            return "Remote", ["remote_unspecified_country"]
        return "", []


# --------------------------------------------------------------------------- #
# Main filter / scorer
# --------------------------------------------------------------------------- #

class JobFilter:
    # Category rules, first match wins (Verification before FPGA so that
    # 'FPGA Verification Intern' is categorized as Verification).
    CATEGORY_RULES = [
        ("Verification", ["verification", "uvm", "design verification", "dv engineer"]),
        ("FPGA", ["fpga", "rtl", "verilog", "systemverilog", "system verilog",
                  "vhdl", "vivado", "quartus", "rfsoc", "hdl", "logic design"]),
        ("DSP", ["dsp", "digital signal processing", "signal processing", "fft",
                 "fir", "iir", "cordic", "adc", "dac", "sdr", "rf engineer"]),
        ("ASIC", ["asic", "tapeout", "physical design", "silicon"]),
        ("Firmware", ["firmware"]),
        ("Embedded", ["embedded"]),
        ("Research", ["research"]),
    ]

    def __init__(self, config: dict):
        kw = config["keywords"]
        s = config["settings"]
        self.core = kw.get("technical_core", [])
        self.secondary = kw.get("technical_secondary", [])
        self.hard_veto = kw.get("hard_veto", [])
        self.soft_exclude = kw.get("soft_exclude", [])
        self.seniority = kw.get("seniority_exclude", [])
        self.intern_terms = kw.get("role_intern_terms", [])
        self.coop_terms = kw.get("role_coop_terms", [])
        self.grad_terms = kw.get("role_graduate_terms", [])

        self.min_confidence = int(s.get("min_confidence", 70))
        self.include_grad = bool(s.get("include_graduate_programs", True))
        self.allow_unknown_location = bool(s.get("allow_unknown_location", True))
        self.locations = LocationMatcher(config)

    # -- helpers ---------------------------------------------------------- #

    def _role_signal(self, title_l, title, desc_l, desc):
        """Returns (job_type|'', strength) where strength: 2=title, 1=desc."""
        if _any(title_l, title, self.coop_terms):
            return "Co-op", 2
        if _any(title_l, title, self.intern_terms):
            return "Internship", 2
        if self.include_grad and _any(title_l, title, self.grad_terms):
            return "Graduate Program", 2
        if _any(desc_l, desc, self.coop_terms):
            return "Co-op", 1
        if _any(desc_l, desc, self.intern_terms):
            return "Internship", 1
        if self.include_grad and _any(desc_l, desc, self.grad_terms):
            return "Graduate Program", 1
        return "", 0

    def classify_category(self, title: str, description: str) -> str:
        tl, dl = title.lower(), (description or "").lower()
        for category, terms in self.CATEGORY_RULES:
            if _any(tl, title, terms):
                return category
        for category, terms in self.CATEGORY_RULES:
            if _any(dl, description or "", terms):
                return category
        return "Hardware"

    # -- scoring ------------------------------------------------------------ #

    def score(self, title: str, location: str, description: str) -> Verdict:
        title = (title or "").strip()
        desc = description or ""
        tl, dl = title.lower(), desc.lower()
        flags: list[str] = []

        if not title:
            return Verdict(False, 0, "empty title")

        # 1) HARD VETO — business functions. Nothing overrides this.
        veto_hits = _any(tl, title, self.hard_veto)
        if veto_hits:
            return Verdict(False, 5, f"hard veto: {veto_hits[0]!r}")

        # 2) Location gate.
        country, loc_flags = self.locations.match(location)
        flags += loc_flags
        if not country:
            if "location_unknown" in flags and self.allow_unknown_location:
                country = "Unknown"
            else:
                return Verdict(False, 0, f"location outside targets: {location!r}")

        # 3) Role signal (internship-only structure).
        job_type, role_strength = self._role_signal(tl, title, dl, desc)

        # 4) Technical signal.
        core_title = _any(tl, title, self.core)
        core_desc = [] if core_title else _any(dl, desc, self.core)
        secondary_hits = _any(tl + " " + dl, title + " " + desc, self.secondary)
        hw_title = _contains(tl, title, "hardware")

        # 5) Seniority / experienced vetoes.
        senior_hits = _any(tl, title, self.seniority)
        soft_hits = _any(tl, title, self.soft_exclude)

        # ---- assemble score ----
        pts = 0
        pts += {2: 45, 1: 18, 0: 0}[role_strength]
        if core_title:
            pts += 40
        elif core_desc:
            pts += 25
        if hw_title and not core_title:
            pts += 30 if role_strength == 2 else 15
        pts += min(len(secondary_hits) * 6, 18)
        if core_title and role_strength == 2:
            pts += 10                                   # unambiguous title synergy
        if job_type and "research" in tl:
            pts += 4
        if senior_hits:
            pts -= 60
            flags.append(f"seniority:{senior_hits[0]}")
        if soft_hits and not core_title:
            pts -= 70
            flags.append(f"soft_exclude:{soft_hits[0]}")
        if role_strength == 1 and re.search(r"\b[3-9]\+?\s*years\b", dl):
            pts -= 30
            flags.append("experienced_desc")
        if country == "Unknown":
            pts -= 10
        if country == "Remote":
            flags.append("remote")

        score = max(5, min(99, pts))
        keep = score >= self.min_confidence and bool(job_type)
        reason = "matched" if keep else (
            "no internship signal" if not job_type else f"score {score} < {self.min_confidence}"
        )
        category = self.classify_category(title, desc) if keep else ""
        if keep:
            logger.debug("KEEP %d%% [%s/%s/%s] %s @ %s",
                         score, country, job_type, category, title, location)
        return Verdict(keep, score, reason, country, job_type, category, flags)

    # Back-compat name used by main/tests.
    def evaluate(self, title: str, location: str, description: str) -> Verdict:
        return self.score(title, location, description)
