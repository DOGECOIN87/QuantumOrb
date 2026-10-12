// qorb-seal-v1 in JavaScript: the same scheme as vault/, written again from SEAL.md.
//
// One module for the browser and for Node 20+ - both have WebCrypto as globalThis.crypto.subtle.
// It checks; it never needs the seed. Everything is async because WebCrypto is.
//
//   salt_i       = HMAC-SHA256(seed, "qorb-seal-v1/salt/" + path_i)
//   commitment_i = HMAC-SHA256(salt_i, content_i)
//   leaf_i       = SHA-256(0x00 || path_i || 0x00 || commitment_i)
//   node         = SHA-256(0x01 || left || right)        an unpaired node is promoted unchanged
//   ledger hash  = SHA-256("qorb-seal-v1/ledger/" || canonical JSON of the entry without its hash)

export const SCHEME = 'qorb-seal-v1';
export const GENESIS = '0'.repeat(64);

const subtle = globalThis.crypto && globalThis.crypto.subtle;
if (!subtle) throw new Error('WebCrypto is not available here (Node 20+ or a browser over https/localhost)');

const enc = new TextEncoder();
const HEX64 = /^[0-9a-f]{64}$/;

// ----------------------------------------------------------------------------- bytes

export const isHex64 = (v) => typeof v === 'string' && HEX64.test(v);

export function hexToBytes(hex) {
  if (typeof hex !== 'string' || hex.length % 2 || /[^0-9a-f]/i.test(hex)) throw new Error('not hex: ' + String(hex).slice(0, 16));
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

export const bytesToHex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

const asBytes = (x) => (typeof x === 'string' ? enc.encode(x) : x instanceof Uint8Array ? x : new Uint8Array(x));

// ----------------------------------------------------------------------------- primitives

export async function sha256(data) {
  return new Uint8Array(await subtle.digest('SHA-256', asBytes(data)));
}

export async function hmacSha256(key, data) {
  const k = await subtle.importKey('raw', asBytes(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await subtle.sign('HMAC', k, asBytes(data)));
}

export const saltFor = (seed, path) => hmacSha256(seed, `${SCHEME}/salt/${path}`);

export async function commitment(salt, content) {
  return bytesToHex(await hmacSha256(salt, content));
}

export function leaf(path, commitmentHex) {
  return sha256(concat([0], enc.encode(path), [0], hexToBytes(commitmentHex)));
}

export function node(left, right) {
  return sha256(concat([1], left, right));
}

// ----------------------------------------------------------------------------- the tree

export async function merkleRoot(leaves) {
  if (!leaves.length) return bytesToHex(await sha256(new Uint8Array()));
  let level = leaves;
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i + 1 < level.length; i += 2) next.push(await node(level[i], level[i + 1]));
    if (level.length % 2) next.push(level[level.length - 1]);
    level = next;
  }
  return bytesToHex(level[0]);
}

/** Climb from a leaf to the root its path implies. Steps are [side, siblingHex], side "L" or "R". */
export async function fold(leafHex, steps) {
  let h = hexToBytes(leafHex);
  for (const [side, sibling] of steps) {
    const s = hexToBytes(sibling);
    if (side === 'L') h = await node(s, h);
    else if (side === 'R') h = await node(h, s);
    else throw new Error(`a step's side is L or R, not ${JSON.stringify(side)}`);
  }
  return bytesToHex(h);
}

// ----------------------------------------------------------------------------- proofs

/** { ok, reason } for one inclusion proof, as `python -m vault proof` writes it. */
export async function verifyProof(p) {
  if (!p || p.scheme !== SCHEME) return { ok: false, reason: `unknown scheme ${JSON.stringify(p && p.scheme)}` };
  if (typeof p.path !== 'string' || !p.path) return { ok: false, reason: 'the proof names no path' };
  if (!isHex64(p.commitment) || !isHex64(p.root)) return { ok: false, reason: 'malformed commitment or root' };
  if (!Array.isArray(p.proof)) return { ok: false, reason: 'the proof has no steps' };
  const lf = bytesToHex(await leaf(p.path, p.commitment));
  if (p.leaf !== undefined && p.leaf !== lf) return { ok: false, reason: 'the leaf does not follow from the path and the commitment' };
  let top;
  try { top = await fold(lf, p.proof); } catch (e) { return { ok: false, reason: e.message }; }
  if (top !== p.root) return { ok: false, reason: 'the path does not lead to the root' };
  return { ok: true, reason: `${p.path} is a leaf of root ${p.root}` };
}

/** An opened file against the proof's commitment: is it byte for byte what was sealed? */
export async function checkOpened(p, content, saltHex) {
  if (!isHex64(saltHex)) return { ok: false, reason: 'the salt is not 64 hex characters' };
  const c = await commitment(hexToBytes(saltHex), asBytes(content));
  return c === p.commitment
    ? { ok: true, reason: `the file is byte for byte the sealed ${p.path}` }
    : { ok: false, reason: 'this file is not the one that was sealed' };
}

/** A whole manifest: every leaf recomputed, the root folded again, the totals summed. */
export async function verifyManifest(m) {
  const problems = [];
  if (m.scheme !== SCHEME) problems.push(`unknown scheme ${JSON.stringify(m.scheme)}`);
  const files = m.files || [];
  const paths = files.map((f) => f.path);
  const sorted = [...paths].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  if (paths.some((p, i) => p !== sorted[i]) || new Set(paths).size !== paths.length) problems.push('files are not sorted by path, or a path repeats');
  const bad = files.find((f) => !isHex64(f.commitment));
  if (bad) return [...problems, `bad commitment for ${bad.path}`];
  const root = await merkleRoot(await Promise.all(files.map((f) => leaf(f.path, f.commitment))));
  if (root !== m.root) problems.push(`manifest root ${m.root} != recomputed ${root}`);
  const t = files.reduce((a, f) => ({ files: a.files + 1, lines: a.lines + f.lines, bytes: a.bytes + f.bytes }), { files: 0, lines: 0, bytes: 0 });
  const s = m.source || {};
  if (t.files !== s.files || t.lines !== s.lines || t.bytes !== s.bytes) problems.push('totals do not add up to the files');
  return problems;
}

// ----------------------------------------------------------------------------- the ledger

/** JSON with sorted keys and no whitespace - Python's json.dumps(sort_keys=True, separators=(",", ":")). */
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}

export async function entryHash(entry) {
  const { hash, ...body } = entry; // eslint-disable-line no-unused-vars
  return bytesToHex(await sha256(`${SCHEME}/ledger/` + canonical(body)));
}

/** The chain only: numbering, links, hashes, time order. Archives and git dates are vault's job. */
export async function verifyLedger(ledger) {
  const problems = [];
  if (!ledger || ledger.scheme !== SCHEME) problems.push('not a qorb-seal-v1 ledger');
  const entries = (ledger && ledger.entries) || [];
  if (!entries.length) return [...problems, 'the ledger is empty'];
  let prev = GENESIS;
  let prevTime = '';
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const tag = `ledger entry ${e.n}`;
    if (e.n !== i + 1) problems.push(`${tag}: expected number ${i + 1}`);
    if (e.prev !== prev) problems.push(`${tag}: does not point at the entry before it`);
    if (e.hash !== (await entryHash(e))) problems.push(`${tag}: its hash does not match its contents`);
    if (prevTime && !(e.sealed_at > prevTime)) problems.push(`${tag}: sealed before the entry it follows`);
    prev = e.hash;
    prevTime = e.sealed_at;
  }
  return problems;
}
