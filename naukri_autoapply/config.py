"""Load config.yaml into a plain dataclass tree."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ApplyDefaults:
    current_ctc_lpa: float = 27
    expected_ctc_lpa: float = 36
    ctc_unit_when_unspecified: str = "rupees"
    notice_period_text: str = "Immediately available / currently serving notice"
    notice_period_option_keywords: list[str] = field(default_factory=lambda: ["immediate", "serving notice"])
    relocate_answer: str = "Yes"
    years_of_experience: float = 5


@dataclass
class Config:
    base_dir: Path
    search_url: str
    max_pages: int
    apply_defaults: ApplyDefaults
    blocklist: list[str]
    model: str
    effort: str
    apply_threshold: int
    review_threshold: int
    max_jd_chars: int
    seconds_between_applies: float
    daily_apply_cap: int
    browser_channel: str
    slow_mo_ms: int
    page_timeout_ms: int
    auth_state: Path
    log_csv: Path
    screenshots_dir: Path
    resume_fallbacks: list[Path]
    reprocess_actions: set[str]


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent

    search = raw.get("search", {})
    ad = raw.get("apply_defaults", {})
    scoring = raw.get("scoring", {})
    pacing = raw.get("pacing", {})
    browser = raw.get("browser", {})
    paths = raw.get("paths", {})
    dedup = raw.get("dedup", {})

    defaults = ApplyDefaults(
        current_ctc_lpa=float(ad.get("current_ctc_lpa", 27)),
        expected_ctc_lpa=float(ad.get("expected_ctc_lpa", 36)),
        ctc_unit_when_unspecified=str(ad.get("ctc_unit_when_unspecified", "rupees")).lower(),
        notice_period_text=str(ad.get("notice_period_text", ApplyDefaults.notice_period_text)),
        notice_period_option_keywords=[str(k) for k in ad.get("notice_period_option_keywords", ["immediate"])],
        relocate_answer=str(ad.get("relocate_answer", "Yes")),
        years_of_experience=float(ad.get("years_of_experience", 5)),
    )

    def p(key: str, default: str) -> Path:
        return (base / paths.get(key, default)).resolve()

    return Config(
        base_dir=base,
        search_url=search.get("url", "https://www.naukri.com/product-manager-jobs?k=product%20manager&jobAge=7"),
        max_pages=int(search.get("max_pages", 100)),
        apply_defaults=defaults,
        blocklist=[str(b) for b in raw.get("blocklist", [])],
        model=scoring.get("model", "claude-opus-5-5"),
        effort=scoring.get("effort", "medium"),
        apply_threshold=int(scoring.get("apply_threshold", 85)),
        review_threshold=int(scoring.get("review_threshold", 70)),
        max_jd_chars=int(scoring.get("max_jd_chars", 30000)),
        seconds_between_applies=float(pacing.get("seconds_between_applies", 40)),
        daily_apply_cap=int(pacing.get("daily_apply_cap", 40)),
        browser_channel=str(browser.get("channel", "chrome") or ""),
        slow_mo_ms=int(browser.get("slow_mo_ms", 150)),
        page_timeout_ms=int(browser.get("page_timeout_ms", 30000)),
        auth_state=p("auth_state", "auth.json"),
        log_csv=p("log_csv", "applied_log.csv"),
        screenshots_dir=p("screenshots_dir", "screenshots"),
        resume_fallbacks=[(base / f).resolve() for f in paths.get("resume_fallbacks", ["resume.pdf", "resume.docx"])],
        reprocess_actions=set(dedup.get("reprocess_actions", ["skipped_daily_cap_reached"])),
    )
