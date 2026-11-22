"""Blast-radius selector: map lookup + diff parsing + worker packing.

Reads /data/commits/*.patch and /data/tests.json, loads a prebuilt map
(maps/map.json.gz, produced by tools/build_map.py from sqlglot @ eb11aec),
and prints one JSON line per commit. No sqlglot import, no pytest at run time.
"""

from __future__ import annotations

import ast
import bisect
import gzip
import heapq
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
MAP_PATH = Path(os.environ.get("MAP_PATH", HERE / "maps" / "map.json.gz"))
SRC = Path(os.environ.get("SQLGLOT_SRC", "/opt/sqlglot"))  # clone from the Dockerfile

MAX_WORKERS = 8
WORKER_COST = 2.0

# Knobs to tune against `make sample` (never against stored sample labels).
# Probe switch: "scope" = changed code uses its enclosing function/class set (default);
# "module" = also add the whole file's union (wider, safer, slower).
SCOPE_WIDEN = "scope"
# How a changed (removed/replaced) line is looked up. Override with env vars while tuning:
#   SEL_NARROW  = stmt (default) | line | scope
#     line : tests that executed that exact line (narrowest, can miss changed conditions)
#     stmt : tests that executed any line of the enclosing statement (a changed `if`/`and`/`or`
#            can only affect tests that reached the statement)
#     scope: the whole enclosing function/class/module (widest)
#   SEL_SMALL_MODULE = a file whose whole-module test set is at most this big runs that whole set
#   SEL_LEAKY = 1 re-enables the whole-file expansion for test_optimizer / test_executor
NARROW = os.environ.get("SEL_NARROW", "stmt")
SMALL_MODULE = int(os.environ.get("SEL_SMALL_MODULE", "60"))
COMMENTS_HARMLESS = os.environ.get("SEL_COMMENTS", "1") == "1"  # comment/blank-only edits select nothing
IMPORT_HEDGE = False  # also add the "importers of this module" set for module-level code edits
LEAKY_FILES = {
    "tests/test_optimizer.py": ("sqlglot/optimizer/",),
    "tests/test_executor.py": ("sqlglot/executor/",),
} if os.environ.get("SEL_LEAKY") == "1" else {}
# MEASUREMENT ONLY - never ship with these set. Drops tests from every selection so a board
# submission shows (via misses and seconds) how many commits need them and what they cost.
DROP_TESTS = {int(x) for x in os.environ.get("SEL_DROP_TESTS", "").split(",") if x.strip()}
DROP_SECONDS = float(os.environ.get("SEL_DROP_SECONDS") or 0)
EXPLAIN = bool(os.environ.get("EXPLAIN"))  # log what each rule contributed
UNKNOWN_FILE_RUNS_ALL = True  # unrecognized file type -> run everything

# Files that cannot affect a test run.
HARMLESS = re.compile(
    r"(^docs/|^posts/|^\.github/|^\.git|\.(md|rst|png|jpg|svg|gif)$|^LICENSE|^CHANGELOG|^README|^CONTRIBUTING)"
)
# Files under tests/ that are shared infrastructure.
TEST_FILE = re.compile(r"(^|/)test_[^/]*\.py$")


def log(*a) -> None:
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- patch parsing


class FileChange:
    def __init__(self) -> None:
        self.old: str | None = None
        self.new: str | None = None
        self.status = "modified"  # modified | new | deleted | renamed
        self.binary = False
        self.removed: list[int] = []  # old-side line numbers that were removed/replaced
        self.inserted: list[tuple[int, int]] = []  # pure insertions: (old line before, old line after)
        self.hunks: list[list] = []  # [old_start, old_len, new_body_lines]
        self.code_change = False  # some added/removed line is neither blank nor a '#' comment
        self.added_new: list[int] = []  # new-side numbers of added lines
        self.added_text: list[str] = []

    @property
    def path(self) -> str:
        return (self.old if self.status == "deleted" else self.new) or self.old or self.new or ""


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT = re.compile(r"^diff --git a/(.*) b/(.*)$")


