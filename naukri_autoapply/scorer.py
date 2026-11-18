"""Resume-vs-JD fit scoring with Claude."""
from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

SYSTEM_TEMPLATE = """You evaluate how well a candidate fits a job posting, for an automated job-application tool.

Score the fit from 0 to 100 based on alignment between the candidate's resume and the job description:
skills, domain, seniority and years of experience, scope of past roles, and any hard requirements
(must-have qualifications, specific tools, industry). Weigh hard requirements the candidate clearly lacks heavily.

Calibration:
- 85-100: strong match; the candidate meets essentially all core requirements at the right seniority.
- 70-84: plausible match with real gaps or ambiguity a human should look at.
- 0-69: weak match (wrong seniority, different function, missing core requirements).

Return a short reason (one or two sentences, under 300 characters) naming the main matches and gaps.

<resume>
{resume}
</resume>"""

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Fit score, 0-100"},
        "reason": {"type": "string", "description": "One or two sentences on key matches and gaps"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}


@dataclass
class Score:
    score: int
    reason: str


class ScoringError(Exception):
    pass


class Scorer:
    def __init__(self, resume_text: str, model: str, effort: str, max_jd_chars: int):
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model
        self.effort = effort
        self.max_jd_chars = max_jd_chars
        # Resume is identical for every job in the run -> cache it.
        self.system = [{
            "type": "text",
            "text": SYSTEM_TEMPLATE.format(resume=resume_text),
            "cache_control": {"type": "ephemeral"},
        }]

    def score(self, title: str, company: str, jd_text: str) -> Score:
        jd = jd_text[: self.max_jd_chars]
        user = (
            f"<job>\n<title>{title}</title>\n<company>{company}</company>\n"
            f"<description>\n{jd}\n</description>\n</job>\n\nScore this job for the candidate."
        )
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
                # If the model declines (safety classifier), the API retries on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as e:
            raise ScoringError(f"Anthropic API connection error: {e}") from e
        except anthropic.RateLimitError as e:
            raise ScoringError("Anthropic API rate limited (after SDK retries)") from e
        except anthropic.AuthenticationError:
            raise  # fatal: bad/missing API key; let the runner stop the whole run
        except anthropic.APIStatusError as e:
            raise ScoringError(f"Anthropic API error {e.status_code}: {e.message}") from e

        if response.stop_reason == "refusal":
            raise ScoringError("Model declined to score this job")
        if response.stop_reason == "max_tokens":
            raise ScoringError("Scoring response was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            score = max(0, min(100, int(data["score"])))
            return Score(score=score, reason=str(data.get("reason", "")).strip())
        except (ValueError, KeyError, TypeError) as e:
            raise ScoringError(f"Could not parse scoring output: {text[:200]!r}") from e
