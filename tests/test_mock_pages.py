"""Exercise scraping and the apply flow against small mock pages shaped like Naukri's.

These do NOT prove the selectors match the live site; they check the logic around them.
Requires a Playwright Chromium (`playwright install chromium`); skipped otherwise.
"""
import pytest

from naukri_autoapply.apply_flow import AlreadyApplied, ApplyAborted, run_apply
from naukri_autoapply.config import ApplyDefaults
from naukri_autoapply.search import scrape_results_page

pw_api = pytest.importorskip("playwright.sync_api")

SEARCH_HTML = """
<html><body><div class="list">
 <div class="srp-jobtuple-wrapper" data-job-id="181125000001">
   <div class="cust-job-tuple">
     <h2><a class="title" title="Senior Product Manager"
        href="https://www.naukri.com/job-listings-senior-product-manager-acme-bengaluru-5-to-10-years-181125000001?src=srp">Senior Product Manager</a></h2>
     <div class="row2"><span class="comp-dtls-wrap"><a class="comp-name" title="Acme Corp" href="/acme-jobs-careers-1">Acme Corp</a></span></div>
     <div class="row3"><span class="loc-wrap"><span class="locWdth" title="Bengaluru">Bengaluru</span></span></div>
     <div class="row6"><span class="job-post-day">2 Days Ago</span></div>
   </div>
 </div>
 <div class="srp-jobtuple-wrapper" data-job-id="181125000002">
   <div class="cust-job-tuple">
     <h2><a class="title" title="Product Manager - Growth"
        href="https://www.naukri.com/job-listings-product-manager-growth-shaadi-com-mumbai-4-to-8-years-181125000002">Product Manager - Growth</a></h2>
     <div class="row2"><a class="comp-name" title="Shaadi.com" href="/shaadi-jobs-careers-2">Shaadi.com</a></div>
     <div class="row3"><span class="locWdth" title="Mumbai">Mumbai</span></div>
     <div class="row6"><span class="job-post-day">Just Now</span></div>
   </div>
 </div>
</div><a class="styles_btn-secondary" href="?page=2"><span>Next</span></a></body></html>
"""

# Detail page whose Apply button either succeeds instantly or opens a questionnaire.
DETAIL_TMPL = """
<html><body>
<h1>Senior Product Manager</h1>
<section class="styles_job-desc-container"><div>Job description</div>
<div class="dang-inner-html">{jd}</div></section>
<button id="apply-button" onclick="startApply()">Apply</button>
<div id="drawer"></div>
<script>
const QUESTIONS = {questions};
let i = 0;
function startApply() {{
  if (QUESTIONS.length === 0) return done();
  const d = document.getElementById('drawer');
  d.className = 'chatbot_Drawer';
  d.innerHTML = '<ul id="msgs"></ul><div class="chatbot_InputContainer"></div>';
  ask();
}}
function ask() {{
  const q = QUESTIONS[i];
  document.getElementById('msgs').insertAdjacentHTML('beforeend', '<li class="botItem"><div class="botMsg">' + q.text + '</div></li>');
  const c = document.querySelector('.chatbot_InputContainer');
  if (q.options) {{
    c.innerHTML = q.options.map((o, k) => '<input type="radio" name="o" id="o' + k + '" value="' + o + '"><label for="o' + k + '">' + o + '</label>').join('')
      + '<div class="sendMsg" onclick="save()">Save</div>';
  }} else {{
    c.innerHTML = '<div class="textArea" contenteditable="true"></div><div class="sendMsg" onclick="save()">Save</div>';
  }}
}}
function save() {{
  const q = QUESTIONS[i];
  const r = document.querySelector('input[type=radio]:checked');
  const t = document.querySelector('[contenteditable=true]');
  window.answers = (window.answers || []).concat([r ? r.value : t.innerText]);
  i++;
  if (i < QUESTIONS.length) ask(); else done();
}}
function done() {{
  document.getElementById('drawer').innerHTML = '';
  document.getElementById('drawer').className = '';
  document.body.insertAdjacentHTML('beforeend', '<div class="success">You have successfully applied to this job.</div>');
  const b = document.getElementById('apply-button'); b.textContent = 'Applied';
}}
</script></body></html>
"""

JD = "We are hiring a senior product manager to own the roadmap. " * 10
DEFAULTS = ApplyDefaults(
    current_ctc_lpa=27, expected_ctc_lpa=36, ctc_unit_when_unspecified="rupees",
    notice_period_text="Immediately available / currently serving notice",
    notice_period_option_keywords=["immediate", "15 days or less"],
)


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


def detail(page, questions):
    import json
    page.set_content(DETAIL_TMPL.format(jd=JD, questions=json.dumps(questions)))


