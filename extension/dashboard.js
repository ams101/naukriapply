import { Runner } from './runner.js';
import { dedupState, loadCv, loadLog, loadSettings, replaceLog, saveCv, saveSettings, toCsv } from './store.js';

const $ = id => document.getElementById(id);
const form = $('settings');
const LIST_FIELDS = ['noticeKeywords', 'blocklist'];
const NUM_FIELDS = ['currentCtcLpa', 'expectedCtcLpa', 'yearsExperience', 'applyThreshold', 'reviewThreshold',
  'secondsBetweenApplies', 'dailyCap', 'maxPages'];
let runner = null;

// ---- settings form ----
function fillForm(s) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in s)) continue;
    if (el.type === 'checkbox') el.checked = !!s[el.name];
    else if (LIST_FIELDS.includes(el.name)) el.value = s[el.name].join('\n');
    else el.value = s[el.name];
  }
}

function readForm(base) {
  const s = { ...base };
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') s[el.name] = el.checked;
    else if (LIST_FIELDS.includes(el.name)) s[el.name] = el.value.split('\n').map(x => x.trim()).filter(Boolean);
    else if (NUM_FIELDS.includes(el.name)) s[el.name] = Number(el.value);
    else s[el.name] = el.value.trim();
  }
  return s;
}

form.addEventListener('submit', async e => {
  e.preventDefault();
  await saveSettings(readForm(await loadSettings()));
  $('saved').textContent = 'Saved.';
  setTimeout(() => ($('saved').textContent = ''), 2000);
  refreshSetup();
});

$('cv').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  if (file.type !== 'application/pdf') { $('cv-status').textContent = 'Please choose a PDF (export your CV as PDF).'; return; }
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  await saveCv({ name: file.name, size: file.size, base64: btoa(bin) });
  refreshSetup();
});

function showProviderFields() {
  const p = $('provider').value;
  for (const el of document.querySelectorAll('[data-provider]')) el.hidden = el.dataset.provider !== p;
}
$('provider').addEventListener('change', showProviderFields);

// ---- setup checklist ----
async function refreshSetup() {
  const s = await loadSettings();
  const cv = await loadCv();
  $('cv-status').textContent = cv ? `Using ${cv.name} (${Math.round(cv.size / 1024)} KB). Upload again to replace.` : 'No CV uploaded yet.';
  const todo = [];
  if (s.provider === 'anthropic' ? !s.apiKey : !s.geminiApiKey) {
    todo.push(`Add your ${s.provider === 'anthropic' ? 'Anthropic' : 'Gemini'} API key in Settings below, then Save.`);
  }
  if (!cv) todo.push('Upload your CV (PDF) in Settings below.');
  $('setup-list').innerHTML = '';
  for (const t of todo) { const li = document.createElement('li'); li.textContent = t; $('setup-list').append(li); }
  $('setup').hidden = todo.length === 0;
  setRunning(!!runner, todo.length === 0);
  return todo.length === 0;
}

// ---- log table ----
async function renderLog() {
  const log = await loadLog();
  const s = await loadSettings();
  const { appliedToday } = dedupState(log, s);
  $('today').textContent = `Applied today: ${appliedToday} / ${s.dailyCap}`;
  const tbody = $('log');
  tbody.innerHTML = '';
  const rows = log.slice(-300).reverse();
  for (const r of rows) tbody.append(rowEl(r));
  $('log-note').textContent = log.length > 300 ? `Showing the latest 300 of ${log.length} rows. Download CSV for all.` : `${log.length} rows.`;
}

function rowEl(r) {
  const tr = document.createElement('tr');
  const cell = (text, cls) => { const td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.append(td); return td; };
  cell(`${r.date} ${r.time}`);
  const job = cell('');
  const a = document.createElement('a');
  a.href = r.job_url; a.target = '_blank'; a.textContent = r.title || r.job_id;
  job.append(a);
  cell(r.company);
  cell(String(r.score ?? ''));
  cell(r.action, `a-${r.action}`);
  cell(r.reason, 'reason');
  return tr;
}

$('csv').addEventListener('click', async () => {
  const blob = new Blob([toCsv(await loadLog())], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'applied_log.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

$('clear-dry').addEventListener('click', async () => {
  const log = await loadLog();
  const kept = log.filter(r => !r.action.startsWith('would_'));
  if (!confirm(`Remove ${log.length - kept.length} dry-run rows from the log?`)) return;
  await replaceLog(kept);
  renderLog();
});

// ---- run ----
function setRunning(running, ready = true) {
  $('dry').disabled = running || !ready;
  $('live').disabled = running || !ready;
  $('stop').disabled = !running;
  $('state').textContent = running ? (runner.dryRun ? 'Dry run' : 'Applying') : 'Idle';
  $('state').className = running ? 'pill running' : 'pill';
}

function showCounts(counts) {
  $('counts').innerHTML = '';
  for (const [k, v] of Object.entries(counts || {})) {
    const span = document.createElement('span');
    span.textContent = `${k}: ${v}`;
    $('counts').append(span);
  }
}

async function start(dryRun) {
  if (!(await refreshSetup())) return;
  if (!dryRun && !confirm('Start applying for real? Strong matches will be submitted on Naukri.')) return;
  const settings = await loadSettings();
  runner = new Runner(settings, await loadCv(), {
    dryRun,
    onEvent: ev => {
      if (ev.type === 'status') $('status').textContent = ev.text;
      if (ev.type === 'row') { $('log').prepend(rowEl(ev.row)); showCounts(ev.counts); }
      if (ev.type === 'done') {
        $('status').textContent = ev.text;
        if (ev.counts) showCounts(ev.counts);
        runner = null;
        setRunning(false);
        renderLog();
      }
    },
  });
  showCounts({});
  setRunning(true);
  runner.run();
}

$('dry').addEventListener('click', () => start(true));
$('live').addEventListener('click', () => start(false));
$('stop').addEventListener('click', () => {
  if (!runner) return;
  runner.stop();
  $('status').textContent = 'Stopping after the current job…';
});

window.addEventListener('beforeunload', e => { if (runner) { e.preventDefault(); e.returnValue = ''; } });

(async () => {
  fillForm(await loadSettings());
  showProviderFields();
  await refreshSetup();
  renderLog();
})();
