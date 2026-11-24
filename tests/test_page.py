"""Page format: round trips, limits, and every way a page can be wrong."""

from __future__ import annotations

import struct
import unittest

from stele import page as pg


class LeafTests(unittest.TestCase):
    def test_round_trip(self):
        items = [(b"alpha", b"1"), (b"beta", b"2"), (b"gamma", b"3")]
        decoded = pg.decode(pg.encode_leaf(17, items))
        self.assertEqual(decoded.kind, pg.KIND_LEAF)
        self.assertEqual(decoded.txid, 17)
        self.assertEqual(list(decoded.entries), items)

    def test_empty_leaf(self):
        decoded = pg.decode(pg.encode_leaf(3, []))
        self.assertEqual(decoded.entries, ())
        self.assertEqual(decoded.txid, 3)

    def test_binary_keys_survive(self):
        items = [(bytes(range(0, 32)), bytes(range(255, 200, -1)))]
        decoded = pg.decode(pg.encode_leaf(1, items))
        self.assertEqual(decoded.entries[0], items[0])

    def test_value_limit(self):
        with self.assertRaises(pg.PageError):
            pg.encode_leaf(1, [(b"k", bytes(pg.MAX_VALUE + 1))])

    def test_key_limit(self):
        with self.assertRaises(pg.PageError):
            pg.encode_leaf(1, [(bytes(pg.MAX_KEY + 1), b"v")])

    def test_empty_key_rejected(self):
        with self.assertRaises(pg.PageError):
            pg.encode_leaf(1, [(b"", b"v")])

    def test_too_many_cells(self):
        items = [(b"k%04d" % i, b"v" * 100) for i in range(200)]
        with self.assertRaises(pg.PageError):
            pg.encode_leaf(1, items)
        self.assertFalse(pg.leaf_fits(items))

    def test_fits_boundary(self):
        items = [(b"k%04d" % i, b"v" * 100) for i in range(30)]
        self.assertTrue(pg.leaf_fits(items))
        self.assertLessEqual(len(pg.encode_leaf(1, items)), pg.PAGE_SIZE)


class BranchTests(unittest.TestCase):
    def test_round_trip(self):
        entries = [(b"b", 4), (b"d", 6)]
        decoded = pg.decode(pg.encode_branch(9, 3, entries))
        self.assertEqual(decoded.kind, pg.KIND_BRANCH)
        self.assertEqual(decoded.leftmost, 3)
        self.assertEqual(list(decoded.entries), entries)

    def test_capacity(self):
        entries = [(b"k%04d" % i, i) for i in range(600)]
        self.assertFalse(pg.branch_fits(entries))
        self.assertTrue(pg.branch_fits(entries[:100]))


class MetaTests(unittest.TestCase):
    def test_round_trip(self):
        meta = pg.decode_meta(pg.encode_meta(1, 900, 42, 7))
        self.assertEqual((meta.page_count, meta.root, meta.txid), (900, 42, 7))

    def test_wrong_kind(self):
        data = bytearray(pg.encode_meta(0, 10, 1, 1))
        data[0] = pg.KIND_LEAF
        with self.assertRaises(pg.PageError):
            pg.decode_meta(bytes(data))

    def test_bad_magic(self):
        data = bytearray(pg.encode_meta(0, 10, 1, 1))
        data[pg.HEADER + pg.SLOT] = ord("X")
        with self.assertRaises(pg.PageError):
            pg.decode_meta(bytes(data))


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        self.data = pg.encode_leaf(5, [(b"a", b"1"), (b"b", b"2"), (b"c", b"3")])

    def test_every_single_bit_flip_is_caught(self):
        for offset in (0, 3, 8, 24, 40, 100, pg.PAGE_SIZE - 1):
            damaged = bytearray(self.data)
            damaged[offset] ^= 0b0000_0001
            with self.assertRaises(pg.PageError, msg=f"offset {offset}"):
                pg.decode(bytes(damaged))

    def test_truncated_page(self):
        with self.assertRaises(pg.PageError):
            pg.decode(self.data[:-1])

    def test_bad_slot_pointer(self):
        damaged = bytearray(self.data)
        struct.pack_into("<H", damaged, pg.HEADER, pg.PAGE_SIZE - 2)
        struct.pack_into("<H", damaged, pg.HEADER + 2, 8)
        struct.pack_into("<I", damaged, 20, pg.checksum(bytes(damaged)))
        with self.assertRaises(pg.PageError):
            pg.decode(bytes(damaged))

    def test_slot_count_must_match_the_header(self):
        damaged = bytearray(self.data)
        struct.pack_into("<H", damaged, 2, 4)  # claims four slots, has three
        struct.pack_into("<I", damaged, 20, pg.checksum(bytes(damaged)))
        with self.assertRaises(pg.PageError):
            pg.decode(bytes(damaged))

    def test_free_page(self):
        decoded = pg.decode(pg.encode_free())
        self.assertEqual(decoded.kind, pg.KIND_FREE)
        self.assertEqual(decoded.entries, ())

    def test_unknown_kind(self):
        damaged = bytearray(self.data)
        damaged[0] = 9
        struct.pack_into("<I", damaged, 20, pg.checksum(bytes(damaged)))
        with self.assertRaises(pg.PageError):
            pg.decode(bytes(damaged))


class LayoutTests(unittest.TestCase):
    def test_checksum_ignores_its_own_field(self):
        data = bytearray(pg.encode_leaf(1, [(b"k", b"v")]))
        before = pg.checksum(bytes(data))
        data[20:24] = b"\xff\xff\xff\xff"
        self.assertEqual(pg.checksum(bytes(data)), before)

    def test_kind_probe(self):
        self.assertEqual(pg.page_kind(pg.encode_leaf(1, [])), pg.KIND_LEAF)
        self.assertEqual(pg.page_kind(pg.encode_meta(0, 2, 0, 0)), pg.KIND_META)
        self.assertEqual(pg.page_kind(b""), -1)

    def test_names(self):
        self.assertEqual(pg.kind_name(pg.KIND_LEAF), "leaf")
        self.assertEqual(pg.kind_name(77), "kind-77")

    def test_keys_are_built_once_when_the_page_is_decoded(self):
        page = pg.decode(pg.encode_leaf(1, [(b"b", b"2"), (b"a", b"1")]))
        self.assertEqual(page.keys, (b"b", b"a"))
        self.assertIs(page.keys, page.keys, "bisecting must not rebuild the key tuple")

    def test_keys_follow_the_entries(self):
        page = pg.Page(pg.KIND_BRANCH, 3, 7, ((b"x", 8), (b"y", 9)))
        self.assertEqual(page.keys, (b"x", b"y"))
        self.assertEqual(len(page), 2)


if __name__ == "__main__":
    unittest.main()