def test_scrape_cards(page):
    page.set_content(SEARCH_HTML)
    jobs = scrape_results_page(page)
    assert [j.job_id for j in jobs] == ["181125000001", "181125000002"]
    j = jobs[0]
    assert (j.title, j.company, j.location, j.posted) == ("Senior Product Manager", "Acme Corp", "Bengaluru", "2 Days Ago")
    assert "?" not in j.url
    assert jobs[1].company == "Shaadi.com"


def test_one_click_apply(page):
    detail(page, [])
    assert run_apply(page, DEFAULTS, timeout_s=10).detail == "One-click apply"


def test_basic_questions_are_answered(page):
    detail(page, [
        {"text": "What is your current CTC?"},
        {"text": "What is your expected CTC (in lakhs)?"},
        {"text": "What is your notice period?", "options": ["Immediate", "1 month", "2 months"]},
    ])
    res = run_apply(page, DEFAULTS, timeout_s=20)
    assert page.evaluate("window.answers") == ["2700000", "36", "Immediate"]
    assert "current_ctc=2700000" in res.detail


def test_free_text_question_aborts(page):
    detail(page, [
        {"text": "What is your current CTC?"},
        {"text": "Why are you a good fit for this role?"},
    ])
    with pytest.raises(ApplyAborted, match="free-text"):
        run_apply(page, DEFAULTS, timeout_s=20)
    assert page.locator(".success").count() == 0


def test_unmatched_notice_options_abort(page):
    detail(page, [{"text": "Notice period?", "options": ["1 month", "2 months", "3 months"]}])
    with pytest.raises(ApplyAborted, match="Notice-period options"):
        run_apply(page, DEFAULTS, timeout_s=20)


def test_already_applied(page):
    page.set_content("<html><body><h1>PM</h1><button>Applied</button></body></html>")
    with pytest.raises(AlreadyApplied):
        run_apply(page, DEFAULTS, timeout_s=5)


def test_company_site(page):
    page.set_content("<html><body><h1>PM</h1><button>Apply on company site</button></body></html>")
    with pytest.raises(ApplyAborted, match="external"):
        run_apply(page, DEFAULTS, timeout_s=5)


# ---- Real Naukri page (results page 2, saved Nov 2026; trimmed to job cards + Next link) ----
from pathlib import Path

REAL_SEARCH = Path(__file__).parent / "fixtures" / "search_real.html"


def test_scrape_real_search_page(page):
    page.set_content(REAL_SEARCH.read_text(encoding="utf-8"))
    jobs = scrape_results_page(page)
    assert len(jobs) == 20
    assert all(j.job_id and j.title and j.company and j.location and j.posted for j in jobs)
    by_id = {j.job_id: j for j in jobs}
    assert by_id["191026914203"].company == "TechBlocks"
    assert by_id["171126505628"].company == "Shaadi.com"
    assert by_id["171126030124"].location == "Ahmedabad"
    assert by_id["191026914203"].url == (
        "https://www.naukri.com/job-listings-product-manager-techblocks-consulting-pvt-ltd-hyderabad-6-to-11-years-191026914203"
    )


def test_real_next_link_detection(page):
    import re
    page.set_content(REAL_SEARCH.read_text(encoding="utf-8"))
    nxt = page.get_by_role("link", name=re.compile(r"^\s*next\s*$", re.I))
    assert nxt.count() == 1
    assert nxt.first.get_attribute("aria-disabled") == "false"


# ---- Real Naukri job pages (saved Nov 2026; trimmed to header + JD section) ----
from naukri_autoapply.apply_flow import _success_visible, precheck
from naukri_autoapply.search import _JD_JS, Job, open_detail

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name,state", [
    ("techblocks", "apply"), ("shaadi", "company_site"), ("infoedge", "already_applied"),
])
def test_real_job_page_state(page, name, state):
    page.set_content((FIX / f"job_{name}.html").read_text(encoding="utf-8"))
    assert precheck(page) == state
    assert len(page.evaluate(_JD_JS)) > 1500
    # "Applied" span must also count as success after clicking Apply.
    assert _success_visible(page) == (state == "already_applied")


def test_real_job_page_metadata_cleanup(page, tmp_path):
    f = tmp_path / "job.html"
    f.write_text((FIX / "job_techblocks.html").read_text(encoding="utf-8"), encoding="utf-8")
    job = Job(job_id="191026914203", title="", company="", location="", posted="", url=f.as_uri())
    jd = open_detail(page, job)
    assert job.company == "TechBlocks" and job.location == "Hyderabad"
    assert "product" in jd.lower()


def test_real_apply_on_company_site_is_flagged(page):
    page.set_content((FIX / "job_shaadi.html").read_text(encoding="utf-8"))
    with pytest.raises(ApplyAborted, match="external"):
        run_apply(page, DEFAULTS, timeout_s=5)
