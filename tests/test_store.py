"""Store behaviour: commits, snapshots, reuse, verification, reopening."""

from __future__ import annotations

import os
import tempfile
import unittest

from stele import page as pg
from stele.disk import FileDisk, MemoryDisk
from stele.store import Snapshot, Store, StoreError


def fill(store: Store, count: int, prefix: bytes = b"k", value: bytes = b"v") -> None:
    with store.write() as writer:
        for index in range(count):
            writer.put(prefix + b"%05d" % index, value + b"-%d" % index)


class BasicTests(unittest.TestCase):
    def test_a_fresh_store_is_empty(self):
        store = Store(MemoryDisk())
        stats = store.stats()
        self.assertEqual((stats.txid, stats.root, stats.items), (0, 0, 0))
        self.assertIsNone(store.get(b"anything"))
        self.assertTrue(store.verify().ok)

    def test_put_get_delete(self):
        store = Store(MemoryDisk())
        with store.write() as writer:
            writer.put(b"a", b"1")
            writer.put(b"b", b"2")
            self.assertEqual(writer.get(b"a"), b"1", "a writer reads its own writes")
        self.assertEqual(store.get(b"a"), b"1")
        with store.write() as writer:
            self.assertTrue(writer.delete(b"a"))
            self.assertFalse(writer.delete(b"a"))
        self.assertIsNone(store.get(b"a"))
        self.assertEqual(dict(store.scan()), {b"b": b"2"})

    def test_scan_is_ordered_and_bounded(self):
        store = Store(MemoryDisk())
        fill(store, 100, prefix=b"row-")
        keys = [key for key, _ in store.scan(b"row-00010", b"row-00015")]
        self.assertEqual(keys, [b"row-%05d" % i for i in range(10, 15)])

    def test_commit_advances_one_transaction(self):
        store = Store(MemoryDisk())
        with store.write() as writer:
            writer.put(b"k", b"v")
        self.assertEqual(store.txid, 1)
        with store.write():
            pass
        self.assertEqual(store.txid, 1, "an empty commit should not write a meta page")

    def test_writer_is_exclusive(self):
        store = Store(MemoryDisk())
        writer = store.write()
        with self.assertRaises(StoreError):
            store.write()
        writer.rollback()

    def test_rollback_discards_everything(self):
        store = Store(MemoryDisk())
        fill(store, 20)
        before = store.stats()
        writer = store.write()
        writer.put(b"k00000", b"changed")
        writer.delete(b"k00001")
        writer.rollback()
        after = store.stats()
        self.assertEqual(after.txid, before.txid)
        self.assertEqual(store.get(b"k00000"), b"v-0")
        self.assertEqual(after.page_count, before.page_count, "rollback must return the pages it took")

    def test_context_manager_rolls_back_on_error(self):
        store = Store(MemoryDisk())
        with self.assertRaises(RuntimeError):
            with store.write() as writer:
                writer.put(b"k", b"v")
                raise RuntimeError("boom")
        self.assertIsNone(store.get(b"k"))
        self.assertEqual(store.txid, 0)

    def test_closed_store_refuses_work(self):
        store = Store(MemoryDisk())
        store.close()
        with self.assertRaises(StoreError):
            store.snapshot()


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_a_point_in_time(self):
        store = Store(MemoryDisk())
        fill(store, 50)
        snapshot = store.snapshot()
        with store.write() as writer:
            for index in range(50):
                writer.put(b"k%05d" % index, b"new")
        self.assertEqual(snapshot.get(b"k00000"), b"v-0")
        self.assertEqual(store.get(b"k00000"), b"new")
        self.assertEqual(len(snapshot), 50)
        snapshot.close()

    def test_snapshot_sees_nothing_after_it_started(self):
        store = Store(MemoryDisk())
        snapshot = store.snapshot()
        with store.write() as writer:
            writer.put(b"later", b"1")
        self.assertIsNone(snapshot.get(b"later"))
        snapshot.close()

    def test_two_snapshots_can_disagree(self):
        store = Store(MemoryDisk())
        with store.write() as writer:
            writer.put(b"k", b"one")
        first = store.snapshot()
        with store.write() as writer:
            writer.put(b"k", b"two")
        second = store.snapshot()
        self.assertEqual(first.get(b"k"), b"one")
        self.assertEqual(second.get(b"k"), b"two")
        first.close()
        second.close()

    def test_pages_wait_for_the_reader(self):
        store = Store(MemoryDisk())
        fill(store, 400)
        snapshot = store.snapshot()
        with store.write() as writer:
            for index in range(400):
                writer.put(b"k%05d" % index, b"second")
        held = store.stats()
        self.assertGreater(held.pending_frees, 0, "pages must not be freed while a reader holds them")
        snapshot.close()
        released = store.stats()
        self.assertEqual(released.pending_frees, 0)
        self.assertGreater(released.free_pages, held.free_pages)

    def test_snapshot_is_read_only(self):
        store = Store(MemoryDisk())
        snapshot: Snapshot = store.snapshot()
        with self.assertRaises(StoreError):
            snapshot.alloc()
        with self.assertRaises(StoreError):
            snapshot.write(0, b"x")
        snapshot.close()
        with self.assertRaises(StoreError):
            snapshot.get(b"k")


