// Resume-vs-JD scoring with Google Gemini or Anthropic Claude (raw HTTP: an MV3 extension has
// no build step to bundle an SDK, and Chrome forbids loading remote code).

const API_URL = 'https://api.anthropic.com/v1/messages';
const GEMINI_URL = model => `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`;

const SYSTEM = `You evaluate how well a candidate fits a job posting, for an automated job-application tool.
The candidate's resume is the attached PDF.

Score the fit from 0 to 100 based on alignment between the resume and the job description:
skills, domain, seniority and years of experience, scope of past roles, and any hard requirements
(must-have qualifications, specific tools, industry). Weigh hard requirements the candidate clearly lacks heavily.

Calibration:
- 85-100: strong match; the candidate meets essentially all core requirements at the right seniority.
- 70-84: plausible match with real gaps or ambiguity a human should look at.
- 0-69: weak match (wrong seniority, different function, missing core requirements).

Return a short reason (one or two sentences, under 300 characters) naming the main matches and gaps.`;

const SCHEMA = {
  type: 'object',
  properties: {
    score: { type: 'integer', description: 'Fit score, 0-100' },
    reason: { type: 'string', description: 'One or two sentences on key matches and gaps' },
  },
  required: ['score', 'reason'],
  additionalProperties: false,
};

export class AuthError extends Error {}
export class ScoringError extends Error {}
/* Rate limit / quota still exhausted after retries: the run should stop, not log errors. */
export class QuotaError extends Error {}

const esc = s => String(s || '').replace(/[<&>]/g, c => ({ '<': '&lt;', '&': '&amp;', '>': '&gt;' }[c]));

const jobPrompt = (job, jdText) =>
  `<job>\n<title>${esc(job.title)}</title>\n<company>${esc(job.company)}</company>\n` +
  `<description>\n${esc(jdText.slice(0, 30000))}\n</description>\n</job>\n\nScore this job for the candidate.`;

function parseScore(text) {
  try {
    const parsed = JSON.parse(text);
    const score = Math.max(0, Math.min(100, Math.round(Number(parsed.score))));
    if (Number.isNaN(score)) throw new Error('score is not a number');
    return { score, reason: String(parsed.reason || '').trim() };
  } catch {
    throw new ScoringError(`Could not parse scoring output: ${String(text).slice(0, 200)}`);
  }
}

/* settings: the extension settings; picks the provider. */
export function scoreJob(settings, cvBase64, sleep, job, jdText) {
  if (settings.provider === 'anthropic') {
    return scoreWithClaude({ apiKey: settings.apiKey, model: settings.model, effort: settings.effort, cvBase64, sleep }, job, jdText);
  }
  return scoreWithGemini({ apiKey: settings.geminiApiKey, model: settings.geminiModel,
    thinkingLevel: settings.geminiThinking, cvBase64, sleep }, job, jdText);
}

