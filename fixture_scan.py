#!/usr/bin/env python3
"""Static fixture -> test map from the pinned checkout (no test run needed).

usage: python tools/fixture_scan.py /path/to/sqlglot  > /path/to/sqlglot/io_log.json
Same format io_probe writes, so build_map.py's data_files() picks it up.
A fixture is linked to every test function whose body contains a string literal equal to the
fixture's relative path, file name or stem; literals outside any test function link the whole file.
"""
import ast, json, sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1]).resolve()
fix = {}
for p in (root / "tests" / "fixtures").rglob("*"):
    if p.is_file():
        rel = p.relative_to(root / "tests" / "fixtures").as_posix()
        fix[str(p.relative_to(root))] = {rel, p.name, p.stem, rel.rsplit(".", 1)[0]}

log = defaultdict(set)

def hit(keys, lit_set):
    """exact match, or a literal that contains a key (f-strings, 'dir/name' forms), or a path fragment."""
    for l in lit_set:
        for k in keys:
            if l == k or (len(k) >= 4 and k in l) or (len(l) >= 6 and k.endswith(l.strip("/"))):
                return True
    return False

def lits(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

for tf in (root / "tests").rglob("*.py"):
    trel = str(tf.relative_to(root))
    tree = ast.parse(tf.read_text())
    funcs, rest = [], []
    def visit(node, cls=None):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                visit(ch, ch.name)
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.append((cls, ch))
            else:
                rest.append(ch)
    visit(tree)
    file_lits = set().union(*[lits(n) for n in rest]) if rest else set()
    for path, keys in fix.items():
        if hit(keys, file_lits):
            log[path].add(trel)                       # module/class level use -> whole file
    for cls, fn in funcs:
        if not fn.name.startswith("test"):
            continue
        l = lits(fn)
        nid = f"{trel}::{cls}::{fn.name}" if cls else f"{trel}::{fn.name}"
        for path, keys in fix.items():
            if hit(keys, l):
                log[path].add(nid)
# Leftovers (e.g. tpc-h/*.csv.gz built from a directory + table name): link by directory name,
# matched against literals, identifiers and test names, normalized ("tpc-ds" -> "tpcds").
def norm(x): return "".join(c for c in x.lower() if c.isalnum())
def names(node):
    out = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return out | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
left = [p for p in fix if p not in log]
dirkeys = {p: norm(Path(p).parent.name) for p in left}
for tf in (root / "tests").rglob("*.py"):
    trel = str(tf.relative_to(root))
    cls_of = {}
    tree = ast.parse(tf.read_text())
    for c in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for f in c.body:
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)): cls_of[id(f)] = c.name
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if not fn.name.startswith("test"): continue
        blob = norm(fn.name) + " " + " ".join(norm(x) for x in lits(fn) | names(fn))
        nid = f"{trel}::{cls_of[id(fn)]}::{fn.name}" if id(fn) in cls_of else f"{trel}::{fn.name}"
        for p, k in dirkeys.items():
            if k and k in blob: log[p].add(nid)
json.dump({k: sorted(v) for k, v in log.items()}, sys.stdout, indent=0)
print(f"{len(log)} of {len(fix)} fixtures linked", file=sys.stderr)
for path in sorted(set(fix) - set(log)):
    print("UNLINKED", path, file=sys.stderr)
