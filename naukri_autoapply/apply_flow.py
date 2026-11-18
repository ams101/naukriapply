"""The Apply flow on a Naukri job page.

How Naukri applies work (as of writing; not verified against a live session):
  * "Apply" on a job page usually submits immediately for a complete profile (one-click).
  * Some jobs open a recruiter questionnaire ("chatbot" drawer) that asks questions one at a time;
    the application is only submitted after the last answer is saved.
  * "Apply on company site" sends you to an external site.

Because one-click jobs submit on the Apply click itself, the form can't be inspected before it
is submitted. The questionnaire is inspected question by question, and the flow aborts (without
submitting) on any question that isn't current CTC / expected CTC / notice period.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page

from .browser import BlockedPage, check_blocked
from .config import ApplyDefaults

APPLY_BTN_RE = re.compile(r"^\s*apply( now)?\s*$", re.I)
COMPANY_SITE_RE = re.compile(r"company\s*site|apply\s*on\s*company", re.I)
ALREADY_APPLIED_RE = re.compile(r"^\s*applied\s*$", re.I)
SUCCESS_RE = re.compile(
    r"(successfully applied|applied successfully|application (has been )?(sent|submitted)|"
    r"you have applied|your application (has been|was) (sent|submitted))",
    re.I,
)
SAVE_BTN_RE = re.compile(r"^\s*(save|submit|send|next|continue)\s*$", re.I)

# Question classification
CURRENT_CTC_RE = re.compile(r"(current|present|existing|last drawn)[^?]{0,40}(ctc|salary|compensation|package|pay)|"
                            r"(ctc|salary|compensation|package)[^?]{0,20}(current|present)", re.I)
EXPECTED_CTC_RE = re.compile(r"expect", re.I)
CTC_WORD_RE = re.compile(r"ctc|salary|compensation|package|lpa|lakh|pay", re.I)
NOTICE_RE = re.compile(r"notice\s*period|how soon can you join|joining (time|date)|when can you join|"
                       r"earliest (you can )?join|serving notice", re.I)
LAKH_RE = re.compile(r"lakh|lac\b|lacs|lpa|\bl\b", re.I)
RUPEE_RE = re.compile(r"rupee|inr|₹|in rs|absolute|annum", re.I)

# Containers that hold the recruiter questionnaire.
CHAT_CONTAINER_SEL = (
    "[class*='chatbot' i], [id*='chatbot' i], [class*='ChatBot'], [class*='questionnaire' i], "
    "[class*='drawer' i]:has([contenteditable='true']), [role='dialog']"
)


class ApplyAborted(Exception):
    """Apply stopped deliberately (free-text / unrecognised question). Nothing was submitted."""


class AlreadyApplied(Exception):
    pass


@dataclass
class ApplyResult:
    detail: str


@dataclass
class Question:
    kind: str  # current_ctc | expected_ctc | notice | other
    text: str


def classify_question(text: str) -> str:
    t = " ".join(text.split())
    if NOTICE_RE.search(t):
        return "notice"
    if EXPECTED_CTC_RE.search(t) and CTC_WORD_RE.search(t):
        return "expected_ctc"
    if CURRENT_CTC_RE.search(t):
        return "current_ctc"
    return "other"


def ctc_answer(question: str, lpa: float, unit_when_unspecified: str) -> str:
    def fmt_lpa(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"

    if LAKH_RE.search(question):
        return fmt_lpa(lpa)
    if RUPEE_RE.search(question):
        return str(int(round(lpa * 100000)))
    return fmt_lpa(lpa) if unit_when_unspecified.startswith("lakh") else str(int(round(lpa * 100000)))


def find_apply_button(page: Page) -> Locator | None:
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=APPLY_BTN_RE)
        if loc.count():
            return loc.first
    loc = page.locator("#apply-button, button[class*='apply-button']")
    return loc.first if loc.count() else None


def precheck(page: Page) -> str:
    """Inspect the job page without clicking. Returns 'apply' | 'company_site' | 'already_applied' | 'none'."""
    if page.get_by_role("button", name=ALREADY_APPLIED_RE).count() or page.locator("#already-applied").count():
        return "already_applied"
    if page.get_by_role("button", name=COMPANY_SITE_RE).count() or page.get_by_role("link", name=COMPANY_SITE_RE).count():
        return "company_site"
    return "apply" if find_apply_button(page) else "none"


def _success_visible(page: Page) -> bool:
    try:
        if page.get_by_text(SUCCESS_RE).first.is_visible(timeout=300):
            return True
    except Exception:
        pass
    # On a real Naukri job page the "Applied" state is <span id="already-applied">, not a button.
    for loc in (page.locator("#already-applied"), page.get_by_role("button", name=ALREADY_APPLIED_RE)):
        try:
            if loc.count() and loc.first.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def _chat_container(page: Page) -> Locator | None:
    loc = page.locator(CHAT_CONTAINER_SEL)
    for i in range(min(loc.count(), 6)):
        el = loc.nth(i)
        try:
            if el.is_visible() and (
                el.locator("[contenteditable='true'], input, textarea, [role='radio'], input[type='radio']").count()
            ):
                return el
        except Exception:
            continue
    return None


def _latest_question(chat: Locator) -> str:
    """Text of the most recent bot message in the questionnaire."""
    for sel in ["[class*='botMsg' i]", "[class*='botItem' i]", "li[class*='bot' i]", "[class*='bot' i] [class*='msg' i]"]:
        msgs = chat.locator(sel)
        n = msgs.count()
        if n:
            return msgs.nth(n - 1).inner_text().strip()
    # Fallback: last few lines of the drawer text.
    lines = [l.strip() for l in chat.inner_text().splitlines() if l.strip()]
    return " ".join(lines[-3:])


def _options(chat: Locator) -> list[tuple[Locator, str]]:
    """Radio buttons / chips offered for the current question."""
    out: list[tuple[Locator, str]] = []
    radios = chat.get_by_role("radio")
    for i in range(radios.count()):
        r = radios.nth(i)
        label = (r.get_attribute("aria-label") or r.get_attribute("value") or "").strip()
        rid = r.get_attribute("id")
        if not label and rid:
            lab = chat.locator(f"label[for='{rid}']")
            if lab.count():
                label = lab.first.inner_text().strip()
        out.append((r, label))
    if not out:
        chips = chat.locator("[class*='chip' i], [class*='suggestion' i] button, [class*='option' i]")
        for i in range(min(chips.count(), 20)):
            c = chips.nth(i)
            try:
                if c.is_visible():
                    out.append((c, c.inner_text().strip()))
            except Exception:
                continue
    return out


def _text_input(chat: Locator) -> Locator | None:
    for sel in ["[contenteditable='true']", "textarea", "input[type='text']", "input[type='number']", "input:not([type])"]:
        loc = chat.locator(sel)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_editable():
                    return el
            except Exception:
                continue
    return None


def _click_save(page: Page, chat: Locator) -> None:
    btn = chat.get_by_role("button", name=SAVE_BTN_RE)
    if btn.count():
        btn.last.click()
        return
    btn = chat.locator("[class*='sendMsg' i], [class*='send' i], [class*='save' i]")
    if btn.count():
        btn.last.click()
        return
    page.keyboard.press("Enter")


def _close_chat(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        close = page.locator("[class*='chatbot' i] [class*='cross' i], [class*='chatbot' i] [class*='close' i]")
        if close.count():
            close.first.click(timeout=2000)
    except Exception:
        pass


def _answer(page: Page, chat: Locator, q: Question, d: ApplyDefaults) -> str:
    opts = _options(chat)
    text_box = _text_input(chat)

    if q.kind == "notice":
        if opts:
            for loc, label in opts:
                if any(k.lower() in label.lower() for k in d.notice_period_option_keywords):
                    try:
                        loc.click(timeout=3000)
                    except Exception:  # styled radios often hide the <input>; force the click
                        loc.click(force=True, timeout=3000)
                    _click_save(page, chat)
                    return label
            labels = ", ".join(l for _, l in opts if l)
            raise ApplyAborted(f"Notice-period options didn't match configured keywords: [{labels}]")
        if text_box:
            text_box.click()
            text_box.fill(d.notice_period_text)
            _click_save(page, chat)
            return d.notice_period_text
        raise BlockedPage("Notice-period question with no recognisable input")

    lpa = d.current_ctc_lpa if q.kind == "current_ctc" else d.expected_ctc_lpa
    if opts and not text_box:
        # CTC asked as ranges; picking a bracket is a judgement call -> human review.
        labels = ", ".join(l for _, l in opts if l)
        raise ApplyAborted(f"CTC question offers ranges, not a number: [{labels}]")
    if not text_box:
        raise BlockedPage("CTC question with no recognisable input")
    value = ctc_answer(q.text, lpa, d.ctc_unit_when_unspecified)
    text_box.click()
    text_box.fill(value)
    _click_save(page, chat)
    return value


def run_apply(page: Page, defaults: ApplyDefaults, max_questions: int = 8, timeout_s: float = 60) -> ApplyResult:
    """Click Apply and complete the flow if it only needs basic fields.

    Raises ApplyAborted (flag for review; nothing submitted), AlreadyApplied,
    or BlockedPage / other exceptions (error).
    """
    state = precheck(page)
    if state == "already_applied":
        raise AlreadyApplied("Job page shows 'Applied'")
    if state == "company_site":
        raise ApplyAborted("Apply is on the company's external site (form can't be checked)")
    if state == "none":
        raise BlockedPage(f"No Apply button found on {page.url}")

    btn = find_apply_button(page)
    btn.scroll_into_view_if_needed()
    btn.click()

    answered: list[str] = []
    last_question = None
    repeats = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _success_visible(page):
            detail = "One-click apply" if not answered else "Answered: " + "; ".join(answered)
            return ApplyResult(detail=detail)
        check_blocked(page)
        chat = _chat_container(page)
        if chat is None:
            continue
        q_text = _latest_question(chat)
        if not q_text:
            continue
        if q_text == last_question:
            repeats += 1
            if repeats >= 6:  # same question ~3s after answering -> our answer was rejected
                _close_chat(page)
                raise BlockedPage(f"Questionnaire did not accept answer to: {q_text[:150]!r}")
            continue
        q = Question(kind=classify_question(q_text), text=q_text)
        if q.kind == "other":
            _close_chat(page)
            kind = "free-text" if _text_input(chat) else "non-basic"
            raise ApplyAborted(f"Apply form has a {kind} question: {q_text[:200]!r}")
        if len(answered) >= max_questions:
            _close_chat(page)
            raise ApplyAborted("Questionnaire longer than expected")
        value = _answer(page, chat, q, defaults)
        answered.append(f"{q.kind}={value}")
        last_question, repeats = q_text, 0

    raise BlockedPage("No success confirmation after clicking Apply (timed out)")
