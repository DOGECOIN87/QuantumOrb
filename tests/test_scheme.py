"""qorb-seal-v1 is frozen: these vectors were computed once and must never change."""
import hashlib

import pytest

from vault import scheme

SEED = bytes(range(32))


def test_known_answers():
    salt = scheme.salt_for(SEED, "quantumorb/db.py")
    assert salt.hex() == "571bfbe8b13a5b6a7abc01ee6b6f444146f4acf2fa5ddf4e514acc0530f80d4e"
    c = scheme.commitment(salt, b"import sqlite3\n")
    assert c == "bd252adc407e51464b446898b71f4e8d9071cc30c5926146d50b896e02f87d43"
    assert scheme.leaf("quantumorb/db.py", c).hex() == (
        "7f9709b24c92014d67710183c8be5499f6ced95202e8227a345746178114c376")


def test_leaf_and_node_are_domain_separated():
    a, b = hashlib.sha256(b"a").digest(), hashlib.sha256(b"b").digest()
    n = scheme.node(a, b)
    assert n == hashlib.sha256(b"\x01" + a + b).digest()
    # a leaf over the same 64 bytes cannot equal the node: different prefixes
    assert scheme.leaf("", (a + b)[:32].hex()) != n


def test_the_salt_depends_on_the_path_only():
    assert scheme.salt_for(SEED, "a.py") == scheme.salt_for(SEED, "a.py")
    assert scheme.salt_for(SEED, "a.py") != scheme.salt_for(SEED, "b.py")
    assert scheme.salt_for(SEED, "a.py") != scheme.salt_for(bytes(32), "a.py")


@pytest.mark.parametrize("body,lines", [(b"", 0), (b"x", 1), (b"x\n", 1), (b"x\ny", 2), (b"x\ny\n", 2),
                                        (b"\x89PNG\x00\x00\n\n", 0)])
def test_count_lines(body, lines):
    assert scheme.count_lines(body) == lines


def test_seed_rules(tmp_path):
    good = tmp_path / "seed"
    good.write_text(SEED.hex() + "\n")
    assert scheme.read_seed(good) == SEED
    with pytest.raises(ValueError, match="at least"):
        scheme.read_seed(b"00" * 16)
    with pytest.raises(ValueError, match="hex"):
        scheme.read_seed(b"not a seed at all")


def test_is_hex64():
    assert scheme.is_hex64("0" * 64)
    assert not scheme.is_hex64("0" * 63)
    assert not scheme.is_hex64("G" * 64)
    assert not scheme.is_hex64("A" * 64)  # lower case only: one spelling per value
    assert not scheme.is_hex64(None)
