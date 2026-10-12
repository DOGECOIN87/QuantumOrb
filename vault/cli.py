"""python -m vault - check the seal, its history and its contracts; cut and check proofs.

    python -m vault status                       one screen: the seal, the ledger, the contracts
    python -m vault verify [--opened DIR]        the manifest adds up and matches sealed/ROOT
    python -m vault ledger [--verify] [--require-history]
    python -m vault proof PATH [--entry N]       the Merkle path of one sealed file
    python -m vault check-proof PROOF.json [--file F --salt HEX]
    python -m vault diff [N M] [--paths]         what moved between two seals (default: the last two)
    python -m vault interfaces [--index]         every contract names a sealed file and its commitment
    python -m vault report [--write | --check]   the README's seal figures

    private side, with the seed:
    python -m vault make --src ../private --seed-file ../private/.seal/seed --note "..."
    python -m vault open --src ../private --seed-file ... --out opened/ PATH...

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .diff import diff, render
from .history import check_history, published_roots, timeline
from .interfaces import audit, index_markdown, scan
from .ledger import CURRENT, LEDGER, ROOT_FILE, Ledger, record, verify_ledger
from .manifest import Manifest, ManifestError, build, verify_manifest
from .merkle import Proof
from .opened import check_opened, open_files
from .report import check, render_blocks, sync
from .scheme import SCHEME, commitment, read_seed

HERE = Path(__file__).resolve().parent.parent
INDEX = Path("interfaces") / "INDEX.md"


def _ok(msg: str) -> int:
    print(f"ok    {msg}")
    return 0


def _fail(problems: List[str]) -> int:
    for p in problems:
        print(f"FAIL  {p}")
    return 1


def _current(repo: Path) -> Manifest:
    return Manifest.load(repo / CURRENT)


def _entry_manifest(repo: Path, ledger: Ledger, n: Optional[int]) -> Manifest:
    if n is None:
        return _current(repo)
    for e in ledger.entries:
        if e.n == n:
            return Manifest.load(repo / e.archive)
    raise SystemExit(f"no ledger entry {n}")


# ------------------------------------------------------------------------- commands

def cmd_verify(args) -> int:
    repo = args.repo
    m = json.loads((repo / CURRENT).read_text(encoding="utf-8"))
    published = (repo / ROOT_FILE).read_text().strip() if (repo / ROOT_FILE).exists() else None
    problems = verify_manifest(m, published)
    opened_note = ""
    if args.opened and not problems:
        results = check_opened(Manifest.from_dict(m), Path(args.opened))
        problems += [r.describe() for r in results if not r.ok]
        opened_note = f" · {sum(r.ok for r in results)} opened file(s) match"
    if problems:
        return _fail(problems)
    s = m["source"]
    return _ok(f"{s['files']} files · {s['lines']:,} lines · root {m['root']}{opened_note}")


def cmd_ledger(args) -> int:
    repo = args.repo
    ledger = Ledger.load(repo / LEDGER)
    published = published_roots(repo)
    if args.verify:
        problems = verify_ledger(ledger, repo)
        if published is None:
            if args.require_history:
                problems.append("no full git history to check the ledger against (fetch-depth: 0)")
        else:
            problems += check_history(ledger, published)
        if problems:
            return _fail(problems)
        hist = "" if published is None else f" · {len(published)} root(s) in git, in order"
        return _ok(f"ledger of {len(ledger.entries)} seal(s) · chain intact · head {ledger.head.root[:16]}…{hist}")
    for row in timeline(ledger, published):
        delta = "" if row["lines_delta"] is None else f" ({row['lines_delta']:+,})"
        print(f"#{row['n']:<3} sealed {row['sealed_at']}  {row['files']} files · {row['lines']:,} lines{delta}")
        print(f"     root {row['root']}")
        if row["published"]:
            print(f"     published {row['published']} in {row['commit']}")
        print(f"     {row['note']}")
    return 0


def cmd_proof(args) -> int:
    ledger = Ledger.load(args.repo / LEDGER)
    m = _entry_manifest(args.repo, ledger, args.entry)
    try:
        p = m.proof(args.path)
    except KeyError as e:
        raise SystemExit(str(e).strip("'\""))
    print(json.dumps(p.to_dict(), indent=1))
    return 0


def cmd_check_proof(args) -> int:
    raw = sys.stdin.read() if args.proof == "-" else Path(args.proof).read_text(encoding="utf-8")
    try:
        p = Proof.from_dict(json.loads(raw))
    except (KeyError, ValueError) as e:
        return _fail([f"not a proof: {e}"])
    problems = [] if p.verify() else [f"{p.path}: the proof does not fold back to root {p.root}"]
    if args.file:
        if not args.salt:
            raise SystemExit("--file needs --salt")
        if commitment(bytes.fromhex(args.salt), Path(args.file).read_bytes()) != p.commitment:
            problems.append(f"{args.file} is not the file this proof commits to")
    known = Ledger.load(args.repo / LEDGER)
    roots = {e.root for e in known.entries}
    if roots and p.root not in roots:
        problems.append(f"root {p.root[:16]}… is not one this repository has published")
    if problems:
        return _fail(problems)
    return _ok(f"{p.path} is in root {p.root[:16]}… ({len(p.steps)} steps)"
               + (" · the file matches" if args.file else ""))


def cmd_diff(args) -> int:
    ledger = Ledger.load(args.repo / LEDGER)
    if args.entries:
        a, b = args.entries
    else:
        if len(ledger.entries) < 2:
            print("one seal so far: nothing to compare")
            return 0
        a, b = ledger.entries[-2].n, ledger.entries[-1].n
    old = Manifest.load(args.repo / next(e.archive for e in ledger.entries if e.n == a))
    new = Manifest.load(args.repo / next(e.archive for e in ledger.entries if e.n == b))
    print(f"seal #{a} -> seal #{b}")
    print(render(diff(old, new), paths=args.paths))
    return 0


def cmd_interfaces(args) -> int:
    root = args.repo / "interfaces"
    m = _current(args.repo)
    contracts = scan(root)
    problems = audit(contracts, m, root)
    if args.index:
        (args.repo / INDEX).write_text("# Interface index\n\n" + index_markdown(contracts, m), encoding="utf-8")
    elif args.check_index:
        want = "# Interface index\n\n" + index_markdown(contracts, m)
        if not (args.repo / INDEX).exists() or (args.repo / INDEX).read_text(encoding="utf-8") != want:
            problems.append("interfaces/INDEX.md is out of date (python -m vault interfaces --index)")
    if problems:
        return _fail(problems)
    names = sum(len(c.names) for c in contracts)
    return _ok(f"{len(contracts)} contracts · {names} public names · every one cut from the sealed version")


def cmd_report(args) -> int:
    readme = args.repo / "README.md"
    blocks = render_blocks(_current(args.repo), Ledger.load(args.repo / LEDGER))
    text = readme.read_text(encoding="utf-8")
    if args.write:
        readme.write_text(sync(text, blocks), encoding="utf-8")
        return _ok("README.md: seal figures rewritten")
    if args.check:
        problems = check(text, blocks)
        return _fail(problems) if problems else _ok("README.md says what the seal says")
    for name, body in blocks.items():
        print(f"<!-- vault:{name} -->\n{body}\n<!-- /vault:{name} -->\n")
    return 0


def cmd_status(args) -> int:
    repo = args.repo
    m = _current(repo)
    ledger = Ledger.load(repo / LEDGER)
    contracts = scan(repo / "interfaces")
    s = m.source
    print(f"scheme      {SCHEME}  (vault {__version__})")
    print(f"seal        #{ledger.head.n if ledger.head else '-'} · {m.sealed.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"root        {m.root}")
    print(f"source      {s.files} files · {s.lines:,} lines · {s.bytes:,} bytes · tree height {m.tree().height}")
    for label, t in m.groups():
        print(f"  {label:<44} {t.files:>4} files {t.lines:>7,} lines")
    print(f"ledger      {len(ledger.entries)} seal(s), oldest {ledger.entries[0].sealed_at if ledger.entries else '-'}")
    print(f"contracts   {len(contracts)} · {sum(len(c.names) for c in contracts)} public names")
    salts = repo / "opened" / "salts.json"
    print(f"opened      {len(json.loads(salts.read_text())) if salts.exists() else 0} file(s)")
    return 0


def cmd_make(args) -> int:
    seed = read_seed(args.seed_file)
    m = build(Path(args.src), seed)
    if (args.repo / CURRENT).exists() and _current(args.repo).root == m.root:
        print(f"nothing to seal: the tree still matches root {m.root}")
        return 0
    entry = record(args.repo, m, args.note)
    print(f"sealed #{entry.n}: {m.source.files} files, {m.source.lines:,} lines -> root {m.root}")
    return 0


def cmd_open(args) -> int:
    seed = read_seed(args.seed_file)
    done = open_files(_current(args.repo), Path(args.src), seed, Path(args.out), args.paths)
    print(f"opened {len(done)} file(s) into {args.out}")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vault", description="QuantumOrb · the seal over the closed source")
    ap.add_argument("--repo", type=Path, default=HERE, help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"vault {__version__} · {SCHEME}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="one screen: the seal, the ledger, the contracts")
    v = sub.add_parser("verify", help="the manifest adds up and matches sealed/ROOT")
    v.add_argument("--opened", help="a directory of opened files with their salts.json")
    lg = sub.add_parser("ledger", help="every seal so far")
    lg.add_argument("--verify", action="store_true")
    lg.add_argument("--require-history", action="store_true")
    pr = sub.add_parser("proof", help="the Merkle path of one sealed file")
    pr.add_argument("path")
    pr.add_argument("--entry", type=int, help="against an earlier seal in the ledger")
    cp = sub.add_parser("check-proof", help="check a proof, optionally with the opened file and its salt")
    cp.add_argument("proof", help="a proof JSON file, or - for stdin")
    cp.add_argument("--file")
    cp.add_argument("--salt")
    df = sub.add_parser("diff", help="what moved between two seals")
    df.add_argument("entries", nargs="*", type=int)
    df.add_argument("--paths", action="store_true")
    it = sub.add_parser("interfaces", help="the contracts against the seal")
    it.add_argument("--index", action="store_true", help="rewrite interfaces/INDEX.md")
    it.add_argument("--check-index", action="store_true")
    rp = sub.add_parser("report", help="the README's seal figures")
    rp.add_argument("--write", action="store_true")
    rp.add_argument("--check", action="store_true")
    mk = sub.add_parser("make", help="private side: seal a tree")
    mk.add_argument("--src", required=True)
    mk.add_argument("--seed-file", required=True)
    mk.add_argument("--note", default="")
    op = sub.add_parser("open", help="private side: open sealed files with their salts")
    op.add_argument("--src", required=True)
    op.add_argument("--seed-file", required=True)
    op.add_argument("--out", required=True)
    op.add_argument("paths", nargs="+")
    return ap


COMMANDS = {"status": cmd_status, "verify": cmd_verify, "ledger": cmd_ledger, "proof": cmd_proof,
            "check-proof": cmd_check_proof, "diff": cmd_diff, "interfaces": cmd_interfaces, "report": cmd_report,
            "make": cmd_make, "open": cmd_open}


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "diff" and args.entries and len(args.entries) != 2:
        raise SystemExit("diff takes two ledger entries, or none")
    try:
        return COMMANDS[args.cmd](args)
    except ManifestError as e:
        return _fail([str(e)])