async function scoreWithGemini({ apiKey, model, thinkingLevel, cvBase64, sleep }, job, jdText) {
  const body = {
    systemInstruction: { parts: [{ text: SYSTEM }] },
    contents: [{
      role: 'user',
      parts: [
        { inlineData: { mimeType: 'application/pdf', data: cvBase64 } },
        { text: jobPrompt(job, jdText) },
      ],
    }],
    generationConfig: {
      responseMimeType: 'application/json',
      responseJsonSchema: SCHEMA,
      maxOutputTokens: 16000,
      ...(thinkingLevel ? { thinkingConfig: { thinkingLevel } } : {}),
    },
  };
  let res, data, lastErr;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      res = await fetch(GEMINI_URL(model), {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-goog-api-key': apiKey },
        body: JSON.stringify(body),
      });
    } catch (e) {
      lastErr = `Network error: ${e.message}`;
      await sleep(2000 * 2 ** attempt);
      continue;
    }
    data = await res.json().catch(() => ({}));
    if (res.ok) break;
    const msg = data?.error?.message || res.statusText;
    if (res.status === 401 || res.status === 403 || (res.status === 400 && /api key/i.test(msg))) {
      throw new AuthError(`Gemini API key was rejected (${msg}). Check it in Settings; in AI Studio the key must be restricted to the Gemini API.`);
    }
    if (res.status === 404) throw new AuthError(`Gemini model "${model}" not found. Check the model name in Settings.`);
    if (res.status === 429) {
      // Daily quota won't recover by waiting; per-minute limits will.
      if (/per.?day|daily/i.test(JSON.stringify(data?.error || {}))) {
        throw new QuotaError(`Gemini daily quota reached (${msg}). Run again after it resets (midnight Pacific time).`);
      }
      const retry = (data?.error?.details || []).find(d => String(d['@type']).endsWith('RetryInfo'))?.retryDelay;
      lastErr = `Gemini rate limit: ${msg}`;
      if (attempt < 4) { await sleep(Math.min((parseFloat(retry) || 15 * (attempt + 1)) + 1, 90) * 1000); continue; }
      throw new QuotaError(`Gemini rate limit still hit after retries (${msg}). Try again later.`);
    }
    if (res.status >= 500 && attempt < 4) { lastErr = `Gemini API error ${res.status}`; await sleep(3000 * 2 ** attempt); continue; }
    throw new ScoringError(`Gemini API error ${res.status}: ${msg}`);
  }
  if (!res || !res.ok) throw new ScoringError(lastErr || 'No response from Gemini API');
  if (data.promptFeedback?.blockReason) throw new ScoringError(`Gemini blocked the request (${data.promptFeedback.blockReason})`);
  const cand = (data.candidates || [])[0];
  if (!cand) throw new ScoringError('Gemini returned no answer');
  if (cand.finishReason === 'MAX_TOKENS') throw new ScoringError('Scoring response was cut off');
  if (cand.finishReason && cand.finishReason !== 'STOP') throw new ScoringError(`Gemini stopped early (${cand.finishReason})`);
  const text = (cand.content?.parts || []).filter(p => p.text && !p.thought).map(p => p.text).join('');
  return parseScore(text);
}

async function scoreWithClaude({ apiKey, model, effort, cvBase64, sleep }, job, jdText) {
  const body = {
    model,
    max_tokens: 16000,
    system: SYSTEM,
    messages: [{
      role: 'user',
      content: [
        // Same PDF on every call -> cached after the first job (cheap and faster).
        { type: 'document', title: 'Candidate resume',
          source: { type: 'base64', media_type: 'application/pdf', data: cvBase64 },
          cache_control: { type: 'ephemeral' } },
        { type: 'text', text: jobPrompt(job, jdText) },
      ],
    }],
    output_config: { effort, format: { type: 'json_schema', schema: SCHEMA } },
    // If the model declines (safety classifier), the API re-runs the request on a fallback model.
    fallbacks: 'default',
  };
  const headers = {
    'content-type': 'application/json',
    'x-api-key': apiKey,
    'anthropic-version': '2026-10-21',
    'anthropic-beta': 'server-side-fallback-2026-07-01',
    'anthropic-dangerous-direct-browser-access': 'true',
  };

  let res, lastErr;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      res = await fetch(API_URL, { method: 'POST', headers, body: JSON.stringify(body) });
    } catch (e) {
      lastErr = `Network error: ${e.message}`;
      await sleep(2000 * 2 ** attempt);
      continue;
    }
    if (res.status === 401 || res.status === 403) {
      throw new AuthError('Anthropic API key is missing or invalid. Fix it in Settings.');
    }
    const retryable = res.status === 429 || res.status >= 500;
    if (retryable && res.headers.get('x-should-retry') !== 'false' && attempt < 3) {
      const after = Number(res.headers.get('retry-after')) || 2 * 2 ** attempt;
      lastErr = `API error ${res.status}`;
      await sleep(Math.min(after, 60) * 1000);
      continue;
    }
    break;
  }
  if (!res) throw new ScoringError(lastErr || 'No response from Anthropic API');
  if (res.status === 429) throw new QuotaError('Anthropic API rate limit still hit after retries. Try again later.');
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ScoringError(`Anthropic API error ${res.status}: ${data?.error?.message || res.statusText}`);
  }
  if (data.stop_reason === 'refusal') throw new ScoringError('Model declined to score this job');
  if (data.stop_reason === 'max_tokens') throw new ScoringError('Scoring response was cut off');
  const text = (data.content || []).find(b => b.type === 'text')?.text || '';
  return parseScore(text);
}
