#!/usr/bin/env python3
"""Build maps/map.json.gz from a sqlglot checkout at eb11aec.

Prerequisites, all run from the sqlglot checkout (see each tool's docstring):
  1. per-test coverage:   COV_DATA=.coverage.ctx PYTHONPATH=<repo>/tools pytest -p cov_plugin -p no:xdist -p no:cacheprovider -q tests
  2. fixture links:       python <repo>/tools/fixture_scan.py . > io_log_static.json
  3. file-open links:     PYTHONPATH=<repo>/tools pytest -p io_probe -p no:xdist -p no:cacheprovider -q tests   (writes io_log.json)
usage: python tools/build_map.py <sqlglot checkout> <out map.json.gz> <tests.json>
"""
import ast, gzip, json, os, subprocess, sys
from collections import defaultdict
from pathlib import Path

from coverage import CoverageData

sys.path.insert(0, str(Path(__file__).resolve().parent))
import names_index  # noqa: E402

SRC = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2])
TESTS_JSON = sys.argv[3]


def log(*a):
    print(*a, file=sys.stderr)


class Pool:
    def __init__(self):
        self.ids, self.sets = {}, []

    def add(self, s):
        k = tuple(sorted(s))
        if k not in self.ids:
            self.ids[k] = len(self.sets)
            self.sets.append(k)
        return self.ids[k]

    def encode(self):  # delta-encode each sorted list
        return [[v - (k[i - 1] if i else 0) for i, v in enumerate(k)] for k in self.sets]


# ---- 0. test index: nodeid -> number
RAW = json.load(open(TESTS_JSON))["tests"]
TESTS = {t["id"]: t["number"] for t in RAW}
BY_FILE = defaultdict(set)
for t in RAW:
    BY_FILE[t["file"]].add(t["number"])


def resolve_ctx(c):
    """Context -> test number, or None.

    "w:<spawner>|<test>" (cov_plugin): the part after the first "|" is the test; empty = idle.
    A bare "w:<spawner>" is a worker's import/idle time and belongs to no test.
    Older formats ("<nodeid>", "<a>|<b>", "<nodeid>|run") are still understood."""
    if c.startswith("w:"):
        if "|" not in c:
            return None
        c = c.split("|", 1)[1]
        if not c:
            return None
    if c in TESTS:
        return TESTS[c]
    i = c.rfind("|")
    while i != -1:
        tail = c[i + 1:]
        if tail in TESTS:
            return TESTS[tail]
        i = c.rfind("|", 0, i)
    return TESTS.get(c.split("|", 1)[0])


def ctx_to_tests(ctxs, miss):
    out = set()
    for c in ctxs:
        if not c or c.endswith("|"):
            continue  # "" or a worker's idle/import time: no test is responsible
        n = resolve_ctx(c)
        if n is None:
            if not c.startswith("w:"):  # "w:..." with no test = worker idle/import time, expected
                miss.add(c)
        else:
            out.add(n)
    return out


# ---- 1. scopes from the AST: module > class > function, decorators included
def get_scopes(text):
    n = text.count("\n") + 2
    sc = [dict(s=1, e=n, kind="module", parent=-1)]

    def walk(node, parent):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                s = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                sc.append(dict(s=s, e=ch.end_lineno, parent=parent,
                               kind="class" if isinstance(ch, ast.ClassDef) else "func"))
                walk(ch, len(sc) - 1)
            else:
                walk(ch, parent)

    walk(ast.parse(text), 0)
    return sc  # preorder: parents precede children


# ---- 2. which sqlglot files each test file imports (fresh interpreter per file)
PROBE = r"""
import sys, json, pytest
pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", sys.argv[1]])
print("@@" + json.dumps(sorted({getattr(m, "__file__", None) or "" for n, m in list(sys.modules.items())
                                if n.startswith("sqlglot")})))
"""


def imports_of(test_file):
    r = subprocess.run([sys.executable, "-c", PROBE, test_file], cwd=SRC, capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.startswith("@@")]
    if not lines:
        log("import probe failed for", test_file, r.stderr[-300:])
        return []
    return [os.path.relpath(p, SRC) for p in json.loads(lines[-1][2:]) if p]


