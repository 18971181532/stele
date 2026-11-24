"""Stele: a copy-on-write key/value store that survives having the power cut.

The public surface is small on purpose:

    Store.open(path)        open or create a store
    store.write()           a read-write transaction
    store.snapshot()        a consistent read-only view
    store.verify()          check every page and tree invariant
    store.trim()            give the tail of the file back

Everything else -- pages, the B+tree, the crash injector -- is an
implementation detail you are welcome to read.
"""

from .disk import Crash, CrashDisk, Disk, DiskError, FileDisk, MemoryDisk
from .page import PAGE_SIZE, PageError
from .store import Snapshot, Store, StoreError, Verification

__version__ = "1.0.0"

__all__ = [
    "Crash",
    "CrashDisk",
    "Disk",
    "DiskError",
    "FileDisk",
    "MemoryDisk",
    "PAGE_SIZE",
    "PageError",
    "Snapshot",
    "Store",
    "StoreError",
    "Verification",
    "__version__",
]
