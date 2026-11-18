// Settings, CV and the job log, kept in chrome.storage.local (stays on this computer).

export const DEFAULT_SETTINGS = {
  apiKey: '',
  searchUrl: 'https://www.naukri.com/product-manager-jobs?k=product%20manager&jobAge=7',
  maxPages: 100,
  currentCtcLpa: 27,
  expectedCtcLpa: 36,
  ctcUnit: 'rupees',            // when a CTC question names no unit: 'rupees' -> 2700000, 'lakhs' -> 27
  noticeText: 'Immediately available / currently serving notice',
  noticeKeywords: ['immediate', 'serving notice', 'currently serving', '15 days or less', '0-15', '0 - 15', 'less than 15', 'within 15'],
  relocateAnswer: 'Yes',
  yearsExperience: 5,
  blocklist: ['Shaadi.com', 'People Group'],
  model: 'claude-opus-5-5',
  effort: 'medium',
  applyThreshold: 85,
  reviewThreshold: 70,
  secondsBetweenApplies: 40,
  dailyCap: 40,
  retryErrors: false,           // true: jobs logged as error_skipped are tried again next run
};

export const COLUMNS = ['date', 'time', 'job_id', 'title', 'company', 'location', 'score', 'action', 'reason', 'job_url'];

export async function loadSettings() {
  const { settings } = await chrome.storage.local.get('settings');
  return { ...DEFAULT_SETTINGS, ...(settings || {}) };
}

export async function saveSettings(settings) {
  await chrome.storage.local.set({ settings });
}

export async function loadCv() {
  const { cv } = await chrome.storage.local.get('cv');
  return cv || null; // {name, base64, size}
}

export async function saveCv(cv) {
  await chrome.storage.local.set({ cv });
}

export async function loadLog() {
  const { log } = await chrome.storage.local.get('log');
  return log || [];
}

export async function appendLog(row) {
  const log = await loadLog();
  log.push(row);
  await chrome.storage.local.set({ log });
}

export async function replaceLog(log) {
  await chrome.storage.local.set({ log });
}

export function today() {
  return localDate(new Date());
}

export function localDate(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function localTime(d) {
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* Job ids that must not be processed again, and how many real applies happened today. */
export function dedupState(log, { retryErrors }) {
  const seen = new Set();
  let appliedToday = 0;
  const t = today();
  for (const r of log) {
    if (r.action === 'applied' && r.date === t) appliedToday++;
    if (!r.job_id || r.action.startsWith('would_')) continue;    // dry-run rows never block a live run
    if (r.action === 'skipped_daily_cap_reached') continue;      // qualified; retry another day
    if (retryErrors && r.action === 'error_skipped') continue;
    seen.add(r.job_id);
  }
  return { seen, appliedToday };
}

export function toCsv(log) {
  const cell = v => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [COLUMNS.join(',')].concat(log.map(r => COLUMNS.map(c => cell(r[c])).join(',')));
  return '﻿' + lines.join('\r\n') + '\r\n'; // BOM so Excel shows ₹ and non-ASCII names
}
