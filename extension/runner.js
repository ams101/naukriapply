// One run: open Naukri in its own window, walk the search results, decide per job, log to storage.
import { applyFlow, pageState, readJob, scrapeResults } from './naukri.js';
import { AuthError, QuotaError, scoreJob } from './scorer.js';
import { appendLog, dedupState, loadLog, localDate, localTime } from './store.js';

const DRY = {
  applied: 'would_apply',
  flagged_review: 'would_flag',
  skipped_low_score: 'would_skip_low_score',
  skipped_blocklist: 'would_skip_blocklist',
  skipped_daily_cap_reached: 'would_skip_daily_cap_reached',
  skipped_already_applied: 'would_skip_already_applied',
  error_skipped: 'would_error_skip',
};

class StopRun extends Error {}

// ---- timing that isn't throttled in background tabs ----
const worker = new Worker('timer-worker.js');
let timerId = 0;
const pending = new Map();
worker.onmessage = e => { const r = pending.get(e.data.id); pending.delete(e.data.id); if (r) r(); };
export const sleep = ms => new Promise(r => { const id = ++timerId; pending.set(id, r); worker.postMessage({ id, ms }); });

export function isBlocklisted(company, blocklist) {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const c = norm(company);
  if (!c) return null;
  // "Shaadi.com" matches "Shaadi.com Pvt Ltd"; a shortened "Shaadi" matches "Shaadi.com" (6+ chars only).
  return blocklist.find(e => { const n = norm(e); return n && (c.includes(n) || (c.length >= 6 && n.includes(c))); }) || null;
}

export function pageNUrl(baseUrl, n) {
  // Naukri's own Next link drops the query string (losing jobAge=7), so build /...-jobs-N?query.
  const u = new URL(baseUrl);
  const path = u.pathname.replace(/\/$/, '').replace(/-\d+$/, '');
  u.pathname = n > 1 ? `${path}-${n}` : path;
  return u.toString();
}

export class Runner {
  constructor(settings, cv, { dryRun, onEvent }) {
    this.s = settings;
    this.cv = cv;
    this.dryRun = dryRun;
    this.emit = onEvent || (() => {});
    this.stopRequested = false;
    this.counts = {};
    this.lastApplyAt = null;
    this.wouldApply = 0;
  }

  stop() { this.stopRequested = true; }

