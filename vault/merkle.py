"""The Merkle tree over the leaves of a seal, and the inclusion proofs cut from it.

Leaves are kept in path order. Pairs are hashed left to right; an unpaired node at the end of a level is
promoted unchanged. A proof is the list of siblings from a leaf up to the root, each marked with the side
it sits on, which is all a verifier needs: the other files, their commitments and their paths stay out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from .scheme import EMPTY_ROOT, SCHEME, is_hex64, leaf, node

Step = Tuple[str, str]  # ("L" | "R", sibling as hex)


class MerkleTree:
    """Every level is kept, so any number of proofs come from one build."""

    def __init__(self, leaves: Sequence[bytes]):
        self.levels: List[List[bytes]] = [list(leaves)]
        level = self.levels[0]
        while len(level) > 1:
            nxt = [node(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
            if len(level) % 2:
                nxt.append(level[-1])
            self.levels.append(nxt)
            level = nxt

    def __len__(self) -> int:
        return len(self.levels[0])

    @property
    def height(self) -> int:
        return len(self.levels) - 1

    @property
    def root(self) -> str:
        return self.levels[-1][0].hex() if self.levels[0] else EMPTY_ROOT

    def path(self, index: int) -> List[Step]:
        if not 0 <= index < len(self):
            raise IndexError(f"leaf {index} is outside a tree of {len(self)}")
        steps: List[Step] = []
        i = index
        for level in self.levels[:-1]:
            if i % 2 == 0 and i + 1 < len(level):
                steps.append(("R", level[i + 1].hex()))
            elif i % 2 == 1:
                steps.append(("L", level[i - 1].hex()))
            i //= 2
        return steps


def fold(leaf_hex: str, steps: Iterable[Sequence[str]]) -> str:
    """Climb from a leaf to the root it implies."""
    current = bytes.fromhex(leaf_hex)
    for side, sibling_hex in steps:
        sibling = bytes.fromhex(sibling_hex)
        if side == "L":
            current = node(sibling, current)
        elif side == "R":
            current = node(current, sibling)
        else:
            raise ValueError(f"a step's side is L or R, not {side!r}")
    return current.hex()


def merkle_root(leaves: Sequence[bytes]) -> str:
    return MerkleTree(leaves).root


def merkle_proof(leaves: Sequence[bytes], index: int) -> List[Step]:
    return MerkleTree(leaves).path(index)


def check_proof(leaf_hex: str, proof: Iterable[Sequence[str]], root_hex: str) -> bool:
    try:
        return fold(leaf_hex, proof) == root_hex
    except ValueError:
        return False


@dataclass(frozen=True)
class Proof:
    """That one sealed file belongs to one root - and nothing about any other file."""

    path: str
    commitment: str
    root: str
    steps: Tuple[Step, ...] = field(default_factory=tuple)
    scheme: str = SCHEME

    @property
    def leaf(self) -> str:
        return leaf(self.path, self.commitment).hex()

    def verify(self) -> bool:
        if self.scheme != SCHEME or not (is_hex64(self.commitment) and is_hex64(self.root)):
            return False
        return check_proof(self.leaf, self.steps, self.root)

    def to_dict(self) -> dict:
        return {"scheme": self.scheme, "path": self.path, "commitment": self.commitment, "leaf": self.leaf,
                "proof": [list(s) for s in self.steps], "root": self.root}

    @classmethod
    def from_dict(cls, d: dict) -> "Proof":
        steps = tuple((str(s[0]), str(s[1])) for s in d.get("proof", []))
        p = cls(path=d["path"], commitment=d["commitment"], root=d["root"], steps=steps,
                scheme=d.get("scheme", SCHEME))
        if "leaf" in d and d["leaf"] != p.leaf:
            raise ValueError("the proof's leaf does not follow from its path and commitment")
        return p
