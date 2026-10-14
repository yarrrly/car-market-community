"""Process locks owned by this package, independent of the operator's infrastructure."""

import json
import os
from pathlib import Path

import portalocker


class FileLock:
    def __init__(self, path: Path, *, shared=False, blocking=True):
        self.path = Path(path)
        self.shared, self.blocking, self.file = shared, blocking, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")
        flags = portalocker.LOCK_SH if self.shared else portalocker.LOCK_EX
        if not self.blocking:
            flags |= portalocker.LOCK_NB
        try:
            portalocker.lock(self.file, flags)
            if not self.shared:
                self.refresh_metadata()
        except portalocker.exceptions.LockException as error:
            self.file.close()
            self.file = None
            raise BlockingIOError("Database writer is already running") from error
        except BaseException:
            self.file.close()
            self.file = None
            raise
        return self

    def refresh_metadata(self):
        if self.file is None or self.shared:
            raise RuntimeError("An exclusive lock is required")
        self.file.seek(0)
        self.file.truncate()
        self.file.write(json.dumps({"pid": os.getpid()}))
        self.file.flush()

    def release(self):
        if self.file is not None:
            try:
                portalocker.unlock(self.file)
            finally:
                self.file.close()
                self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()
