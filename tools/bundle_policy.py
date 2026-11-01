"""Small, testable filters for the Fedora-specific PyInstaller collection."""
from __future__ import annotations

from pathlib import PurePosixPath


def _normalized(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def keep_data_member(name: str) -> bool:
    """Keep app assets and Python modules; use host GTK/GI registries and themes."""
    path = _normalized(name)
    return not (
        path.startswith(("share/", "gi_typelibs/", "gio_modules/"))
    )


def keep_binary_member(name: str) -> bool:
    """Keep CPython and Python extension modules, not Fedora's desktop libraries."""
    path = _normalized(name)
    parts = PurePosixPath(path).parts
    base = parts[-1] if parts else ""
    if path.startswith("gio_modules/") or (len(parts) == 1 and base.startswith(".") and base.endswith(".hmac")):
        return False
    # The executable carries CPython itself. GTK, GLib, OpenSSL, X11, Cairo,
    # and their other OS-owned shared libraries resolve from the target distro.
    if len(parts) == 1 and base.startswith("lib") and ".so" in base:
        return base.startswith("libpython")
    return True


def filter_toc(toc: list[tuple], kind: str) -> list[tuple]:
    """Filter PyInstaller TOC entries by destination name without deleting files."""
    if kind == "binaries":
        predicate = keep_binary_member
    elif kind == "datas":
        predicate = keep_data_member
    else:
        raise ValueError("kind must be 'binaries' or 'datas'")
    return [entry for entry in toc if entry and predicate(str(entry[0]))]
