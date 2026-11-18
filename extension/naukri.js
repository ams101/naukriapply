// Functions injected into the Naukri tab with chrome.scripting.executeScript.
// Each one must be SELF-CONTAINED (Chrome serialises the function body only), so helpers
// are defined inside. Selectors were checked against saved real Naukri pages (Nov 2026).

/* Page health: login wall / CAPTCHA. */
export function pageState() {
  const url = location.href;
  const pw = document.querySelector('input[type=password]');
  const pwVisible = !!pw && pw.getClientRects().length > 0;
  const login = /\/nlogin\/|\/login\b|central-login/i.test(url) || pwVisible;
  let blocked = null;
  if (document.querySelector("iframe[src*='captcha' i], iframe[title*='captcha' i], #captcha, .g-recaptcha")) {
    blocked = 'CAPTCHA element on page';
  } else {
    const body = (document.body && document.body.innerText || '').slice(0, 3000);
    const re = /verify (that )?you are (a )?human|are you a robot|unusual traffic|captcha|access denied|request (has been )?blocked/i;
    if (re.test(document.title) || (re.test(body) && body.length < 2500)) blocked = `Bot-check page: ${document.title}`;
  }
  return { url, title: document.title, login, blocked };
}

/* Search results page -> job cards + whether a "Next" page exists. */
export function scrapeResults() {
  const txt = el => (el && (el.getAttribute('title') || el.innerText || '').trim()) || '';
  const pick = (card, sels) => {
    for (const s of sels) { const t = txt(card.querySelector(s)); if (t) return t; }
    return '';
  };
  const jobs = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="job-listings-"]')) {
    const m = a.href.match(/job-listings-[^?#]*?-(\d{9,})(?:[?#/]|$)/);
    let card = a.closest('[data-job-id], .srp-jobtuple-wrapper, .cust-job-tuple, article');
    if (!card) { card = a; for (let i = 0; i < 5 && card.parentElement; i++) card = card.parentElement; }
    const id = m ? m[1] : (card.getAttribute('data-job-id') || '').trim();
    if (!id || seen.has(id)) continue;
    // Skip "Jobs you might be interested in" side widgets: real result cards sit in the main list.
    if (card.closest('[class*="simjobs" i], [class*="right-section" i]')) continue;
    seen.add(id);
    const u = new URL(a.href);
    jobs.push({
      job_id: id,
      title: txt(a),
      company: pick(card, ['a.comp-name', '[class*="comp-name"]', 'a[href*="-jobs-careers-"]']),
      location: pick(card, ['.locWdth', '[class*="locWdth"]', '[class*="loc-wrap"] [title]', '[class*="location"]']),
      posted: pick(card, ['.job-post-day', '[class*="job-post-day"]', '[class*="post-day"]']),
      url: u.origin + u.pathname,
    });
  }
  const next = [...document.querySelectorAll('a, button')].find(e => /^\s*next\s*$/i.test(e.innerText || ''));
  const nextDisabled = !next || (next.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
    || next.hasAttribute('disabled') || /disabled/i.test(next.className || '');
  return { jobs, hasNext: !nextDisabled };
}

/* Job detail page -> metadata, JD text, and which apply control is shown. */
export function readJob() {
  const t = s => { const el = document.querySelector(s); return el ? (el.getAttribute('title') || el.innerText || '').trim() : ''; };
  const visible = el => !!el && el.getClientRects().length > 0;
  let jd = '';
  for (const s of ['section[class*="job-desc"]', '[class*="job-desc-container"]', '[class*="dang-inner-html"]']) {
    const el = document.querySelector(s);
    if (el && el.innerText.trim().length > 200) { jd = el.innerText.trim(); break; }
  }
  const buttons = [...document.querySelectorAll('button, a')].filter(visible);
  const byText = re => buttons.find(b => re.test((b.innerText || '').trim()));
  let state = 'none';
  if (visible(document.querySelector('#already-applied')) || byText(/^applied$/i)) state = 'already_applied';
  else if (document.querySelector('#company-site-button') || byText(/company\s*site/i)) state = 'company_site';
  else if (document.querySelector('#apply-button') || byText(/^apply( now)?$/i)) state = 'apply';
  return {
    title: t('h1'),
    company: t('[class*="jd-header-comp-name"] a').replace(/\s+careers$/i, ''),
    location: t('[class*="location"] a').replace(/^jobs in\s+/i, ''),
    jd,
    state,
  };
}

/* Click Apply and get through the recruiter questionnaire if it only asks what we can answer.
 * Returns {status: 'applied'|'aborted'|'already_applied'|'error', detail}.
 * 'aborted' means stopped on purpose BEFORE submitting (job gets flagged for review). */
export async function applyFlow(d, timeoutMs) {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  // ---- question classification (same rules as the tested Python version) ----
  const NOTICE = /notice\s*period|how soon can you join|joining (time|date)|when can you join|earliest (you can )?join|serving notice/i;
  const RELOCATE = /relocat|currently (living|based|residing|staying) in|willing to (move|shift)|open to (move|moving|shift)/i;
  const YEARS = /(how many|number of|no\.? of|total)\s+(years|yrs)|(years|yrs)\s+of\s+(relevant\s+|total\s+|work\s+)?(experience|exp)\b|experience\s*\((in )?(years|yrs)\)/i;
  const CTC_WORD = /ctc|salary|compensation|package|lpa|lakh|pay/i;
  const CURRENT_CTC = /(current|present|existing|last drawn)[^?]{0,40}(ctc|salary|compensation|package|pay)|(ctc|salary|compensation|package)[^?]{0,20}(current|present)/i;
  const classify = q => {
    if (NOTICE.test(q)) return 'notice';
    if (RELOCATE.test(q)) return 'relocate';
    if (YEARS.test(q) && !CTC_WORD.test(q)) return 'years_experience';
    if (/expect/i.test(q) && CTC_WORD.test(q)) return 'expected_ctc';
    if (CURRENT_CTC.test(q)) return 'current_ctc';
    return 'other';
  };
  const fmt = v => (Number.isInteger(v) ? String(v) : String(+v.toFixed(2)));
  const ctcAnswer = (q, lpa) => {
    if (/lakh|lac\b|lacs|lpa|\bl\b/i.test(q)) return fmt(lpa);
    if (/rupee|inr|₹|in rs|absolute|annum/i.test(q)) return String(Math.round(lpa * 100000));
    return d.ctcUnit === 'lakhs' ? fmt(lpa) : String(Math.round(lpa * 100000));
  };
  const pickYears = (labels, years) => labels.findIndex(label => {
    const t = label.toLowerCase();
    const nums = (t.match(/\d+(?:\.\d+)?/g) || []).map(Number);
    if (nums.length >= 2) return nums[0] <= years && years <= nums[1];
    if (nums.length === 1) {
      const n = nums[0];
      if (/\+|or more|and above|or above/.test(t)) return years >= n;
      if (/more than|above|over|greater than/.test(t)) return years > n;
      if (/less than|under|below|fewer than/.test(t)) return years < n;
      return years === n;
    }
    return false;
  });

  // ---- page helpers ----
  const SUCCESS = /successfully applied|applied successfully|application (has been )?(sent|submitted)|you have applied|your application (has been|was) (sent|submitted)/i;
  const succeeded = () => visible(document.querySelector('#already-applied'))
    || [...document.querySelectorAll('button')].some(b => visible(b) && /^\s*applied\s*$/i.test(b.innerText || ''))
    || SUCCESS.test(document.body.innerText || '');
  const findChat = () => {
    const sels = ['.chatbot_Drawer', '._chatBotContainer', '[class*="chatbot" i]', '[id*="chatbot" i]', '[class*="questionnaire" i]', '[role="dialog"]'];
    for (const s of sels) {
      for (const el of document.querySelectorAll(s)) {
        if (visible(el) && el.querySelector('[contenteditable="true"], input, textarea')) return el;
      }
    }
    return null;
  };
  const latestQuestion = chat => {
    const msgs = chat.querySelectorAll('.botMsg, [class*="botMsg" i], li[class*="botItem" i]');
    if (msgs.length) return norm(msgs[msgs.length - 1].innerText);
    return '';
  };
  const options = chat => {
    const out = [];
    for (const r of chat.querySelectorAll('input[type="radio"], [role="radio"]')) {
      let label = r.getAttribute('aria-label') || r.value || '';
      if (r.id) { const l = chat.querySelector(`label[for="${CSS.escape(r.id)}"]`); if (l) label = l.innerText; }
      out.push({ el: r, label: norm(label) });
    }
    if (!out.length) {
      for (const c of chat.querySelectorAll('[class*="chip" i], [class*="suggestion" i] button, [class*="option" i]')) {
        if (c.closest('li, ul')) continue; // message history (Naukri's logo sits in div.chipMsg there)
        const label = norm(c.innerText);
        if (label && visible(c)) out.push({ el: c, label });
      }
    }
    return out;
  };
  const textBox = chat => [...chat.querySelectorAll('[contenteditable="true"], textarea, input[type="text"], input[type="number"], input:not([type])')]
    .find(el => visible(el) && !el.disabled && !el.readOnly);
  const typeInto = (el, value) => {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand('selectAll', false);
      document.execCommand('insertText', false, value);
      if (norm(el.innerText) !== value) { el.innerText = value; el.dispatchEvent(new InputEvent('input', { bubbles: true })); }
    } else {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set;
      setter.call(el, value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    // Naukri enables Save on key events.
    for (const type of ['keydown', 'keyup']) el.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: value.slice(-1) || 'a' }));
  };
  const clickSave = chat => {
    const btn = chat.querySelector('.sendMsg') || chat.querySelector('[class*="sendMsg" i]')
      || [...chat.querySelectorAll('button, [role="button"]')].find(b => /^\s*(save|submit|send|next|continue)\s*$/i.test(b.innerText || ''));
    if (!btn) throw new Error('Questionnaire Save button not found');
    btn.click();
  };
  const closeChat = chat => {
    const x = chat && chat.querySelector('.crossIcon, [class*="cross" i], [class*="close" i]');
    if (x) x.click(); else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  };
  const choose = (o) => { o.el.click(); if (o.el.matches('input')) o.el.dispatchEvent(new Event('change', { bubbles: true })); };

  const answer = (chat, kind, q) => {
    const opts = options(chat);
    const box = textBox(chat);
    const list = () => opts.map(o => o.label).filter(Boolean).join(', ');
    const pickByText = (test, why) => {
      const o = opts.find(o => test(o.label.toLowerCase()));
      if (!o) throw { abort: `${why}: [${list()}]` };
      choose(o); clickSave(chat); return o.label;
    };
    const typeAndSave = v => { typeInto(box, v); clickSave(chat); return v; };
    if (kind === 'notice') {
      if (opts.length) return pickByText(l => d.noticeKeywords.some(k => l.includes(k.toLowerCase())), 'Notice-period options matched none of your keywords');
      if (box) return typeAndSave(d.noticeText);
      throw new Error('Notice-period question with no input');
    }
    if (kind === 'relocate') {
      if (opts.length) return pickByText(l => l.startsWith(d.relocateAnswer.toLowerCase()), `Relocation options have no '${d.relocateAnswer}'`);
      if (box) return typeAndSave(d.relocateAnswer);
      throw new Error('Relocation question with no input');
    }
    if (kind === 'years_experience') {
      if (opts.length) {
        const i = pickYears(opts.map(o => o.label), d.yearsExperience);
        if (i < 0) throw { abort: `Experience options don't cover ${d.yearsExperience} years: [${list()}]` };
        choose(opts[i]); clickSave(chat); return opts[i].label;
      }
      if (box) return typeAndSave(fmt(d.yearsExperience));
      throw new Error('Experience question with no input');
    }
    // CTC
    if (opts.length && !box) throw { abort: `CTC question offers ranges, not a number: [${list()}]` };
    if (!box) throw new Error('CTC question with no input');
    return typeAndSave(ctcAnswer(q, kind === 'current_ctc' ? d.currentCtcLpa : d.expectedCtcLpa));
  };

  // ---- flow ----
  if (visible(document.querySelector('#already-applied'))) return { status: 'already_applied', detail: "Job page shows 'Applied'" };
  if (document.querySelector('#company-site-button')) return { status: 'aborted', detail: "Apply is on the company's external site (form can't be checked)" };
  const applyBtn = document.querySelector('#apply-button')
    || [...document.querySelectorAll('button')].find(b => visible(b) && /^\s*apply( now)?\s*$/i.test(b.innerText || ''));
  if (!applyBtn) return { status: 'error', detail: 'No Apply button found' };
  applyBtn.scrollIntoView({ block: 'center' });
  applyBtn.click();

  const answered = [];
  let lastQ = null, repeats = 0;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(500);
    if (succeeded()) return { status: 'applied', detail: answered.length ? `Answered: ${answered.join('; ')}` : 'One-click apply' };
    const chat = findChat();
    if (!chat) continue;
    const q = latestQuestion(chat);
    if (!q) continue;
    if (q === lastQ) {
      if (++repeats >= 6) { closeChat(chat); return { status: 'error', detail: `Questionnaire didn't accept the answer to: "${q.slice(0, 150)}"` }; }
      continue;
    }
    const kind = classify(q);
    if (kind === 'other') {
      const how = textBox(chat) ? 'free-text' : 'non-basic';
      closeChat(chat);
      return { status: 'aborted', detail: `Apply form has a ${how} question: "${q.slice(0, 200)}"` };
    }
    if (answered.length >= 8) { closeChat(chat); return { status: 'aborted', detail: 'Questionnaire longer than expected' }; }
    try {
      answered.push(`${kind}=${answer(chat, kind, q)}`);
    } catch (e) {
      closeChat(chat);
      if (e && e.abort) return { status: 'aborted', detail: e.abort };
      return { status: 'error', detail: String(e && e.message || e) };
    }
    lastQ = q; repeats = 0;
  }
  return { status: 'error', detail: 'No success confirmation after clicking Apply (timed out)' };
}
