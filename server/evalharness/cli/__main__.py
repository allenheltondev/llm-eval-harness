"""``python -m evalharness.cli`` -- the same entry point as the console script.

The ``__name__`` guard is what keeps this importable: without it, merely
importing the module would parse arguments and exit.
"""

from evalharness.cli.main import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
