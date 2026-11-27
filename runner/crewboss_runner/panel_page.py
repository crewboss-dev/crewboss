"""The control panel's page: HTML, CSS and JS as three local assets (no CDN, no inline script or style: the CSP is 'self' only)."""
from __future__ import annotations

HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Crewboss runner</title><link rel="stylesheet" href="/app.css"></head><body>
<a class="skip" href="#main">Skip to the controls</a>
<header class="top"><div class="brand"><span class="mark" aria-hidden="true"></span><h1>Crewboss runner</h1></div>
<a id="open-crewboss" class="link" href="#" hidden target="_blank" rel="noopener noreferrer">Open Crewboss</a></header>
<main id="main"><p id="boot" role="status">Opening...</p>
<div id="app" hidden>
<section class="card hero" aria-labelledby="h-state"><div class="state-line"><span id="dot" class="dot grey" aria-hidden="true"></span>
<div><h2 id="h-state">Status</h2><p id="status-text" class="big" aria-live="polite"></p></div>
<div class="btns"><button id="btn-start" class="btn primary" type="button">Start</button><button id="btn-stop" class="btn" type="button">Stop</button></div></div>
<p id="action-msg" class="msg" role="status" aria-live="polite"></p>
<dl class="facts"><div><dt>Server</dt><dd id="f-server"></dd></div><div><dt>Runner name</dt><dd id="f-name"></dd></div>
<div><dt>State</dt><dd id="f-state"></dd></div><div><dt>Keeping this PC awake</dt><dd id="f-awake"></dd></div></dl>
<div id="job" class="job" hidden><h3>Current job</h3><p class="job-title"><span id="job-title"></span> <a id="job-link" class="link" href="#" target="_blank" rel="noopener noreferrer" hidden>Open in Crewboss</a></p><p id="job-meta" class="muted"></p></div>
<div id="recent" hidden><h3>Last jobs</h3><ul id="recent-list" class="list"></ul></div></section>

<section id="enrol" class="card" aria-labelledby="h-enrol" hidden><h2 id="h-enrol">Enrol this PC</h2>
<p class="muted">Make a code on the Crewboss Runners page and paste it here. It is used once and sent only to Crewboss.</p>
<form id="enrol-form" class="row" autocomplete="off"><label for="enrol-code" class="sr">Enrolment code</label>
<input id="enrol-code" class="input" type="password" spellcheck="false" autocomplete="off" placeholder="host:port#fingerprint-secret">
<button class="btn primary" type="submit">Enrol</button></form></section>

<section class="card" aria-labelledby="h-auto"><h2 id="h-auto">Start automatically</h2>
<fieldset class="radios" id="auto-radios"><legend class="sr">Automatic start</legend></fieldset>
<p id="auto-msg" class="msg" role="status" aria-live="polite"></p>
<label class="check" id="tray-auto-row"><input type="checkbox" id="tray-auto"><span>Show this tray icon when I sign in</span></label></section>

<section class="card" aria-labelledby="h-ka"><h2 id="h-ka">Keep awake during jobs</h2>
<label class="switch"><input type="checkbox" id="ka-toggle" role="switch"><span class="track" aria-hidden="true"></span><span id="ka-label">Keep this PC awake while a job runs</span></label>
<p id="ka-note" class="muted"></p></section>

<section class="card" aria-labelledby="h-health"><div class="row between"><h2 id="h-health">Health</h2><button id="btn-recheck" class="btn small" type="button">Check again</button></div>
<p id="elevated" class="muted" hidden></p><ul id="health-list" class="list checks"></ul><div id="repairs"></div><p id="health-msg" class="msg" role="status" aria-live="polite"></p></section>

