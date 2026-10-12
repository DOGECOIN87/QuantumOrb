# Examples

Ways to check the seal that do not ask you to trust anything in this repository's tooling.

| | |
|---|---|
| [`verify_offline.py`](verify_offline.py) | 60 lines, standard library, the scheme written out again from [`SEAL.md`](../SEAL.md): checks one inclusion proof, and an opened file against it |
| [`js/`](js/) | the scheme again in JavaScript (WebCrypto): one module for Node 20+ and the browser, a command line, and tests that hold it to the Python side |
| [`browser/`](browser/) | a page that checks the ledger, a proof and an opened file in your browser - nothing leaves it |
| [`proofs/`](proofs/) | inclusion proofs for a few sealed files, cut with `python -m vault proof PATH` |

```bash
python examples/verify_offline.py examples/proofs/quantumorb-db.json
#  ok    quantumorb/db.py is a leaf of root …
```

```bash
node examples/js/verify.mjs examples/proofs/*.json --ledger sealed/ledger.json
python -m http.server          # from the repository root, then open /examples/browser/
```

A proof shows that a path and its commitment are one leaf of a published root - and says nothing about
any other file. Once a file is opened, the same proof with `--file` and `--salt` shows that the opened
bytes are the ones that were sealed.

## By hand

```python
import hashlib, json
p = json.load(open("examples/proofs/quantumorb-db.json"))
h = hashlib.sha256(b"\x00" + p["path"].encode() + b"\x00" + bytes.fromhex(p["commitment"])).digest()
for side, sib in p["proof"]:
    s = bytes.fromhex(sib)
    h = hashlib.sha256(b"\x01" + (s + h if side == "L" else h + s)).digest()
assert h.hex() == p["root"]
```