def parse_patch(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur: FileChange | None = None
    old_n = old_left = new_left = new_n = 0
    blk_removed: list[int] = []
    blk_added = 0
    blk_prev = 0
    body: list[str] | None = None

    def flush() -> None:
        nonlocal blk_removed, blk_added
        if cur is not None:
            if blk_removed:
                cur.removed.extend(blk_removed)
            elif blk_added:
                cur.inserted.append((blk_prev, old_n))
        blk_removed, blk_added = [], 0

    for line in text.split("\n"):
        if old_left > 0 or new_left > 0:
            c = line[:1]
            if c == "-":
                if line[1:].strip() and not line[1:].lstrip().startswith("#"):
                    cur.code_change = True
                if not blk_removed and not blk_added:
                    blk_prev = old_n - 1
                blk_removed.append(old_n)
                old_n += 1
                old_left -= 1
            elif c == "+":
                if line[1:].strip() and not line[1:].lstrip().startswith("#"):
                    cur.code_change = True
                cur.added_new.append(new_n)
                cur.added_text.append(line[1:])
                new_n += 1
                if not blk_removed and not blk_added:
                    blk_prev = old_n - 1
                blk_added += 1
                new_left -= 1
                body.append(line[1:])
            elif c == "\\":
                pass
            else:  # context (a blank context line may arrive as "")
                flush()
                old_n += 1
                new_n += 1
                old_left -= 1
                new_left -= 1
                body.append(line[1:])
            if old_left <= 0 and new_left <= 0:
                flush()
            continue

        if line.startswith("diff --git "):
            cur = FileChange()
            files.append(cur)
            m = GIT.match(line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "new"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status, cur.old = "renamed", line[12:]
        elif line.startswith("rename to "):
            cur.new = line[10:]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        else:
            m = HUNK.match(line)
            if m:
                o_start = int(m.group(1))
                o_len = int(m.group(2)) if m.group(2) is not None else 1
                n_len = int(m.group(4)) if m.group(4) is not None else 1
                old_n, old_left, new_left = o_start, o_len, n_len
                new_n = int(m.group(3))
                body = []
                cur.hunks.append([o_start, o_len, body])
                blk_removed, blk_added = [], 0
    return files


def apply_hunks(old_lines: list[str], hunks: list[list]) -> list[str]:
    out: list[str] = []
    pos = 0
    for start, olen, body in hunks:
        s = start - 1 if olen > 0 else start
        out.extend(old_lines[pos:s])
        out.extend(body)
        pos = s + olen
    out.extend(old_lines[pos:])
    return out


# --------------------------------------------------------------------------- comments


def _string_spans(text: str) -> list[tuple[int, int]] | None:
    """Row spans (first, last) of every multi-line string token."""
    spans = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.start[0] != tok.end[0] and tok.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)):
                spans.append((tok.start[0], tok.end[0]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return spans


def comment_only(old_text: str, new_text: str, fc: "FileChange") -> bool:
    """True if every changed line is a blank/'#' line that is not inside a string literal."""
    if fc.code_change:
        return False
    new_spans = _string_spans(new_text)
    old_spans = _string_spans(old_text)
    if new_spans is None or old_spans is None:
        return False
    for n in fc.added_new:
        if any(a <= n <= b for a, b in new_spans):
            return False
    for o in fc.removed:
        if any(a <= o <= b for a, b in old_spans):
            return False
    return True


# --------------------------------------------------------------------------- statements


_STMT_CACHE: dict[str, list[tuple[int, int, str]] | None] = {}


def stmt_ranges(rel: str) -> list[tuple[int, int, str]] | None:
    """Line ranges of simple statements and of compound-statement headers (old file)."""
    if rel in _STMT_CACHE:
        return _STMT_CACHE[rel]
    out: list[tuple[int, int, str]] | None
    try:
        tree = ast.parse((SRC / rel).read_text())
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            body = getattr(node, "body", None)
            if isinstance(body, list) and body:
                start = node.lineno
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                end = max(start, body[0].lineno - 1)
            else:
                start, end = node.lineno, node.end_lineno or node.lineno
            out.append((start, end, type(node).__name__))
    except (OSError, SyntaxError, ValueError):
        out = None
    _STMT_CACHE[rel] = out
    return out


def innermost_stmt(rel: str, line: int) -> tuple[int, int, str] | None:
    best = None
    for s, e, kind in stmt_ranges(rel) or ():
        if s <= line <= e and (best is None or e - s < best[1] - best[0]):
            best = (s, e, kind)
    return best


IMPORT_LINE = re.compile(r"^\s*(import|from)\s")


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, path: Path) -> None:
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.sets = d["sets"]
        self.src = d["src"]
        self.data = d.get("data", {})
        self.tfile = d.get("tfile", {})
        self.assign = d.get("assign", {})
        self.names = d.get("names", {})
        self._cache: dict[int, frozenset] = {}
        self._starts: dict[str, list[int]] = {}

    def S(self, sid: int) -> frozenset:
        s = self._cache.get(sid)
        if s is None:
            acc, out = 0, []
            for dlt in self.sets[sid]:
                acc += dlt
                out.append(acc)
            s = self._cache[sid] = frozenset(out)
        return s

    def line_set(self, rel: str, line: int) -> frozenset | None:
        lines = self.src[rel]["lines"]
        starts = self._starts.get(rel)
        if starts is None:
            starts = self._starts[rel] = [r[0] for r in lines]
        i = bisect.bisect_right(starts, line) - 1
        if i >= 0 and lines[i][0] <= line <= lines[i][1]:
            return self.S(lines[i][2])
        return None

    def range_set(self, rel: str, a: int, b: int) -> frozenset:
        lines = self.src[rel]["lines"]
        starts = self._starts.get(rel)
        if starts is None:
            starts = self._starts[rel] = [r[0] for r in lines]
        i = max(bisect.bisect_right(starts, a) - 1, 0)
        out: set[int] = set()
        while i < len(lines) and lines[i][0] <= b:
            if lines[i][1] >= a:
                out |= self.S(lines[i][2])
            i += 1
        return frozenset(out)

    def scope_index(self, rel: str, a: int, b: int | None = None) -> int:
        best = 0
        for i, sc in enumerate(self.src[rel]["scopes"]):
            if sc[0] <= a <= sc[1] and (b is None or sc[0] <= b <= sc[1]):
                best = i  # preorder: the last container is the innermost
        return best

    def scope_total(self, rel: str, i: int) -> frozenset:
        return self.S(self.src[rel]["scopes"][i][4])


# --------------------------------------------------------------------------- selection


class Selector:
    def __init__(self, tests: list[dict], m: Map | None) -> None:
        self.m = m
        self.secs = {t["number"]: float(t["seconds"]) for t in tests}
        self.all = frozenset(self.secs)
        self.paths: dict[str, set[int]] = {}
        for t in tests:
            p = self._path(t)
            if p:
                self.paths.setdefault(p, set()).add(t["number"])

    @staticmethod
    def _path(t: dict) -> str | None:
        for k in ("path", "file", "file_path", "filename"):
            if isinstance(t.get(k), str):
                return t[k]
        for v in t.values():
            if isinstance(v, str) and v.endswith(".py"):
                return v
        return None

    def tests_in_file(self, path: str) -> frozenset | None:
        if self.m is not None and path in self.m.tfile:
            return self.m.S(self.m.tfile[path])
        if path in self.paths:
            return frozenset(self.paths[path])
        return None

    # -- per file kinds
    def for_source(self, fc: FileChange) -> frozenset:
        rel = fc.path
        m = self.m
        if m is None or rel not in m.src:
            return frozenset() if fc.status == "new" else self.all
        entry = m.src[rel]
        out: set[int] = set()

        if fc.status in ("deleted", "renamed"):
            out |= m.scope_total(rel, 0)
            out |= m.S(entry["imp"])
            return frozenset(out)

        # 1. does the new file still parse? If not, every importer breaks.
        if fc.hunks and not fc.binary:
            try:
                old_text = (SRC / rel).read_text()
                old_lines = old_text.split("\n")
                new_text = "\n".join(apply_hunks(old_lines, fc.hunks))
                ast.parse(new_text)
                if COMMENTS_HARMLESS and comment_only(old_text, new_text, fc):
                    return frozenset()  # same code, different comments/blank lines
            except SyntaxError:
                out |= m.S(entry["imp"]) | m.scope_total(rel, 0)
                return frozenset(out)
            except OSError:
                pass

        # 2. removed / replaced lines: narrow set if covered, else the enclosing scope.
        # Small module: just run everything that touches it (cheap, and immune to leaky coverage).
        mod_total = m.scope_total(rel, 0)
        if len(mod_total) <= SMALL_MODULE:
            out |= mod_total
        if EXPLAIN:
            log(f"    module union {len(mod_total)} tests" + (" (small-module rule: used)" if len(mod_total) <= SMALL_MODULE else ""))

        for r in fc.removed:
            ls = None
            if NARROW == "line":
                ls = m.line_set(rel, r)
            elif NARROW == "stmt":
                rng = innermost_stmt(rel, r)
                if rng is not None:
                    ls = m.range_set(rel, rng[0], rng[1]) or None  # empty -> scope fallback
                    if rng[2] in ("Import", "ImportFrom"):  # runs once at load time: every importer
                        out |= m.S(entry["imp"])
            if EXPLAIN:
                log(f"    removed line {r}: " + (f"narrow set {len(ls)} tests" if ls is not None
                    else f"scope fallback {len(m.scope_total(rel, m.scope_index(rel, r)))} tests"))
            if ls is not None:
                out |= ls
            else:
                out |= m.scope_total(rel, m.scope_index(rel, r))
            if IMPORT_HEDGE and m.src[rel]["scopes"][m.scope_index(rel, r)][3] != "func":
                out |= m.S(entry["imp"])

        # 2b. a changed constant/class attribute is read from other files: add its readers' tests.
        for r in fc.removed:
            for s_, e_, name in m.assign.get(rel, ()):
                key = f"{rel}::{name}" if f"{rel}::{name}" in m.names else name
                if s_ <= r <= e_ and key in m.names:
                    if EXPLAIN:
                        log(f"    names[{key}] -> {len(m.S(m.names[key]))} tests")
                    out |= m.S(m.names[key])

        # 2c. an added import can fail at load time for every importer of this module.
        if any(IMPORT_LINE.match(t) for t in fc.added_text):
            out |= m.S(entry["imp"])

        # 3. pure insertions: the smallest scope containing both neighbours.
        for a, b in fc.inserted:
            i = m.scope_index(rel, max(a, 1), b)
            if EXPLAIN:
                log(f"    insertion after line {a}: scope union {len(m.scope_total(rel, i))} tests")
            out |= m.scope_total(rel, i)
            if IMPORT_HEDGE and m.src[rel]["scopes"][i][3] != "func":
                out |= m.S(entry["imp"])
        return frozenset(out)

    def for_other(self, fc: FileChange) -> frozenset:
        path = fc.path
        if path.startswith("sqlglot/") and path.endswith(".py"):
            return self.for_source(fc)
        if path.startswith("sqlglot/") and path.endswith("py.typed"):
            return frozenset()
        if path.startswith("tests/fixtures/") or (path.startswith("tests/") and not path.endswith(".py")):
            if self.m is not None and path in self.m.data:
                return self.m.S(self.m.data[path])
            if fc.status == "new":
                return frozenset()
            return self.all
        if path.startswith("tests/") and path.endswith(".py"):
            if TEST_FILE.search(path):
                own = self.tests_in_file(path)
                if own is not None:
                    return own
                return frozenset() if fc.status == "new" else self.all
            return self.all  # conftest, helpers, __init__
        if HARMLESS.search(path):
            return frozenset()
        return self.all if UNKNOWN_FILE_RUNS_ALL else frozenset()

    def select(self, patch_text: str) -> frozenset:
        files = parse_patch(patch_text)
        if not files:
            return self.all  # unparseable: be safe
        out: set[int] = set()
        for fc in files:
            r = self.for_other(fc)
            if fc.status == "renamed" and fc.old:
                r = r | self.for_other(_as_deleted(fc))
            if EXPLAIN:
                log(f"  {fc.path}: {len(r)} tests")
            out |= r
            if len(out) == len(self.all):
                break
        out = frozenset(out)
        # leaky expansion only when sqlglot source changed; a fixture/test-file edit is already exact
        src_changed = any(fc.path.startswith("sqlglot/") and fc.path.endswith(".py") for fc in files)
        if src_changed and out and len(out) < len(self.all):
            changed = [fc.path for fc in files]
            for tf, prefixes in LEAKY_FILES.items():
                own = self.paths.get(tf)
                if own and (out & own or any(p.startswith(pre) for p in changed for pre in prefixes)):
                    out |= own
        return frozenset(out)

    # -- worker packing
    def pack(self, sel) -> list[list[int]]:
        if not sel:
            return []
        secs = self.secs
        items = sorted(sel, key=lambda n: -secs[n])
        best_cost, best = None, None
        for k in range(1, min(MAX_WORKERS, len(items)) + 1):
            heap = [(0.0, i) for i in range(k)]
            bins: list[list[int]] = [[] for _ in range(k)]
            for n in items:
                load, i = heapq.heappop(heap)
                bins[i].append(n)
                heapq.heappush(heap, (load + secs[n], i))
            _improve(bins, secs)
            cost = max(sum(secs[n] for n in b) for b in bins) + WORKER_COST * k
            if best_cost is None or cost < best_cost - 1e-9:
                best_cost, best = cost, bins
        return best


def _improve(bins, secs) -> None:
    """Shrink the busiest bin by moving or swapping tests with the lightest bin."""
    if len(bins) < 2:
        return
    loads = [sum(secs[n] for n in b) for b in bins]
    for _ in range(300):
        a = loads.index(max(loads))
        b = loads.index(min(loads))
        diff = loads[a] - loads[b]
        if diff <= 1e-9:
            return
        half = diff / 2
        best_gain, move = 0.0, None
        for x in bins[a]:
            w = secs[x]
            if 0 < w < diff:  # move x from a to b
                gain = min(w, diff - w)
                if gain > best_gain + 1e-12:
                    best_gain, move = gain, (x, None)
            for y in bins[b]:  # swap x with y
                d = w - secs[y]
                if 0 < d < diff:
                    gain = min(d, diff - d)
                    if gain > best_gain + 1e-12:
                        best_gain, move = gain, (x, y)
        if move is None:
            return
        x, y = move
        bins[a].remove(x)
        bins[b].append(x)
        loads[a] -= secs[x]
        loads[b] += secs[x]
        if y is not None:
            bins[b].remove(y)
            bins[a].append(y)
            loads[a] += secs[y]
            loads[b] -= secs[y]


def _as_deleted(fc: FileChange) -> FileChange:
    d = FileChange()
    d.old, d.status = fc.old, "deleted"
    return d


def main() -> None:
    raw = json.loads((DATA / "tests.json").read_text())
    tests = raw["tests"] if isinstance(raw, dict) else raw
    try:
        m = Map(MAP_PATH)
    except Exception as e:  # missing/corrupt map -> safe degraded mode
        log("map unavailable, degraded mode:", repr(e))
        m = None
    sel = Selector(tests, m)
    all_workers = sel.pack(sel.all)
    n_all = n_none = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = sel.select(patch.read_text(errors="replace"))
        except Exception as e:
            log(patch.stem, "selection failed, running all:", repr(e))
            chosen = sel.all
        if DROP_TESTS or DROP_SECONDS:
            chosen = frozenset(n for n in chosen if n not in DROP_TESTS
                               and not (DROP_SECONDS and sel.secs[n] >= DROP_SECONDS))
            workers = sel.pack(chosen)
            n_none += not workers
        elif len(chosen) == len(sel.all):
            workers = all_workers
            n_all += 1
        else:
            workers = sel.pack(chosen)
            n_none += not workers
        log(f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers")
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    log(f"all-tests commits: {n_all}, empty commits: {n_none}")


if __name__ == "__main__":
    main()
