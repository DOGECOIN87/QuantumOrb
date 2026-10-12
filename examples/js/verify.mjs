#!/usr/bin/env node
// Check QuantumOrb seal files from Node, with nothing but the module next to this file.
//
//   node examples/js/verify.mjs examples/proofs/*.json
//   node examples/js/verify.mjs --ledger sealed/ledger.json --manifest sealed/manifest.json
//   node examples/js/verify.mjs PROOF.json --file opened/quantumorb/db.py --salt <hex>

import { readFile } from 'node:fs/promises';
import { checkOpened, verifyLedger, verifyManifest, verifyProof } from './qorb-seal.mjs';

function parse(argv) {
  const out = { proofs: [], ledger: null, manifest: null, file: null, salt: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--ledger') out.ledger = argv[++i];
    else if (a === '--manifest') out.manifest = argv[++i];
    else if (a === '--file') out.file = argv[++i];
    else if (a === '--salt') out.salt = argv[++i];
    else if (a === '-h' || a === '--help') out.help = true;
    else out.proofs.push(a);
  }
  return out;
}

const json = async (path) => JSON.parse(await readFile(path, 'utf8'));
const say = (ok, msg) => console.log(`${ok ? 'ok   ' : 'FAIL '} ${msg}`);

async function main() {
  const args = parse(process.argv.slice(2));
  if (args.help || (!args.proofs.length && !args.ledger && !args.manifest)) {
    console.log('usage: verify.mjs [PROOF.json ...] [--file F --salt HEX] [--ledger L.json] [--manifest M.json]');
    return args.help ? 0 : 2;
  }
  let failed = 0;
  for (const path of args.proofs) {
    const p = await json(path);
    const r = await verifyProof(p);
    say(r.ok, r.reason);
    if (!r.ok) failed++;
    if (r.ok && args.file) {
      if (!args.salt) throw new Error('--file needs --salt');
      const o = await checkOpened(p, await readFile(args.file), args.salt);
      say(o.ok, o.reason);
      if (!o.ok) failed++;
    }
  }
  if (args.manifest) {
    const m = await json(args.manifest);
    const problems = await verifyManifest(m);
    problems.forEach((p) => say(false, p));
    if (!problems.length) say(true, `${m.source.files} files · ${m.source.lines.toLocaleString('en')} lines · root ${m.root}`);
    failed += problems.length;
  }
  if (args.ledger) {
    const l = await json(args.ledger);
    const problems = await verifyLedger(l);
    problems.forEach((p) => say(false, p));
    if (!problems.length) say(true, `ledger of ${l.entries.length} seal(s) · chain intact · head ${l.entries.at(-1).root.slice(0, 16)}…`);
    failed += problems.length;
  }
  return failed ? 1 : 0;
}

main().then((code) => process.exit(code), (e) => { console.error('FAIL ', e.message); process.exit(1); });
