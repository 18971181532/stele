"""Measurements, not claims.

Every number in the README comes from here.  The benchmarks run against an
in-memory device because they are about the engine -- page copies, tree shape,
write amplification -- not about this machine's disk.  The file device is
measured separately, because there the interesting number is the fsync count.
"""

from __future__ import annotations

import gc
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .disk import Crash, CrashDisk, FileDisk, MemoryDisk
from .render import number, seconds, size
from .store import Store


@dataclass
class Measurement:
    name: str
    rows: int
    seconds: float
    pages_written: int
    bytes_written: int
    logical_bytes: int
    page_count: int
    note: str = ""

    @property
    def ops_per_second(self) -> float:
        return self.rows / self.seconds if self.seconds > 0 else float("inf")

    @property
    def bytes_per_row(self) -> float:
        return self.bytes_written / self.rows if self.rows else 0.0

    @property
    def amplification(self) -> float:
        return self.bytes_written / self.logical_bytes if self.logical_bytes else 0.0

    def row(self) -> List[str]:
        amplification = f"{self.amplification:.1f}x" if self.logical_bytes else "-"
        return [
            self.name,
            f"{self.rows:,}",
            f"{number(self.ops_per_second)}/s",
            f"{self.bytes_per_row:.0f} B",
            amplification,
            seconds(self.seconds),
            f"{self.pages_written:,}",
        ]


@dataclass
class CrashReport:
    points: int
    old_state: int = 0
    new_state: int = 0
    verified: int = 0
    torn: int = 0
    seconds: float = 0.0
    failures: List[str] = field(default_factory=list)

    @property
    def runs(self) -> int:
        return self.points * 2

    @property
    def ok(self) -> bool:
        return not self.failures and self.old_state + self.new_state == self.runs


def _keys(count: int, seed: Optional[int] = None) -> List[bytes]:
    if seed is None:
        return [b"key-%06d" % index for index in range(count)]
    rng = random.Random(seed)
    return [b"key-%06d" % value for value in rng.sample(range(count * 10), count)]


def _measure(
    name: str,
    rows: int,
    logical: int,
    work: Callable[[Store], None],
    prepare: Optional[Callable[[Store], None]] = None,
    note: str = "",
) -> Measurement:
    disk = MemoryDisk()
    store = Store(disk)
    if prepare is not None:
        prepare(store)
    before_writes, before_bytes = disk.writes, disk.bytes_written
    elapsed = _timed(work, store)
    return Measurement(
        name=name,
        rows=rows,
        seconds=elapsed,
        pages_written=disk.writes - before_writes,
        bytes_written=disk.bytes_written - before_bytes,
        logical_bytes=logical,
        page_count=store.stats().page_count,
        note=note,
    )


def _timed(work: Callable[[Store], None], store: Store) -> float:
    """Run ``work`` without the collector in the room.

    The garbage left by the previous workload would otherwise be collected
    *inside* this one's timed region, and the report would read like a property
    of the engine rather than of the process that ran it.
    """
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    started = time.perf_counter()
    try:
        work(store)
    finally:
        elapsed = time.perf_counter() - started
        if enabled:
            gc.enable()
    return elapsed


def sequential_insert(
    rows: int = 10_000, value_size: int = 32, batch: Optional[int] = None
) -> Measurement:
    keys = _keys(rows)
    value = b"v" * value_size
    step = batch or rows

    def work(store: Store) -> None:
        for start in range(0, rows, step):
            with store.write() as writer:
                for key in keys[start : start + step]:
                    writer.put(key, value)

    name = f"insert-seq/b{batch}" if batch else "insert-sequential"
    return _measure(name, rows, rows * (len(keys[0]) + value_size), work)


def random_insert(
    rows: int = 10_000, value_size: int = 32, seed: int = 11, batch: Optional[int] = None
) -> Measurement:
    keys = _keys(rows, seed=seed)
    value = b"v" * value_size
    step = batch or rows

    def work(store: Store) -> None:
        for start in range(0, rows, step):
            with store.write() as writer:
                for key in keys[start : start + step]:
                    writer.put(key, value)

    name = f"insert-rnd/b{batch}" if batch else "insert-random"
    return _measure(name, rows, rows * (len(keys[0]) + value_size), work)


