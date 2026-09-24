"""Browsing the local disk from the browser.

A browser will not tell a web page where a file lives - an upload arrives as
bytes with a name, and nothing else. That does not fit this tool: a project
records its exports BY PATH, relative to the project file when they sit under
it, so a project folder can be moved or copied, and reopening it reads the
exports from where they are. Uploading copies would break every one of those
properties.

The server runs on this machine, bound to localhost, so it can list folders
itself and the browser can show them as a file dialog. That is all this is:
the listing a native dialog would have shown.
"""

from __future__ import annotations

import os
from pathlib import Path


def roots() -> list[str]:
    """Top-level places to start from: drive letters on Windows, / elsewhere."""
    listdrives = getattr(os, "listdrives", None)       # Windows, Python 3.12+
    if listdrives is not None:
        try:
            return list(listdrives())
        except OSError:
            pass
    return [os.path.abspath(os.sep)]


def listing(directory: str | Path, extensions: list[str] | None = None) -> dict:
    """Folders and matching files in one directory.

    Hidden entries are left out, as a native dialog would. Folders come first,
    then files, each sorted case-insensitively. Anything that cannot be read
    - a permission error, a dangling link - is skipped rather than failing
    the whole listing.
    """
    path = Path(directory).expanduser()
    if not path.is_dir():
        raise ValueError(f"{path} is not a folder")
    path = path.resolve()
    wanted = {e.lower() for e in (extensions or []) if e}

    folders, files = [], []
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from exc

    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
            info = entry.stat()
        except OSError:
            continue
        if is_dir:
            folders.append({"name": entry.name, "path": entry.path,
                            "dir": True})
        elif not wanted or Path(entry.name).suffix.lower() in wanted:
            files.append({"name": entry.name, "path": entry.path, "dir": False,
                          "size": info.st_size, "mtime": info.st_mtime})

    key = lambda e: e["name"].lower()                         # noqa: E731
    parent = path.parent if path.parent != path else None
    return {
        "dir": str(path),
        "parent": str(parent) if parent is not None else None,
        "sep": os.sep,
        "entries": sorted(folders, key=key) + sorted(files, key=key),
        "roots": roots(),
    }