  // ---- browser helpers ----
  async exec(func, args = []) {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: this.tabId }, func, args });
    if (!res) throw new Error('Script did not run (tab closed or not a Naukri page?)');
    return res.result;
  }

  async navigate(url) {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); reject(new Error(`Page load timed out: ${url}`)); }, 45000);
      const listener = (id, info) => {
        if (id === this.tabId && info.status === 'complete') {
          clearTimeout(timeout); chrome.tabs.onUpdated.removeListener(listener); resolve();
        }
      };
      chrome.tabs.onUpdated.addListener(listener);
      chrome.tabs.update(this.tabId, { url }).catch(e => { clearTimeout(timeout); reject(e); });
    });
    await sleep(2000); // let Naukri's scripts render
    const st = await this.exec(pageState);
    if (st.login) throw new StopRun('Naukri is asking you to log in. Log into naukri.com in this Chrome, then press Start again.');
    return st;
  }

  async waitFor(func, ok, ms = 15000) {
    const end = Date.now() + ms;
    let r;
    do { r = await this.exec(func); if (ok(r)) return r; await sleep(1000); } while (Date.now() < end);
    return r;
  }

  // ---- logging ----
  async record(job, action, reason, score = null) {
    const written = this.dryRun ? (DRY[action] || `would_${action}`) : action;
    const now = new Date();
    const row = {
      date: localDate(now), time: localTime(now), job_id: job.job_id, title: job.title,
      company: job.company, location: job.location, score: score ?? '', action: written,
      reason: String(reason).replace(/\s+/g, ' ').trim(), job_url: job.url,
    };
    await appendLog(row);
    if (!written.startsWith('would_') && written !== 'skipped_daily_cap_reached') this.seen.add(job.job_id);
    if (written === 'applied') this.appliedToday++;
    this.counts[written] = (this.counts[written] || 0) + 1;
    this.emit({ type: 'row', row, counts: { ...this.counts } });
  }

  // ---- main ----
  async run() {
    const s = this.s;
    const log = await loadLog();
    ({ seen: this.seen, appliedToday: this.appliedToday } = dedupState(log, s));
    this.emit({ type: 'status', text: `${this.dryRun ? 'Dry run' : 'Live run'} started. ${this.seen.size} jobs already handled earlier.` });
    if (!this.dryRun && this.appliedToday >= s.dailyCap) {
      this.emit({ type: 'done', text: `Daily cap already reached (${this.appliedToday}/${s.dailyCap} applied today).` });
      return;
    }

    const win = await chrome.windows.create({ url: 'about:blank', focused: true, state: 'maximized' });
    this.windowId = win.id;
    this.tabId = win.tabs[0].id;
    let endText = 'No more new jobs in the search results.';
    try {
      for (let n = 1; n <= s.maxPages; n++) {
        if (this.stopRequested) throw new StopRun('Stopped by you.');
        const st = await this.navigate(pageNUrl(s.searchUrl, n));
        if (st.blocked) throw new StopRun(`Naukri showed a bot check on the results page (${st.blocked}). Try again later.`);
        const { jobs, hasNext } = await this.waitFor(scrapeResults, r => r.jobs.length > 0);
        this.emit({ type: 'status', text: `Results page ${n}: ${jobs.length} jobs.` });
        if (!jobs.length) break;
        for (const job of jobs) {
          if (this.stopRequested) throw new StopRun('Stopped by you.');
          if (this.seen.has(job.job_id)) continue;
          await this.processJob(job);
        }
        if (!hasNext) break;
      }
    } catch (e) {
      if (e instanceof StopRun) endText = e.message;
      else endText = `Run stopped by an unexpected error: ${e.message}`;
    } finally {
      chrome.windows.remove(this.windowId).catch(() => {});
      const applied = this.dryRun ? `${this.wouldApply} would-apply` : `${this.appliedToday}/${s.dailyCap} applied today`;
      this.emit({ type: 'done', text: `${endText} (${applied})`, counts: { ...this.counts } });
    }
  }

  async processJob(job) {
    const s = this.s;
    this.emit({ type: 'status', text: `Checking: ${job.title} @ ${job.company || '?'}` });
    let score = null;
    try {
      let hit = isBlocklisted(job.company, s.blocklist);
      if (hit) return this.record(job, 'skipped_blocklist', `Company matches blocklist entry '${hit}'`);

      const st = await this.navigate(job.url);
      if (st.blocked) return this.record(job, 'error_skipped', `${st.blocked} | page: ${st.url}`);
      const info = await this.waitFor(readJob, r => r.jd.length > 150 && r.state !== 'none', 12000);
      job.title ||= info.title; job.company ||= info.company; job.location ||= info.location;
      if (info.jd.length < 150) return this.record(job, 'error_skipped', `Job description not found | page: ${st.url} | title: ${st.title}`);
      hit = isBlocklisted(job.company, s.blocklist);
      if (hit) return this.record(job, 'skipped_blocklist', `Company matches blocklist entry '${hit}'`);

      const r = await scoreJob(s, this.cv.base64, sleep, job, info.jd);
      score = r.score;
      if (score < s.reviewThreshold) return this.record(job, 'skipped_low_score', r.reason, score);
      if (score < s.applyThreshold) return this.record(job, 'flagged_review', `Score in review band. ${r.reason}`, score);

      if (info.state === 'already_applied') return this.record(job, 'skipped_already_applied', "Job page shows 'Applied'", score);
      if (info.state === 'company_site') return this.record(job, 'flagged_review', `Apply is on company's external site. ${r.reason}`, score);
      if (info.state === 'none') return this.record(job, 'error_skipped', `No Apply button found | page: ${st.url}`, score);

      const appliedSoFar = this.appliedToday + this.wouldApply;
      if (appliedSoFar >= s.dailyCap) {
        await this.record(job, 'skipped_daily_cap_reached', `Daily cap of ${s.dailyCap} reached. ${r.reason}`, score);
        throw new StopRun(`Daily cap of ${s.dailyCap} applications reached.`);
      }

      if (this.dryRun) {
        this.wouldApply++;
        return this.record(job, 'applied', `${r.reason} (dry run: Apply not clicked, so recruiter questions weren't checked)`, score);
      }

      // Exactly secondsBetweenApplies between submitted applications.
      if (this.lastApplyAt) {
        const wait = s.secondsBetweenApplies * 1000 - (Date.now() - this.lastApplyAt);
        if (wait > 0) {
          this.emit({ type: 'status', text: `Waiting ${Math.ceil(wait / 1000)}s before the next application…` });
          await sleep(wait);
        }
      }
      if (this.stopRequested) throw new StopRun('Stopped by you.');
      const res = await this.exec(applyFlow, [{
        currentCtcLpa: s.currentCtcLpa, expectedCtcLpa: s.expectedCtcLpa, ctcUnit: s.ctcUnit,
        noticeText: s.noticeText, noticeKeywords: s.noticeKeywords,
        relocateAnswer: s.relocateAnswer, yearsExperience: s.yearsExperience,
      }, 60000]);
      if (res.status === 'applied') {
        this.lastApplyAt = Date.now();
        return this.record(job, 'applied', `${res.detail}. ${r.reason}`, score);
      }
      if (res.status === 'aborted') return this.record(job, 'flagged_review', `${res.detail} | ${r.reason}`, score);
      if (res.status === 'already_applied') return this.record(job, 'skipped_already_applied', res.detail, score);
      return this.record(job, 'error_skipped', `${res.detail} | page: ${job.url}`, score);
    } catch (e) {
      if (e instanceof StopRun) throw e;
      if (e instanceof AuthError || e instanceof QuotaError) throw new StopRun(e.message);
      let where = job.url;
      try { const st = await this.exec(pageState); where = `${st.url} | title: ${st.title}`; } catch { /* tab gone */ }
      return this.record(job, 'error_skipped', `${e.name}: ${e.message} | page: ${where}`, score);
    }
  }
}
