"""Where pages live, and how to take the power away from them.

The engine talks to a tiny interface: read a page, write a page, sync.  Three
implementations matter:

``FileDisk``    a real file, with a real ``fsync``.
``MemoryDisk``  a dictionary, used by tests and by the crash matrix.
``CrashDisk``   a fault injector.  It buffers writes and only lets them reach
                the durable image on ``sync()``, so "pull the plug after N
                writes" means exactly that: everything unsynced is gone.

The last one is the point of the project.  A storage engine that has never
been interrupted is a storage engine whose crash story is a guess.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from .page import PAGE_SIZE


class Crash(RuntimeError):
    """Simulated power loss.  Not an engine error: the process simply stops."""


class DiskError(Exception):
    """The device handed back something that cannot be a page."""


class Disk:
    """The interface every device implements."""

    page_size = PAGE_SIZE

    def read_page(self, page_id: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def write_page(self, page_id: int, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def sync(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def page_count(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def truncate(self, pages: int) -> None:
        """Drop every page at or above ``pages``."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def label(self) -> str:
        return type(self).__name__


class MemoryDisk(Disk):
    """An in-memory device.  Pages are written through immediately."""

    def __init__(self, pages: Optional[Dict[int, bytes]] = None):
        self.pages: Dict[int, bytes] = dict(pages or {})
        self.writes = 0
        self.bytes_written = 0
        self.syncs = 0

    def read_page(self, page_id: int) -> bytes:
        data = self.pages.get(page_id)
        if data is None:
            return bytes(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise DiskError(f"page {page_id} holds {len(data)} bytes")
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise DiskError(f"refusing to write {len(data)} bytes as a page")
        if page_id < 0:
            raise DiskError(f"negative page id {page_id}")
        self.pages[page_id] = data
        self.writes += 1
        self.bytes_written += len(data)

    def sync(self) -> None:
        self.syncs += 1

    @property
    def page_count(self) -> int:
        return max(self.pages) + 1 if self.pages else 0

    def truncate(self, pages: int) -> None:
        for page_id in [page_id for page_id in self.pages if page_id >= pages]:
            del self.pages[page_id]

    def copy(self) -> "MemoryDisk":
        return MemoryDisk(self.pages)

    def label(self) -> str:
        return f"memory({self.page_count} pages)"


class FileDisk(Disk):
    """A file that behaves like a page device."""

    def __init__(self, path: str, create: bool = True):
        self.path = path
        mode = "r+b" if os.path.exists(path) else ("w+b" if create else "rb")
        if mode == "rb" and not os.path.exists(path):
            raise DiskError(f"no such file: {path}")
        self.handle = open(path, mode)
        self.handle.seek(0, os.SEEK_END)
        self._count = self.handle.tell() // PAGE_SIZE
        self.writes = 0
        self.bytes_written = 0
        self.syncs = 0

    def read_page(self, page_id: int) -> bytes:
        self.handle.seek(page_id * PAGE_SIZE)
        data = self.handle.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data = data + bytes(PAGE_SIZE - len(data))
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise DiskError(f"refusing to write {len(data)} bytes as a page")
        self.handle.seek(page_id * PAGE_SIZE)
        self.handle.write(data)
        self.writes += 1
        self.bytes_written += len(data)
        if page_id >= self._count:
            self._count = page_id + 1

    def sync(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.syncs += 1

    def truncate(self, pages: int) -> None:
        self.handle.flush()
        self.handle.truncate(pages * PAGE_SIZE)
        os.fsync(self.handle.fileno())
        self._count = pages

    @property
    def page_count(self) -> int:
        return self._count

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()

    def label(self) -> str:
        return f"file({os.path.basename(self.path)}, {self._count} pages)"


class CrashDisk(Disk):
    """A device that loses everything it has not synced.

    ``crash_after`` counts ``write_page`` calls.  When the counter reaches it,
    :class:`Crash` is raised from inside the write, which is as close as a
    single-threaded test can get to someone kicking the power cable.
    """

    def __init__(self, durable: Optional[MemoryDisk] = None, crash_after: Optional[int] = None):
        self.durable = durable or MemoryDisk()
        self.buffer: Dict[int, bytes] = {}
        self.crash_after = crash_after
        self.writes = 0
        self.bytes_written = 0
        self.syncs = 0
        self.crashed = False

    def read_page(self, page_id: int) -> bytes:
        if page_id in self.buffer:
            return self.buffer[page_id]
        return self.durable.read_page(page_id)

    def write_page(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise DiskError(f"refusing to write {len(data)} bytes as a page")
        self.buffer[page_id] = data
        self.writes += 1
        self.bytes_written += len(data)
        if self.crash_after is not None and self.writes >= self.crash_after:
            self.crashed = True
            raise Crash(f"power lost after {self.writes} page writes")

    def sync(self) -> None:
        self.durable.pages.update(self.buffer)
        self.buffer.clear()
        self.syncs += 1

    def truncate(self, pages: int) -> None:
        self.durable.truncate(pages)
        for page_id in [page_id for page_id in self.buffer if page_id >= pages]:
            del self.buffer[page_id]

    @property
    def page_count(self) -> int:
        seen = set(self.durable.pages) | set(self.buffer)
        return max(seen) + 1 if seen else 0

    def crash(self, torn: bool = False, drop_head: bool = False) -> MemoryDisk:
        """Return the image that survives a power loss.

        Plain ``crash()`` discards everything that was not synced, which is the
        strict reading of a power cut.  ``torn=True`` keeps the first 512-byte
        sector of one buffered page, because devices write whole sectors: a
        4 KiB page can arrive with its tail missing.  ``drop_head=True`` loses
        that first sector instead, which is the only tear that can actually
        damage a meta page.
        """
        image = self.durable.copy()
        if (torn or drop_head) and self.buffer:
            page_id = sorted(self.buffer)[0]
            data = self.buffer[page_id]
            sector = 512
            if drop_head:
                image.pages[page_id] = bytes(sector) + data[sector:]
            else:
                image.pages[page_id] = data[:sector] + bytes(len(data) - sector)
        self.buffer.clear()
        return image

    def label(self) -> str:
        return f"crash({len(self.buffer)} buffered, {self.writes} writes)"
