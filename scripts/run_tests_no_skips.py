"""Run unittest suites and fail when any configured test is skipped."""

from __future__ import annotations

import sys
import unittest


def main() -> int:
    loader = unittest.defaultTestLoader
    suite = (
        loader.loadTestsFromNames(sys.argv[1:])
        if len(sys.argv) > 1
        else loader.discover("tests")
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(f"UNEXPECTED_SKIPPED={len(result.skipped)}", file=sys.stderr)
        return 1
    print(f"TESTS_RUN={result.testsRun}")
    print("UNEXPECTED_SKIPPED=0")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
