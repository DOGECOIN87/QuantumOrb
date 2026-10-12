// The page's only script: wires the three panels to examples/js/qorb-seal.mjs. No network beyond this
// repository's own files, no storage, nothing sent anywhere.

import { checkOpened, verifyLedger, verifyProof } from '../js/qorb-seal.mjs';

const $ = (id) => document.getElementById(id);
const EXAMPLES = [
  ['quantumorb/db.py', '../proofs/quantumorb-db.json'],
  ['site/server/engine/live.js', '../proofs/site-server-engine-live.json'],
  ['guard.py · seal #1', '../proofs/quantumorb-pipeline-guard.seal1.json'],
];

let ledger = null;

function line(out, ok, text) {
  const p = document.createElement('div');
  p.className = ok === null ? 'line info' : ok ? 'line ok' : 'line fail';
  const tag = document.createElement('b');
  tag.textContent = ok === null ? '·' : ok ? 'ok' : 'FAIL';
  p.append(tag, ' ', text);
  out.append(p);
}

function clear(out) {
  out.replaceChildren();
}

async function readJson(file) {
  return JSON.parse(await file.text());
}

// ----------------------------------------------------------------------------- 01 the ledger

function showSeals(entries) {
  const body = $('seals').querySelector('tbody');
  body.replaceChildren();
  for (const e of entries) {
    const tr = document.createElement('tr');
    const cells = [e.n, e.sealed_at.replace('T', ' ').replace('Z', ''), e.files, e.lines.toLocaleString('en'), e.root.slice(0, 16) + '…'];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = String(c);
      tr.append(td);
    }
    tr.title = `${e.root}\n${e.note}`;
    body.append(tr);
  }
  $('seals').hidden = !entries.length;
}

async function checkLedger(data) {
  const out = $('ledger-out');
  clear(out);
  ledger = data;
  const problems = await verifyLedger(data);
  if (problems.length) problems.forEach((p) => line(out, false, p));
  else line(out, true, `${data.entries.length} seal(s) · chain intact · head ${data.entries.at(-1).root.slice(0, 16)}…`);
  showSeals(problems.length ? [] : data.entries);
}

$('load-ledger').addEventListener('click', async () => {
  try {
    const r = await fetch('../../sealed/ledger.json', { cache: 'no-store' });
    if (!r.ok) throw new Error(`sealed/ledger.json: HTTP ${r.status}`);
    await checkLedger(await r.json());
  } catch (e) {
    clear($('ledger-out'));
    line($('ledger-out'), false, `${e.message} - serve the repository root (python -m http.server) and open /examples/browser/`);
  }
});

$('ledger-file').addEventListener('change', async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  try { await checkLedger(await readJson(f)); } catch (e) { clear($('ledger-out')); line($('ledger-out'), false, e.message); }
});

// ----------------------------------------------------------------------------- 02 one proof

for (const [label, url] of EXAMPLES) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'chip';
  b.textContent = label;
  b.addEventListener('click', async () => {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      $('proof').value = JSON.stringify(await r.json(), null, 1);
      await checkProof();
    } catch (e) {
      clear($('proof-out'));
      line($('proof-out'), false, e.message);
    }
  });
  $('examples').append(b);
}

function currentProof() {
  return JSON.parse($('proof').value);
}

async function checkProof() {
  const out = $('proof-out');
  clear(out);
  let p;
  try { p = currentProof(); } catch (e) { return line(out, false, `not JSON: ${e.message}`); }
  const r = await verifyProof(p);
  line(out, r.ok, r.reason);
  if (r.ok && ledger) {
    const e = ledger.entries.find((x) => x.root === p.root);
    if (e) line(out, true, `that root is seal #${e.n}, sealed ${e.sealed_at}`);
    else line(out, false, 'that root is not in the loaded ledger');
  } else if (r.ok) {
    line(out, null, 'load the ledger above to see which seal this root belongs to');
  }
}

$('check-proof').addEventListener('click', checkProof);
$('proof-file').addEventListener('change', async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $('proof').value = await f.text();
  await checkProof();
});

// ----------------------------------------------------------------------------- 03 an opened file

$('check-opened').addEventListener('click', async () => {
  const out = $('opened-out');
  clear(out);
  const f = $('opened-file').files[0];
  if (!f) return line(out, false, 'choose the opened file first');
  let p;
  try { p = currentProof(); } catch { return line(out, false, 'paste or load its proof in 02 first'); }
  const proof = await verifyProof(p);
  if (!proof.ok) return line(out, false, `the proof itself fails: ${proof.reason}`);
  const r = await checkOpened(p, new Uint8Array(await f.arrayBuffer()), $('salt').value.trim().toLowerCase());
  line(out, r.ok, r.reason);
});
