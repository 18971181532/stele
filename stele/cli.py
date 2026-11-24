"""Command line interface.

Exit codes are part of the contract:

0  the command did what it said
1  the file could not be read or understood
2  the arguments are wrong
3  the answer is "no": no such key, or verification failed
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List, Optional, Sequence

from . import __version__, bench, demos
from . import render
from .disk import DiskError, FileDisk
from .page import PageError
from .store import Store, StoreError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO = 3


class LoadError(Exception):
    """The file is not a store this build can open."""


def _open(path: str) -> Store:
    try:
        return Store(FileDisk(path))
    except (OSError, DiskError, PageError, StoreError) as exc:
        raise LoadError(str(exc)) from None


def _text(value: bytes) -> str:
    return value.decode("utf-8", "backslashreplace")


def _json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_put(args: argparse.Namespace) -> int:
    store = _open(args.file)
    key = args.key.encode("utf-8")
    value = args.value.encode("utf-8")
    with store.write() as writer:
        writer.put(key, value)
    txid = store.txid
    store.close()
    if args.json:
        _json({"key": args.key, "bytes": len(value), "txid": txid})
    else:
        print(f"put {args.key!r} ({len(value)} bytes) in transaction {txid}")
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    store = _open(args.file)
    value = store.get(args.key.encode("utf-8"))
    store.close()
    if value is None:
        if args.json:
            _json({"key": args.key, "found": False})
        else:
            print(f"no value for {args.key!r}")
        return EXIT_NO
    if args.json:
        _json({"key": args.key, "found": True, "value": _text(value)})
    else:
        print(_text(value))
    return EXIT_OK


def cmd_del(args: argparse.Namespace) -> int:
    store = _open(args.file)
    with store.write() as writer:
        removed = writer.delete(args.key.encode("utf-8"))
    txid = store.txid
    store.close()
    if args.json:
        _json({"key": args.key, "removed": removed, "txid": txid})
    else:
        print(f"{'deleted' if removed else 'no such key:'} {args.key!r}")
    return EXIT_OK if removed else EXIT_NO


def cmd_scan(args: argparse.Namespace) -> int:
    store = _open(args.file)
    low = args.low.encode("utf-8") if args.low else None
    high = args.high.encode("utf-8") if args.high else None
    rows: List[List[str]] = []
    for key, value in store.scan(low, high):
        rows.append([_text(key), _text(value)])
        if args.limit and len(rows) >= args.limit:
            break
    store.close()
    if args.json:
        _json({"rows": [{"key": key, "value": value} for key, value in rows]})
    elif rows:
        print(render.table(["key", "value"], rows))
    else:
        print("no rows in range")
    return EXIT_OK if rows else EXIT_NO


def cmd_stat(args: argparse.Namespace) -> int:
    store = _open(args.file)
    stats = store.stats(deep=args.deep)
    store.close()
    payload: Dict[str, object] = {
        "txid": stats.txid,
        "root": stats.root,
        "pages": stats.page_count,
        "file_pages": stats.file_pages,
        "free_pages": stats.free_pages,
        "pending_frees": stats.pending_frees,
        "items": stats.items,
        "depth": stats.depth,
        "leaves": stats.leaves,
        "branches": stats.branches,
        "cache_hits": stats.cache_hits,
        "cache_misses": stats.cache_misses,
    }
    if args.json:
        _json(payload)
        return EXIT_OK
    print(f"txid      {stats.txid}")
    print(f"root      page {stats.root}" if stats.root else "root      empty")
    print(f"pages     {stats.page_count:,} ({render.size(stats.page_count * 4096)}), "
          f"{stats.free_pages:,} free, {stats.pending_frees:,} held for readers")
    print(f"file      {stats.file_pages:,} pages on disk")
    if args.deep:
        print(
            f"items     {render.plural(stats.items, 'key')} in "
            f"{render.plural(stats.leaves, 'leaf', 'leaves')}, depth {stats.depth}"
        )
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    store = _open(args.file)
    report = store.verify()
    store.close()
    payload = {
        "ok": report.ok,
        "problem": report.problem,
        "page": report.page,
        "items": report.items,
        "leaves": report.leaves,
        "branches": report.branches,
        "depth": report.depth,
        "free": report.free,
        "min_key": _text(report.min_key) if report.min_key else None,
        "max_key": _text(report.max_key) if report.max_key else None,
    }
    if args.json:
        _json(payload)
    elif report.ok:
        print(
            f"ok: {render.plural(report.items, 'key')}, "
            f"{render.plural(report.leaves, 'leaf', 'leaves')}, "
            f"{render.plural(report.branches, 'branch', 'branches')}, depth {report.depth}, "
            f"{render.plural(report.free, 'free page')}"
        )
    else:
        print(f"broken at page {report.page}: {report.problem}")
    return EXIT_OK if report.ok else EXIT_NO


def cmd_bench(args: argparse.Namespace) -> int:
    suite = args.suite
    measurements = bench.run(suite)
    total = bench.totals(measurements)
    header = ["workload", "rows", "rate", "bytes/row", "amplify", "time", "pages"]
    rows = [item.row() for item in measurements]
    if args.json:
        _json(
            {
                "suite": suite,
                "measurements": [
                    {
                        "name": item.name,
                        "rows": item.rows,
                        "seconds": item.seconds,
                        "pages_written": item.pages_written,
                        "bytes_written": item.bytes_written,
                        "logical_bytes": item.logical_bytes,
                        "amplification": item.amplification,
                    }
                    for item in measurements
                ],
                "total_seconds": total["seconds"],
            }
        )
        return EXIT_OK
    print(render.table(header, rows, aligns=["left", "right", "right", "right", "right", "right", "right"]))
    print()
    print(
        f"{render.plural(len(measurements), 'workload')}, {int(total['rows']):,} rows, "
        f"{int(total['pages']):,} page writes, {render.seconds(total['seconds'])} total"
    )
    if suite != "file":
        print()
        rows = 2_000 if suite == "quick" else 20_000
        for line in bench.footprint(rows, sizes=(1, 100, rows)):
            print(line)
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    if args.name is None:
        print("demos:")
        for name in demos.DEMO_NAMES:
            print(f"  {name}")
        return EXIT_OK
    demo = demos.build(args.name)
    print(demo.title)
    print(render.glyph("dash") * render.display_width(demo.title))
    print(demo.body)
    return EXIT_OK


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stele",
        description="A copy-on-write key/value store that survives having the power cut.",
    )
    parser.add_argument("--version", action="version", version=f"stele {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    put = sub.add_parser("put", help="write one key")
    put.add_argument("file")
    put.add_argument("key")
    put.add_argument("value")
    put.add_argument("--json", action="store_true")
    put.set_defaults(func=cmd_put)

    get = sub.add_parser("get", help="read one key")
    get.add_argument("file")
    get.add_argument("key")
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=cmd_get)

    delete = sub.add_parser("del", help="remove one key")
    delete.add_argument("file")
    delete.add_argument("key")
    delete.add_argument("--json", action="store_true")
    delete.set_defaults(func=cmd_del)

    scan = sub.add_parser("scan", help="list keys in order")
    scan.add_argument("file")
    scan.add_argument("--low", default=None)
    scan.add_argument("--high", default=None)
    scan.add_argument("--limit", type=int, default=0)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    stat = sub.add_parser("stat", help="show page and tree statistics")
    stat.add_argument("file")
    stat.add_argument("--deep", action="store_true", help="walk the tree to count keys")
    stat.add_argument("--json", action="store_true")
    stat.set_defaults(func=cmd_stat)

    verify = sub.add_parser("verify", help="check every page and invariant")
    verify.add_argument("file")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    bench_parser = sub.add_parser("bench", help="run a benchmark suite")
    bench_parser.add_argument("suite", nargs="?", default="quick", choices=bench.suites())
    bench_parser.add_argument("--json", action="store_true")
    bench_parser.set_defaults(func=cmd_bench)

    demo = sub.add_parser("demo", help="run a self-checking demo")
    demo.add_argument("name", nargs="?", choices=list(demos.DEMO_NAMES))
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    render.use_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LoadError as exc:
        print(f"{getattr(args, 'file', '?')}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (DiskError, PageError, StoreError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
