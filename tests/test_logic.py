import csv
from types import SimpleNamespace

from naukri_autoapply import logbook as L
from naukri_autoapply.apply_flow import classify_question, ctc_answer
from naukri_autoapply.runner import is_blocklisted
from naukri_autoapply.search import _page_n_url, job_id_from_url

BL = ["Shaadi.com", "People Group"]


def job(jid="1"):
    return SimpleNamespace(job_id=jid, title="PM", company="Acme", location="Pune", url=f"https://x/{jid}")


def test_blocklist():
    assert is_blocklisted("Shaadi.com", BL)
    assert is_blocklisted("SHAADI.COM Pvt Ltd", BL)
    assert is_blocklisted("People Group (Shaadi.com)", BL)
    assert is_blocklisted("Peoplegroup", BL)
    assert not is_blocklisted("Razorpay", BL)
    assert not is_blocklisted("", BL)
    assert not is_blocklisted("Group", BL)  # too short to reverse-match


def test_job_id():
    url = "https://www.naukri.com/job-listings-product-manager-acme-bengaluru-5-to-10-years-181125012345?src=jobsearchDesk"
    assert job_id_from_url(url) == "181125012345"
    assert job_id_from_url("https://www.naukri.com/product-manager-jobs") is None


def test_page_n_url():
    base = "https://www.naukri.com/product-manager-jobs?k=product%20manager&jobAge=7"
    assert _page_n_url(base, 2) == "https://www.naukri.com/product-manager-jobs-2?k=product%20manager&jobAge=7"
    assert _page_n_url(base, 1) == "https://www.naukri.com/product-manager-jobs?k=product%20manager&jobAge=7"


def test_classify():
    assert classify_question("What is your current CTC?") == "current_ctc"
    assert classify_question("Please mention your present salary in lakhs") == "current_ctc"
    assert classify_question("What is your expected CTC?") == "expected_ctc"
    assert classify_question("Expected salary (LPA)") == "expected_ctc"
    assert classify_question("What is your notice period?") == "notice"
    assert classify_question("How soon can you join?") == "notice"
    assert classify_question("Why are you a good fit for this role?") == "other"
    assert classify_question("Are you willing to relocate to Pune?") == "other"
    assert classify_question("How many years of experience in B2B SaaS?") == "other"


def test_ctc_answer():
    assert ctc_answer("Current CTC in lakhs", 27, "rupees") == "27"
    assert ctc_answer("Current CTC (INR)", 27, "lakhs") == "2700000"
    assert ctc_answer("Current CTC?", 27, "rupees") == "2700000"
    assert ctc_answer("Current CTC?", 27.5, "lakhs") == "27.5"


def test_logbook_dedup_and_cap(tmp_path):
    p = tmp_path / "log.csv"
    lb = L.Logbook(p, {"skipped_daily_cap_reached"}, dry_run=False)
    lb.record(job("1"), L.APPLIED, "ok", 90)
    lb.record(job("2"), L.CAP_REACHED, "cap", 88)
    lb.record(job("3"), L.ERROR, "boom")
    dry = L.Logbook(p, {"skipped_daily_cap_reached"}, dry_run=True)
    dry.record(job("4"), L.APPLIED, "would", 91)

    rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
    assert [r["action"] for r in rows] == ["applied", "skipped_daily_cap_reached", "error_skipped", "would_apply"]
    assert list(rows[0].keys()) == L.COLUMNS

    fresh = L.Logbook(p, {"skipped_daily_cap_reached"}, dry_run=False)
    assert fresh.is_seen("1") and fresh.is_seen("3")
    assert not fresh.is_seen("2")  # cap-reached jobs are retried next day
    assert not fresh.is_seen("4")  # dry-run rows never block a live run
    assert fresh.applied_today == 1
