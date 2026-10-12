#!/usr/bin/env python3
"""The QuantumOrb seal: prove what the source was on a given day, without showing it.

The code behind QuantumOrb is closed while it is in development. This is how that stays checkable.
Every file of the private tree is committed to with a salted hash, the commitments are folded into one
Merkle root, and the root is published in this repository - so its date is fixed by git and by GitHub.
Later, any file can be opened together with its salt, and anyone can confirm that it is byte for byte
the file that was sealed, and that it was part of the sealed tree.

    salt_i       = HMAC-SHA256(seed, "qorb-seal-v1/salt/" + path_i)
    commitment_i = HMAC-SHA256(salt_i, content_i)
    leaf_i       = SHA-256(0x00 || path_i || 0x00 || commitment_i)
    node         = SHA-256(0x01 || left || right)       odd nodes are promoted unchanged
    root         = the last node; leaves are ordered by path

    python tools/seal.py verify                          # the manifest is consistent and matches ROOT
    python tools/seal.py verify --opened opened/         # and every opened file matches its commitment
    python tools/seal.py proof quantumorb/db.py          # one file's Merkle path up to the root

This file is the stable entry point the README and CI have always used; the work is done by the `vault`
package next to it (`python -m vault --help` lists everything it can check). Standard library only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault.cli import main  # noqa: E402
from vault.manifest import COMPONENTS, component_of, verify_manifest  # noqa: E402,F401
from vault.merkle import check_proof, merkle_proof, merkle_root  # noqa: E402,F401
from vault.scheme import SCHEME, commitment, leaf, salt_for  # noqa: E402,F401
from vault.scheme import count_lines as _lines  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
