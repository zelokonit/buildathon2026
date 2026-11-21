"""Names index: constant/attribute edits are read elsewhere, so line coverage can't see them.

For every UPPER_CASE name assigned at module/class level, find every other place in sqlglot/ that
reads that name (ast.Name / ast.Attribute) and union the covering sets of the enclosing scopes.
Adds out["assign"][rel] = [[start, end, name], ...] and out["names"][name] = setid.

called from tools/build_map.py (already wired in).
`pool` is the builder's Pool. Scope sets are already in out["src"][rel]["scopes"][i][4].
"""
import ast
from collections import defaultdict
from pathlib import Path


def _upper(n):
    return n.isupper() or (n.replace("_", "").isalnum() and n == n.upper() and any(c.isalpha() for c in n))


def _assigns(tree):
    out = []

    def visit(body, top):
        for st in body:
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    if isinstance(t, ast.Name) and _upper(t.id) and not t.id.startswith("__"):
                        out.append((st.lineno, st.end_lineno, t.id))
            elif isinstance(st, ast.ClassDef):
                visit(st.body, False)
    visit(tree.body, True)
    return out


def add_names(out, src, pool):
    src = Path(src)
    out["assign"], out["names"] = {}, {}
    trees, all_names = {}, set()
    for rel in out["src"]:
        p = src / rel
        try:
            trees[rel] = ast.parse(p.read_text())
        except (OSError, SyntaxError):
            continue
        a = _assigns(trees[rel])
        if a:
            out["assign"][rel] = [[s, e, n] for s, e, n in a]
            all_names |= {n for _, _, n in a}

    by_name = defaultdict(set)  # name -> set of test ids that executed a line reading it
    for rel, tree in trees.items():
        ent = out["src"][rel]
        scopes, lines = ent["scopes"], ent["lines"]
        for node in ast.walk(tree):
            nm = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if nm not in all_names:
                continue
            ln = node.lineno
            hit = None
            for s0, e0, sid in lines:  # per-line coverage of the reading statement
                if s0 <= ln <= e0:
                    hit = sid
                    break
            if hit is None:  # uncovered or continuation line: the enclosing function/class
                best = 0
                for i, sc in enumerate(scopes):
                    if sc[0] <= ln <= sc[1]:
                        best = i
                hit = scopes[best][4]
            by_name[nm] |= set(pool.sets[hit])
    for nm, u in by_name.items():
        out["names"][nm] = pool.add(u)
    return
