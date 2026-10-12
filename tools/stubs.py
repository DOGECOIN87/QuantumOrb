#!/usr/bin/env python3
"""Interface contracts from the sealed source: every public signature, none of the bodies.

    python tools/stubs.py --src ../private        (private side; writes interfaces/)

For each Python module of the bot it writes a `.pyi` with the public classes, methods and functions,
their annotations, and the first line of their docstring. For each server module of the observatory it
writes a `.d.ts` with the exported names and their parameters. Every stub names the commitment of the
file it was cut from, so the contract and the sealed implementation can be matched the day a file is
opened. Constants, private helpers, thresholds and anything that names an endpoint stay out.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "interfaces"
REVEALING = re.compile(r"https?://|\b(GET|POST)\b|/[a-z0-9_-]+/|\d{2,}|\.env|[A-Z_]{4,}=|created-coins|profile-api|swap-api|frontend-api|getAccountInfo|getTokenLargest")


def _seal() -> dict[str, str]:
    m = json.loads((HERE / "sealed" / "manifest.json").read_text(encoding="utf-8"))
    return {f["path"]: f["commitment"] for f in m["files"]}


def _doc(node) -> str | None:
    d = ast.get_docstring(node)
    if not d:
        return None
    first = d.strip().split("\n\n")[0].replace("\n", " ").strip()
    first = re.split(r"(?<=[.!?])\s", first)[0]
    if REVEALING.search(first) or len(first) > 110:
        return None
    return first


def _args(a: ast.arguments) -> str:
    def one(arg: ast.arg, default) -> str:
        s = arg.arg + (f": {ast.unparse(arg.annotation)}" if arg.annotation else "")
        return s + (" = ..." if default is not None else "")

    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    parts = [one(x, d) for x, d in zip(pos, defaults)]
    if a.posonlyargs:
        parts.insert(len(a.posonlyargs), "/")
    if a.vararg:
        parts.append("*" + one(a.vararg, None))
    elif a.kwonlyargs:
        parts.append("*")
    parts += [one(x, d) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        parts.append("**" + one(a.kwarg, None))
    return ", ".join(parts)


def _func(f, indent: str) -> list[str]:
    out = []
    for d in f.decorator_list:
        name = ast.unparse(d)
        if name in ("staticmethod", "classmethod", "property"):
            out.append(f"{indent}@{name}")
    prefix = "async def" if isinstance(f, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(f.returns)}" if f.returns else ""
    doc = _doc(f)
    body = f'\n{indent}    """{doc}"""\n{indent}    ...' if doc else " ..."
    out.append(f"{indent}{prefix} {f.name}({_args(f.args)}){ret}:{body}")
    return out


def python_stub(path: Path, rel: str, commit: str) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = [f"# QuantumOrb · interface contract · {rel}",
             f"# implementation: sealed · commitment {commit[:16]}…{commit[-8:]}",
             "# signatures only - the bodies are in the sealed source (see SEAL.md)",
             "from __future__ import annotations", "", "from typing import Any", ""]
    doc = _doc(tree)
    if doc:
        lines.insert(0, f'"""{doc}"""')
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            lines += _func(node, "") + [""]
            count += 1
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            lines.append(f"class {node.name}{f'({bases})' if bases else ''}:")
            cdoc = _doc(node)
            if cdoc:
                lines.append(f'    """{cdoc}"""')
            members = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not m.name.startswith("_") or m.name == "__init__")]
            for m in members:
                lines += _func(m, "    ")
            if not members and not cdoc:
                lines.append("    ...")
            lines.append("")
            count += 1
    return "\n".join(lines).rstrip() + "\n" if count else None


JS_EXPORTS = re.compile(r"module\.exports\s*=\s*\{")
JS_GETTER = re.compile(r"^get\s+([A-Za-z_$][\w$]*)\s*\(")
JS_METHOD = re.compile(r"^(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")


def _object_body(src: str, start: int) -> str:
    """The text between the brace at `start` and its partner, skipping strings and nested braces."""
    depth, i, quote = 0, start, None
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
        i += 1
    raise ValueError("module.exports never closes")


def _top_level_parts(body: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], None
    for ch in body:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p and not p.startswith("...")]


def _params(raw: str) -> str:
    return ", ".join(f"{p.split('=')[0].strip()}?: unknown" for p in raw.split(",") if p.strip())


def js_stub(path: Path, rel: str, commit: str) -> str | None:
    src = path.read_text(encoding="utf-8")
    m = JS_EXPORTS.search(src)
    if not m:
        return None
    lines = [f"// QuantumOrb · interface contract · {rel}",
             f"// implementation: sealed · commitment {commit[:16]}…{commit[-8:]}",
             "// exported names only - the bodies are in the sealed source (see SEAL.md)", ""]
    for part in _top_level_parts(_object_body(src, m.end() - 1)):
        g, meth = JS_GETTER.match(part), JS_METHOD.match(part)
        if g:  # a live binding: read-only from outside
            lines.append(f"export declare const {g.group(1)}: unknown; // read-only, always current")
            continue
        if meth:
            lines.append(f"export declare function {meth.group(1)}({_params(meth.group(2))}): unknown;")
            continue
        key, _, val = part.partition(":")
        key, val = key.strip(), (val or key).strip()
        fm = re.search(r"function\s+" + re.escape(val) + r"\s*\(([^)]*)\)", src)
        if fm:
            lines.append(f"export declare function {key}({_params(fm.group(1))}): unknown;")
        else:
            lines.append(f"export declare const {key}: unknown;")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    args = ap.parse_args()
    src = Path(args.src).resolve()
    seal = _seal()
    written = 0
    for rel, commit in sorted(seal.items()):
        p = src / rel
        if rel.startswith("quantumorb/") and rel.endswith(".py"):
            out, text = OUT / "bot" / (rel[len("quantumorb/"):-3] + ".pyi"), python_stub(p, rel, commit)
        elif rel.startswith("site/server/") and rel.endswith(".js"):
            out, text = OUT / "observatory" / (rel[len("site/server/"):-3] + ".d.ts"), js_stub(p, rel, commit)
        else:
            continue
        if text:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            written += 1
    print(f"{written} interface files -> {OUT.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
