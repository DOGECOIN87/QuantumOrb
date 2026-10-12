import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SEED = bytes(range(32))

# a small private tree, as the seal sees one: paths across several components
TREE = {
    "quantumorb/db.py": b"import sqlite3\n\nSCHEMA = 2\n",
    "quantumorb/pipeline/feed.py": b"async def run():\n    ...\n",
    "site/server/index.js": b"module.exports = {};\n",
    "tests/test_db.py": b"def test_it():\n    assert True\n",
    "docs/STATUS.md": b"# status\n",
    "assets/brand/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
}


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture
def tree(tmp_path) -> Path:
    src = tmp_path / "private"
    for rel, body in TREE.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(body)
    return src


@pytest.fixture
def showcase(tmp_path) -> Path:
    """An empty public side, laid out like this repository."""
    base = tmp_path / "showcase"
    (base / "sealed").mkdir(parents=True)
    return base
