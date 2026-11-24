#!/usr/bin/env python3
"""A guided tour of the store, on a real file, with no install step.

    python examples/tour.py                # temporary file, thrown away
    python examples/tour.py --keep tour.db # keep it, then poke at it with run.py

The script loads a small glossary, reads it back, edits one entry, deletes
another, then asks the engine to describe itself and to check its own pages.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stele import render  # noqa: E402
from stele.page import PAGE_SIZE  # noqa: E402
from stele.store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GLOSSARY = os.path.join(HERE, "data", "glossary.tsv")


def load_rows(path: str):
    rows = []
    # utf-8-sig: the file may have been saved by an editor that adds a BOM.
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("\t")
            rows.append((key.encode("utf-8"), value.encode("utf-8")))
    return rows


def cut(text: str, width: int = 30) -> str:
    """Shorten a string to at most ``width`` terminal cells."""
    if render.display_width(text) <= width:
        return text
    kept = ""
    for char in text:
        if render.display_width(kept + char) > width - 1:
            break
        kept += char
    return kept + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", metavar="PATH", help="write to this file instead of a temp one")
    args = parser.parse_args()

    directory = None
    path = args.keep
    if path is None:
        directory = tempfile.TemporaryDirectory()
        path = os.path.join(directory.name, "tour.db")

    rows = load_rows(GLOSSARY)
    print(f"file: {path}")
    print(f"glossary: {len(rows)} entries from examples/data/glossary.tsv")
    print()

    store = Store.open(path)
    try:
        with store.write() as writer:
            for key, value in rows:
                writer.put(key, value)
        print(f"1. loaded {len(rows)} entries in transaction {store.txid}")

        print()
        print("2. every key, in byte order, with the value cut to fit:")
        print(
            render.table(
                ["key", "value"],
                [[key.decode(), cut(value.decode())] for key, value in store.scan()],
            )
        )

        with store.write() as writer:
            writer.put(b"fsync", "落盘：本引擎每次提交调用两次".encode("utf-8"))
            removed = writer.delete(b"wal")
        print()
        print(f"3. one key replaced, 'wal' removed: {removed}, transaction {store.txid}")

        key, value = next(iter(store.scan(b"fsync", b"fsyncz")))
        print(f"   fsync -> {value.decode()}")

        stats = store.stats(deep=True)
        report = store.verify()
        print()
        print("4. the engine describing itself:")
        print(
            render.table(
                ["field", "value"],
                [
                    ["transaction", f"{stats.txid}"],
                    ["root page", f"{stats.root}"],
                    ["items", f"{stats.items}"],
                    ["leaves / branches", f"{stats.leaves} / {stats.branches}"],
                    ["depth", f"{stats.depth}"],
                    ["pages", f"{stats.page_count} ({render.size(stats.page_count * PAGE_SIZE)})"],
                    ["free pages", f"{stats.free_pages}"],
                    ["pages on disk", f"{stats.file_pages}"],
                    ["verify", "ok" if report.ok else f"broken: {report.problem}"],
                ],
            )
        )

        deleted = 0
        with store.write() as writer:
            for key, _ in rows:
                if writer.delete(key):
                    deleted += 1
        emptied = store.stats().page_count
        trimmed = store.trim()
        print()
        print(f"5. deleted {deleted} keys: still {emptied} pages on disk")
        print(f"   trim() gave the tail back: {trimmed} pages ({render.size(trimmed * PAGE_SIZE)})")
        print(f"   tree is empty: {store.verify().items == 0}")
    finally:
        store.close()

    if directory is not None:
        directory.cleanup()
    else:
        print()
        print(f"kept: {path} -- try  python run.py stat {path} --deep")
    return 0


if __name__ == "__main__":
    sys.exit(main())
