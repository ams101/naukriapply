"""Orchestrates a run: session check -> resume -> search -> per-job decide/apply -> CSV."""
from __future__ import annotations

import time

import anthropic
from playwright.sync_api import Page, sync_playwright

from . import logbook as L
from .apply_flow import AlreadyApplied, ApplyAborted, precheck, run_apply
from .browser import (
    NAUKRI_LOGIN, BlockedPage, SessionExpired, assert_logged_in, launch, new_context, screenshot,
)
from .config import Config
from .resume import get_resume_text
from .scorer import Scorer
from .search import Job, iter_search_results, open_detail


def is_blocklisted(company: str, blocklist: list[str]) -> str | None:
    """Case-insensitive, punctuation-tolerant substring match in either direction."""
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    c = norm(company)
    if not c:
        return None
    for entry in blocklist:
        e = norm(entry)
        # e in c: "Shaadi.com" matches "Shaadi.com Pvt Ltd".
        # c in e: a shortened card name like "Shaadi" still matches (only if not too short).
        if e and (e in c or (len(c) >= 6 and c in e)):
            return entry
    return None


class StopRun(Exception):
    pass


def login(cfg: Config) -> None:
    with sync_playwright() as pw:
        browser = launch(pw, cfg.browser_channel, slow_mo_ms=0)
        ctx = new_context(browser, None, cfg.page_timeout_ms)
        page = ctx.new_page()
        page.goto(NAUKRI_LOGIN, wait_until="domcontentloaded")
        print("\nA browser window has opened. Log into Naukri there (OTP etc. as usual).")
        input("When you can see your Naukri homepage, come back here and press Enter... ")
        try:
            assert_logged_in(page)
        except SessionExpired:
            print("Still looks logged out (login page shown). Nothing saved; run `python main.py login` again.")
            browser.close()
            return
        cfg.auth_state.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(cfg.auth_state))
        print(f"Session saved to {cfg.auth_state}")
        browser.close()