def point_reads(rows: int = 10_000, lookups: int = 20_000, seed: int = 5) -> Measurement:
    keys = _keys(rows)
    rng = random.Random(seed)
    probes = [keys[rng.randrange(rows)] for _ in range(lookups)]

    def prepare(store: Store) -> None:
        with store.write() as writer:
            for key in keys:
                writer.put(key, b"value")

    def work(store: Store) -> None:
        for key in probes:
            store.get(key)

    return _measure("point-reads", lookups, 0, work, prepare)


def updates(rows: int = 5_000, rounds: int = 2) -> Measurement:
    keys = _keys(rows)

    def prepare(store: Store) -> None:
        with store.write() as writer:
            for key in keys:
                writer.put(key, b"first")

    def work(store: Store) -> None:
        for round_index in range(rounds):
            with store.write() as writer:
                for key in keys:
                    writer.put(key, b"round-%d" % round_index)

    return _measure(
        "update-in-place",
        rows * rounds,
        rows * rounds * (len(keys[0]) + 8),
        work,
        prepare,
        note="each update copies the path to the root",
    )


def range_scan(rows: int = 20_000, span: int = 1000) -> Measurement:
    keys = _keys(rows)

    def prepare(store: Store) -> None:
        with store.write() as writer:
            for key in keys:
                writer.put(key, b"v")

    def work(store: Store) -> None:
        seen = sum(1 for _ in store.scan(b"key-001000", b"key-%06d" % (1000 + span)))
        assert seen == span, seen

    return _measure(f"range-scan-{span}", span, 0, work, prepare)


def file_commits(rows: int = 500) -> Measurement:
    """One transaction per row, on a real file, fsync included."""
    directory = tempfile.TemporaryDirectory()
    try:
        path = os.path.join(directory.name, "bench.db")
        disk = FileDisk(path)
        store = Store(disk)
        before_writes, before_bytes = disk.writes, disk.bytes_written

        def work(active: Store) -> None:
            for index in range(rows):
                with active.write() as writer:
                    writer.put(b"key-%06d" % index, b"value")

        elapsed = _timed(work, store)
        pages = disk.writes - before_writes
        written = disk.bytes_written - before_bytes
        store.close()
        return Measurement(
            name="file-commit-each",
            rows=rows,
            seconds=elapsed,
            pages_written=pages,
            bytes_written=written,
            logical_bytes=rows * 17,
            page_count=pages,
            note="two fsync per commit",
        )
    finally:
        directory.cleanup()


SUITES: Dict[str, List[Callable[[], Measurement]]] = {
    "quick": [
        lambda: sequential_insert(2_000),
        lambda: random_insert(2_000),
        lambda: point_reads(2_000, 4_000),
        lambda: updates(500, 2),
        lambda: range_scan(5_000),
    ],
    "core": [
        sequential_insert,
        random_insert,
        point_reads,
        updates,
        range_scan,
    ],
    "file": [file_commits],
}

SUITE_HELP = {
    "quick": "small versions of every workload",
    "core": "the numbers quoted in the README",
    "file": "one transaction per row, with real fsync",
}


def suites() -> List[str]:
    return list(SUITES)


def run(suite: str) -> List[Measurement]:
    try:
        builders = SUITES[suite]
    except KeyError:
        raise KeyError(f"unknown suite {suite!r}; choose from {', '.join(SUITES)}") from None
    return [build() for build in builders]


def totals(measurements: Sequence[Measurement]) -> Dict[str, float]:
    return {
        "rows": sum(item.rows for item in measurements),
        "seconds": sum(item.seconds for item in measurements),
        "bytes": sum(item.bytes_written for item in measurements),
        "pages": sum(item.pages_written for item in measurements),
    }


def batch_effect(
    rows: int = 4_000, sizes: Sequence[int] = (1, 10, 100, 1000, 4000)
) -> List[Tuple[int, float, int]]:
    """How commit size changes the bill: (batch, pages written per row, file pages)."""
    out: List[Tuple[int, float, int]] = []
    for batch in sizes:
        measurement = sequential_insert(rows, batch=batch)
        out.append((batch, measurement.pages_written / rows, measurement.page_count))
    return out


