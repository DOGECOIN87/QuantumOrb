# The seal

QuantumOrb's source is closed while it is in development. The seal is how that stays verifiable: it fixes,
in public and on a date, exactly what the code was - without revealing a single line of it.

## What is published

| | |
|---|---|
| [`sealed/manifest.json`](sealed/manifest.json) | every file of the private tree: path, component, size, line count, one 256-bit **commitment** |
| [`sealed/ROOT`](sealed/ROOT) | the Merkle root over all of them |
| [`sealed/ledger.json`](sealed/ledger.json) | every seal so far, in order, each one bound to the one before it |
| [`sealed/archive/`](sealed/archive/) | the full manifest of every seal in the ledger |

The commit that adds a root to this repository is its timestamp: git records it, GitHub records when it
arrived, and neither can be moved backwards without everyone seeing.

## The scheme (`qorb-seal-v1`)

```
salt_i       = HMAC-SHA256(seed, "qorb-seal-v1/salt/" + path_i)
commitment_i = HMAC-SHA256(salt_i, content_i)
leaf_i       = SHA-256(0x00 || path_i || 0x00 || commitment_i)
node         = SHA-256(0x01 || left || right)        an unpaired node is promoted unchanged
root         = the last node                         leaves in path order
```

- **Hiding.** Every file has its own 256-bit salt, so a commitment says nothing about its file - not even
  for a five-line file whose content could otherwise be guessed and confirmed.
- **Binding.** Opening a file to anything other than what was sealed would need an HMAC-SHA256 collision.
- **One secret.** The salts derive from a single seed that never leaves the private tree. Opening a file
  publishes that file's salt and nothing else.
- **Domain separation.** Leaves and inner nodes are hashed with different prefixes, so a leaf can never be
  passed off as a node.

## The ledger

A root on its own says what the code was on one day. The ledger says how it got there:

```
entry.hash = SHA-256("qorb-seal-v1/ledger/" || canonical JSON of the entry without its hash)
entry.prev = the previous entry's hash          (64 zeros for the first)
```

No seal can be dropped, reordered or rewritten later without every following hash changing. CI also holds
the ledger to git: every root in it must have been committed to `sealed/ROOT`, in the ledger's order, and
never before the moment it was sealed.

Because a file's salt depends only on the seed and its path, a file that did not change between two seals
keeps its commitment. So two seals can be compared file by file - *which* files moved, and by how many
lines - and still nothing about *how*:

```bash
python -m vault diff            # the last two seals
python -m vault diff 1 2 --paths
```

## Checking it

Standard library only, Python 3.9+:

```bash
python tools/seal.py verify                 # the manifest is consistent and its root matches sealed/ROOT
python -m vault status                      # one screen: the seal, the ledger, the contracts
python -m vault ledger --verify             # the chain, the archives, and git's own dates
python -m vault proof quantumorb/db.py      # the Merkle path of one file up to the root
python -m vault interfaces                  # every contract was cut from the sealed version of its file
```

CI runs all of these, the vault's tests on Python 3.9, 3.11 and 3.13, and a lint, on every push
([`.github/workflows/checks.yml`](.github/workflows/checks.yml)). The formats are pinned as JSON Schema in
[`schemas/`](schemas/).

## Without trusting the tooling here

[`examples/verify_offline.py`](examples/verify_offline.py) checks an inclusion proof in sixty lines that
import nothing from this repository - the scheme written out again from this page. If the vault were
wrong in a way that happened to agree with itself, the independent copy would disagree.

```bash
python examples/verify_offline.py examples/proofs/quantumorb-db.json
node examples/js/verify.mjs examples/proofs/*.json --ledger sealed/ledger.json
```

The same scheme is implemented a third time in JavaScript ([`examples/js/`](examples/js/)), for Node and for
the browser ([`examples/browser/`](examples/browser/): serve the repository root with
`python -m http.server` and open `/examples/browser/`). CI holds it to the same frozen vectors, the same
published seals and the same proofs as the Python side.

## When a file is opened

An opened file lands in `opened/` with its salt in `opened/salts.json`. Anyone can then run

```bash
python tools/seal.py verify --opened opened/
```

which recomputes each opened file's commitment from its bytes and its salt and compares it with the one
published on the day of the seal. One changed byte, one wrong salt, a missing file, a file that was never
sealed, a path that tries to leave `opened/` - each is reported by name.

## When the code moves on

A new seal is a new root in a new commit and a new entry in the ledger. The old ones stay - in the ledger,
in the archive and in this repository's history - still checkable, so the record of what existed when
only grows.
