#!/usr/bin/env python3
"""Run every demo; each one checks its own claims."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stele import demos, render  # noqa: E402


def main() -> int:
    render.use_utf8_stdout()
    for index, demo in enumerate(demos.all_demos()):
        if index:
            print()
        print(demo.title)
        print(render.glyph("dash") * render.display_width(demo.title))
        print(demo.body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
