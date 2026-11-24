#!/usr/bin/env python3
"""Watch a snapshot survive three rewrites of everything it can see.

    python examples/snapshot_tour.py

A snapshot in this engine is a root page id and a transaction number.  Because
no page is ever modified in place, that integer is all a reader needs to keep
seeing the database as it was.  The price is that the pages it can still reach
cannot be recycled until it closes -- the script prints that bill too.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stele import render  # noqa: E402
from stele.disk import MemoryDisk  # noqa: E402
from stele.page import PAGE_SIZE  # noqa: E402
from stele.store import Store  # noqa: E402

ROWS = 1_000
ROUNDS = 3


def main() -> int:
    disk = MemoryDisk()
    store = Store(disk)
    with store.write() as writer:
        for index in range(ROWS):
            writer.put(b"row-%05d" % index, b"version 1")

    reader = store.snapshot()
    print(f"{ROWS:,} rows written in transaction {store.txid}.")
    print(f"The snapshot is (root page {reader.root}, transaction {reader.txid}).")
    print()

    rows = []
    for round_index in range(ROUNDS):
        before = store.stats()
        with store.write() as writer:
            for index in range(ROWS):
                writer.put(b"row-%05d" % index, b"version %d" % (round_index + 2))
        after = store.stats()
        rows.append(
            [
                f"{after.txid}",
                f"{reader.txid}",
                f"{reader.get(b'row-00000')!r}",
                f"{store.get(b'row-00000')!r}",
                f"{after.pending_frees:,}",
                f"{after.page_count:,}",
            ]
        )
        assert before.txid + 1 == after.txid

    print(
        render.table(
            ["store txid", "reader txid", "reader sees", "store sees", "pages held", "file pages"],
            rows,
            aligns=["right", "right", "left", "left", "right", "right"],
        )
    )
    print()
    print(f"The reader's answer never changed; the store's changed {ROUNDS} times.")

    held = store.stats()
    print()
    print(f"pages a reader could still reach: {held.pending_frees:,}")
    print(f"pages available for reuse:      {held.free_pages:,}")
    print("nothing that can be reached from a live snapshot goes back on the free list")

    reader.close()
    released = store.stats()
    print()
    print(f"reader closed -> {released.pending_frees:,} held, {released.free_pages:,} free")
    trimmed = store.trim()
    print(
        f"trim() -> {trimmed:,} pages ({render.size(trimmed * PAGE_SIZE)}), "
        f"tree still reads {store.get(b'row-00000')!r}"
    )
    print(f"verify: {'ok' if store.verify().ok else 'BROKEN'}")

    with store.snapshot() as again:
        assert again.get(b"row-00000") == b"version %d" % (ROUNDS + 1)
        assert len(again) == ROWS
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