class Runner:
    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        self.log = L.Logbook(cfg.log_csv, cfg.reprocess_actions, dry_run)
        self.would_apply_count = 0  # dry run: simulated applies this run, for the cap
        self.last_apply_at: float | None = None
        self.counts: dict[str, int] = {}

    # ---- helpers -------------------------------------------------------------------------
    def _record(self, job: Job, action: str, reason: str, score: int | None = None) -> None:
        written = self.log.record(job, action, reason, score)
        self.counts[written] = self.counts.get(written, 0) + 1
        s = "" if score is None else f" score={score}"
        print(f"  -> {written}{s} | {job.company} | {job.title[:60]} | {reason[:120]}")

    def _applied_so_far(self) -> int:
        return self.log.applied_today + self.would_apply_count

    def _wait_for_pacing(self) -> None:
        if self.last_apply_at is None:
            return
        remaining = self.cfg.seconds_between_applies - (time.monotonic() - self.last_apply_at)
        if remaining > 0:
            print(f"  (pacing: waiting {remaining:.0f}s before next apply)")
            time.sleep(remaining)

    # ---- main ----------------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        mode = "DRY RUN (no applications will be submitted)" if self.dry_run else "LIVE"
        print(f"=== Naukri auto-apply: {mode} ===")
        if not cfg.auth_state.exists():
            raise SystemExit(f"No saved session ({cfg.auth_state.name}). Run first:  python main.py login")
        if not self.dry_run and self.log.applied_today >= cfg.daily_apply_cap:
            print(f"Daily cap already reached ({self.log.applied_today}/{cfg.daily_apply_cap} applied today). Exiting.")
            return

        with sync_playwright() as pw:
            browser = launch(pw, cfg.browser_channel, cfg.slow_mo_ms)
            ctx = new_context(browser, cfg.auth_state, cfg.page_timeout_ms)
            search_page = ctx.new_page()
            try:
                assert_logged_in(search_page)
            except SessionExpired as e:
                browser.close()
                raise SystemExit(str(e))

            detail_page = ctx.new_page()
            try:
                resume_text, source = get_resume_text(detail_page, cfg.resume_fallbacks, cfg.resume_file_first)
            except RuntimeError as e:
                browser.close()
                raise SystemExit(str(e))
            print(f"Resume: {len(resume_text)} chars from {source}")
            scorer = Scorer(resume_text, cfg.model, cfg.effort, cfg.max_jd_chars)

            try:
                for jobs in iter_search_results(search_page, cfg.search_url, cfg.max_pages):
                    for job in jobs:
                        if self.log.is_seen(job.job_id):
                            continue
                        self.process(job, detail_page, scorer)
                print("No more new listings in the search results.")
            except StopRun as e:
                print(str(e))
            except SessionExpired as e:
                print(f"\nSTOPPED: {e}")
            except BlockedPage as e:
                shot = screenshot(search_page, cfg.screenshots_dir, "search", "blocked")
                print(f"\nSTOPPED: search results page blocked ({e}). Screenshot: {shot}")
            except KeyboardInterrupt:
                print("\nInterrupted by user.")
            finally:
                self._summary()
                browser.close()

    def process(self, job: Job, page: Page, scorer: Scorer) -> None:
        cfg = self.cfg
        print(f"\n[{job.job_id}] {job.title} @ {job.company or '?'} ({job.location})")
        score = None
        try:
            hit = is_blocklisted(job.company, cfg.blocklist)
            if hit:
                self._record(job, L.BLOCKLIST, f"Company matches blocklist entry '{hit}'")
                return

            jd = open_detail(page, job)
            hit = is_blocklisted(job.company, cfg.blocklist)  # company may only be known from the detail page
            if hit:
                self._record(job, L.BLOCKLIST, f"Company matches blocklist entry '{hit}'")
                return

            result = scorer.score(job.title, job.company, jd)
            score = result.score
            if score < cfg.review_threshold:
                self._record(job, L.LOW_SCORE, result.reason, score)
                return
            if score < cfg.apply_threshold:
                self._record(job, L.FLAGGED, f"Score in review band. {result.reason}", score)
                return

            # score >= apply threshold
            if self._applied_so_far() >= cfg.daily_apply_cap:
                self._record(job, L.CAP_REACHED,
                             f"Daily cap of {cfg.daily_apply_cap} reached. {result.reason}", score)
                raise StopRun(f"Daily apply cap ({cfg.daily_apply_cap}) reached; ending run.")

            if self.dry_run:
                state = precheck(page)  # looks at buttons only; never clicks
                if state == "already_applied":
                    self._record(job, L.ALREADY_APPLIED, "Job page shows 'Applied'", score)
                elif state == "company_site":
                    self._record(job, L.FLAGGED, f"Apply is on company's external site. {result.reason}", score)
                elif state == "none":
                    self._record(job, L.ERROR, f"No Apply button found on {page.url}", score)
                else:
                    self.would_apply_count += 1
                    self._record(job, L.APPLIED,
                                 f"{result.reason} (dry run: Apply not clicked, so any recruiter "
                                 f"questions were not checked; a live run flags free-text ones)", score)
                return

            self._wait_for_pacing()
            try:
                outcome = run_apply(page, cfg.apply_defaults)
            except ApplyAborted as e:
                self._record(job, L.FLAGGED, f"{e} | {result.reason}", score)
                return
            except AlreadyApplied as e:
                self._record(job, L.ALREADY_APPLIED, str(e), score)
                return
            self.last_apply_at = time.monotonic()
            self._record(job, L.APPLIED, f"{outcome.detail}. {result.reason}", score)

        except (StopRun, SessionExpired, KeyboardInterrupt):
            raise
        except anthropic.AuthenticationError:
            raise StopRun("Anthropic API key missing or invalid (set ANTHROPIC_API_KEY). Stopping.")
        except Exception as e:  # BlockedPage, ScoringError, timeouts, selector failures
            shot = screenshot(page, cfg.screenshots_dir, job.job_id, "error")
            detail = f"{e.__class__.__name__}: {str(e)[:300]}"
            if shot:
                detail += f" | screenshot: {shot}"
            detail += f" | page: {page.url}"
            self._record(job, L.ERROR, detail, score)

    def _summary(self) -> None:
        print("\n=== Run summary ===")
        for action, n in sorted(self.counts.items()):
            print(f"  {action:32s} {n}")
        if not self.dry_run:
            print(f"  applied today (incl. earlier runs): {self.log.applied_today}/{self.cfg.daily_apply_cap}")
        print(f"Log: {self.cfg.log_csv}")