class ReuseTests(unittest.TestCase):
    def test_rewrites_reach_a_steady_state(self):
        store = Store(MemoryDisk())
        fill(store, 2000)

        def rewrite(tag: bytes) -> int:
            with store.write() as writer:
                for index in range(2000):
                    writer.put(b"k%05d" % index, tag)
            return store.stats().page_count

        first = rewrite(b"one")
        second = rewrite(b"two")
        third = rewrite(b"three")
        self.assertEqual(first, second, "the free list should absorb a rewrite of the same size")
        self.assertEqual(second, third)
        # One transaction's worth of path copies has to fit somewhere: the file
        # plateaus at roughly twice the live tree, and stays there.
        self.assertLess(third, 8000)
        self.assertTrue(store.verify().ok)

    def test_free_pages_exist_after_a_rewrite(self):
        store = Store(MemoryDisk())
        fill(store, 1000)
        with store.write() as writer:
            for index in range(0, 1000, 2):
                writer.delete(b"k%05d" % index)
        self.assertGreater(store.stats().free_pages, 0)


class InsideTransactionTests(unittest.TestCase):
    """Pages that never reached the disk are the allocator's own business."""

    def test_one_big_transaction_does_not_allocate_a_page_per_write(self):
        store = Store(MemoryDisk())
        with store.write() as writer:
            for index in range(1000):
                writer.put(b"k%05d" % index, b"v")
        # A thousand puts copy the root-to-leaf path a thousand times.  Only
        # the pages the final tree needs are kept: a few leaves and a branch.
        self.assertLess(store.stats().page_count, 60)

    def test_those_pages_are_not_written_either(self):
        disk = MemoryDisk()
        store = Store(disk)
        with store.write() as writer:
            for index in range(1000):
                writer.put(b"k%05d" % index, b"v")
        self.assertLess(disk.writes, 60, "superseded pages inside one transaction are never durable")

    def test_reuse_does_not_resurrect_old_bytes(self):
        store = Store(MemoryDisk())
        with store.write() as writer:
            for index in range(300):
                writer.put(b"k%03d" % index, b"first")
            for index in range(0, 300, 3):
                writer.delete(b"k%03d" % index)
            for index in range(300):
                writer.put(b"k%03d" % index, b"second")
        self.assertEqual(len(store.snapshot()), 300)
        for index in range(300):
            self.assertEqual(store.get(b"k%03d" % index), b"second")
        report = store.verify()
        self.assertTrue(report.ok, report.problem)

    def test_a_page_freed_twice_is_refused(self):
        store = Store(MemoryDisk())
        writer = store.write()
        page = writer.alloc()
        writer.free(page)
        with self.assertRaises(StoreError):
            writer.free(page)
        writer.rollback()

    def test_a_page_may_be_reused_after_it_was_freed(self):
        store = Store(MemoryDisk())
        writer = store.write()
        first = writer.alloc()
        writer.free(first)
        second = writer.alloc()
        self.assertEqual(first, second, "the pool hands back the page it was just given")
        writer.free(second)  # legal: this is a second allocation of that id
        writer.rollback()


class ReopenTests(unittest.TestCase):
    def test_reopening_a_file(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "store.db")
        store = Store.open(path)
        fill(store, 500)
        store.close()
        again = Store.open(path)
        self.assertEqual(again.txid, 1)
        self.assertEqual(again.get(b"k00000"), b"v-0")
        self.assertEqual(len(again.snapshot()), 500)
        self.assertTrue(again.verify().ok)
        again.close()

    def test_reopening_a_memory_image(self):
        disk = MemoryDisk()
        store = Store(disk)
        fill(store, 100)
        image = MemoryDisk(dict(disk.pages))
        store.close()
        clone = Store(image)
        self.assertEqual(len(clone.snapshot()), 100)
        clone.close()

    def test_writes_after_reopen_continue_the_sequence(self):
        disk = MemoryDisk()
        store = Store(disk)
        fill(store, 10)
        image = MemoryDisk(dict(disk.pages))
        store.close()
        again = Store(image)
        with again.write() as writer:
            writer.put(b"after", b"1")
        self.assertEqual(again.txid, 2)
        self.assertEqual(again.get(b"after"), b"1")


class VerifyTests(unittest.TestCase):
    def test_empty_store(self):
        report = Store(MemoryDisk()).verify()
        self.assertTrue(report.ok)
        self.assertEqual(report.items, 0)

    def test_populated_store(self):
        store = Store(MemoryDisk())
        fill(store, 3000)
        report = store.verify()
        self.assertTrue(report.ok, report.problem)
        self.assertEqual(report.items, 3000)
        self.assertGreater(report.leaves, 5)
        self.assertGreater(report.depth, 1)
        self.assertEqual(report.min_key, b"k00000")
        self.assertEqual(report.max_key, b"k02999")

    def test_a_flipped_byte_is_located(self):
        disk = MemoryDisk()
        store = Store(disk)
        fill(store, 200)
        root = store.root
        data = bytearray(disk.pages[root])
        data[pg.PAGE_SIZE - 5] ^= 0xFF
        disk.pages[root] = bytes(data)
        report = store.verify()
        self.assertFalse(report.ok)
        self.assertEqual(report.page, root)
        self.assertIn("checksum", report.problem or "")

    def test_a_broken_meta_falls_back_to_the_older_one(self):
        disk = MemoryDisk()
        store = Store(disk)
        fill(store, 100)
        with store.write() as writer:  # a second commit, so both meta slots are in use
            writer.put(b"k00000", b"different")
        newest = store.slot
        damaged = bytearray(disk.pages[newest])
        damaged[40] ^= 0xFF
        disk.pages[newest] = bytes(damaged)
        recovered = Store(disk)
        self.assertEqual(recovered.txid, 1, "the older meta page should win")
        self.assertEqual(len(recovered.snapshot()), 100)

    def test_no_usable_meta_is_an_error(self):
        disk = MemoryDisk()
        store = Store(disk)
        fill(store, 50)
        for slot in (0, 1):
            if slot in disk.pages:
                disk.pages[slot] = bytes(pg.PAGE_SIZE)
        with self.assertRaises(StoreError):
            Store(disk)


if __name__ == "__main__":
    unittest.main()
