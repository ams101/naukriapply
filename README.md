# Naukri Auto-Apply (Product Manager roles)

Searches Naukri for Product Manager jobs posted in the last 7 days. It scores each new job against
your resume with Claude, applies to strong matches, and flags unclear ones for you to review.
Everything goes into one CSV file, `applied_log.csv`.

It runs locally in a **visible** Chrome window. You log in yourself once, and the tool never sees or
types your password.

## Setup (Windows, PowerShell)

```powershell
cd path\to\naukriapply
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Only needed if you don't have Google Chrome installed (or set browser.channel: "" in config.yaml):
playwright install chromium
```

Set your Anthropic API key, either for this session only:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

or permanently (takes effect in new terminals):

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

Optional: put a text-based `resume.pdf` or `resume.docx` in this folder. It's only used if the tool
can't read your resume from your Naukri profile.

## Use

**1. Log in once.** A browser opens. Log in normally (OTP etc.), then press Enter in the terminal.
The session is saved to `auth.json`.

```powershell
python main.py login
```

**2. Dry run first.** This searches, scrapes and scores exactly like a live run, and logs what it
*would* do (`would_apply`, `would_flag`, `would_skip_low_score`, ...), but never clicks Apply.

```powershell
python main.py run --dry-run
```

**3. Live.**

```powershell
python main.py run
```

Keep the browser window open while it runs. Press Ctrl+C in the terminal to stop early; the CSV is
written row by row, so nothing is lost. If the session has expired, the run stops and tells you to
run `python main.py login` again.

## What it does per job

1. **Blocklist.** If the company name matches `blocklist` in `config.yaml`, the job is logged as
   `skipped_blocklist` without scoring. The match ignores case and punctuation and works on
   substrings.
2. **Score** (0–100, plus a short reason). Claude compares your resume with the job description.
   - below 70: `skipped_low_score`
   - 70–84: `flagged_review`
   - 85 and above: try to apply
3. **Apply** (85 and above):
   - One-click apply: submitted, logged as `applied`.
   - Recruiter questionnaire: questions about **current CTC, expected CTC or notice period** are
     answered from `config.yaml`. **Any other question** (free text such as "why are you a fit", or
     anything else) aborts *before* submitting and logs `flagged_review` with the question text.
   - "Apply on company site": `flagged_review`, because the external form can't be checked.
   - The job page already shows "Applied": `skipped_already_applied`.
4. **Errors.** A timeout, CAPTCHA, unexpected page or missing element logs `error_skipped`, with the
   error, a screenshot path in `screenshots/` and the page URL. The run then moves to the next job.

**Pacing and cap.** Submitted applications are spaced 40 seconds apart. Once 40 have been submitted
in a calendar day (counted from the CSV, across runs), the next qualifying job is logged as
`skipped_daily_cap_reached` and the run exits. A run also ends when the search results have no more
new jobs.

## Things to know

- **One-click jobs submit on the Apply click.** On Naukri, clicking Apply often *is* the submission,
  so there is no form to inspect beforehand. The free-text check applies to the recruiter
  questionnaire, where questions appear one at a time before anything is submitted. For the same
  reason, a dry run can't see questionnaire questions: its `would_apply` rows mean "score 85+ and an
  Apply button is present".
- **Dedup.** A job ID logged as `applied`, `flagged_review`, `skipped_*` or `error_skipped` is never
  processed again. There are two exceptions. `skipped_daily_cap_reached` jobs are reconsidered next
  time, because they qualified and only the cap stopped them. Dry-run (`would_*`) rows never count,
  so your first live run isn't blocked by the dry run. Change this under `dedup.reprocess_actions`.
- **CTC format.** If a question says "lakhs"/"LPA", the tool types `27`. If it says "INR"/"rupees",
  it types `2700000`. If it says neither, `apply_defaults.ctc_unit_when_unspecified` decides. If the
  CTC question offers ranges to choose from instead of a text box, the job is flagged for review
  rather than guessing.
- **Notice period.** For multiple-choice questions, the first option containing one of
  `notice_period_option_keywords` is picked. If none matches, the job is flagged.
- **Selectors are unverified.** This was built without access to a logged-in Naukri session.
  Locators lean on stable hooks: `job-listings-…` links, button/role names and visible text. Class
  names are only fallbacks. If something breaks, the `error_skipped` row has the screenshot and URL.
  Selectors live in `naukri_autoapply/search.py` (results and job pages) and
  `naukri_autoapply/apply_flow.py` (Apply button and questionnaire).

## Files

| File | Purpose |
|---|---|
| `config.yaml` | CTC and notice defaults, blocklist, thresholds, pacing, model |
| `auth.json` | saved Naukri session (git-ignored, keep it private) |
| `applied_log.csv` | the log: `date, time, job_id, title, company, location, score, action, reason, job_url` |
| `screenshots/` | screenshots of pages that errored |

## Tests

```powershell
pip install pytest
python -m pytest tests
```

`tests/test_mock_pages.py` runs the scraper and the apply flow against small mock pages built to
look like Naukri's. These tests check the logic, not that the selectors match the live site.
