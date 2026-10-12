#!/usr/bin/env python3
"""Check a QuantumOrb inclusion proof with nothing from this repository - not even the vault package.

    python examples/verify_offline.py examples/proofs/quantumorb-db.json
    python examples/verify_offline.py PROOF.json --file opened/quantumorb/db.py --salt <hex>

The scheme is written out again below from SEAL.md, on purpose: if the vault's own code were wrong in a
way that happened to agree with itself, an independent copy would disagree. Read it, it is short.
"""
import argparse
import hashlib
import hmac
import json
import sys


def leaf(path: str, commitment_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + path.encode() + b"\x00" + bytes.fromhex(commitment_hex)).digest()


def climb(start: bytes, steps) -> str:
    h = start
    for side, sibling_hex in steps:
        sibling = bytes.fromhex(sibling_hex)
        pair = sibling + h if side == "L" else h + sibling
        h = hashlib.sha256(b"\x01" + pair).digest()
    return h.hex()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("proof")
    ap.add_argument("--file", help="the opened file, to check it is the one committed to")
    ap.add_argument("--salt", help="that file's salt, from opened/salts.json")
    a = ap.parse_args()

    with open(a.proof, encoding="utf-8") as f:
        p = json.load(f)
    if p.get("scheme") != "qorb-seal-v1":
        print("FAIL  unknown scheme", p.get("scheme"))
        return 1

    lf = leaf(p["path"], p["commitment"])
    if lf.hex() != p["leaf"]:
        print("FAIL  the leaf does not follow from the path and the commitment")
        return 1
    if climb(lf, p["proof"]) != p["root"]:
        print("FAIL  the path does not lead to the root")
        return 1
    print(f"ok    {p['path']} is a leaf of root {p['root']}")

    if a.file:
        with open(a.file, "rb") as f:
            content = f.read()
        c = hmac.new(bytes.fromhex(a.salt), content, hashlib.sha256).hexdigest()
        if c != p["commitment"]:
            print("FAIL  this file is not the one that was sealed")
            return 1
        print(f"ok    {a.file} is byte for byte the sealed file")

    print("      now find that root in sealed/ledger.json, and the commit that published it in git log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
