"""The demos: each one checks its own numbers before printing them."""

from __future__ import annotations

import os
import tempfile
import unittest

from stele import demos
from stele.disk import FileDisk
from stele.store import Store


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = {name: demos.build(name) for name in demos.DEMO_NAMES}

    def test_names_are_stable(self):
        self.assertEqual(demos.DEMO_NAMES, ("basic", "snapshot", "crash", "batch", "reclaim"))

    def test_every_demo_has_a_title_and_a_body(self):
        for name, demo in self.built.items():
            self.assertEqual(demo.name, name)
            self.assertTrue(demo.title)
            self.assertGreater(len(demo.body.splitlines()), 3)
            self.assertNotIn("FAILED", demo.body)

    def test_all_demos_keeps_the_documented_order(self):
        self.assertEqual([demo.name for demo in demos.all_demos()], list(demos.DEMO_NAMES))

    def test_basic_reports_the_rows_it_actually_holds(self):
        body = self.built["basic"].body
        self.assertIn("499 items", body, "500 keys, one deleted")
        self.assertIn("verify: ok", body)
        self.assertIn("user:0010", body)

    def test_snapshot_shows_the_reader_holding_the_old_version(self):
        body = self.built["snapshot"].body
        self.assertIn("b'version 1'", body, "the snapshot still reads its own version")
        self.assertIn("b'version 4'", body, "the store moved on")
        self.assertIn("pages freed after it closed", body)

    def test_crash_reports_a_clean_matrix(self):
        body = self.built["crash"].body
        self.assertIn("recovered and verified", body)
        self.assertIn("landed on the old state", body)
        self.assertIn("landed on the new state", body)
        self.assertNotIn("FAILED", body)

    def test_batch_shows_the_trade(self):
        body = self.built["batch"].body
        self.assertIn("writes / row", body)
        self.assertIn("2,000 rows", body)

    def test_reclaim_reports_an_empty_tree(self):
        body = self.built["reclaim"].body
        self.assertIn("after trim", body)
        self.assertIn("tree is empty: True", body)

    def test_an_unknown_demo_lists_the_real_ones(self):
        with self.assertRaises(KeyError) as caught:
            demos.build("turbo")
        self.assertIn("basic", str(caught.exception))


class FileDemoTests(unittest.TestCase):
    def test_writes_commits_reopens_and_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "demo.db")
            summary = demos.file_demo(path)
            self.assertIn("100 keys", summary)
            self.assertIn("b'v050'", summary)
            self.assertGreater(os.path.getsize(path), 4096, "at least one page reached the file")

            store = Store(FileDisk(path))
            try:
                self.assertEqual(store.get(b"k050"), b"v050")
                self.assertTrue(store.verify().ok)
                self.assertEqual(store.stats(deep=True).items, 100)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
