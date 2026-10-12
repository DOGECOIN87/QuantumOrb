"""vault - the seal over QuantumOrb's closed source, and everything needed to check it.

    scheme       qorb-seal-v1: salts, commitments, leaves, nodes
    merkle       the tree, inclusion proofs
    manifest     a seal's file list: strict model, consistency checks
    opened       files published with their salts, checked against the seal
    ledger       every seal, hash-linked, each manifest archived
    history      the ledger held to git's own dates
    diff         which sealed files moved between two seals
    interfaces   the published contracts held to the seal they were cut from
    report       the README's figures, generated from the seal
    cli          python -m vault

Nothing in here can run QuantumOrb. It checks that QuantumOrb exists, and what it was on each day.
"""
from .manifest import Manifest, verify_manifest
from .merkle import MerkleTree, Proof, check_proof, merkle_proof, merkle_root
from .scheme import SCHEME, commitment, leaf, salt_for

__version__ = "1.0.0"

__all__ = ["SCHEME", "Manifest", "MerkleTree", "Proof", "check_proof", "commitment", "leaf", "merkle_proof",
           "merkle_root", "salt_for", "verify_manifest", "__version__"]
