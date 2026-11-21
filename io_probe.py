"""pytest plugin: record which non-.py files each test opens (audit hook sees every open()).

run (from the sqlglot checkout, no xdist):  PYTHONPATH=<repo>/tools pytest -p io_probe tests -q
writes io_log.json: {"tests/fixtures/x.sql": ["tests/test_a.py::TestA::test_x", "tests/test_b.py", ...]}
Opens during collection/module import are credited to the whole test file.
"""
import json, os, sys, atexit
import pytest

ROOT = os.path.realpath(os.getcwd())
N = [0]
LOG = {}
cur = ["<none>"]
active = [False]


def _hook(event, args):
    if event != "open" or not active[0]:
        return
    p = args[0]
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    if not isinstance(p, str):
        return
    if p.endswith((".py", ".pyc", ".so")):
        return
    ap = os.path.realpath(p)
    if not ap.startswith(ROOT + os.sep) or "/.git/" in ap or "__pycache__" in ap or "/.pytest_cache" in ap:
        return
    N[0] += 1
    LOG.setdefault(os.path.relpath(ap, ROOT), set()).add(cur[0])


sys.addaudithook(_hook)


@pytest.hookimpl(hookwrapper=True)
def pytest_collectstart(collector):
    p = getattr(collector, "path", None)
    cur[0] = os.path.relpath(str(p), ROOT) if p is not None and os.path.isfile(str(p)) else cur[0]
    active[0] = True
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    cur[0] = item.nodeid
    active[0] = True
    yield


def pytest_configure(config):
    sys.__stderr__.write(f"[io_probe] loaded in pid {os.getpid()} root={ROOT} "
                         f"xdist_worker={os.environ.get('PYTEST_XDIST_WORKER')}\n")


def _dump():
    active[0] = False
    w = os.environ.get("PYTEST_XDIST_WORKER")
    name = f"io_log.{w}.json" if w else "io_log.json"   # never let workers overwrite each other
    with open(os.path.join(ROOT, name), "w") as fh:
        json.dump({k: sorted(v) for k, v in LOG.items()}, fh)
    sys.__stderr__.write(f"[io_probe] wrote {name}: {len(LOG)} files, {N[0]} opens\n")


atexit.register(_dump)
