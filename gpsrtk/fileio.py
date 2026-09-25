"""Writing files so that no failure leaves half of one behind.

A project, plan or site file is the only record of what it holds - the
solved vertical terms, the typed-in readings, the benchmark - and one
written in place and interrupted (a crash, a full disk, a sync client
grabbing it) is left truncated. So it is written to a temporary file beside
the target and swapped in with `os.replace`, which is atomic on one volume:
the old file or the new one, never a mixture.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# OneDrive locks a file for a moment while it syncs it; one retry after
# this long gets past that.
RETRY_AFTER_S = 0.5


def write_text_atomic(path: str | Path, text: str,
                      encoding: str = "utf-8") -> Path:
    """Write `text` to `path` all at once, or not at all."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        try:
            os.replace(tmp, path)
        except PermissionError:
            time.sleep(RETRY_AFTER_S)
            os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
