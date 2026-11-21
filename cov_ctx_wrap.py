"""Imported inside pool workers: runs a task under the submitting test's coverage context."""


class CtxCall:
    def __init__(self, fn, ctx):
        self.fn = fn
        self.ctx = ctx

    def __call__(self, *args, **kwargs):
        cov = None
        try:
            import coverage

            cov = coverage.Coverage.current()
            if cov is not None:
                cov.switch_context(self.ctx)
        except Exception:
            cov = None
        # no reset afterwards: the worker pickles the result (calling serde.dump) in a feeder
        # thread after this returns; it stays credited to this test until the next task.
        return self.fn(*args, **kwargs)