def crash_matrix(rows: int = 300, extra: int = 40) -> CrashReport:
    """Crash at every write of a commit, with and without a torn sector."""
    disk = MemoryDisk()
    store = Store(disk)
    with store.write() as writer:
        for index in range(rows):
            writer.put(b"key-%06d" % index, b"old")
    store.close()
    base = MemoryDisk(dict(disk.pages))

    probe = CrashDisk(MemoryDisk(dict(base.pages)))
    with Store(probe).write() as writer:
        for index in range(rows, rows + extra):
            writer.put(b"key-%06d" % index, b"new")
    points = probe.writes

    report = CrashReport(points=points)
    started = time.perf_counter()
    for point in range(1, points + 1):
        for torn in (False, True):
            device = CrashDisk(MemoryDisk(dict(base.pages)), crash_after=point)
            try:
                with Store(device).write() as writer:
                    for index in range(rows, rows + extra):
                        writer.put(b"key-%06d" % index, b"new")
            except Crash:
                pass
            else:  # pragma: no cover - the commit should be longer than every crash point
                report.failures.append(f"point {point}: the commit finished before the crash")
                continue
            recovered = Store(device.crash(torn=torn))
            check = recovered.verify()
            if not check.ok:
                report.failures.append(f"point {point} torn={torn}: {check.problem}")
                continue
            report.verified += 1
            if torn:
                report.torn += 1
            if recovered.txid == 1:
                report.old_state += 1
            elif recovered.txid == 2:
                report.new_state += 1
            else:  # pragma: no cover - would be a protocol bug
                report.failures.append(f"point {point}: unexpected txid {recovered.txid}")
    report.seconds = time.perf_counter() - started
    return report


def describe(report: CrashReport) -> List[str]:
    lines = [
        f"commit needs {report.points} page writes",
        f"{report.verified} of {report.runs} crash runs recovered and verified",
        f"  landed on the old state: {report.old_state}",
        f"  landed on the new state: {report.new_state}",
        f"  of which torn sectors:   {report.torn}",
        f"matrix took {seconds(report.seconds)}",
    ]
    for failure in report.failures[:5]:
        lines.append(f"  FAILED {failure}")
    return lines


def reclaim(rows: int = 5_000) -> List[str]:
    """Delete every row and give the file back."""
    disk = MemoryDisk()
    store = Store(disk)
    for start in range(0, rows, 100):
        with store.write() as writer:
            for index in range(start, min(rows, start + 100)):
                writer.put(b"key-%06d" % index, b"value-" + b"x" * 20)
    built = store.stats().page_count
    for start in range(0, rows, 100):
        with store.write() as writer:
            for index in range(start, min(rows, start + 100)):
                writer.delete(b"key-%06d" % index)
    emptied = store.stats().page_count
    trimmed = store.trim()
    report = store.verify()
    return [
        f"{rows:,} rows built with batches of 100: {built:,} pages ({size(built * 4096)})",
        f"after deleting every row:          {emptied:,} pages ({size(emptied * 4096)})",
        f"after trim:                        {trimmed:,} pages ({size(trimmed * 4096)})",
        f"tree is empty: {report.items == 0}, verified: {report.ok}",
    ]


def footprint(rows: int = 20_000, sizes: Sequence[int] = (1, 100, 20_000)) -> List[str]:
    """What the same data costs at three commit sizes.

    Copy-on-write never writes in place, but a page that is created *and*
    dropped inside one transaction was never durable, so the allocator hands it
    out again immediately.  The file therefore tracks the tree, not the work:
    the same rows land on the same footprint whether they take one transaction
    or twenty thousand.  What changes is the number of page writes.
    """
    logical = rows * (10 + 32)
    lines = [f"{rows:,} rows, {size(int(logical))} of logical data"]
    for batch in sizes:
        item = sequential_insert(rows, batch=batch)
        lines.append(
            f"  batch {batch:>6,}: {item.page_count:>5,} pages "
            f"({size(item.page_count * 4096):>9}), {item.pages_written:>7,} page writes"
        )
    return lines
