"""Get resume text: Naukri profile first (attached resume file, then profile sections), then local files."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from playwright.sync_api import Page

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
MIN_RESUME_CHARS = 600  # below this we treat the extraction as failed (e.g. a scanned PDF)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return _clean("\n".join((p.extract_text() or "") for p in reader.pages))
    if suffix == ".docx":
        import docx

        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return _clean("\n".join(parts))
    if suffix in (".txt", ".md"):
        return _clean(path.read_text(encoding="utf-8", errors="ignore"))
    raise ValueError(f"Unsupported resume format: {path.name}")


def _download_attached_resume(page: Page) -> str:
    """Click the profile's resume download control and parse the file."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"download\s*(resume|cv)?", re.I)),
        page.get_by_role("link", name=re.compile(r"download\s*(resume|cv)?", re.I)),
        page.locator("[data-title*='download' i], [title*='download' i], [class*='download' i]"),
    ]
    for loc in candidates:
        if loc.count() == 0:
            continue
        try:
            with page.expect_download(timeout=15000) as dl_info:
                loc.first.click()
            dl = dl_info.value
            suffix = Path(dl.suggested_filename).suffix or ".pdf"
            with tempfile.TemporaryDirectory() as td:
                target = Path(td) / f"naukri_resume{suffix}"
                dl.save_as(str(target))
                return parse_file(target)
        except Exception as e:
            print(f"[resume] Download attempt failed: {e.__class__.__name__}: {str(e)[:120]}")
    return ""


def _profile_sections_text(page: Page) -> str:
    """Fallback inside the profile: visible text of the main profile content."""
    for sel in ["main", "#root", "body"]:
        loc = page.locator(sel)
        if loc.count():
            try:
                return _clean(loc.first.inner_text(timeout=5000))
            except Exception:
                continue
    return ""


def _from_files(paths: list[Path]) -> tuple[str, str] | None:
    for path in paths:
        if not path.exists():
            continue
        try:
            text = parse_file(path)
        except Exception as e:
            print(f"[resume] Could not parse {path.name}: {e}")
            continue
        if len(text) >= MIN_RESUME_CHARS:
            return text, f"local file {path.name}"
        print(f"[resume] {path.name} yielded only {len(text)} chars (scanned PDF?); skipping.")
    return None


def _from_profile(page: Page) -> tuple[str, str] | None:
    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        text = _download_attached_resume(page)
        if len(text) >= MIN_RESUME_CHARS:
            return text, "Naukri profile (attached resume file)"
        if text:
            print(f"[resume] Attached resume gave only {len(text)} chars (scanned PDF?).")
        text = _profile_sections_text(page)
        # The page wraps the profile in nav/footer text; require clear resume-ish content.
        if len(text) >= MIN_RESUME_CHARS and re.search(r"key skills|employment|experience", text, re.I):
            return text, "Naukri profile page text"
    except Exception as e:
        print(f"[resume] Profile extraction failed: {e.__class__.__name__}: {str(e)[:160]}")
    return None


def get_resume_text(page: Page, files: list[Path], file_first: bool = True) -> tuple[str, str]:
    """Returns (text, source_description). Used only for scoring; Naukri itself sends the
    CV attached to your profile when you apply. Raises RuntimeError if nothing usable is found."""
    sources = [lambda: _from_files(files), lambda: _from_profile(page)]
    if not file_first:
        sources.reverse()
    for source in sources:
        result = source()
        if result:
            return result
    raise RuntimeError(
        "Could not get resume text from local files or the Naukri profile. "
        "Place a text-based resume.pdf or resume.docx in the project folder."
    )
