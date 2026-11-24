"""Five demos, each of which checks its own numbers before printing them."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from . import bench, render
from .disk import FileDisk, MemoryDisk
from .page import PAGE_SIZE
from .store import Store

DEMO_NAMES = ("basic", "snapshot", "crash", "batch", "reclaim")


@dataclass
class Demo:
    name: str
    title: str
    body: str


def build(name: str) -> Demo:
    try:
        builder = {
            "basic": _basic,
            "snapshot": _snapshot,
            "crash": _crash,
            "batch": _batch,
            "reclaim": _reclaim,
        }[name]
    except KeyError:
        raise KeyError(f"unknown demo {name!r}; choose from {', '.join(DEMO_NAMES)}") from None
    return builder()


def all_demos() -> List[Demo]:
    return [build(name) for name in DEMO_NAMES]


# --------------------------------------------------------------------------
# basic
# --------------------------------------------------------------------------


def _basic() -> Demo:
    store = Store(MemoryDisk())
    for start in range(0, 500, 100):
        with store.write() as writer:
            for index in range(start, start + 100):
                writer.put(b"user:%04d" % index, b'{"name":"user %d"}' % index)
    with store.write() as writer:
        assert writer.delete(b"user:0007")
        assert not writer.delete(b"user:0007")
        writer.put(b"user:0008", b'{"name":"updated"}')

    assert store.get(b"user:0000") == b'{"name":"user 0"}'
    assert store.get(b"user:0007") is None
    assert store.get(b"user:0008") == b'{"name":"updated"}'
    page = [key for key, _ in store.scan(b"user:0010", b"user:0015")]
    assert page == [b"user:%04d" % index for index in range(10, 15)], page

    stats = store.stats(deep=True)
    check = store.verify()
    assert check.ok and check.items == 499

    lines = [
        "500 keys inserted, one deleted, one replaced in a second transaction.",
        f"tree: {render.plural(stats.items, 'item')}, "
        f"{render.plural(stats.leaves, 'leaf', 'leaves')}, depth {stats.depth}",
        f"file: {stats.page_count:,} pages ({render.size(stats.page_count * PAGE_SIZE)})",
        f"verify: {'ok' if check.ok else check.problem}",
        "",
        "scan('user:0010', 'user:0015') gives:",
    ]
    lines += [f"  {key.decode():<12} {value.decode()}" for key, value in store.scan(b"user:0010", b"user:0015")]
    return Demo("basic", "Basic operations, checked against expectations", "\n".join(lines))


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def _snapshot() -> Demo:
    store = Store(MemoryDisk())
    rows = 1_000
    for start in range(0, rows, 100):
        with store.write() as writer:
            for index in range(start, start + 100):
                writer.put(b"row:%05d" % index, b"version 1")
    before = store.stats().page_count

    reader = store.snapshot()
    assert reader.get(b"row:00000") == b"version 1"

    for round_index in range(3):
        for start in range(0, rows, 100):
            with store.write() as writer:
                for index in range(start, start + 100):
                    writer.put(b"row:%05d" % index, b"version %d" % (round_index + 2))

    held_value = reader.get(b"row:00000")
    assert held_value == b"version 1", "the reader must not see later commits"
    assert len(reader) == rows
    held = store.stats()
    assert held.pending_frees > 0, "pages a reader may still reach cannot be recycled"

    reader.close()
    released = store.stats()
    assert released.pending_frees == 0
    assert released.free_pages > held.free_pages

    lines = [
        "A reader pins the root page id it started from; nothing else.",
        "",
        f"3 rewrites of {rows:,} rows happened while the snapshot was open.",
        f"  the snapshot read before closing: {held_value!r}",
        f"  the store now reads:      {store.get(b'row:00000')!r}",
        "",
        f"pages held for the reader:   {held.pending_frees}",
        f"pages freed after it closed: {released.free_pages - held.free_pages}",
        f"file pages: {before:,} -> {released.page_count:,}: nothing could be recycled "
        f"until the reader left, and the file keeps that peak",
    ]
    return Demo("snapshot", "A snapshot is an integer, and it holds pages hostage", "\n".join(lines))


# --------------------------------------------------------------------------
# crash
# --------------------------------------------------------------------------


def _crash() -> Demo:
    report = bench.crash_matrix(rows=200, extra=40)
    assert report.ok, report.failures
    assert report.old_state > 0 and report.new_state > 0
    lines = [
        "The commit protocol in one sentence: pages first, fsync, then one meta page.",
        "",
        *bench.describe(report),
        "",
        "Every crash point produced one of exactly two databases: the one before",
        "the transaction, or the one after it. Never a mixture.",
    ]
    return Demo("crash", "Pull the plug at every write", "\n".join(lines))


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------


def _batch() -> Demo:
    rows = 2_000
    effect = bench.batch_effect(rows)
    assert effect[0][1] > effect[-1][1] * 10, "batching should dominate the bill"
    pages = [page_count for _, _, page_count in effect]
    assert max(pages) < min(pages) * 1.25, "the file should not care how the rows were batched"
    lines = [
        f"{rows:,} rows, committed in different batch sizes.",
        "",
        render.table(
            ["batch", "writes / row", "file pages", "file size"],
            [
                [f"{batch:,}", f"{per_row:.2f}", f"{page_count:,}", render.size(page_count * PAGE_SIZE)]
                for batch, per_row, page_count in effect
            ],
            aligns=["right", "right", "right", "right"],
        ),
        "",
        "One row per transaction pays three page writes: the leaf it changed, the",
        "branch above it, and the meta page that publishes the new root.  A",
        "thousand rows at a time pays 0.03.  The file barely notices either way --",
        "a page created and dropped inside a transaction was never durable, so the",
        "allocator reuses it immediately -- and that is the point: batching buys",
        "fsyncs, and the currency is memory, not disk.",
    ]
    return Demo("batch", "Commit size is the write-amplification knob", "\n".join(lines))


# --------------------------------------------------------------------------
# reclaim
# --------------------------------------------------------------------------


def _reclaim() -> Demo:
    lines = [
        "Delete everything, then hand the tail of the file back.",
        "",
        *bench.reclaim(3_000),
        "",
        "trim() walks the tree, finds the highest reachable page, and truncates",
        "above it -- then rewrites the meta page so the new size is durable.",
    ]
    return Demo("reclaim", "What is left after the rows are gone", "\n".join(lines))


# --------------------------------------------------------------------------
# a file-backed smoke test used by the CLI demo and the tests
# --------------------------------------------------------------------------


def file_demo(path: str) -> str:
    """Write, commit, reopen, read.  Returns a one-line summary."""
    store = Store(FileDisk(path))
    with store.write() as writer:
        for index in range(100):
            writer.put(b"k%03d" % index, b"v%03d" % index)
    store.close()
    again = Store(FileDisk(path))
    value = again.get(b"k050")
    size_bytes = os.path.getsize(path)
    again.close()
    return f"100 keys, {size_bytes} bytes on disk, reopened and read {value!r}"
