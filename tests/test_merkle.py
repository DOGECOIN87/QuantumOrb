"""The tree and its proofs, against an independent fold written out here."""
import hashlib

import pytest

from vault.merkle import MerkleTree, Proof, check_proof, fold, merkle_proof, merkle_root
from vault.scheme import EMPTY_ROOT, commitment, leaf, salt_for

SEED = bytes(range(32))


def naive_root(leaves):
    level = list(leaves)
    while len(level) > 1:
        nxt = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def leaves(n):
    return [hashlib.sha256(str(i).encode()).digest() for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 31, 32, 33, 155, 160])
def test_root_and_every_proof(n):
    ls = leaves(n)
    tree = MerkleTree(ls)
    assert tree.root == naive_root(ls) == merkle_root(ls)
    assert tree.height == (n - 1).bit_length()
    for i in range(n):
        path = tree.path(i)
        assert path == merkle_proof(ls, i)
        assert fold(ls[i].hex(), path) == tree.root
        if n > 1:  # a proof is bound to its leaf
            assert not check_proof(ls[(i + 1) % n].hex(), path, tree.root)


def test_empty_tree():
    assert MerkleTree([]).root == EMPTY_ROOT
    with pytest.raises(IndexError):
        MerkleTree([]).path(0)


def test_a_promoted_node_adds_no_step():
    ls = leaves(3)
    # leaf 2 is unpaired on the first level: one step only, to the node over 0 and 1
    assert [s for s, _ in MerkleTree(ls).path(2)] == ["L"]


def test_bad_steps_are_refused():
    ls = leaves(4)
    path = merkle_proof(ls, 1)
    assert not check_proof(ls[1].hex(), [("X", path[0][1])] + path[1:], merkle_root(ls))
    assert not check_proof(ls[1].hex(), [(path[0][0], "zz")], merkle_root(ls))
    assert not check_proof(ls[1].hex(), list(reversed(path)), merkle_root(ls))


def _proof_for(paths, target):
    entries = sorted((p, commitment(salt_for(SEED, p), p.encode())) for p in paths)
    tree = MerkleTree([leaf(p, c) for p, c in entries])
    i = [p for p, _ in entries].index(target)
    return Proof(path=target, commitment=entries[i][1], root=tree.root, steps=tuple(tree.path(i)))


def test_proof_round_trip_and_tamper():
    p = _proof_for(["a.py", "b.py", "c/d.py", "e.js", "f.md"], "c/d.py")
    assert p.verify()
    again = Proof.from_dict(p.to_dict())
    assert again == p and again.verify()

    d = p.to_dict()
    d["path"] = "c/e.py"  # the same commitment under another path is another leaf
    with pytest.raises(ValueError, match="leaf"):
        Proof.from_dict(d)
    d = p.to_dict()
    d.pop("leaf")
    d["path"] = "c/e.py"
    assert not Proof.from_dict(d).verify()
    assert not Proof(path=p.path, commitment=p.commitment, root="0" * 64, steps=p.steps).verify()
    assert not Proof(path=p.path, commitment=p.commitment, root=p.root, steps=p.steps, scheme="v0").verify()
