// The JavaScript copy of the scheme against the Python one: the same frozen vectors, the same published
// seal, the same proofs. If the two implementations ever disagree, one of them is wrong - and CI says so.
//
//   node --test examples/js/

import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  bytesToHex, canonical, checkOpened, commitment, entryHash, fold, hexToBytes, leaf, merkleRoot, saltFor,
  verifyLedger, verifyManifest, verifyProof,
} from './qorb-seal.mjs';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const json = async (...p) => JSON.parse(await readFile(join(REPO, ...p), 'utf8'));
const SEED = Uint8Array.from({ length: 32 }, (_, i) => i);

test('the frozen vectors of tests/test_scheme.py', async () => {
  const salt = await saltFor(SEED, 'quantumorb/db.py');
  assert.equal(bytesToHex(salt), '571bfbe8b13a5b6a7abc01ee6b6f444146f4acf2fa5ddf4e514acc0530f80d4e');
  const c = await commitment(salt, 'import sqlite3\n');
  assert.equal(c, 'bd252adc407e51464b446898b71f4e8d9071cc30c5926146d50b896e02f87d43');
  assert.equal(bytesToHex(await leaf('quantumorb/db.py', c)), '7f9709b24c92014d67710183c8be5499f6ced95202e8227a345746178114c376');
});

test('the published manifest folds to sealed/ROOT', async () => {
  const m = await json('sealed', 'manifest.json');
  const root = (await readFile(join(REPO, 'sealed', 'ROOT'), 'utf8')).trim();
  assert.deepEqual(await verifyManifest(m), []);
  assert.equal(m.root, root);
});

test('every archived manifest is intact and matches its ledger entry', async () => {
  const ledger = await json('sealed', 'ledger.json');
  const archived = (await readdir(join(REPO, 'sealed', 'archive'))).sort();
  assert.equal(archived.length, ledger.entries.length);
  for (const [i, name] of archived.entries()) {
    const m = await json('sealed', 'archive', name);
    assert.deepEqual(await verifyManifest(m), [], name);
    assert.equal(m.root, ledger.entries[i].root, name);
  }
});

test('a changed commitment, a reordered list or a wrong total is caught', async () => {
  const m = await json('sealed', 'manifest.json');
  const a = structuredClone(m);
  a.files[7].commitment = '0'.repeat(64);
  assert.ok((await verifyManifest(a)).some((p) => p.includes('root')));
  const b = structuredClone(m);
  [b.files[0], b.files[1]] = [b.files[1], b.files[0]];
  assert.ok((await verifyManifest(b)).some((p) => p.includes('sorted')));
  const c = structuredClone(m);
  c.source.lines += 1;
  assert.ok((await verifyManifest(c)).some((p) => p.includes('totals')));
});

test('the ledger chain, and Python\'s canonical JSON', async () => {
  const ledger = await json('sealed', 'ledger.json');
  assert.deepEqual(await verifyLedger(ledger), []);
  assert.equal(canonical({ b: 1, a: [true, null, 'x·y'] }), '{"a":[true,null,"x·y"],"b":1}');
  const forged = structuredClone(ledger);
  forged.entries[0].note = 'rewritten later';
  assert.ok((await verifyLedger(forged)).some((p) => p.includes('its hash')));
  forged.entries[0].hash = await entryHash(forged.entries[0]);
  if (forged.entries.length > 1) assert.ok((await verifyLedger(forged)).some((p) => p.includes('does not point')));
});

test('every example proof checks out, and a tampered one does not', async () => {
  const dir = join(REPO, 'examples', 'proofs');
  const ledger = await json('sealed', 'ledger.json');
  const roots = new Set(ledger.entries.map((e) => e.root));
  for (const name of await readdir(dir)) {
    const p = JSON.parse(await readFile(join(dir, name), 'utf8'));
    assert.deepEqual(await verifyProof(p), { ok: true, reason: `${p.path} is a leaf of root ${p.root}` }, name);
    assert.ok(roots.has(p.root), `${name}: a root this repository never published`);
    const bad = structuredClone(p);
    bad.proof[0][1] = 'f'.repeat(64);
    assert.equal((await verifyProof(bad)).ok, false);
    assert.equal((await verifyProof({ ...p, path: p.path + 'x' })).ok, false);
  }
});

test('an opened file is checked byte for byte', async () => {
  const content = new TextEncoder().encode('def run():\n    ...\n');
  const salt = await saltFor(SEED, 'a/b.py');
  const c = await commitment(salt, content);
  const leaves = [await leaf('a/a.py', '1'.repeat(64)), await leaf('a/b.py', c), await leaf('a/c.py', '2'.repeat(64))];
  const root = await merkleRoot(leaves);
  const p = { scheme: 'qorb-seal-v1', path: 'a/b.py', commitment: c, proof: [['L', bytesToHex(leaves[0])], ['R', bytesToHex(leaves[2])]], root };
  assert.equal(await fold(bytesToHex(leaves[1]), p.proof), root);
  assert.ok((await verifyProof(p)).ok);
  assert.ok((await checkOpened(p, content, bytesToHex(salt))).ok);
  assert.equal((await checkOpened(p, new TextEncoder().encode('def run():\n    pass\n'), bytesToHex(salt))).ok, false);
  assert.equal((await checkOpened(p, content, 'nothex')).ok, false);
});

test('hex helpers refuse what is not hex', () => {
  assert.deepEqual([...hexToBytes('00ff10')], [0, 255, 16]);
  assert.throws(() => hexToBytes('abc'));
  assert.throws(() => hexToBytes('zz'));
});
