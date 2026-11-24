#!/usr/bin/env python3
"""Run the test suite without installing anything."""

from __future__ import annotations

import os
import sys
import unittest


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, root)
    suite = unittest.defaultTestLoader.discover(
        os.path.join(root, "tests"), top_level_dir=root
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
