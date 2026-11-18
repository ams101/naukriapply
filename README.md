# Naukri Auto-Apply (Product Manager roles)

A Chrome extension that searches Naukri for Product Manager jobs posted in the last 7 days. It
scores each new job against your CV with Claude, applies to strong matches, and flags unclear ones
for you to review. Nothing to install besides Chrome; it uses the Naukri login you already have.

## One-time setup

1. **Get the extension folder.** Unzip `naukri-auto-apply-extension.zip` somewhere permanent, e.g.
   `Documents\naukri-auto-apply`. Chrome loads it from there, so don't delete or move it later.
2. **Load it into Chrome.**
   - Open `chrome://extensions`.
   - Turn on **Developer mode** (top right).
   - Click **Load unpacked** and select the unzipped folder: the one containing `manifest.json`.
   - Click the puzzle-piece icon in the toolbar and pin **Naukri Auto-Apply**.
3. **Open it.** Click its toolbar icon. The dashboard opens in a tab.
4. **Settings → Essentials.**
   - Paste your Anthropic API key (from console.anthropic.com) and click **Save settings**.
   - Upload your CV as a PDF. It's used **only to score jobs**: Naukri sends the CV on your
     Naukri profile when you apply, so keep the two in sync.
5. **Check the other settings** (CTC, notice period, relocate, years of experience, blocklist,
   thresholds, pacing, daily cap) and save. The defaults match your requirements.
6. Make sure you're **logged into naukri.com** in this Chrome.

## Every time

Click the toolbar icon, then:

- **Dry run** does everything except clicking Apply. It logs what it *would* do (`would_apply`,
  `would_flag`, ...). Do this first.
- **Start applying** runs for real.
- **Stop** ends the run after the current job.

A run opens Naukri in its own window. Leave both that window and the dashboard tab open until the
dashboard says it's finished. **Download CSV** exports the full log as `applied_log.csv`.

## What it does per job

1. **Blocklist.** If the company name matches the blocklist, the job is logged as
   `skipped_blocklist` without scoring. The match ignores case and punctuation and works on
   substrings.
2. **Score** (0–100, plus a short reason). Claude compares your CV with the job description.
   - below 70: `skipped_low_score`
   - 70–84: `flagged_review`
   - 85 and above: try to apply
3. **Apply** (85 and above):
   - One-click apply: submitted, logged as `applied`.
   - Recruiter questions about **current CTC, expected CTC, notice period, "ready to relocate?"**
     (Yes) and **"how many years of experience (in X)?"** (5) are answered from Settings.
   - **Any other question** (free text such as "why do you want to join", a portfolio link, a
     yes/no "do you have experience in X?") stops the apply *before* submitting. The job is logged
     as `flagged_review` with the question text.
   - "Apply on company site": `flagged_review`. The page already shows "Applied":
     `skipped_already_applied`.
4. **Errors.** A timeout, CAPTCHA or unexpected page logs `error_skipped` with the error and the
   page URL, and the run moves on to the next job.

**Pacing and cap.** Submitted applications are spaced 40 seconds apart. After 40 in a calendar day,
the next qualifying job is logged as `skipped_daily_cap_reached` and the run ends. A run also ends
when there are no new jobs left in the results.

## Things to know

- **Cost.** Each job scored is one Claude call. A rough guide is a few US cents per job; your CV is
  cached between calls. The first run can cover hundreds or thousands of listings from the last
  7 days, so for the first dry run consider setting **Advanced → Max result pages** to 5 (about
  100 jobs) to see how it does. Later runs only score jobs they haven't seen.
- **Dedup.** A job logged as `applied`, `flagged_review`, `skipped_*` or `error_skipped` is never
  processed again. There are two exceptions. Dry-run (`would_*`) rows don't count, so your first
  live run isn't blocked by the dry run. `skipped_daily_cap_reached` jobs are reconsidered next
  day. Tick "Retry jobs that errored" to retry `error_skipped` jobs as well.
- **Dry run can't see recruiter questions.** On Naukri, clicking Apply often *is* the submission,
  so a dry run never clicks it. `would_apply` means "score 85+ and an Apply button is there".
- **Years of experience = 5 for any skill asked**, including skills not on your CV.
- **Your data stays in this Chrome.** The log, settings, API key and CV are kept in the
  extension's local storage. Only the CV and job description are sent to the Claude API for scoring.
- **Selectors** were checked against real saved Naukri pages (search results, Apply / company site /
  Applied job pages, and the recruiter questionnaire). Naukri can change its pages. If you start
  seeing `error_skipped` rows, send the rows plus a saved copy of the page and the selectors in
  `extension/naukri.js` can be updated.

## Repository layout

| Path | What |
|---|---|
| `extension/` | The Chrome extension: `naukri.js` (code that runs on Naukri pages), `runner.js` (run logic), `scorer.js` (Claude API), `store.js` (settings and log), `dashboard.*` (UI) |
| `naukri_autoapply/`, `main.py`, `config.yaml` | The earlier Python + Playwright version, same behaviour. Not needed for the extension. |
| `tests/` | Tests for both versions. They use trimmed real Naukri pages in `tests/fixtures/`, and the extension tests load it into Chrome end to end with Naukri and the Claude API simulated. |

Run the tests (needs Python and Playwright):

```bash
pip install -r requirements.txt pytest
```
```bash
python -m pytest tests
```
