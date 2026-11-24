"""The store: two meta pages, a free list, snapshots, and the commit protocol.

The whole durability story fits in one sentence: **new pages are written and
synced first, then one meta page is replaced and synced**.  A reader is a root
page id, so taking a snapshot costs an integer; a crash costs at most the
transaction that was in flight, because the other meta page still points at a
tree whose pages were never touched.

Two rules keep that true:

1. A page freed by a committed transaction may only be reused once no live
   snapshot can still reach it, *and* once the meta page that described it is
   about to be recycled.  ``_reclaim`` implements the first half; the second
   half holds automatically because the writer recycles the older slot.
2. Nothing is ever written in place.  Every modification produces new pages.

Rule 2 has a corollary that the allocator exploits: a page that was allocated
*inside* the running transaction and dropped again before commit never reached
the disk at all.  No committed version can name it, no snapshot can reach it,
so it goes straight back into the allocation pool (``Writer.free``), not into
the pending list.  Without that, one large transaction would allocate a fresh
page id for every path copy it ever made; with it, the file grows with the
tree, not with the work.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Dict, Iterator, List, Optional, Tuple

from . import btree
from . import page as pg
from .disk import Crash, Disk, DiskError, FileDisk
from .page import PageError

Key = bytes
Value = bytes

CACHE_PAGES = 512

# What a page id handed out by the running transaction is currently doing.
_LIVE = 1  # it is in the tree the writer is building
_REUSABLE = 2  # it was dropped again and never reached the disk


class StoreError(Exception):
    """The store cannot be opened, or is inconsistent."""


@dataclass
class Stats:
    txid: int
    root: int
    page_count: int
    file_pages: int
    free_pages: int
    pending_frees: int
    readers: int
    cache_hits: int = 0
    cache_misses: int = 0
    max_key_bytes: int = 0
    items: int = 0
    depth: int = 0
    leaves: int = 0
    branches: int = 0


@dataclass
class Verification:
    ok: bool
    problem: Optional[str] = None
    page: Optional[int] = None
    depth: int = 0
    leaves: int = 0
    branches: int = 0
    items: int = 0
    reachable: int = 0
    free: int = 0
    file_pages: int = 0
    min_key: Optional[bytes] = None
    max_key: Optional[bytes] = None
    notes: List[str] = field(default_factory=list)


class Store:
    """An ordered key/value store with snapshots, backed by a page device."""

    def __init__(self, disk: Disk, cache_pages: int = CACHE_PAGES):
        self.disk = disk
        self.cache_pages = cache_pages
        self._cache: "OrderedDict[int, pg.Page]" = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0
        self.slot = 0
        self.root = 0
        self.txid = 0
        self.page_count = 2
        self.free: List[int] = []
        self.pending: List[Tuple[int, List[int]]] = []
        self._readers: List[int] = []
        self._writer_open = False
        self._closed = False
        self._load()

    # -- lifecycle ------------------------------------------------------
    @classmethod
    def open(cls, path: str) -> "Store":
        return cls(FileDisk(path))

    @classmethod
    def memory(cls) -> "Store":
        from .disk import MemoryDisk

        return cls(MemoryDisk())

    def close(self) -> None:
        if not self._closed:
            self.disk.close()
            self._closed = True

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- opening --------------------------------------------------------
    def _load(self) -> None:
        candidates: List[pg.Meta] = []
        for slot in (0, 1):
            try:
                meta = pg.decode_meta(self.disk.read_page(slot))
            except (PageError, DiskError):
                continue
            candidates.append(replace(meta, slot=slot))
        if not candidates:
            if self.disk.page_count > 2:
                raise StoreError(
                    f"{self.disk.label()} has {self.disk.page_count} pages but neither "
                    "meta page is readable; refusing to guess"
                )
            self._bootstrap()
            return

        # Newest first; fall back to the older one if the root it names is gone.
        candidates.sort(key=lambda meta: meta.txid, reverse=True)
        problems = []
        for meta in candidates:
            self.slot = meta.slot
            self.root = meta.root
            self.txid = meta.txid
            self.page_count = max(meta.page_count, self.disk.page_count, 2)
            try:
                reachable = self._reachable()
            except (PageError, DiskError) as exc:
                problems.append(f"slot {meta.slot} (txid {meta.txid}): {exc}")
                continue
            self.free = [
                page_id
                for page_id in range(2, self.page_count)
                if page_id not in reachable
            ]
            return
        raise StoreError("no usable meta page: " + "; ".join(problems))

    def _bootstrap(self) -> None:
        """A brand new device: one empty meta page, nothing else."""
        self.disk.write_page(0, pg.encode_meta(0, 2, 0, 0))
        self.disk.sync()
        self.slot, self.root, self.txid, self.page_count = 0, 0, 0, 2
        self.free = []

    def _read(self, page_id: int, fresh: bool = False) -> pg.Page:
        """Read a page through the cache.  Pages are immutable, so a cache hit
        can never be stale -- except when a page id is recycled, and ``_commit``
        drops those entries on the way out."""
        if not fresh:
            cached = self._cache.get(page_id)
            if cached is not None:
                self.cache_hits += 1
                self._cache.move_to_end(page_id)
                return cached
        self.cache_misses += 1  # a fresh read is still a miss
        page = pg.decode(self.disk.read_page(page_id))
        if not fresh:
            self._cache[page_id] = page
            if len(self._cache) > self.cache_pages:
                self._cache.popitem(last=False)
        return page

    def trim(self) -> int:
        """Give the tail of the file back to the filesystem.

        Copy-on-write leaves holes; when the highest reachable page is far
        below the end of the file and everything above it is free, the file can
        simply be shortened.  The meta page is rewritten with the new page
        count, which is itself a normal commit.
        """
        reachable = self._reachable()
        highest = max(reachable) if reachable else 1
        if self.page_count <= highest + 1:
            return self.page_count
        above = set(range(highest + 1, self.page_count))
        if not above.issubset(set(self.free)):
            return self.page_count
        new_count = highest + 1
        self.disk.truncate(new_count)
        self.page_count = new_count
        self.free = [page_id for page_id in self.free if page_id < new_count]
        self._cache = OrderedDict(
            (page_id, page) for page_id, page in self._cache.items() if page_id < new_count
        )
        self.txid += 1
        slot = 1 - self.slot if self.slot in (0, 1) else 0
        self.disk.write_page(slot, pg.encode_meta(slot, self.page_count, self.root, self.txid))
        self.disk.sync()
        self.slot = slot
        return new_count

    def _reachable(self) -> set[int]:
        seen: set[int] = set()
        if not self.root:
            return seen
        stack = [self.root]
        while stack:
            page_id = stack.pop()
            if page_id in seen:
                raise PageError(f"page {page_id} is reachable twice")
            seen.add(page_id)
            page = pg.decode(self.disk.read_page(page_id))
            if page.kind == pg.KIND_BRANCH:
                stack.append(page.leftmost)
                stack.extend(int(child) for _, child in page.entries)
            elif page.kind != pg.KIND_LEAF:
                raise PageError(f"page {page_id} is {pg.kind_name(page.kind)}, not a tree page")
        return seen

    # -- transactions ---------------------------------------------------
    def get(self, key: Key) -> Optional[Value]:
        """Read one key through a short-lived snapshot."""
        snapshot = self.snapshot()
        try:
            return snapshot.get(key)
        finally:
            snapshot.close()

    def scan(
        self, low: Optional[Key] = None, high: Optional[Key] = None
    ) -> Iterator[Tuple[Key, Value]]:
        """Scan through a short-lived snapshot."""
        snapshot = self.snapshot()
        try:
            yield from snapshot.scan(low, high)
        finally:
            snapshot.close()

    def snapshot(self) -> "Snapshot":
        self._check_open()
        snapshot = Snapshot(self, self.root or None, self.txid)
        self._readers.append(self.txid)
        return snapshot

    def write(self) -> "Writer":
        self._check_open()
        if self._writer_open:
            raise StoreError("this store already has a writer")
        self._writer_open = True
        return Writer(self)

    def _release_reader(self, txid: int) -> None:
        try:
            self._readers.remove(txid)
        except ValueError:
            return
        self._reclaim()

    def _reclaim(self) -> None:
        if not self.pending:
            return
        floor = min(self._readers) if self._readers else None
        keep: List[Tuple[int, List[int]]] = []
        for txid, pages in self.pending:
            if floor is None or txid < floor:
                self.free.extend(pages)
            else:
                keep.append((txid, pages))
        self.pending = keep

    def _alloc(self) -> int:
        if self.free:
            return self.free.pop()
        page_id = self.page_count
        self.page_count += 1
        return page_id

    def _commit(self, root: Optional[int], dirty: Dict[int, bytes], freed: List[int]) -> int:
        root_id = root or 0
        # Pages that were created and dropped again inside this transaction
        # never existed on disk and nothing points at them: writing them would
        # only inflate the bill.
        dead = set(freed)
        live = {page_id: data for page_id, data in dirty.items() if page_id not in dead}
        if not live and not freed and root_id == self.root:
            return self.txid
        for page_id, data in live.items():
            self.disk.write_page(page_id, data)
        if live:
            self.disk.sync()
        txid = self.txid + 1
        slot = 1 - self.slot if self.slot in (0, 1) else 0
        self.disk.write_page(slot, pg.encode_meta(slot, self.page_count, root_id, txid))
        self.disk.sync()
        self.slot, self.root, self.txid = slot, root_id, txid
        for page_id in live:
            self._cache.pop(page_id, None)
        if freed:
            self.pending.append((txid, list(freed)))
        self._reclaim()
        return txid

    # -- introspection --------------------------------------------------
    def stats(self, deep: bool = False) -> Stats:
        stats = Stats(
            txid=self.txid,
            root=self.root,
            page_count=self.page_count,
            file_pages=self.disk.page_count,
            free_pages=len(self.free),
            pending_frees=sum(len(pages) for _, pages in self.pending),
            readers=len(self._readers),
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
        )
        if deep and self.root:
            walk = self.verify()
            stats.items = walk.items
            stats.depth = walk.depth
            stats.leaves = walk.leaves
            stats.branches = walk.branches
            stats.max_key_bytes = len(walk.max_key or b"")
        return stats

    def verify(self) -> Verification:
        """Walk the tree from the meta page and check every invariant."""
        report = Verification(ok=True, file_pages=self.disk.page_count)
        if not self.root:
            report.free = self.page_count - 2
            return report
        seen: set[int] = set()
        leaf_depth: Optional[int] = None
        stack: List[Tuple[int, Optional[bytes], Optional[bytes], int]] = [
            (self.root, None, None, 1)
        ]
        while stack:
            page_id, low, high, depth = stack.pop()
            if page_id in seen:
                return self._fail(report, f"page {page_id} is reachable twice", page_id)
            seen.add(page_id)
            try:
                page = self._read(page_id, fresh=True)
            except (PageError, DiskError) as exc:
                return self._fail(report, str(exc), page_id)
            if page.kind == pg.KIND_LEAF:
                if leaf_depth is None:
                    leaf_depth = depth
                elif depth != leaf_depth:
                    return self._fail(
                        report, f"leaf at depth {depth}, earlier leaves at {leaf_depth}", page_id
                    )
                report.leaves += 1
                report.items += len(page.entries)
                previous = None
                for key, _ in page.entries:
                    if low is not None and key < low:
                        return self._fail(report, f"key {key!r} is below its range", page_id)
                    if high is not None and key >= high:
                        return self._fail(report, f"key {key!r} is above its range", page_id)
                    if previous is not None and key <= previous:
                        return self._fail(report, f"keys are not increasing at {key!r}", page_id)
                    previous = key
                if page.entries:
                    first = page.entries[0][0]
                    last = page.entries[-1][0]
                    report.min_key = first if report.min_key is None else min(report.min_key, first)
                    report.max_key = last if report.max_key is None else max(report.max_key, last)
                continue
            if page.kind != pg.KIND_BRANCH:
                return self._fail(report, f"tree page is {pg.kind_name(page.kind)}", page_id)
            report.branches += 1
            if not page.entries:
                return self._fail(report, "branch page has no keys", page_id)
            keys = page.keys
            for left, right in zip(keys, keys[1:]):
                if left >= right:
                    return self._fail(report, f"separator keys are not increasing: {left!r}", page_id)
            bounds = [low] + list(keys) + [high]
            children = [page.leftmost] + [int(child) for _, child in page.entries]
            for index, child in enumerate(children):
                stack.append((child, bounds[index], bounds[index + 1], depth + 1))
        report.depth = leaf_depth or 0
        report.reachable = len(seen)
        report.free = max(0, max(self.page_count, self.disk.page_count) - len(seen) - 2)
        return report

    @staticmethod
    def _fail(report: Verification, problem: str, page_id: int) -> Verification:
        report.ok = False
        report.problem = problem
        report.page = page_id
        return report

    def _check_open(self) -> None:
        if self._closed:
            raise StoreError("this store is closed")


class Snapshot:
    """A consistent read-only view, pinned at one transaction id."""

    def __init__(self, store: Store, root: Optional[int], txid: int):
        self.store = store
        self.root = root
        self.txid = txid
        self._closed = False

    def get(self, key: Key) -> Optional[Value]:
        self._check()
        return btree.get(self, self.root, key)

    def scan(
        self, low: Optional[Key] = None, high: Optional[Key] = None
    ) -> Iterator[Tuple[Key, Value]]:
        self._check()
        return btree.scan(self, self.root, low, high)

    def __len__(self) -> int:
        return sum(1 for _ in self.scan())

    def _check(self) -> None:
        if self._closed:
            raise StoreError("this snapshot is closed")

    # -- NodeContext ----------------------------------------------------
    def read(self, page_id: int) -> pg.Page:
        if self._closed:
            raise StoreError("this snapshot is closed")
        return self.store._read(page_id)

    def alloc(self) -> int:  # pragma: no cover - read-only
        raise StoreError("snapshots are read-only")

    def write(self, page_id: int, data: bytes) -> None:  # pragma: no cover
        raise StoreError("snapshots are read-only")

    def free(self, page_id: int) -> None:  # pragma: no cover
        raise StoreError("snapshots are read-only")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.store._release_reader(self.txid)

    def __enter__(self) -> "Snapshot":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Writer:
    """A read-write transaction.

    Reads see the writer's own uncommitted tree, so ``put`` then ``get`` works
    the way callers expect.  ``commit`` publishes the new root with one meta
    page write; ``rollback`` throws the dirty pages away, which is all an abort
    has to do when nothing was ever written in place.
    """

    def __init__(self, store: Store):
        self.store = store
        self.txid = store.txid + 1
        self.root = store.root or None
        self.dirty: Dict[int, bytes] = {}
        self.freed: List[int] = []
        self.allocated: List[int] = []
        # What became of every page id this transaction handed out: still in
        # the tree, or dropped and waiting to be handed out again.
        self._state: Dict[int, int] = {}
        self._dropped: set[int] = set()
        self.reusable: List[int] = []
        self._done = False

    # -- data -----------------------------------------------------------
    def get(self, key: Key) -> Optional[Value]:
        return btree.get(self, self.root, key)

    def put(self, key: Key, value: Value) -> None:
        self.root = btree.put(self, self.root, key, value)

    def delete(self, key: Key) -> bool:
        if btree.get(self, self.root, key) is None:
            return False
        self.root = btree.delete(self, self.root, key)
        return True

    def scan(
        self, low: Optional[Key] = None, high: Optional[Key] = None
    ) -> Iterator[Tuple[Key, Value]]:
        return btree.scan(self, self.root, low, high)

    def __len__(self) -> int:
        return sum(1 for _ in self.scan())

    # -- NodeContext ----------------------------------------------------
    def read(self, page_id: int) -> pg.Page:
        data = self.dirty.get(page_id)
        if data is not None:
            return pg.decode(data)
        return self.store._read(page_id)

    def alloc(self) -> int:
        if self.reusable:
            page_id = self.reusable.pop()
            self.dirty.pop(page_id, None)  # the earlier life of this id is gone
            self._state[page_id] = _LIVE
            return page_id
        page_id = self.store._alloc()
        self.allocated.append(page_id)
        self._state[page_id] = _LIVE
        return page_id

    def write(self, page_id: int, data: bytes) -> None:
        self.dirty[page_id] = data

    def free(self, page_id: int) -> None:
        state = self._state.get(page_id)
        if state == _LIVE:
            # Born and dropped inside this transaction: it was never durable,
            # so nothing -- no meta page, no snapshot -- can still point at it.
            # It goes back into the pool instead of waiting for the commit.
            self._state[page_id] = _REUSABLE
            self.reusable.append(page_id)
            return
        if state == _REUSABLE:
            raise StoreError(f"page {page_id} was freed twice in one transaction")
        if page_id in self._dropped:
            raise StoreError(f"page {page_id} was freed twice in one transaction")
        # A page from an earlier commit.  It stays out of the free list until
        # this transaction lands, because an older snapshot may still read it.
        self._dropped.add(page_id)
        self.freed.append(page_id)

    # -- completion -----------------------------------------------------
    def commit(self) -> int:
        if self._done:
            raise StoreError("this transaction is already finished")
        # Pages that were allocated here, dropped here, and never handed out
        # again are still ours to return: nothing points at them and they must
        # not be written, so they join the freed list for this commit.
        for page_id in self.reusable:
            self.dirty.pop(page_id, None)
            self.freed.append(page_id)
        self.reusable.clear()
        txid = self.store._commit(self.root, self.dirty, self.freed)
        self._state.clear()
        self._dropped.clear()
        self._done = True
        self.store._writer_open = False
        return txid

    def rollback(self) -> None:
        if self._done:
            raise StoreError("this transaction is already finished")
        for page_id in self.allocated:
            self.store.free.append(page_id)
        self.dirty.clear()
        self.freed.clear()
        self.reusable.clear()
        self._state.clear()
        self._dropped.clear()
        self._done = True
        self.store._writer_open = False

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._done:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
