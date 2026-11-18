"""The CSV log: the single output artifact, plus dedup and daily-cap state derived from it."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

COLUMNS = ["date", "time", "job_id", "title", "company", "location", "score", "action", "reason", "job_url"]

# Live actions
APPLIED = "applied"
FLAGGED = "flagged_review"
LOW_SCORE = "skipped_low_score"
BLOCKLIST = "skipped_blocklist"
CAP_REACHED = "skipped_daily_cap_reached"
ALREADY_APPLIED = "skipped_already_applied"
ERROR = "error_skipped"

# Dry-run equivalents: the decision that would have been made.
DRY_RUN_ACTIONS = {
    APPLIED: "would_apply",
    FLAGGED: "would_flag",
    LOW_SCORE: "would_skip_low_score",
    BLOCKLIST: "would_skip_blocklist",
    CAP_REACHED: "would_skip_daily_cap_reached",
    ALREADY_APPLIED: "would_skip_already_applied",
    ERROR: "would_error_skip",
}


class Logbook:
    def __init__(self, path: Path, reprocess_actions: set[str], dry_run: bool):
        self.path = path
        self.reprocess_actions = reprocess_actions
        self.dry_run = dry_run
        self.seen: set[str] = set()
        self.applied_today = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        with self.path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                action = (row.get("action") or "").strip()
                job_id = (row.get("job_id") or "").strip()
                if action == APPLIED and row.get("date") == today:
                    self.applied_today += 1
                if not job_id or action.startswith("would_") or action in self.reprocess_actions:
                    continue
                self.seen.add(job_id)

    def is_seen(self, job_id: str) -> bool:
        return job_id in self.seen

    def record(self, job, action: str, reason: str, score: int | None = None) -> str:
        """Append one row. `action` is the live action name; dry-run rewrites it."""
        if self.dry_run:
            action = DRY_RUN_ACTIONS.get(action, f"would_{action}")
        now = datetime.now()
        row = {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "score": "" if score is None else score,
            "action": action,
            "reason": " ".join(str(reason).split()),  # one line per row
            "job_url": job.url,
        }
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig so Excel on Windows shows ₹ and non-ASCII names correctly.
        with self.path.open("a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(row)
        if not action.startswith("would_") and action not in self.reprocess_actions:
            self.seen.add(job.job_id)
        if action == APPLIED:
            self.applied_today += 1
        return action
