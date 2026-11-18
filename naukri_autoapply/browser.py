"""Browser launch, session validation, and page-state checks (login wall, CAPTCHA)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

NAUKRI_HOME = "https://www.naukri.com/mnjuser/homepage"
NAUKRI_LOGIN = "https://www.naukri.com/nlogin/login"

LOGIN_URL_RE = re.compile(r"/nlogin/|/login\b|central-login", re.I)
CAPTCHA_TEXT_RE = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|unusual traffic|captcha|access denied|"
    r"request (has been )?blocked",
    re.I,
)


class SessionExpired(Exception):
    pass


class BlockedPage(Exception):
    """CAPTCHA, bot wall, or a page we don't recognise."""


def launch(pw: Playwright, channel: str, slow_mo_ms: int) -> Browser:
    """Headed browser. Prefer the installed Chrome; fall back to bundled Chromium."""
    kwargs = dict(headless=False, slow_mo=slow_mo_ms, args=["--start-maximized"])
    if channel:
        try:
            return pw.chromium.launch(channel=channel, **kwargs)
        except Exception as e:  # channel not installed
            print(f"[browser] Could not launch channel '{channel}' ({e.__class__.__name__}); using bundled Chromium.")
    return pw.chromium.launch(**kwargs)


def new_context(browser: Browser, storage_state: Path | None, timeout_ms: int) -> BrowserContext:
    ctx = browser.new_context(
        storage_state=str(storage_state) if storage_state else None,
        no_viewport=True,  # use the real window size
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    ctx.set_default_timeout(timeout_ms)
    ctx.set_default_navigation_timeout(timeout_ms)
    return ctx


def is_login_page(page: Page) -> bool:
    if LOGIN_URL_RE.search(page.url):
        return True
    try:
        # Login form: a password field plus a "Login" button and no logged-in nav.
        has_password = page.locator("input[type=password]").first.is_visible(timeout=1000)
    except Exception:
        has_password = False
    return has_password


def check_blocked(page: Page) -> None:
    """Raise BlockedPage if the current page looks like a CAPTCHA or bot wall."""
    try:
        if page.locator("iframe[src*='captcha'], iframe[title*='captcha' i], #captcha, .g-recaptcha").count():
            raise BlockedPage(f"CAPTCHA element on {page.url}")
        title = page.title()
        body = page.locator("body").inner_text(timeout=3000)[:3000]
    except BlockedPage:
        raise
    except Exception:
        return
    if CAPTCHA_TEXT_RE.search(title) or (CAPTCHA_TEXT_RE.search(body) and len(body) < 2500):
        # Short page dominated by bot-check text. (Long pages may mention "captcha" in a footer.)
        raise BlockedPage(f"Bot-check / CAPTCHA page at {page.url}: {title!r}")


def assert_logged_in(page: Page) -> None:
    page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if is_login_page(page):
        raise SessionExpired(
            "Naukri session is missing or expired (redirected to the login page).\n"
            "Re-run the login step:   python main.py login"
        )


def screenshot(page: Page, folder: Path, job_id: str, tag: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{job_id or 'nojob'}_{tag}.png"
    path = folder / name
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return ""
