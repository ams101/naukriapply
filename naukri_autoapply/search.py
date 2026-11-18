"""Search-results scraping and job-detail extraction."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page

from .browser import BlockedPage, SessionExpired, check_blocked, is_login_page

# Naukri detail URLs end in a long numeric id:
#   https://www.naukri.com/job-listings-product-manager-acme-bengaluru-5-to-10-years-181125012345?src=...
JOB_ID_RE = re.compile(r"job-listings-[^?#]*?-(\d{9,})(?:[?#/]|$)")


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str
    posted: str
    url: str


def job_id_from_url(url: str) -> str | None:
    m = JOB_ID_RE.search(url)
    return m.group(1) if m else None


def _strip_query(url: str) -> str:
    s = urlsplit(url)
    return urlunsplit((s.scheme, s.netloc, s.path, "", ""))


# Runs in the page. Anchors on the stable part (links to /job-listings-...) and walks up to
# the enclosing card; within the card it tries several hints for each field.
_EXTRACT_CARDS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const links = Array.from(document.querySelectorAll('a[href*="job-listings-"]'));
  const txt = el => (el && (el.getAttribute('title') || el.innerText || '').trim()) || '';
  const pick = (card, sels) => {
    for (const s of sels) { const el = card.querySelector(s); const t = txt(el); if (t) return t; }
    return '';
  };
  for (const a of links) {
    const href = a.href;
    if (seen.has(href)) continue;
    let card = a.closest('[data-job-id], article, .srp-jobtuple-wrapper, .cust-job-tuple, [class*="jobTuple"], [class*="job-tuple"]');
    if (!card) { card = a; for (let i = 0; i < 5 && card.parentElement; i++) card = card.parentElement; }
    seen.add(href);
    out.push({
      href,
      dataJobId: card.getAttribute('data-job-id') || '',
      title: txt(a),
      company: pick(card, ['a.comp-name', '[class*="comp-name"]', 'a[href*="-jobs-careers-"]', '[class*="companyInfo"] a', '[class*="company"]']),
      location: pick(card, ['[class*="locWdth"]', '[class*="loc-wrap"] [title]', '[class*="location"]', '[class*="loc"]']),
      posted: pick(card, ['[class*="job-post-day"]', '[class*="postedDate"]', '[class*="post-day"]', '[class*="date"]']),
    });
  }
  return out;
}
"""


def _ensure_ok(page: Page) -> None:
    if is_login_page(page):
        raise SessionExpired("Naukri session expired mid-run (redirected to login). Re-run: python main.py login")
    check_blocked(page)


def scrape_results_page(page: Page) -> list[Job]:
    try:
        page.locator('a[href*="job-listings-"]').first.wait_for(state="attached", timeout=15000)
    except Exception:
        _ensure_ok(page)
        return []
    jobs: list[Job] = []
    for c in page.evaluate(_EXTRACT_CARDS_JS):
        jid = job_id_from_url(c["href"]) or (c["dataJobId"] or "").strip()
        if not jid:
            continue
        jobs.append(Job(
            job_id=jid,
            title=c["title"],
            company=c["company"],
            location=c["location"],
            posted=c["posted"],
            url=_strip_query(c["href"]),
        ))
    return jobs


def _page_n_url(base_url: str, n: int) -> str:
    """Naukri's page-N URL: /product-manager-jobs-2?k=... (keeps the query string)."""
    s = urlsplit(base_url)
    path = re.sub(r"-\d+$", "", s.path.rstrip("/"))
    return urlunsplit((s.scheme, s.netloc, f"{path}-{n}" if n > 1 else path, s.query, ""))


def _go_next(page: Page, base_url: str, next_n: int) -> bool:
    """Go to results page N, keeping the search filters.

    Naukri's own "Next" link (href="/product-manager-jobs-3") drops the query string, which
    would lose jobAge=7, so it's only used to detect the last page (aria-disabled="true");
    navigation always goes to the page-N URL built from the configured search URL.
    """
    nxt = page.get_by_role("link", name=re.compile(r"^\s*next\s*$", re.I))
    if nxt.count() == 0:
        nxt = page.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I))
    if nxt.count():
        el = nxt.first
        if (
            (el.get_attribute("aria-disabled") or "").lower() == "true"
            or el.get_attribute("disabled") is not None
            or "disabled" in (el.get_attribute("class") or "").lower()
        ):
            return False
    page.goto(_page_n_url(base_url, next_n), wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    return True


def iter_search_results(page: Page, search_url: str, max_pages: int) -> Iterator[list[Job]]:
    """Yields one list of jobs per results page. Stops when a page adds no new job ids."""
    page.goto(search_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    all_ids: set[str] = set()
    for n in range(1, max_pages + 1):
        _ensure_ok(page)
        jobs = scrape_results_page(page)
        fresh = [j for j in jobs if j.job_id not in all_ids]
        print(f"[search] Page {n}: {len(jobs)} listings, {len(fresh)} not seen earlier this run")
        if not fresh:
            return
        all_ids.update(j.job_id for j in fresh)
        yield fresh
        if not _go_next(page, search_url, n + 1):
            return


_JD_JS = r"""
() => {
  const sels = ['section[class*="job-desc"]', '[class*="job-desc-container"]', '[class*="JDC"]',
                '[class*="dang-inner-html"]', '[class*="jobDesc"]', '[class*="job-description"]'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.innerText.trim().length > 200) return el.innerText.trim();
  }
  const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,div,span'))
    .filter(e => /^\s*job description\s*$/i.test(e.innerText || ''));
  for (const h of heads) {
    const sec = h.closest('section') || h.parentElement;
    if (sec && sec.innerText.trim().length > 200) return sec.innerText.trim();
  }
  const main = document.querySelector('main') || document.body;
  return main.innerText.trim();
}
"""

_DETAIL_META_JS = r"""
() => {
  const t = s => { const el = document.querySelector(s); return el ? (el.getAttribute('title') || el.innerText || '').trim() : ''; };
  return {
    title: t('h1'),
    company: t('[class*="jd-header-comp-name"] a') || t('[class*="comp-name"]') || t('a[href*="-jobs-careers-"]'),
    location: t('[class*="location"] a') || t('[class*="location"]'),
  };
}
"""


def open_detail(page: Page, job: Job) -> str:
    """Navigate to the job page, fill in missing metadata, return the JD text."""
    page.goto(job.url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    _ensure_ok(page)
    try:
        meta = page.evaluate(_DETAIL_META_JS)
        job.title = job.title or meta.get("title", "")
        # Job-page header links read "TechBlocks Careers" / "Jobs in Hyderabad".
        company = re.sub(r"\s+careers$", "", meta.get("company", ""), flags=re.I)
        location = re.sub(r"^jobs in\s+", "", meta.get("location", ""), flags=re.I)
        job.company = job.company or company
        job.location = job.location or location
    except Exception:
        pass
    jd = page.evaluate(_JD_JS)
    if len(jd) < 150:
        raise BlockedPage(f"Job description not found / too short ({len(jd)} chars) on {page.url}")
    return jd
