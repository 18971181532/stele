"""Devices: a real file, a dictionary, and one that pulls the plug."""

from __future__ import annotations

import os
import tempfile
import unittest

from stele import page as pg
from stele.disk import Crash, CrashDisk, DiskError, FileDisk, MemoryDisk


def a_page(marker: int) -> bytes:
    return pg.encode_leaf(1, [(b"k%d" % marker, b"v%d" % marker)])


class MemoryDiskTests(unittest.TestCase):
    def test_round_trip(self):
        disk = MemoryDisk()
        disk.write_page(5, a_page(1))
        self.assertEqual(disk.read_page(5), a_page(1))
        self.assertEqual(disk.page_count, 6)
        self.assertEqual(disk.writes, 1)
        self.assertEqual(disk.bytes_written, pg.PAGE_SIZE)

    def test_unwritten_pages_read_as_zeroes(self):
        disk = MemoryDisk()
        self.assertEqual(disk.read_page(0), bytes(pg.PAGE_SIZE))
        self.assertEqual(disk.page_count, 0)

    def test_rejects_bad_sizes(self):
        disk = MemoryDisk()
        with self.assertRaises(DiskError):
            disk.write_page(0, b"short")
        with self.assertRaises(DiskError):
            disk.write_page(-1, a_page(1))

    def test_copy_is_independent(self):
        disk = MemoryDisk()
        disk.write_page(0, a_page(1))
        clone = disk.copy()
        clone.write_page(1, a_page(2))
        self.assertEqual(disk.page_count, 1)
        self.assertEqual(clone.page_count, 2)


class FileDiskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "store.db")

    def test_writes_survive_a_reopen(self):
        disk = FileDisk(self.path)
        disk.write_page(0, a_page(3))
        disk.write_page(1, a_page(4))
        disk.sync()
        disk.close()
        again = FileDisk(self.path)
        self.assertEqual(again.page_count, 2)
        self.assertEqual(again.read_page(0), a_page(3))
        self.assertEqual(again.read_page(1), a_page(4))
        again.close()

    def test_growing_file(self):
        disk = FileDisk(self.path)
        disk.write_page(9, a_page(9))
        disk.sync()
        self.assertEqual(disk.page_count, 10)
        self.assertEqual(os.path.getsize(self.path), 10 * pg.PAGE_SIZE)
        disk.close()

    def test_missing_file_is_refused(self):
        with self.assertRaises(DiskError):
            FileDisk(os.path.join(self.directory.name, "nope.db"), create=False)


class CrashDiskTests(unittest.TestCase):
    def test_nothing_is_durable_before_sync(self):
        disk = CrashDisk()
        disk.write_page(0, a_page(1))
        self.assertEqual(disk.durable.page_count, 0)
        disk.sync()
        self.assertEqual(disk.durable.page_count, 1)

    def test_crash_drops_the_buffer(self):
        disk = CrashDisk(crash_after=2)
        disk.write_page(0, a_page(1))
        with self.assertRaises(Crash):
            disk.write_page(1, a_page(2))
        image = disk.crash()
        self.assertEqual(image.pages, {}, "unsynced writes must not survive")

    def test_crash_keeps_synced_pages(self):
        disk = CrashDisk(crash_after=2)
        disk.write_page(0, a_page(1))
        disk.sync()
        with self.assertRaises(Crash):
            disk.write_page(1, a_page(2))
        image = disk.crash()
        self.assertEqual(image.read_page(0), a_page(1))
        self.assertEqual(image.page_count, 1)

    def test_torn_write_leaves_half_a_page(self):
        disk = CrashDisk(crash_after=1)
        with self.assertRaises(Crash):
            disk.write_page(3, a_page(7))
        image = disk.crash(torn=True)
        page = image.read_page(3)
        self.assertEqual(len(page), pg.PAGE_SIZE)
        self.assertEqual(page[: pg.PAGE_SIZE // 2], a_page(7)[: pg.PAGE_SIZE // 2])
        self.assertEqual(page[pg.PAGE_SIZE // 2 :], bytes(pg.PAGE_SIZE // 2))

    def test_reads_see_buffered_writes(self):
        disk = CrashDisk()
        disk.write_page(0, a_page(1))
        self.assertEqual(disk.read_page(0), a_page(1), "the writer must read its own writes")

    def test_page_count_includes_the_buffer(self):
        disk = CrashDisk()
        disk.write_page(4, a_page(1))
        self.assertEqual(disk.page_count, 5)


if __name__ == "__main__":
    unittest.main()
