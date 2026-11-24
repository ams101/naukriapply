"""Load the real Chrome extension and run it end to end.

naukri.com is served from the saved real pages in tests/fixtures (via request routing), and the
Claude API is replaced by a stub that returns a fixed score per company. Checks the decisions,
dry-run vs live behaviour, dedup, pacing, the daily cap, and what was sent to the API.
"""
import json
import re
import tempfile
import time
from pathlib import Path

import pytest

pw_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
EXT = str((ROOT / "extension").resolve())
CHROME = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"

SEARCH_URL = "https://www.naukri.com/product-manager-jobs?k=product%20manager&jobAge=7"

# job id -> (fixture, questionnaire questions or None, score returned by the stub API)
JOBS = {
    "191026914203": ("job_with_questionnaire.html", [], 91),                      # TechBlocks: one-click
    "171126505628": ("job_shaadi.html", None, 99),                                # Shaadi.com: blocklisted
    "171126030124": ("job_infoedge.html", None, 90),                              # Intas card -> 'Applied' page
    "181126503130": ("job_with_questionnaire.html", [                             # Aerchain: Bajaj questions
        {"text": "Are you currently living in or ready to relocate to Pune ?"},
        {"text": "How many years of experience do you have in Digital Lending?"},
    ], 92),
    "181126502651": ("job_with_questionnaire.html", [                             # Ixigo: free-text question
        {"text": "Why do you want to join us?"}], 88),
    "171126503706": ("job_techblocks.html", None, 75),                            # Yourdost: review band
    "111126505428": ("job_techblocks.html", None, 40),                            # Rategain: low score
}
SCORES_BY_COMPANY = {}


def search_page() -> str:
    """Real results page cut down to the jobs above, with Next disabled (last page)."""
    html = (FIX / "search_real.html").read_text(encoding="utf-8")
    blocks = re.split(r'(?=<div class="srp-jobtuple-wrapper")', html.split('<div class="list">', 1)[1])
    keep = [b for b in blocks if re.search(r'data-job-id="(\d+)"', b) and re.search(r'data-job-id="(\d+)"', b).group(1) in JOBS]
    keep[-1] = keep[-1].split("</div><a ")[0]
    assert len(keep) == len(JOBS)
    nxt = '<a href="https://www.naukri.com/product-manager-jobs-2" aria-disabled="true"><span>Next</span></a>'
    return '<html><body><div class="list">' + "".join(keep) + "</div>" + nxt + "</body></html>"


def job_page(job_id: str) -> str:
    fixture, questions, _ = JOBS[job_id]
    html = (FIX / fixture).read_text(encoding="utf-8")
    if questions is not None:
        html = html.replace("<body>", f"<body><script>window.QUESTIONS = {json.dumps(questions)};</script>", 1)
    return html


