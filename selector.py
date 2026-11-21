"""The starter selector: run every test on every commit, spread over 3 workers.

It never misses a failing test and it is slow. Everything you do from here
is deciding which tests a change cannot break.
"""

import json
import sys
from pathlib import Path

DATA = Path("/data")
WORKERS = 3


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0.0] * worker_count
    for test in sorted(tests, key=lambda test: test["seconds"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += test["seconds"]
    return workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    workers = split(tests, WORKERS)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"selected {len(tests)} tests on every commit", file=sys.stderr)


if __name__ == "__main__":
    main()
