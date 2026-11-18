"""The Chrome extension's injected functions (extension/naukri.js), run against the real saved
Naukri pages in tests/fixtures. Same scenarios as the Python tests."""
from pathlib import Path

import pytest

pw_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
# naukri.js is an ES module; as a classic script its functions become globals.
NAUKRI_JS = (ROOT / "extension" / "naukri.js").read_text(encoding="utf-8").replace("export function", "function") \
    .replace("export async function", "async function")

DEFAULTS = {
    "currentCtcLpa": 27, "expectedCtcLpa": 36, "ctcUnit": "rupees",
    "noticeText": "Immediately available / currently serving notice",
    "noticeKeywords": ["immediate", "serving notice", "15 days or less"],
    "relocateAnswer": "Yes", "yearsExperience": 5,
}


@pytest.fixture(scope="module")
def browser():
    with pw_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception:
            try:
                b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium-1243/chrome-linux64/chrome")
            except Exception as e:
                pytest.skip(f"No Chromium available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def load(page, name, questions=None):
    page.set_content((FIX / name).read_text(encoding="utf-8"))
    page.add_script_tag(content=NAUKRI_JS)
    if questions is not None:
        page.evaluate("q => { window.QUESTIONS = q; }", questions)


def apply(page, timeout_ms=30000):
    return page.evaluate(f"d => applyFlow(d, {timeout_ms})", DEFAULTS)


def test_scrape_real_results(page):
    load(page, "search_real.html")
    r = page.evaluate("scrapeResults()")
    assert len(r["jobs"]) == 20 and r["hasNext"] is True
    by_id = {j["job_id"]: j for j in r["jobs"]}
    tb = by_id["191026914203"]
    assert (tb["company"], tb["location"]) == ("TechBlocks", "Hyderabad")
    assert tb["url"].endswith("-191026914203") and "?" not in tb["url"]
    assert all(j["title"] and j["company"] and j["location"] and j["posted"] for j in r["jobs"])


@pytest.mark.parametrize("name,state,company", [
    ("job_techblocks.html", "apply", "TechBlocks"),
    ("job_shaadi.html", "company_site", "Shaadi.com"),
    ("job_infoedge.html", "already_applied", "Info Edge"),
])
def test_read_real_job_pages(page, name, state, company):
    load(page, name)
    r = page.evaluate("readJob()")
    assert r["state"] == state
    assert len(r["jd"]) > 1500
    assert r["company"] == company


def test_page_state_normal(page):
    load(page, "job_techblocks.html")
    st = page.evaluate("pageState()")
    assert st["login"] is False and st["blocked"] is None


def test_page_state_login_and_captcha(page):
    page.set_content("<html><body><form><input type=password><button>Login</button></form></body></html>")
    page.add_script_tag(content=NAUKRI_JS)
    assert page.evaluate("pageState()")["login"] is True
    page.set_content("<html><head><title>Access Denied</title></head><body>Access denied</body></html>")
    page.add_script_tag(content=NAUKRI_JS)
    assert page.evaluate("pageState()")["blocked"]


def test_apply_company_site_and_already_applied(page):
    load(page, "job_shaadi.html")
    assert apply(page, 3000)["status"] == "aborted"
    load(page, "job_infoedge.html")
    assert apply(page, 3000)["status"] == "already_applied"


def test_apply_one_click(page):
    load(page, "job_with_questionnaire.html", [])
    r = apply(page, 10000)
    assert r == {"status": "applied", "detail": "One-click apply"}


def test_apply_real_bajaj_questions(page):
    load(page, "job_with_questionnaire.html", [
        {"text": "Are you currently living in or ready to relocate to Pune ?"},
        {"text": "How many years of experience do you have in Digital Lending?"},
        {"text": "How many years of experience do you have in FLDG?"},
    ])
    r = apply(page)
    assert r["status"] == "applied", r
    assert page.evaluate("window.answers") == ["Yes", "5", "5"]


def test_apply_ctc_notice_options(page):
    load(page, "job_with_questionnaire.html", [
        {"text": "What is your current CTC (in lakhs)?"},
        {"text": "What is your expected CTC?"},
        {"text": "What is your notice period?", "options": ["1 Month", "15 Days or less", "2 Months"]},
        {"text": "How many years of experience do you have in fintech?", "options": ["0-2", "3-5", "6+"]},
    ])
    r = apply(page)
    assert r["status"] == "applied", r
    assert page.evaluate("window.answers") == ["27", "3600000", "15 Days or less", "3-5"]


def test_apply_free_text_question_aborts_before_submit(page):
    load(page, "job_with_questionnaire.html", [
        {"text": "What is your current CTC?"},
        {"text": "Why do you want to join Bajaj Finserv?"},
    ])
    r = apply(page)
    assert r["status"] == "aborted" and "Why do you want to join" in r["detail"], r
    assert page.evaluate("window.answers") == ["2700000"]
    assert page.locator("#already-applied").count() == 0


def test_apply_unmatched_notice_options_abort(page):
    load(page, "job_with_questionnaire.html", [{"text": "Notice period?", "options": ["1 month", "2 months"]}])
    r = apply(page)
    assert r["status"] == "aborted" and "Notice-period" in r["detail"], r