class Harness:
    def __init__(self, pw):
        self.api_calls = []
        self.gemini_errors = []   # queued error responses to return before succeeding
        self.ctx = pw.chromium.launch_persistent_context(
            tempfile.mkdtemp(), headless=True, executable_path=CHROME,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--headless=new"])
        self.ctx.route(re.compile(r"https://www\.naukri\.com/.*"), self.naukri)
        self.ctx.route("https://api.anthropic.com/v1/messages", self.claude)
        self.ctx.route(re.compile(r"https://generativelanguage\.googleapis\.com/.*"), self.gemini)
        sw = self.ctx.service_workers[0] if self.ctx.service_workers else self.ctx.wait_for_event("serviceworker")
        self.ext_id = sw.url.split("/")[2]
        self.dash = self.ctx.new_page()
        self.dash.on("dialog", lambda d: d.accept())
        self.dash.goto(f"chrome-extension://{self.ext_id}/dashboard.html")

    def naukri(self, route):
        url = route.request.url
        m = re.search(r"-(\d{9,})$", url.split("?")[0])
        if m and m.group(1) in JOBS:
            body = job_page(m.group(1))
        elif "/product-manager-jobs" in url:
            body = search_page()
        else:
            body = "<html><body>not found</body></html>"
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    def claude(self, route):
        req = route.request
        body = json.loads(req.post_data)
        self.api_calls.append({"headers": req.headers, "body": body})
        text = body["messages"][0]["content"][1]["text"]
        title_company = re.search(r"<company>(.*?)</company>", text).group(1)
        score = next(s for c, s in SCORES_BY_COMPANY.items() if c == title_company)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "thinking", "thinking": "", "signature": "x"},
                        {"type": "text", "text": json.dumps({"score": score, "reason": f"stub score {score}"})}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}))

    def gemini(self, route):
        req = route.request
        body = json.loads(req.post_data)
        self.api_calls.append({"url": req.url, "headers": req.headers, "body": body})
        if self.gemini_errors:
            status, err = self.gemini_errors.pop(0)
            return route.fulfill(status=status, content_type="application/json", body=json.dumps({"error": err}))
        text = body["contents"][0]["parts"][1]["text"]
        company = re.search(r"<company>(.*?)</company>", text).group(1)
        score = SCORES_BY_COMPANY[company]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "candidates": [{"content": {"role": "model", "parts": [
                {"text": json.dumps({"score": score, "reason": f"stub score {score}"})}]},
                "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}))

    def configure(self, **overrides):
        settings = {"provider": "gemini", "geminiApiKey": "AQ.test-key", "apiKey": "sk-ant-test",
                    "secondsBetweenApplies": 3, **overrides}
        self.dash.evaluate("""s => chrome.storage.local.set({settings: s,
            cv: {name: 'resume.pdf', size: 10, base64: 'JVBERi0xLjQK'}})""", settings)
        self.dash.reload()

    def run(self, button: str, timeout_s: float = 120):
        # (wait_for_function is blocked by the extension page's CSP, so poll.)
        self.dash.click(f"#{button}")
        state = self.dash.locator("#state")
        end = time.time() + 10
        while state.inner_text() == "Idle" and time.time() < end:
            time.sleep(0.1)
        end = time.time() + timeout_s
        while state.inner_text() != "Idle":
            assert time.time() < end, "run did not finish"
            time.sleep(0.3)
        return self.dash.locator("#status").inner_text()

    def log(self):
        return self.dash.evaluate("chrome.storage.local.get('log').then(r => r.log || [])")

    def actions(self, rows):
        return {r["job_id"]: r["action"] for r in rows}


@pytest.fixture
def h():
    with pw_api.sync_playwright() as pw:
        try:
            harness = Harness(pw)
        except Exception as e:
            pytest.skip(f"Can't load the extension in this Chromium: {e}")
        yield harness
        harness.ctx.close()


@pytest.fixture(autouse=True)
def company_scores():
    names = {"191026914203": "TechBlocks", "171126505628": "Shaadi.com", "171126030124": "Intas",
             "181126503130": "Aerchain", "181126502651": "Ixigo", "171126503706": "Yourdost",
             "111126505428": "Rategain"}
    SCORES_BY_COMPANY.clear()
    SCORES_BY_COMPANY.update({names[j]: JOBS[j][2] for j in JOBS})


def test_dry_run_then_live_run(h):
    h.configure()
    status = h.run("dry")
    assert "No more new jobs" in status, status
    dry = h.actions(h.log())
    assert dry == {
        "191026914203": "would_apply",
        "171126505628": "would_skip_blocklist",
        "171126030124": "would_skip_already_applied",
        "181126503130": "would_apply",
        "181126502651": "would_apply",          # dry run can't see recruiter questions
        "171126503706": "would_flag",
        "111126505428": "would_skip_low_score",
    }
    assert len(h.api_calls) == 6                 # blocklisted company is never scored

    # What was sent to Gemini: model, key header, PDF CV, JSON schema output, thinking level.
    call = h.api_calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-hydrogen:generateContent"
    assert call["headers"]["x-goog-api-key"] == "AQ.test-key"
    b = call["body"]
    assert b["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf"
    assert b["generationConfig"]["responseMimeType"] == "application/json"
    assert b["generationConfig"]["responseJsonSchema"]["required"] == ["score", "reason"]
    assert b["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "medium"}
    assert "resume" in b["systemInstruction"]["parts"][0]["text"]

    # Live run: dry-run rows don't block it.
    status = h.run("live")
    rows = h.log()[7:]
    live = h.actions(rows)
    assert live == {
        "191026914203": "applied",
        "171126505628": "skipped_blocklist",
        "171126030124": "skipped_already_applied",
        "181126503130": "applied",
        "181126502651": "flagged_review",
        "171126503706": "flagged_review",
        "111126505428": "skipped_low_score",
    }, rows
    by = {r["job_id"]: r for r in rows}
    assert "relocate=Yes" in by["181126503130"]["reason"] and "years_experience=5" in by["181126503130"]["reason"]
    assert "free-text question" in by["181126502651"]["reason"]
    assert by["191026914203"]["company"] == "TechBlocks" and by["191026914203"]["score"] == 91

    # Pacing: the two submitted applications are at least secondsBetweenApplies apart.
    applied = [r for r in rows if r["action"] == "applied"]
    def secs(r):
        hh, mm, ss = map(int, r["time"].split(":"))
        return hh * 3600 + mm * 60 + ss
    assert secs(applied[1]) - secs(applied[0]) >= 3

    # A third run finds nothing new (everything live-processed is deduped).
    calls_before = len(h.api_calls)
    h.run("live")
    assert len(h.log()) == 14 and len(h.api_calls) == calls_before

    # CSV export has the required columns.
    csv_text = h.dash.evaluate("import('./store.js').then(m => chrome.storage.local.get('log').then(r => m.toCsv(r.log)))")
    assert csv_text.lstrip("﻿").splitlines()[0] == "date,time,job_id,title,company,location,score,action,reason,job_url"


def test_daily_cap_stops_run(h):
    h.configure(dailyCap=1)
    status = h.run("live")
    assert "Daily cap of 1" in status, status
    acts = [r["action"] for r in h.log()]
    assert acts.count("applied") == 1
    assert acts[-1] == "skipped_daily_cap_reached"
    # Next run the same day exits immediately.
    status = h.run("live")
    assert "Daily cap already reached" in status


def test_missing_api_key_blocks_start(h):
    h.dash.evaluate("chrome.storage.local.clear()")
    h.dash.reload()
    assert h.dash.locator("#setup").is_visible()
    assert "Gemini API key" in h.dash.locator("#setup").inner_text()
    assert h.dash.locator("#live").is_disabled()


def test_login_wall_stops_run(h):
    h.configure()
    h.ctx.unroute(re.compile(r"https://www\.naukri\.com/.*"))
    h.ctx.route(re.compile(r"https://www\.naukri\.com/.*"), lambda r: r.fulfill(
        status=200, content_type="text/html",
        body="<html><body><form><input type=password><button>Login</button></form></body></html>"))
    status = h.run("dry")
    assert "log into naukri.com" in status.lower(), status
    assert h.log() == []


def test_claude_provider(h):
    h.configure(provider="anthropic", maxPages=1)
    h.run("dry")
    assert h.actions(h.log())["191026914203"] == "would_apply"
    call = h.api_calls[0]
    assert call["headers"]["x-api-key"] == "sk-ant-test"
    assert call["headers"]["anthropic-version"] == "2026-10-21"
    assert call["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    b = call["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    doc = b["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert doc["cache_control"] == {"type": "ephemeral"}
    assert b["output_config"]["format"]["type"] == "json_schema"


def test_gemini_per_minute_limit_waits_and_retries(h):
    h.configure()
    h.gemini_errors = [(429, {"code": 429, "status": "RESOURCE_EXHAUSTED",
                              "message": "Quota exceeded for requests per minute",
                              "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"}]})]
    h.run("dry")
    acts = h.actions(h.log())
    assert len(acts) == 7 and "would_error_skip" not in acts.values()


def test_gemini_daily_quota_stops_run_without_logging_errors(h):
    h.configure()
    h.gemini_errors = [(429, {"code": 429, "status": "RESOURCE_EXHAUSTED",
                              "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 0",
                              "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                           "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]})]
    status = h.run("dry")
    assert "daily quota" in status.lower(), status
    # Only the blocklisted job (no API call) was logged; the quota-hit job stays unseen for next time.
    assert all(r["action"] == "would_skip_blocklist" for r in h.log())


def test_gemini_bad_key_stops_run(h):
    h.configure()
    h.gemini_errors = [(400, {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."})]
    status = h.run("dry")
    assert "rejected" in status and "Settings" in status, status