<section class="card" aria-labelledby="h-log"><h2 id="h-log">Log</h2><pre id="log" class="log" tabindex="0" aria-label="The last 100 lines of the runner log"></pre></section>
<section class="card danger-zone" aria-labelledby="h-danger"><h2 id="h-danger">Danger zone</h2>
<p class="muted">Take the runner off this PC completely.</p><button id="btn-remove" class="btn danger-outline" type="button">Remove runner from this PC</button>
<div id="removed" hidden><p class="msg">The runner was removed. To remove the program itself, run this in a terminal:</p>
<div class="row"><code id="final-cmd" class="cmd"></code><button id="btn-copy" class="btn small" type="button">Copy</button></div></div></section>
</div></main>
<dialog id="confirm" aria-labelledby="c-title"><form method="dialog"><h2 id="c-title">Stop the runner?</h2>
<p>A job is running. Stopping now cancels it, and it goes back to the queue.</p>
<div class="btns end"><button class="btn" value="no" autofocus>Keep running</button><button class="btn danger" value="yes" id="c-yes">Stop and cancel the job</button></div></form></dialog>
<dialog id="remove-dlg" aria-labelledby="r-title"><form method="dialog" id="remove-form"><h2 id="r-title">Remove the runner from this PC?</h2>
<p>This stops the runner, removes its automatic start and the tray's, and deletes this PC's device key and token, its settings and its job folders.</p>
<p>Your own repositories are not touched. The device stays listed on <a id="runners-link" class="link" href="#" target="_blank" rel="noopener noreferrer">the Crewboss Runners page</a> until someone revokes it there.</p>
<label for="remove-type">Type <strong>REMOVE</strong> to confirm</label><input id="remove-type" class="input" autocomplete="off" spellcheck="false">
<p id="remove-msg" class="msg" role="status"></p>
<div class="btns end"><button class="btn" value="no" id="remove-cancel">Cancel</button><button class="btn danger" id="remove-go" type="button" disabled>Remove runner</button></div></form></dialog>
<script src="/app.js"></script></body></html>"""

CSS = """
:root{--bg:#f4f1ea;--card:#fbf9f5;--ink:#2a2723;--muted:#6f685e;--line:#e2ddd2;--accent:#c4643b;--accent-ink:#fff;--focus:#2d6cdf;
--grey:#9a948a;--green:#3f8f5b;--blue:#3b6fd1;--amber:#d08a1a;--danger:#a8372a;--shadow:0 1px 2px rgba(60,40,20,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#1d1b18;--card:#26231f;--ink:#efeae1;--muted:#a79f92;--line:#38342e;--accent:#e0805a;--accent-ink:#1d1b18;
--focus:#7fa6f5;--grey:#7d776d;--green:#58b378;--blue:#6f97ee;--amber:#e0a23a;--danger:#e0705f;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:var(--card);padding:.5rem .75rem;border-radius:8px;z-index:9}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.top{max-width:48rem;margin:0 auto;padding:1.4rem 1rem .4rem;display:flex;align-items:center;justify-content:space-between;gap:1rem}
.brand{display:flex;align-items:center;gap:.6rem}.brand h1{font-size:1.15rem;font-weight:600;margin:0;letter-spacing:-.01em}
.mark{width:1.15rem;height:1.15rem;border-radius:.35rem;background:var(--accent);display:inline-block}
main{max-width:48rem;margin:0 auto;padding:.5rem 1rem 3rem;display:grid;gap:.9rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem;box-shadow:var(--shadow)}
h2{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 .5rem}
h3{font-size:.9rem;margin:1rem 0 .3rem}.big{font-size:1.25rem;font-weight:600;margin:0;letter-spacing:-.01em}
.muted{color:var(--muted);margin:.2rem 0}.msg{min-height:1.3em;margin:.5rem 0 0;color:var(--muted);font-size:.9rem}.msg.err{color:var(--danger)}
.state-line{display:flex;align-items:center;gap:.9rem;flex-wrap:wrap}.state-line>div:nth-child(2){flex:1;min-width:12rem}
.dot{width:.95rem;height:.95rem;border-radius:50%;background:var(--grey);flex:none;box-shadow:0 0 0 4px color-mix(in srgb,var(--grey) 22%,transparent)}
.dot.green{background:var(--green);box-shadow:0 0 0 4px color-mix(in srgb,var(--green) 22%,transparent)}
.dot.blue{background:var(--blue);box-shadow:0 0 0 4px color-mix(in srgb,var(--blue) 22%,transparent)}
.dot.amber{background:var(--amber);box-shadow:0 0 0 4px color-mix(in srgb,var(--amber) 24%,transparent)}
.btns{display:flex;gap:.5rem}.btns.end{justify-content:flex-end;margin-top:1rem}
.btn{font:inherit;font-weight:500;padding:.5rem 1rem;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;
transition:transform .08s,background-color .12s,border-color .12s}.btn:hover:not(:disabled){border-color:var(--muted)}.btn:active:not(:disabled){transform:scale(.97)}
.btn:disabled{opacity:.45;cursor:not-allowed}.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.danger{background:var(--danger);border-color:var(--danger);color:#fff}.btn.small{padding:.25rem .7rem;font-size:.85rem}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.7rem 1.4rem;margin:1rem 0 0}.facts div{min-width:0}
dt{font-size:.78rem;color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.job{margin-top:1rem;padding:.8rem 1rem;border-radius:10px;background:color-mix(in srgb,var(--blue) 10%,var(--card));border:1px solid color-mix(in srgb,var(--blue) 30%,var(--line))}
.job h3{margin:0 0 .2rem;color:var(--blue)}.job-title{font-weight:600;margin:0}
.list{list-style:none;margin:.2rem 0;padding:0}.list li{padding:.3rem 0;border-top:1px solid var(--line);display:flex;gap:.6rem;align-items:baseline}.list li:first-child{border-top:0}
.list .o{margin-left:auto;color:var(--muted);font-size:.85rem;white-space:nowrap}
.checks li .b{flex:none;font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:.05rem .4rem;border-radius:6px;border:1px solid var(--line)}
.b.pass{color:var(--green)}.b.warn{color:var(--amber)}.b.fail{color:var(--danger)}
.checks .id{font-weight:500}.checks .d{color:var(--muted);overflow-wrap:anywhere}
.radios{border:0;margin:0;padding:0;display:grid;gap:.4rem}
.radios label,.check{display:flex;gap:.65rem;align-items:flex-start;padding:.55rem .7rem;border:1px solid var(--line);border-radius:10px;cursor:pointer}
.radios label:has(input:checked){border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--card))}
.check{margin-top:.7rem}input[type=radio],input[type=checkbox]{accent-color:var(--accent);width:1.05rem;height:1.05rem;margin:.2rem 0 0;flex:none}
.switch{display:flex;align-items:center;gap:.7rem;cursor:pointer}.switch input{position:absolute;opacity:0}
.track{width:2.5rem;height:1.45rem;border-radius:999px;background:var(--grey);position:relative;flex:none;transition:background-color .15s}
.track::after{content:"";position:absolute;top:.17rem;left:.17rem;width:1.1rem;height:1.1rem;border-radius:50%;background:#fff;transition:transform .15s}
.switch input:checked+.track{background:var(--accent)}.switch input:checked+.track::after{transform:translateX(1.05rem)}
.switch input:focus-visible+.track{outline:2px solid var(--focus);outline-offset:2px}.switch input:disabled+.track{opacity:.5}
.row{display:flex;gap:.6rem;align-items:center}.row.between{justify-content:space-between}
.input{flex:1;font:inherit;padding:.5rem .7rem;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.link{color:var(--accent);font-weight:500;text-decoration:none}.link:hover{text-decoration:underline}
.log{margin:0;max-height:15rem;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:.7rem;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);padding:1.2rem 1.3rem;max-width:26rem}
dialog::backdrop{background:rgba(20,15,10,.45)}dialog h2{text-transform:none;font-size:1.05rem;letter-spacing:0;color:var(--ink)}
.danger-zone{border-color:color-mix(in srgb,var(--danger) 45%,var(--line))}.danger-outline{color:var(--danger);border-color:var(--danger)}
.cmd{flex:1;padding:.5rem .7rem;background:var(--bg);border:1px solid var(--line);border-radius:10px;font:13px ui-monospace,Menlo,Consolas,monospace}
#repairs{display:grid;gap:.4rem;margin-top:.6rem}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""(() => {
'use strict';
const $ = id => document.getElementById(id);
let session = '', last = null, busy = false;
function keep(v) { try { if (v) sessionStorage.setItem('cb', v); return sessionStorage.getItem('cb') || ''; } catch (e) { return v || ''; } }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function say(id, text, err) { const n = $(id); n.textContent = text || ''; n.classList.toggle('err', !!err); }
async function api(path, opts) { let r; try { r = await fetch(path, {...opts, headers: {...((opts || {}).headers || {}), 'Authorization': 'Bearer ' + session}}); } catch (e) { return {status: 0, body: {detail: 'cannot reach the runner'}}; } let j = {}; try { j = await r.json(); } catch (e) {} return {status: r.status, body: j}; }
async function act(name, body) {
  const r = await api('/api/action/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  return r;
}
function fmt(s) { if (s == null) return ''; const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? h + ' h ' + (m % 60) + ' min' : m ? m + ' min ' + (s % 60) + ' s' : s + ' s'; }
function ago(t) { const s = Math.max(0, Math.floor(Date.now() / 1000 - t)); return s < 90 ? 'just now' : fmt(s - s % 60) + ' ago'; }
const NAMES = {done: 'finished', failed: 'failed', released: 'could not start', lost: 'lost its lease'};

// The connected server is shown ONLY here: one profile = plain text, several = a switcher (#975).
function renderServer(s) {
  const dd = $('f-server'), ps = s.profiles || [];
  $('f-name').textContent = s.name ? s.name + (s.device_id ? ' (#' + s.device_id + ')' : '') : 'not enrolled';
  if (ps.length < 2) { dd.textContent = s.server || 'not enrolled'; return; }
  let sel = $('server-sel');
  if (!sel) {
    dd.replaceChildren(); sel = el('select'); sel.id = 'server-sel'; sel.setAttribute('aria-label', 'Server this runner connects to');
    sel.addEventListener('change', () => switchServer(sel.value));
    const m = el('div', 'msg'); m.id = 'server-msg'; m.setAttribute('role', 'status'); dd.append(sel, m);
  }
  const keys = ps.map(p => p.name + '|' + p.server + '|' + p.state).join();
  if (sel.dataset.keys !== keys) {
    sel.dataset.keys = keys; sel.replaceChildren();
    ps.forEach(p => { const o = el('option', '', p.server + (p.name !== p.server ? ' (' + p.name + ')' : '') + (p.state !== 'enrolled' ? ' - ' + p.state : '')); o.value = p.name; sel.append(o); });
  }
  const act2 = ps.find(p => p.active); if (document.activeElement !== sel) sel.value = act2 ? act2.name : '';
  sel.disabled = busy;
}
async function switchServer(name) {
  const r = await doAct('use_profile', {name}, 'server-msg');
  if (r.body.needs_confirm && window.confirm(r.body.message + ' Switch anyway?')) await doAct('use_profile', {name, confirm: true}, 'server-msg');
  await poll();
  const sel = $('server-sel'), a = last && (last.profiles || []).find(p => p.active); if (sel && a) sel.value = a.name;   // a refused switch snaps back
}
function render(s) {
  if (s.runners_page) $('runners-link').href = s.runners_page;

  last = s;
  $('dot').className = 'dot ' + s.colour;
  $('status-text').textContent = s.status;
  renderServer(s);
  $('f-state').textContent = s.running ? (s.link === 'ready' ? 'Running, connected' : 'Running, ' + s.link) : 'Not running';
  const ka = s.keep_awake;
  $('f-awake').textContent = ka.holding ? 'Yes: keeping this PC awake while the job runs' : ka.enabled ? 'Not now (only while a job runs)' : 'Off';
  const a = $('open-crewboss'); if (s.crewboss_url) { a.href = s.crewboss_url; a.hidden = false; } else a.hidden = true;
  $('btn-start').disabled = s.running || !s.enrolled || busy; $('btn-stop').disabled = !s.running || busy;
  const j = s.job; $('job').hidden = !j;
  if (j) { $('job-title').textContent = (j.project ? j.project + ': ' : '') + 'Task #' + j.task_id; const jl = $('job-link'); jl.hidden = !s.crewboss_url; if (s.crewboss_url) jl.href = s.crewboss_url + '/all/tasks/' + encodeURIComponent(j.task_id); $('job-meta').textContent = [j.project, j.branch, j.state, j.elapsed_s != null ? 'running for ' + fmt(j.elapsed_s) : ''].filter(Boolean).join(' · '); }
  const rl = $('recent-list'); rl.replaceChildren(); $('recent').hidden = !s.recent_jobs.length;
  s.recent_jobs.forEach(x => { const li = el('li'); li.append(el('span', '', 'Task #' + (x.task_id || '?') + ' (job ' + x.id + ')'), el('span', 'o', (NAMES[x.outcome] || x.outcome) + ' · ' + ago(x.ended))); rl.append(li); });
  $('enrol').hidden = s.enrolled;
  // automatic start
  const box = $('auto-radios'), keys = s.autostart.options.map(o => o.key).join();
  if (box.dataset.keys !== keys) {
    box.dataset.keys = keys; box.querySelectorAll('label').forEach(n => n.remove());
    s.autostart.options.forEach(o => { const l = el('label'); const i = el('input'); i.type = 'radio'; i.name = 'auto'; i.value = o.key; l.append(i, el('span', '', o.label)); box.append(l); });
  }
  box.querySelectorAll('input').forEach(i => { i.checked = i.value === s.autostart.kind; i.disabled = busy || s.elevation.state === 'running'; });
  const el2 = $('elevated'); el2.hidden = s.elevated == null; if (s.elevated != null) el2.textContent = s.elevated ? 'Administrator rights: YES. The runner and its agents run elevated; start it from a normal window or the service instead.' : 'Administrator rights: no. The runner and its agents run with a standard-user token.';
  const e = s.elevation; if (e.state !== 'idle' && e.message) say('auto-msg', e.message, e.state === 'failed');
  const t = $('tray-auto'); $('tray-auto-row').hidden = !s.tray_autostart.available; if (document.activeElement !== t) t.checked = s.tray_autostart.on;
  // keep awake
  const kt = $('ka-toggle'); if (document.activeElement !== kt) kt.checked = ka.enabled; kt.disabled = ka.locked_by_env || !ka.available;
  $('ka-note').textContent = ka.locked_by_env ? 'Set by an environment variable on this PC, so it cannot be changed here.' : ka.available ? 'Stops the PC going to sleep on its own, only while a job runs; the screen can still turn off. Closing a laptop lid still sleeps it. (' + ka.how + ')' : 'Not available here: ' + ka.how;
}
async function poll() {
  const r = await api('/api/state');
  if (r.status === 200) { render(r.body); } else if (r.status !== 0) { $('app').hidden = true; $('boot').hidden = false; $('boot').textContent = 'This page has ended. Open the control panel again from the tray icon or with crewboss-runner tray.'; clearInterval(timer); clearInterval(logTimer); }
}
async function loadLog() { const r = await api('/api/log'); if (r.status !== 200) return; const p = $('log'), atEnd = p.scrollTop + p.clientHeight >= p.scrollHeight - 8;
  p.textContent = r.body.lines.length ? r.body.lines.join('\n') : 'No log file yet. A runner started from a console prints there instead.'; if (atEnd) p.scrollTop = p.scrollHeight; }
function renderHealth(h) {
  const ul = $('health-list'); ul.replaceChildren(); say('health-msg', h.error, true);
  h.rows.forEach(r => { const li = el('li'); li.append(el('span', 'b ' + r.status, r.status), el('span', 'id', r.id.replace(/_/g, ' ')), el('span', 'd', r.detail + (r.fix ? ' (' + r.fix + ')' : ''))); ul.append(li); });
  const rp = $('repairs'); rp.replaceChildren();
  h.repairs.forEach(x => { const b = el('button', 'btn small', 'Repair: ' + x.text); b.type = 'button'; b.addEventListener('click', async () => { b.disabled = true; const r = await act('repair', {id: x.id}); say('health-msg', r.body.message || r.body.detail, !r.body.ok); loadHealth(); }); rp.append(b); });
}
async function loadHealth() { const r = await api('/api/health'); if (r.status === 200) renderHealth(r.body); }

async function doAct(name, body, msgId, after) {
  busy = true; if (last) render(last); say(msgId, 'Working...');
  const r = await act(name, body); busy = false;
  say(msgId, r.body.message || r.body.detail || '', !(r.body.ok));
  await poll(); if (after) after(r); return r;
}
$('btn-start').addEventListener('click', () => doAct('start', {}, 'action-msg'));
$('btn-stop').addEventListener('click', async () => {
  if (last && last.job) { const d = $('confirm'); d.returnValue = ''; d.showModal(); return; }
  doAct('stop', {}, 'action-msg');
});
$('confirm').addEventListener('close', () => { if ($('confirm').returnValue === 'yes') doAct('stop', {confirm: true}, 'action-msg'); });
$('auto-radios').addEventListener('change', async ev => {
  const v = ev.target.value; if (v === 'service') say('auto-msg', 'Windows will ask for administrator permission, then ask for your Windows password in its own window (never here).');
  const r = await doAct('autostart', {mode: v}, 'auto-msg', poll);
  if (r.body.needs_confirm && window.confirm(r.body.message + ' Change the automatic start anyway?')) doAct('autostart', {mode: v, confirm: true}, 'auto-msg', poll);
});
$('tray-auto').addEventListener('change', ev => doAct('tray_autostart', {on: ev.target.checked}, 'auto-msg'));
$('ka-toggle').addEventListener('change', ev => doAct('keep_awake', {on: ev.target.checked}, 'ka-note'));
$('btn-recheck').addEventListener('click', async () => { say('health-msg', 'Checking...'); await act('recheck'); await loadHealth(); say('health-msg', ''); });
$('enrol-form').addEventListener('submit', async ev => { ev.preventDefault(); const c = $('enrol-code'); const r = await doAct('enrol', {code: c.value}, 'action-msg'); if (r.body.ok) c.value = ''; });

$('btn-remove').addEventListener('click', () => { $('remove-type').value = ''; $('remove-go').disabled = true; say('remove-msg', ''); $('remove-dlg').showModal(); $('remove-type').focus(); });
$('remove-type').addEventListener('input', () => { $('remove-go').disabled = $('remove-type').value !== 'REMOVE'; });
function removed(cmd) { $('remove-dlg').close(); clearInterval(timer); clearInterval(logTimer); document.querySelectorAll('section.card:not(.danger-zone), #btn-remove').forEach(n => n.hidden = true); $('removed').hidden = false; $('final-cmd').textContent = cmd; }
$('remove-go').addEventListener('click', async () => {
  $('remove-go').disabled = true; say('remove-msg', 'Removing...');
  const r = await act('remove', {confirm: $('remove-type').value});
  if (r.body.ok && r.body.pending) {   // an administrator step: only report "removed" once the elevated child really finished
    say('remove-msg', 'Waiting for administrator approval...');
    for (;;) {
      await new Promise(res => setTimeout(res, 1200));
      const st = await api('/api/state'); if (st.status === 0) { removed(r.body.final_command); return; }   // the tray quit: it finished
      if (st.status !== 200) break;
      const e = st.body.elevation;
      if (e.state === 'done') { removed(r.body.final_command); return; }
      if (e.state === 'failed') { say('remove-msg', e.message, true); $('remove-go').disabled = false; return; }
    }
    return;
  }
  if (r.body.ok) { removed(r.body.final_command); return; }
  if (r.status === 0) { $('remove-dlg').close(); return; }
  say('remove-msg', r.body.message || r.body.detail, true); $('remove-go').disabled = false;
});
$('btn-copy').addEventListener('click', async () => { try { await navigator.clipboard.writeText($('final-cmd').textContent); $('btn-copy').textContent = 'Copied'; } catch (e) { const r = document.createRange(); r.selectNodeContents($('final-cmd')); getSelection().removeAllRanges(); getSelection().addRange(r); } });

let timer, logTimer;
(async () => {
  const boot = $('boot'), token = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  if (token) {
    const ex = await api('/api/exchange', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})});
    if (ex.status !== 200) { boot.textContent = 'That link was already used. Open the control panel again from the tray icon, or run crewboss-runner status for a fresh link.'; return; }
    session = keep(ex.body.session);
  } else { session = keep(''); }
  const r = await api('/api/state');
  if (r.status !== 200) { boot.textContent = 'Open the control panel from the tray icon, or run crewboss-runner status for a link (it carries a one-use key).'; return; }
  boot.hidden = true; $('app').hidden = false; render(r.body); loadHealth(); loadLog();
  timer = setInterval(poll, 3000); logTimer = setInterval(loadLog, 3000);
})();
})();
"""

ASSETS = {"/": (HTML, "text/html; charset=utf-8"), "/index.html": (HTML, "text/html; charset=utf-8"),
          "/app.css": (CSS, "text/css; charset=utf-8"), "/app.js": (JS, "text/javascript; charset=utf-8")}
