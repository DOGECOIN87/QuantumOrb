"""qorb-seal-v1: the handful of functions everything else in the vault is built from.

    salt_i       = HMAC-SHA256(seed, "qorb-seal-v1/salt/" + path_i)
    commitment_i = HMAC-SHA256(salt_i, content_i)
    leaf_i       = SHA-256(0x00 || path_i || 0x00 || commitment_i)
    node         = SHA-256(0x01 || left || right)

These four lines are the whole contract between the private tree and this repository. They never change
under this name: a different construction would be a different scheme string, and every manifest says
which scheme it was made with.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Union

SCHEME = "qorb-seal-v1"
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
SEED_MIN_BYTES = 32
EMPTY_ROOT = hashlib.sha256(b"").hexdigest()

_HEX64 = re.compile(r"[0-9a-f]{64}")


def salt_for(seed: bytes, path: str) -> bytes:
    """The salt of one file. Derived, not stored: the seed is the only secret there is."""
    return hmac.new(seed, f"{SCHEME}/salt/{path}".encode(), hashlib.sha256).digest()


def commitment(salt: bytes, content: bytes) -> str:
    """Hiding while the salt is private, binding once it is published."""
    return hmac.new(salt, content, hashlib.sha256).hexdigest()


def leaf(path: str, commit_hex: str) -> bytes:
    """A file's place in the tree: its path and its commitment, under the leaf prefix."""
    return hashlib.sha256(LEAF_PREFIX + path.encode() + LEAF_PREFIX + bytes.fromhex(commit_hex)).digest()


def node(left: bytes, right: bytes) -> bytes:
    """An inner node. The prefix differs from a leaf's, so neither can be passed off as the other."""
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def count_lines(content: bytes) -> int:
    """Lines as an editor counts them; 0 for a binary file."""
    if b"\x00" in content[:4096]:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") or not content else 1)


def read_seed(source: Union[str, Path, bytes]) -> bytes:
    """The seed from a file of hex (private side only). Too short a seed is refused, not padded."""
    raw = source if isinstance(source, bytes) else Path(source).read_text(encoding="ascii")
    text = raw.decode("ascii") if isinstance(raw, bytes) else raw
    try:
        seed = bytes.fromhex(text.strip())
    except ValueError as e:
        raise ValueError("the seed file must hold hex") from e
    if len(seed) < SEED_MIN_BYTES:
        raise ValueError(f"the seed must be at least {SEED_MIN_BYTES} bytes ({2 * SEED_MIN_BYTES} hex characters)")
    return seed
