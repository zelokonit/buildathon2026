#!/usr/bin/env python3
# tools/build_map.py
import ast, gzip, json, os, subprocess, sys
from collections import defaultdict
from pathlib import Path
from coverage import CoverageData

SRC = Path(sys.argv[1]).resolve()        # sqlglot checkout
OUT = Path("maps/map.json.gz")
TESTS_JSON = sys.argv[2]                 # sample's tests.json

def log(*a): print(*a, file=sys.stderr)

class Pool:
    def __init__(self): self.ids, self.sets = {}, []
    def add(self, s):
        k = tuple(sorted(s))
        if k not in self.ids:
            self.ids[k] = len(self.sets); self.sets.append(k)
        return self.ids[k]
    def encode(self):                    # delta-encode each sorted list
        return [[v - (k[i-1] if i else 0) for i, v in enumerate(k)] for k in self.sets]

# ---- 0. test index: nodeid -> number (ADAPT to tests.json's real schema)
def load_tests():
    raw = json.load(open(TESTS_JSON))
    return {t["id"]: t["number"] for t in raw["tests"]}
TESTS = load_tests()
BY_FILE = defaultdict(set)
for nid, n in TESTS.items(): BY_FILE[nid.split("::")[0]].add(n)

# ---- 1. per-test coverage (slow: run once, then keep .coverage out of git)
def run_coverage():
    env = dict(os.environ, COVERAGE_FILE=str(SRC / ".coverage"), COVERAGE_CORE="ctrace")
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-n", "4",
                    "-p", "no:cacheprovider", "--cov=sqlglot", "--cov-context=test",
                    "--cov-report="], cwd=SRC, env=env, check=False)

def ctx_to_tests(ctxs, miss):
    out = set()
    for c in ctxs:
        if not c: continue                        # "" = import time / outside any test
        nid = c.rsplit("|", 1)[0]                 # strip |setup / |run / |teardown
        if nid in TESTS: out.add(TESTS[nid])
        else: miss.add(nid)
    return out

# ---- 2. scopes from the AST: module > class > function, decorators included
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
    return sc                                     # preorder: parents precede children

# ---- 3. which sqlglot files each test file imports (fresh interpreter per file)
PROBE = r"""
import sys, json, pytest
pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", sys.argv[1]])
print("@@" + json.dumps(sorted({getattr(m, "__file__", None) or "" for n, m in list(sys.modules.items())
                                if n.startswith("sqlglot")})))
"""
def imports_of(test_file):
    r = subprocess.run([sys.executable, "-c", PROBE, test_file], cwd=SRC,
                       capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith("@@")][-1]
    return [os.path.relpath(p, SRC) for p in json.loads(line[2:]) if p]

# ---- 4. non-.py files tests read (see tools/io_probe.py below)
def data_files():
    p = SRC / "io_log.json"                       # {"path": [nodeid or test-file, ...]}
    return json.load(open(p)) if p.exists() else {}

def main():
    if not (SRC / ".coverage").exists(): run_coverage()
    pool, miss = Pool(), set()
    cov = CoverageData(str(SRC / ".coverage")); cov.read()
    out = {"meta": {"n_tests": len(TESTS), "py": sys.version.split()[0]},
           "src": {}, "data": {}, "tfile": {}}

    imp = defaultdict(set)                         # src file -> tests via importing test files
    for tf in sorted(BY_FILE):
        for f in imports_of(tf):
            if f.startswith("sqlglot/"): imp[f] |= BY_FILE[tf]
        out["tfile"][tf] = pool.add(BY_FILE[tf])

    for f in sorted(Path(SRC / "sqlglot").rglob("*.py")):
        rel = str(f.relative_to(SRC))
        text = f.read_text()
        sc = get_scopes(text)
        owner = [0] * (text.count("\n") + 3)
        for i, s in enumerate(sc):                 # preorder => inner overwrites outer
            for l in range(s["s"], s["e"] + 1): owner[l] = i
        own = [set() for _ in sc]
        lines, prev = [], None
        by_line = cov.contexts_by_lineno(str(f)) if str(f) in cov.measured_files() else {}
        for l in sorted(by_line):
            t = ctx_to_tests(by_line[l], miss)
            if not t: continue                     # import-time only => no direct set
            own[owner[l]] |= t
            sid = pool.add(t)
            if prev and prev[1] == l - 1 and prev[2] == sid: prev[1] = l
            else: prev = [l, l, sid]; lines.append(prev)
        tot = [set(o) for o in own]
        for i in range(len(sc) - 1, 0, -1): tot[sc[i]["parent"]] |= tot[i]
        out["src"][rel] = {
            "lines": lines,
            "scopes": [[s["s"], s["e"], s["parent"], s["kind"], pool.add(tot[i])]
                       for i, s in enumerate(sc)],
            "imp": pool.add(imp.get(rel, set()))}

    for path, who in data_files().items():
        ts = set()
        for w in who: ts |= {TESTS[w]} if w in TESTS else BY_FILE.get(w, set())
        out["data"][path] = pool.add(ts)

    sys.path.insert(0, str(Path(__file__).parent))
    import names_index
    names_index.add_names(out, SRC, pool)
    
    out["sets"] = pool.encode()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt") as fh: json.dump(out, fh, separators=(",", ":"))
    log(f"{len(pool.sets)} distinct sets, {OUT.stat().st_size/1e6:.2f} MB, "
        f"{len(miss)} contexts not in tests.json (check these!)")
    log(sorted(miss)[:20])

if __name__ == "__main__": main()
