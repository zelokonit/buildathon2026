"""Per-test coverage with exact contexts, including multiprocessing children.

Why: sqlglot's optimizer tests run work in child processes (a ProcessPoolExecutor with the spawn
method, created once per class in setUpClass). Plain coverage / pytest-cov either
miss it or record it with no test context, so the optimizer code looks uncovered by those tests.

How: coverage's concurrency=multiprocessing measures children (spawn included). Because pool
workers outlive a single test, every task submitted to a ProcessPoolExecutor is wrapped so the
worker switches to the submitting test's context for the duration of that task
(tools/cov_ctx_wrap.py). The parent switches its own dynamic context per test.

run from the sqlglot checkout (serial; no xdist):
  COV_DATA=.coverage.ctx PYTHONPATH=<repo>/tools pytest -p cov_plugin -p no:xdist -p no:cacheprovider -q tests
Output files: .coverage.ctx.<host>.<pid>.<rand> (parent and every child); build_map.py merges them all.
"""
import os
import sys

import coverage
import pytest

_cov = None


def pytest_configure(config):
    global _cov
    root = os.getcwd()
    data = os.environ.get("COV_DATA", os.path.join(root, ".coverage.ctx"))
    rc = os.path.join(root, ".covrc.ctx")
    with open(rc, "w") as fh:
        fh.write(
            "[run]\n"
            f"source = {os.path.join(root, 'sqlglot')}\n"
            f"data_file = {data}\n"
            "parallel = True\n"
            "concurrency = multiprocessing,thread\n"
            "context = ${COV_CTX}\n"
        )
    os.environ["COV_CTX"] = ""
    # spawned workers must be able to import cov_ctx_wrap
    here = os.path.dirname(os.path.abspath(__file__))
    os.environ["PYTHONPATH"] = here + os.pathsep + os.environ.get("PYTHONPATH", "")
    sys.path.insert(0, here)
    _patch_pool()
    _cov = coverage.Coverage(config_file=rc)
    _cov.erase()
    _cov.start()
    sys.__stderr__.write(f"[cov_plugin] rcfile={rc} data={data}\n")


def _patch_pool():
    import concurrent.futures.process as cfp
    from cov_ctx_wrap import CtxCall

    orig = cfp.ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        ctx = os.environ.get("COV_CTX", "")
        return orig(self, CtxCall(fn, ctx) if ctx else fn, *args, **kwargs)

    cfp.ProcessPoolExecutor.submit = submit


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    os.environ["COV_CTX"] = item.nodeid  # inherited by any process the test starts
    _cov.switch_context(item.nodeid)
    yield
    os.environ["COV_CTX"] = ""
    _cov.switch_context("")


def pytest_unconfigure(config):
    if _cov is not None:
        _cov.stop()
        _cov.save()
        sys.__stderr__.write("[cov_plugin] saved (children save on their own exit)\n")
