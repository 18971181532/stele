"""Power loss, at every possible instant.

The claim under test is narrow and total: a transaction is either entirely
there or entirely absent, and the file is always openable afterwards.  The
matrix below crashes the process at *every* page write of a commit, with and
without a torn last write, and checks both halves of that sentence.
"""

from __future__ import annotations

import random
import unittest
from typing import Dict, List, Tuple

from stele.disk import Crash, CrashDisk, MemoryDisk
from stele.store import Store


def snapshot_image(keys: int = 200, value: bytes = b"old") -> MemoryDisk:
    disk = MemoryDisk()
    store = Store(disk)
    with store.write() as writer:
        for index in range(keys):
            writer.put(b"k%05d" % index, value)
    store.close()
    return MemoryDisk(dict(disk.pages))


def apply(disk: CrashDisk, first: int, count: int, value: bytes) -> None:
    store = Store(disk)
    with store.write() as writer:
        for index in range(first, first + count):
            writer.put(b"k%05d" % index, value)


def writes_needed(image: MemoryDisk, first: int = 200, count: int = 60) -> int:
    probe = CrashDisk(MemoryDisk(dict(image.pages)))
    apply(probe, first, count, b"new")
    return probe.writes


class AtomicityTests(unittest.TestCase):
    def test_every_crash_point_lands_on_one_of_two_states(self):
        image = snapshot_image()
        total = writes_needed(image)
        self.assertGreater(total, 1)
        seen = set()
        for point in range(1, total + 2):
            disk = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=point)
            crashed = False
            try:
                apply(disk, 200, 60, b"new")
            except Crash:
                crashed = True
            if crashed:
                store = Store(disk.crash())
            else:
                store = Store(disk.durable)
            report = store.verify()
            self.assertTrue(report.ok, f"point {point}: {report.problem}")
            seen.add(store.txid)
            if store.txid == 1:
                self.assertIsNone(store.get(b"k00200"), f"point {point} published a lost commit")
                self.assertEqual(store.get(b"k00000"), b"old")
            else:
                self.assertEqual(store.txid, 2)
                self.assertEqual(store.get(b"k00200"), b"new", f"point {point} lost a commit")
                self.assertEqual(store.get(b"k00000"), b"old")
        self.assertEqual(seen, {1, 2}, "the matrix should produce both outcomes")

    def test_every_crash_point_with_a_torn_last_write(self):
        image = snapshot_image()
        total = writes_needed(image)
        for point in range(1, total + 1):
            disk = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=point)
            try:
                apply(disk, 200, 60, b"new")
            except Crash:
                pass
            else:
                continue
            store = Store(disk.crash(torn=True))
            report = store.verify()
            self.assertTrue(report.ok, f"torn at {point}: {report.problem}")
            self.assertIn(store.txid, (1, 2))
            # A torn sector can still leave a complete meta page behind, so
            # the transaction may legitimately survive.  What may not happen is
            # a half-applied transaction.
            expected = b"new" if store.txid == 2 else None
            self.assertEqual(store.get(b"k00200"), expected, f"torn at {point}")

    def test_a_meta_page_that_lost_its_first_sector_falls_back(self):
        image = snapshot_image()
        total = writes_needed(image)
        disk = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=total)
        with self.assertRaises(Crash):
            apply(disk, 200, 60, b"new")
        store = Store(disk.crash(drop_head=True))
        self.assertEqual(store.txid, 1, "a meta page without its header must not be believed")
        self.assertEqual(store.get(b"k00000"), b"old")
        self.assertTrue(store.verify().ok)

    def test_an_empty_transaction_is_never_half_visible(self):
        image = snapshot_image()
        disk = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=1)
        with self.assertRaises(Crash):
            apply(disk, 200, 1, b"new")
        store = Store(disk.crash())
        self.assertEqual(store.txid, 1)
        self.assertIsNone(store.get(b"k00200"))


class DeeperTreeTests(unittest.TestCase):
    def test_crash_in_the_middle_of_a_deep_rewrite(self):
        disk = MemoryDisk()
        store = Store(disk)
        with store.write() as writer:
            for index in range(3000):
                writer.put(b"k%05d" % index, b"first")
        image = MemoryDisk(dict(disk.pages))
        store.close()

        for point in (5, 10, 25, 40):
            crashed = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=point)
            try:
                with Store(crashed).write() as writer:
                    for index in range(0, 3000, 7):
                        writer.put(b"k%05d" % index, b"second")
            except Crash:
                pass
            else:
                continue
            recovered = Store(crashed.crash(torn=True))
            report = recovered.verify()
            self.assertTrue(report.ok, report.problem)
            self.assertEqual(report.items, 3000)
            self.assertEqual(recovered.txid, 1)
            self.assertEqual(recovered.get(b"k00000"), b"first")


class RecoveryTests(unittest.TestCase):
    def test_repeated_crashes_never_corrupt_or_leak(self):
        rng = random.Random(99)
        disk = MemoryDisk()
        store = Store(disk)
        with store.write() as writer:
            for index in range(100):
                writer.put(b"k%03d" % index, b"0")
        disk = MemoryDisk(dict(disk.pages))
        model: Dict[bytes, bytes] = {b"k%03d" % i: b"0" for i in range(100)}

        committed = 0
        for round_index in range(12):
            point = rng.randrange(1, 6)
            crashy = CrashDisk(MemoryDisk(dict(disk.pages)), crash_after=point)
            batch = {b"k%03d" % i: b"%d" % round_index for i in range(0, 100, 3)}
            try:
                with Store(crashy).write() as writer:
                    for key, value in batch.items():
                        writer.put(key, value)
            except Crash:
                disk = crashy.crash()
            else:
                disk = crashy.durable
                model.update(batch)
                committed += 1
            store = Store(disk)
            report = store.verify()
            self.assertTrue(report.ok, f"round {round_index}: {report.problem}")
            for key, value in model.items():
                self.assertEqual(store.get(key), value, f"round {round_index} lost {key!r}")
            self.assertLess(
                store.stats().page_count,
                4000,
                "recovery should rebuild the free list, not grow the file every round",
            )
        self.assertGreater(committed, 0)

    def test_recovery_returns_orphan_pages_to_the_free_list(self):
        # Crash during the very last write of a commit -- the meta page.  Every
        # data page of that transaction is already durable, and none of them can
        # be reached from the old root: they are orphans, and recovery must put
        # them on the free list instead of leaving a hole in the file.
        image = snapshot_image(400)
        total = writes_needed(image, 400, 100)
        disk = CrashDisk(MemoryDisk(dict(image.pages)), crash_after=total)
        with self.assertRaises(Crash):
            apply(disk, 400, 100, b"new")
        recovered = disk.crash()
        store = Store(recovered)
        report = store.verify()
        self.assertTrue(report.ok)
        self.assertGreater(report.free, 1, "pages written by the aborted commit are garbage")

        before = store.stats().page_count
        with store.write() as writer:
            writer.put(b"k09999", b"x")
        self.assertEqual(store.stats().page_count, before, "the recovered free list should be reused")
        self.assertTrue(store.verify().ok)


if __name__ == "__main__":
    unittest.main()