# ---- 3. non-.py files tests read: union of the runtime probe and the static scan
def data_files():
    merged = defaultdict(set)
    for p in sorted(SRC.glob("io_log*.json")):
        for k, v in json.load(open(p)).items():
            merged[k] |= set(v)
        log("read", p.name)
    if not merged:
        log("WARNING: no io_log*.json found; fixture edits will run every test")
    return merged


def main():
    parts = sorted(SRC.glob(".coverage.ctx*"))
    if not parts:
        sys.exit("no .coverage.ctx* files: run tools/cov_plugin.py first (see the docstring)")
    merged = SRC / ".coverage.merged"
    cov = CoverageData(str(merged))
    cov.erase()
    for f in parts:
        o = CoverageData(str(f))
        o.read()
        cov.update(o)
    cov.read()

    pool, miss = Pool(), set()
    out = {"meta": {"n_tests": len(TESTS), "py": sys.version.split()[0]}, "src": {}, "data": {}, "tfile": {}}

    imp = defaultdict(set)  # src file -> tests via importing test files
    for tf in sorted(BY_FILE):
        for f in imports_of(tf):
            if f.startswith("sqlglot/"):
                imp[f] |= BY_FILE[tf]
        out["tfile"][tf] = pool.add(BY_FILE[tf])

    measured = set(cov.measured_files())
    for f in sorted((SRC / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(SRC))
        text = f.read_text()
        sc = get_scopes(text)
        owner = [0] * (text.count("\n") + 3)
        for i, s in enumerate(sc):  # preorder => inner overwrites outer
            for l in range(s["s"], s["e"] + 1):
                owner[l] = i
        own = [set() for _ in sc]
        lines, prev = [], None
        by_line = cov.contexts_by_lineno(str(f)) if str(f) in measured else {}
        for l in sorted(by_line):
            t = ctx_to_tests(by_line[l], miss)
            if not t:
                continue  # import-time only => no direct set
            own[owner[l]] |= t
            sid = pool.add(t)
            if prev and prev[1] == l - 1 and prev[2] == sid:
                prev[1] = l
            else:
                prev = [l, l, sid]
                lines.append(prev)
        tot = [set(o) for o in own]
        for i in range(len(sc) - 1, 0, -1):
            tot[sc[i]["parent"]] |= tot[i]
        out["src"][rel] = {
            "lines": lines,
            "scopes": [[s["s"], s["e"], s["parent"], s["kind"], pool.add(tot[i])] for i, s in enumerate(sc)],
            "imp": pool.add(imp.get(rel, set())),
        }

    for path, who in data_files().items():
        ts = set()
        for w in who:
            ts |= {TESTS[w]} if w in TESTS else BY_FILE.get(w, set())
        out["data"][path] = pool.add(ts)

    users = {}
    du = SRC / "dialect_use.json"
    if du.exists():
        for fam, nodeids in json.load(open(du)).items():
            users[fam] = {TESTS[n] for n in nodeids if n in TESTS}
        log(f"dialect_use.json: {len(users)} dialects, e.g. tsql -> {len(users.get('tsql', ()))} tests")
    else:
        log("WARNING: no dialect_use.json; dialect-constant edits will not be narrowed")
    names_index.add_names(out, SRC, pool, users)

    out["sets"] = pool.encode()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt") as fh:
        json.dump(out, fh, separators=(",", ":"))
    log(f"{len(pool.sets)} distinct sets, {OUT.stat().st_size / 1e6:.2f} MB, {len(out['data'])} data files, "
        f"{len(out['names'])} names")
    log(f"{len(miss)} contexts not in tests.json:", sorted(miss)[:20])
    seen = set()
    for e in out["src"].values():
        for _, _, sid in e["lines"]:
            seen |= set(pool.sets[sid])
    log(f"{len(seen)} of {len(TESTS)} tests have at least one covered source line")


if __name__ == "__main__":
    main()
