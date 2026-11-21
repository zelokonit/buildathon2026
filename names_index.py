"""Names index: constant/attribute edits are read elsewhere, so line coverage can't see them.

For every UPPER_CASE name assigned at module/class level, find every other place in sqlglot/ that
reads that name (ast.Name / ast.Attribute) and union the covering sets of the enclosing scopes.
Adds out["assign"][rel] = [[start, end, name], ...] and out["names"][name] = setid.

call from build_map.main() after the per-file loop, before out["sets"] = pool.encode():
    import names_index; names_index.add_names(out, SRC, pool, ctx_sets)   # see below
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

    by_name = defaultdict(set)  # name -> {setid}
    for rel, tree in trees.items():
        scopes = out["src"][rel]["scopes"]
        for node in ast.walk(tree):
            nm = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if nm in all_names:
                best = 0
                for i, sc in enumerate(scopes):
                    if sc[0] <= node.lineno <= sc[1]:
                        best = i
                by_name[nm].add(scopes[best][4])
    for nm, sids in by_name.items():
        u = set()
        for sid in sids:
            u |= set(pool.sets[sid])
        out["names"][nm] = pool.add(u)
