"""The on-disk page format.

Every page is exactly ``PAGE_SIZE`` bytes: a 24-byte header, a slot array that
grows up from the header, and cells that grow down from the end of the page.

    +--------------------------------------------------------------+
    | kind | flags | nslots | start | end | leftmost | txid | crc32 |
    +--------------------------------------------------------------+
    | slot 0 | slot 1 | ...                     (offset, length)   |
    +--------------------------------------------------------------+
    |                          free space                          |
    +--------------------------------------------------------------+
    | ... | cell 1 | cell 0                       (grow downwards) |
    +--------------------------------------------------------------+

Cells are serialised from scratch every time a page is written.  That would be
wasteful for a page cache that mutates pages in place, but this engine is
copy-on-write: a page is written once and never touched again, so a rebuild
per write costs nothing extra.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Tuple, Union

PAGE_SIZE = 4096

KIND_FREE = 0
KIND_META = 1
KIND_BRANCH = 2
KIND_LEAF = 3

MAGIC = b"STELE001"
FORMAT_VERSION = 1

HEADER = 24
SLOT = 4
MAX_KEY = 512
MAX_VALUE = 3500

_META = struct.Struct("<8sIIIII")  # magic, page_size, page_count, root, txid, version
_HEAD = struct.Struct("<BBHIIII")  # kind, flags, nslots, start, end, leftmost, txid
_CELL = struct.Struct("<HH")  # key length, value length
_BRANCH_CELL = struct.Struct("<HI")  # key length, child page
_SLOT = struct.Struct("<HH")  # offset, length
_U32 = struct.Struct("<I")

Entry = Tuple[bytes, Union[bytes, int]]


class PageError(Exception):
    """A page could not be decoded: bad checksum, bad layout, or bad kind."""


@dataclass(frozen=True)
class Page:
    """A decoded page.  ``entries`` holds ``(key, value)`` for leaves and
    ``(key, child)`` for branches.

    ``keys`` is derived once, at decode time, because every lookup and every
    split bisects it: rebuilding it per access turns a point read into a walk
    over the whole page.
    """

    kind: int
    txid: int = 0
    leftmost: int = 0
    entries: Tuple[Entry, ...] = ()
    keys: Tuple[bytes, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", tuple(key for key, _ in self.entries))

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class Meta:
    """One of the two meta pages.  ``slot`` is the page id it lives in."""

    slot: int
    page_size: int
    page_count: int
    root: int
    txid: int
    version: int


def checksum(page: bytes) -> int:
    """CRC32 over the page with the checksum field itself zeroed out."""
    crc = zlib.crc32(page[:20])
    crc = zlib.crc32(b"\x00\x00\x00\x00", crc)
    return zlib.crc32(page[24:], crc) & 0xFFFFFFFF


def leaf_cell(key: bytes, value: bytes) -> bytes:
    _check_key(key)
    if len(value) > MAX_VALUE:
        raise PageError(f"value is {len(value)} bytes; the limit is {MAX_VALUE}")
    return _CELL.pack(len(key), len(value)) + key + value


def branch_cell(key: bytes, child: int) -> bytes:
    _check_key(key)
    return _BRANCH_CELL.pack(len(key), child) + key


def _check_key(key: bytes) -> None:
    if not key:
        raise PageError("keys must not be empty")
    if len(key) > MAX_KEY:
        raise PageError(f"key is {len(key)} bytes; the limit is {MAX_KEY}")


def _encode(kind: int, txid: int, leftmost: int, cells: Iterable[bytes]) -> bytes:
    cells = list(cells)
    start = HEADER + SLOT * len(cells)
    buffer = bytearray(PAGE_SIZE)
    _HEAD.pack_into(buffer, 0, kind, 0, len(cells), start, PAGE_SIZE, leftmost, txid)
    end = PAGE_SIZE
    for index, cell in enumerate(cells):
        end -= len(cell)
        if end < start:
            raise PageError(
                f"{kind_name(kind)} page needs more than {PAGE_SIZE} bytes; "
                "split the node or shorten the value"
            )
        buffer[end : end + len(cell)] = cell
        _SLOT.pack_into(buffer, HEADER + SLOT * index, end, len(cell))
    _U32.pack_into(buffer, 8, end)
    _U32.pack_into(buffer, 20, _crc32_with_zero_crc(buffer))
    return bytes(buffer)


def _crc32_with_zero_crc(buffer: bytearray) -> int:
    saved = bytes(buffer[20:24])
    buffer[20:24] = b"\x00\x00\x00\x00"
    value = zlib.crc32(buffer) & 0xFFFFFFFF
    buffer[20:24] = saved
    return value


def encode_leaf(txid: int, items: Iterable[Tuple[bytes, bytes]]) -> bytes:
    return _encode(KIND_LEAF, txid, 0, (leaf_cell(k, v) for k, v in items))


def encode_branch(txid: int, leftmost: int, entries: Iterable[Tuple[bytes, int]]) -> bytes:
    return _encode(KIND_BRANCH, txid, leftmost, (branch_cell(k, c) for k, c in entries))


def encode_free(txid: int = 0) -> bytes:
    return _encode(KIND_FREE, txid, 0, ())


def encode_meta(slot: int, page_count: int, root: int, txid: int) -> bytes:
    buffer = bytearray(PAGE_SIZE)
    _HEAD.pack_into(buffer, 0, KIND_META, 0, 1, HEADER + SLOT, PAGE_SIZE, 0, txid)
    _SLOT.pack_into(buffer, HEADER, HEADER + SLOT, PAGE_SIZE - HEADER - SLOT)
    _META.pack_into(
        buffer, HEADER + SLOT, MAGIC, PAGE_SIZE, page_count, root, txid, FORMAT_VERSION
    )
    _U32.pack_into(buffer, 20, _crc32_with_zero_crc(buffer))
    return bytes(buffer)


def decode(data: bytes) -> Page:
    """Decode and validate a page.  Raises :class:`PageError` on any doubt."""
    if len(data) != PAGE_SIZE:
        raise PageError(f"page is {len(data)} bytes, expected {PAGE_SIZE}")
    kind = data[0]
    if kind == KIND_FREE:
        return Page(KIND_FREE, 0, 0, ())
    nslots, start, end, leftmost, txid = struct.unpack_from("<HIIII", data, 2)
    stored = struct.unpack_from("<I", data, 20)[0]
    actual = checksum(data)
    if actual != stored:
        raise PageError(f"checksum mismatch: header says {stored:#010x}, page is {actual:#010x}")
    if start != HEADER + SLOT * nslots:
        raise PageError(f"slot array ends at {start}, header claims {HEADER + SLOT * nslots}")
    if not HEADER <= start <= end <= PAGE_SIZE:
        raise PageError(f"cell area {start}..{end} is outside the page")
    if kind == KIND_LEAF:
        items = []
        for index in range(nslots):
            cell = _cell(data, index, end)
            if len(cell) < _CELL.size:
                raise PageError(f"leaf cell {index} is truncated")
            klen, vlen = _CELL.unpack_from(cell, 0)
            if _CELL.size + klen + vlen != len(cell):
                raise PageError(f"leaf cell {index} declares {klen}+{vlen} bytes, has {len(cell)}")
            key = cell[_CELL.size : _CELL.size + klen]
            items.append((key, cell[_CELL.size + klen :]))
        return Page(kind, txid, leftmost, tuple(items))
    if kind == KIND_BRANCH:
        entries = []
        for index in range(nslots):
            cell = _cell(data, index, end)
            if len(cell) < _BRANCH_CELL.size:
                raise PageError(f"branch cell {index} is truncated")
            klen, child = _BRANCH_CELL.unpack_from(cell, 0)
            if _BRANCH_CELL.size + klen != len(cell):
                raise PageError(f"branch cell {index} declares {klen} key bytes, has {len(cell)}")
            entries.append((cell[_BRANCH_CELL.size :], child))
        return Page(kind, txid, leftmost, tuple(entries))
    if kind == KIND_META:
        return Page(kind, txid, 0, ())
    raise PageError(f"unknown page kind {kind}")


def _cell(data: bytes, index: int, top: int) -> bytes:
    """Fetch one cell.  Cells live in ``[top, PAGE_SIZE)``."""
    offset, length = _SLOT.unpack_from(data, HEADER + SLOT * index)
    if offset < top or offset + length > PAGE_SIZE or length == 0:
        raise PageError(f"slot {index} points at {offset}..{offset + length}, outside the cell area")
    return data[offset : offset + length]


def decode_meta(data: bytes) -> Meta:
    """Decode a meta page.  The caller supplies which slot it read."""
    if len(data) != PAGE_SIZE:
        raise PageError("meta page has the wrong size")
    if data[0] != KIND_META:
        raise PageError(f"page kind is {data[0]}, not meta")
    if checksum(data) != struct.unpack_from("<I", data, 20)[0]:
        raise PageError("meta checksum mismatch")
    magic, page_size, page_count, root, txid, version = _META.unpack_from(
        data, HEADER + SLOT
    )
    if magic != MAGIC:
        raise PageError(f"bad magic {magic!r}")
    if page_size != PAGE_SIZE:
        raise PageError(f"page size is {page_size}, this build writes {PAGE_SIZE}")
    if version != FORMAT_VERSION:
        raise PageError(f"format version {version}, this build understands {FORMAT_VERSION}")
    return Meta(0, page_size, page_count, root, txid, version)


def leaf_cell_size(key: bytes, value: bytes) -> int:
    return _CELL.size + len(key) + len(value)


def branch_cell_size(key: bytes) -> int:
    return _BRANCH_CELL.size + len(key)


def leaf_fits(items: Iterable[Tuple[bytes, bytes]]) -> bool:
    """Would these leaf entries fit in one page?"""
    items = list(items)
    need = HEADER + SLOT * len(items) + sum(leaf_cell_size(k, v) for k, v in items)
    return need <= PAGE_SIZE


def branch_fits(entries: Iterable[Tuple[bytes, int]]) -> bool:
    """Would these branch entries (plus the leftmost child) fit?"""
    entries = list(entries)
    need = HEADER + SLOT * len(entries) + sum(branch_cell_size(k) for k, _ in entries)
    return need <= PAGE_SIZE


def kind_name(kind: int) -> str:
    return {
        KIND_FREE: "free",
        KIND_META: "meta",
        KIND_BRANCH: "branch",
        KIND_LEAF: "leaf",
    }.get(kind, f"kind-{kind}")


def page_kind(data: bytes) -> int:
    """Cheap kind probe that does not validate anything."""
    return data[0] if data else -1
