"""A copy-on-write B+tree.

Nothing here knows about files, transactions or persistence: the tree asks a
context object for pages, asks it for a fresh page id when it needs one, and
hands back the new root.  The context decides whether "new page" means "write
to disk" or "keep it in memory until commit".

The copy-on-write part is what makes snapshots free: a reader only needs the
root page id it started with, because no page it can reach is ever modified.

Deletion deliberately does not rebalance.  A leaf that loses a key keeps its
remaining keys and an empty leaf is unlinked; pages are never merged with a
sibling.  Merging would shrink the tree, but it also means touching pages the
delete did not need to touch.  The trade is measured in ``stele bench`` rather
than hidden.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterator, List, Optional, Protocol, Sequence, Tuple

from . import page as pg

Key = bytes
Value = bytes


class NodeContext(Protocol):
    """What the tree needs from whoever owns the pages."""

    txid: int

    def read(self, page_id: int) -> pg.Page: ...

    def alloc(self) -> int: ...

    def write(self, page_id: int, data: bytes) -> None: ...

    def free(self, page_id: int) -> None: ...


@dataclass(frozen=True)
class Split:
    """A node that grew too big reports the separator and its two halves."""

    key: Key
    left: int
    right: int


def child_for(page: pg.Page, key: Key) -> int:
    """Which child of a branch covers ``key``."""
    index = bisect_right(page.keys, key)
    if index == 0:
        return page.leftmost
    return int(page.entries[index - 1][1])


def get(ctx: NodeContext, root: Optional[int], key: Key) -> Optional[Value]:
    """Point lookup.  Returns ``None`` when the key is absent."""
    page_id = root
    while page_id is not None:
        page = ctx.read(page_id)
        if page.kind == pg.KIND_LEAF:
            index = bisect_left(page.keys, key)
            if index < len(page.entries) and page.entries[index][0] == key:
                return page.entries[index][1]  # type: ignore[return-value]
            return None
        page_id = child_for(page, key)
    return None


def put(ctx: NodeContext, root: Optional[int], key: Key, value: Value) -> int:
    """Insert or replace one key; returns the new root."""
    if root is None:
        page_id = ctx.alloc()
        ctx.write(page_id, pg.encode_leaf(ctx.txid, [(key, value)]))
        return page_id
    result = _insert(ctx, root, key, value)
    if isinstance(result, Split):
        page_id = ctx.alloc()
        ctx.write(page_id, pg.encode_branch(ctx.txid, result.left, [(result.key, result.right)]))
        return page_id
    return result


def _insert(ctx: NodeContext, page_id: int, key: Key, value: Value):
    page = ctx.read(page_id)
    if page.kind == pg.KIND_LEAF:
        items = list(page.entries)
        index = bisect_left([item[0] for item in items], key)
        if index < len(items) and items[index][0] == key:
            if items[index][1] == value:
                return page_id  # nothing to do; leave the page alone
            items[index] = (key, value)
        else:
            items.insert(index, (key, value))
        if pg.leaf_fits(items):
            fresh = ctx.alloc()
            ctx.write(fresh, pg.encode_leaf(ctx.txid, items))
            ctx.free(page_id)
            return fresh
        return _split_leaf(ctx, items, page_id)

    index = _child_index(page, key)
    child = _child_at(page, index)
    result = _insert(ctx, child, key, value)
    entries = list(page.entries)
    leftmost = page.leftmost
    if isinstance(result, Split):
        if index == 0:
            leftmost = result.left
            entries.insert(0, (result.key, result.right))
        else:
            entries[index - 1] = (entries[index - 1][0], result.left)
            entries.insert(index, (result.key, result.right))
    else:
        if result == child:
            return page_id
        if index == 0:
            leftmost = result
        else:
            entries[index - 1] = (entries[index - 1][0], result)
    if pg.branch_fits(entries):
        fresh = ctx.alloc()
        ctx.write(fresh, pg.encode_branch(ctx.txid, leftmost, entries))
        ctx.free(page_id)
        return fresh
    return _split_branch(ctx, leftmost, entries, page_id)


def _split_leaf(ctx: NodeContext, items: List[Tuple[Key, Value]], old: int):
    left_items, right_items = _halve(items, lambda item: pg.leaf_cell_size(item[0], item[1]))
    left = ctx.alloc()
    ctx.write(left, pg.encode_leaf(ctx.txid, left_items))
    right = ctx.alloc()
    ctx.write(right, pg.encode_leaf(ctx.txid, right_items))
    ctx.free(old)
    return Split(right_items[0][0], left, right)


def _split_branch(ctx: NodeContext, leftmost: int, entries: List[Tuple[Key, int]], old: int):
    middle = len(entries) // 2
    separator, right_leftmost = entries[middle]
    left_entries = entries[:middle]
    right_entries = entries[middle + 1 :]
    left = ctx.alloc()
    ctx.write(left, pg.encode_branch(ctx.txid, leftmost, left_entries))
    right = ctx.alloc()
    ctx.write(right, pg.encode_branch(ctx.txid, right_leftmost, right_entries))
    ctx.free(old)
    return Split(separator, left, right)


def _halve(items: Sequence, weigh) -> Tuple[List, List]:
    """Split a list near the middle by bytes, never producing an empty side."""
    total = sum(weigh(item) for item in items)
    running = 0
    cut = 1
    for index, item in enumerate(items[:-1], start=1):
        running += weigh(item)
        cut = index
        if running * 2 >= total:
            break
    return list(items[:cut]), list(items[cut:])


def delete(ctx: NodeContext, root: Optional[int], key: Key) -> Optional[int]:
    """Remove one key; returns the new root (``None`` if the tree is empty)."""
    if root is None:
        return None
    fresh, empty = _delete(ctx, root, key)
    return None if empty else fresh


def _delete(ctx: NodeContext, page_id: int, key: Key):
    page = ctx.read(page_id)
    if page.kind == pg.KIND_LEAF:
        items = [entry for entry in page.entries if entry[0] != key]
        if len(items) == len(page.entries):
            return page_id, False
        ctx.free(page_id)
        if not items:
            return None, True
        fresh = ctx.alloc()
        ctx.write(fresh, pg.encode_leaf(ctx.txid, items))
        return fresh, False

    index = _child_index(page, key)
    child = _child_at(page, index)
    fresh_child, empty = _delete(ctx, child, key)
    if not empty and fresh_child == child:
        return page_id, False

    entries = list(page.entries)
    leftmost = page.leftmost
    if empty:
        if index == 0:
            if not entries:
                ctx.free(page_id)
                return None, True
            leftmost = entries[0][1]
            entries.pop(0)
        else:
            entries.pop(index - 1)
    else:
        assert fresh_child is not None
        if index == 0:
            leftmost = fresh_child
        else:
            entries[index - 1] = (entries[index - 1][0], fresh_child)

    ctx.free(page_id)
    if not entries:
        return leftmost, False
    fresh = ctx.alloc()
    ctx.write(fresh, pg.encode_branch(ctx.txid, leftmost, entries))
    return fresh, False


def scan(
    ctx: NodeContext,
    root: Optional[int],
    low: Optional[Key] = None,
    high: Optional[Key] = None,
) -> Iterator[Tuple[Key, Value]]:
    """Yield ``(key, value)`` in key order, with ``low <= key < high``."""
    if root is None:
        return
    yield from _scan(ctx, root, low, high)


def _scan(ctx: NodeContext, page_id: int, low: Optional[Key], high: Optional[Key]):
    page = ctx.read(page_id)
    if page.kind == pg.KIND_LEAF:
        for key, value in page.entries:
            if low is not None and key < low:
                continue
            if high is not None and key >= high:
                return
            yield key, value
        return
    bounds = [None] + list(page.keys) + [None]
    children = [page.leftmost] + [int(child) for _, child in page.entries]
    for position, child in enumerate(children):
        floor = bounds[position]
        ceiling = bounds[position + 1]
        if high is not None and floor is not None and floor >= high:
            continue
        if low is not None and ceiling is not None and ceiling <= low:
            continue
        yield from _scan(ctx, child, low, high)


def _child_index(page: pg.Page, key: Key) -> int:
    return bisect_right(page.keys, key)


def _child_at(page: pg.Page, index: int) -> int:
    if index == 0:
        return page.leftmost
    return int(page.entries[index - 1][1])
