"""The B+tree against a dictionary: same answers, or the test fails."""

from __future__ import annotations

import random
import unittest
from typing import Dict, List, Optional, Tuple

from stele import btree
from stele import page as pg


class DictContext:
    """A NodeContext that keeps pages in a dict and never reuses a page id."""

    def __init__(self) -> None:
        self.pages: Dict[int, bytes] = {}
        self.next_page = 2
        self.txid = 1
        self.allocated: List[int] = []
        self.freed: List[int] = []

    def read(self, page_id: int) -> pg.Page:
        return pg.decode(self.pages[page_id])

    def alloc(self) -> int:
        page_id = self.next_page
        self.next_page += 1
        self.allocated.append(page_id)
        return page_id

    def write(self, page_id: int, data: bytes) -> None:
        self.pages[page_id] = data

    def free(self, page_id: int) -> None:
        self.freed.append(page_id)

    def walk(self, root: Optional[int]) -> Tuple[int, int, int]:
        """Return (leaves, items, depth) and assert the shape on the way."""
        if root is None:
            return 0, 0, 0

        def visit(page_id: int, depth: int):
            page = self.read(page_id)
            if page.kind == pg.KIND_LEAF:
                keys = page.keys
                assert list(keys) == sorted(keys), "leaf keys are not sorted"
                assert len(set(keys)) == len(keys), "leaf has duplicate keys"
                return 1, len(page.entries), depth
            assert page.kind == pg.KIND_BRANCH
            assert page.entries, "branch without separators"
            keys = page.keys
            assert list(keys) == sorted(keys), "separators are not sorted"
            leaves = items = 0
            deepest = 0
            children = [page.leftmost] + [int(child) for _, child in page.entries]
            depths = set()
            for child in children:
                l, i, d = visit(child, depth + 1)
                leaves += l
                items += i
                deepest = max(deepest, d)
                depths.add(d)
            assert len(depths) == 1, f"children sit at different depths: {sorted(depths)}"
            return leaves, items, deepest

        return visit(root, 1)


class BtreeTests(unittest.TestCase):
    def setUp(self):
        self.ctx = DictContext()
        self.root: Optional[int] = None
        self.model: Dict[bytes, bytes] = {}

    def put(self, key: bytes, value: bytes):
        self.root = btree.put(self.ctx, self.root, key, value)
        self.model[key] = value

    def delete(self, key: bytes):
        self.root = btree.delete(self.ctx, self.root, key)
        self.model.pop(key, None)

    def assert_matches(self):
        self.assertEqual(dict(btree.scan(self.ctx, self.root)), self.model)
        self.assertEqual(len(self.model), sum(1 for _ in btree.scan(self.ctx, self.root)))

    def test_empty_tree(self):
        self.assertIsNone(btree.get(self.ctx, None, b"x"))
        self.assertIsNone(btree.delete(self.ctx, None, b"x"))
        self.assertEqual(list(btree.scan(self.ctx, None)), [])

    def test_single_key(self):
        self.put(b"k", b"v")
        self.assertEqual(btree.get(self.ctx, self.root, b"k"), b"v")
        self.assertIsNone(btree.get(self.ctx, self.root, b"j"))
        self.delete(b"k")
        self.assertIsNone(self.root)
        self.assertEqual(self.model, {})

    def test_writing_the_same_value_again_costs_nothing(self):
        self.put(b"k", b"one")
        self.put(b"k", b"two")
        self.assertEqual(btree.get(self.ctx, self.root, b"k"), b"two")
        settled = self.root
        freed = len(self.ctx.freed)
        self.put(b"k", b"two")
        self.assertEqual(self.root, settled, "an unchanged value should not copy the path")
        self.assertEqual(len(self.ctx.freed), freed)
        self.assert_matches()

    def test_sequential_keys_split(self):
        for index in range(5000):
            self.put(b"k%06d" % index, b"value-%d" % index)
        leaves, items, depth = self.ctx.walk(self.root)
        self.assertEqual(items, 5000)
        self.assertGreater(leaves, 10)
        self.assertGreaterEqual(depth, 2)
        self.assert_matches()

    def test_random_workload_matches_dict(self):
        rng = random.Random(2026)
        for _ in range(4000):
            key = b"k%05d" % rng.randrange(1500)
            if rng.random() < 0.35:
                self.delete(key)
            else:
                self.put(key, b"v%d" % rng.randrange(1000))
        self.assert_matches()
        leaves, items, _ = self.ctx.walk(self.root)
        self.assertEqual(items, len(self.model))

    def test_delete_everything(self):
        for index in range(800):
            self.put(b"k%04d" % index, b"v")
        for index in range(800):
            self.delete(b"k%04d" % index)
        self.assertIsNone(self.root, "the last delete should leave no root")
        self.assertEqual(list(btree.scan(self.ctx, self.root)), [])

    def test_delete_half(self):
        for index in range(1000):
            self.put(b"k%04d" % index, b"v")
        for index in range(0, 1000, 2):
            self.delete(b"k%04d" % index)
        self.assertEqual(len(self.model), 500)
        self.assert_matches()
        self.ctx.walk(self.root)

    def test_deleting_a_missing_key_touches_nothing(self):
        self.put(b"a", b"1")
        before = self.root
        self.assertEqual(btree.delete(self.ctx, before, b"zzz"), before)
        self.assertEqual(self.ctx.freed, [])

    def test_range_scan(self):
        for index in range(100):
            self.put(b"k%03d" % index, b"v")
        got = [k for k, _ in btree.scan(self.ctx, self.root, b"k020", b"k030")]
        self.assertEqual(got, [b"k%03d" % i for i in range(20, 30)])
        # high bound is exclusive, low bound is inclusive
        got = [k for k, _ in btree.scan(self.ctx, self.root, b"k020", b"k020")]
        self.assertEqual(got, [])
        got = [k for k, _ in btree.scan(self.ctx, self.root, None, b"k003")]
        self.assertEqual(got, [b"k000", b"k001", b"k002"])

    def test_keys_of_very_different_lengths(self):
        rng = random.Random(5)
        for index in range(400):
            key = bytes([65 + index % 26]) * rng.randrange(1, 200)
            self.put(key, b"x" * rng.randrange(1, 300))
        self.assert_matches()
        self.ctx.walk(self.root)

    def test_large_values_split_early(self):
        for index in range(40):
            self.put(b"k%02d" % index, b"v" * 1200)
        leaves, items, depth = self.ctx.walk(self.root)
        self.assertEqual(items, 40)
        self.assertGreater(leaves, 3)


if __name__ == "__main__":
    unittest.main()
