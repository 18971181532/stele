#!/usr/bin/env python3
"""Cut the power at every page write of one commit, and see what is left.

    python examples/crash_tour.py

The engine claims a commit is atomic: after any crash the file holds either
the database from before the transaction or the one after it, and it always
opens.  This script does not take that on faith.  It measures how many page
writes a commit needs, then re-runs the commit once per write with the power
cut at exactly that write -- twice, because a device can also stop mid-sector.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stele import render  # noqa: E402
from stele.disk import Crash, CrashDisk, MemoryDisk  # noqa: E402
from stele.store import Store  # noqa: E402

ROWS = 300
EXTRA = 40


def build(rows: int = ROWS) -> MemoryDisk:
    disk = MemoryDisk()
    store = Store(disk)
    with store.write() as writer:
        for index in range(rows):
            writer.put(b"key-%06d" % index, b"old")
    store.close()
    return MemoryDisk(dict(disk.pages))


def commit(device: CrashDisk, rows: int = ROWS, extra: int = EXTRA) -> None:
    with Store(device).write() as writer:
        for index in range(rows, rows + extra):
            writer.put(b"key-%06d" % index, b"new")


def writes_for_a_commit(image: MemoryDisk) -> int:
    probe = CrashDisk(MemoryDisk(dict(image.pages)))
    commit(probe)
    return probe.writes


def main() -> int:
    image = build()
    points = writes_for_a_commit(image)
    print(f"{ROWS} rows on disk; the next commit inserts {EXTRA} more.")
    print(f"That commit needs {points} page writes, so there are {points * 2} ways to lose power.")
    print()

    rows = []
    counts = {"before": 0, "after": 0}
    failures = []
    for point in range(1, points + 1):
        for torn in (False, True):
            device = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=point)
            try:
                commit(device)
            except Crash:
                pass
            recovered = Store(device.crash(torn=torn))
            report = recovered.verify()
            outcome = "before" if recovered.txid == 1 else "after"
            if not report.ok:
                failures.append(f"write {point} torn={torn}: {report.problem}")
            elif outcome == "before":
                counts["before"] += 1
            else:
                counts["after"] += 1
            rows.append(
                [
                    f"{point}",
                    "torn" if torn else "clean",
                    f"{recovered.txid}",
                    f"{report.items}",
                    f"{recovered.get(b'key-000000')!r}",
                    "ok" if report.ok else "BROKEN",
                    outcome,
                ]
            )

    print(
        render.table(
            ["write", "tear", "txid", "keys", "first key", "verify", "state"],
            rows,
            aligns=["right", "left", "right", "right", "left", "left", "left"],
        )
    )
    print()
    print(f"recovered and verified: {counts['before'] + counts['after']} of {points * 2}")
    print(f"  the database from before the transaction: {counts['before']}")
    print(f"  the database from after it:               {counts['after']}")
    print("  anything else:                           " f"{len(failures)}")
    for failure in failures:
        print(f"  FAILED {failure}")

    # The nastiest tear: the meta page loses its first 512-byte sector, so the
    # page that publishes the commit is unreadable.  The older meta page must
    # take over, and the whole transaction must disappear with it.
    device = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=points)
    try:
        commit(device)
    except Crash:
        pass
    damaged = Store(device.crash(drop_head=True))
    dropped = damaged.get(b"key-%06d" % (ROWS + EXTRA - 1))
    print()
    print("If the meta page itself loses its first sector:")
    print(f"  reopened at transaction {damaged.txid}, {damaged.verify().items} keys")
    print(f"  the last inserted key reads back as {dropped!r} -- the commit is simply gone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
