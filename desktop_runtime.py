"""Private loopback desktop runtime shared by the native shell and tests."""
from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from server import OwnedServer, create_server


def private_directory(path: Path) -> Path:
    """Create a per-user app directory and restrict permissions where supported."""
    path = Path(path).expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("The selected application data path must be a real directory.")
    return path


def selected_directory(path: Path, *, restrict_existing: bool) -> Path:
    path = Path(path).expanduser().resolve()
    existed = path.exists()
    if restrict_existing or not existed:
        return private_directory(path)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("The selected application data path must be a real directory.")
    return path


def default_data_directory(*, platform: str | None = None, environ: dict[str, str] | None = None,
                           home: Path | None = None) -> Path:
    platform = platform or sys.platform
    env = os.environ if environ is None else environ
    home = Path(home or Path.home())
    if platform.startswith("win"):
        root = env.get("LOCALAPPDATA")
        return Path(root) / "RKO" if root else home / "AppData" / "Local" / "RKO"
    root = env.get("XDG_DATA_HOME")
    return Path(root) / "RKO" if root else home / ".local" / "share" / "RKO"


class InstanceLock:
    """OS advisory lock scoped to one persistent data directory."""
    def __init__(self, data_dir: Path, *, restrict_existing: bool = True):
        self.data_dir = selected_directory(data_dir, restrict_existing=restrict_existing)
        self.path = self.data_dir / "instance.lock"
        if self.path.is_symlink():
            raise RuntimeError("The instance-lock path cannot be a symbolic link.")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.fd = os.open(self.path, flags, 0o600)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._locked = False

    def acquire(self) -> None:
        if self._locked:
            return
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b"\0")
                    os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.close()
            raise RuntimeError("RKO is already open for this data directory.") from exc
        self._locked = True

    def close(self) -> None:
        if self.fd is None:
            return
        if self._locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(self.fd)
        self.fd = None
        self._locked = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.close()


def _desktop_build_manifest(asset_root: Path) -> dict[str, Any] | None:
    if not getattr(sys, "frozen", False):
        return None
    import json
    from desktop_build_info import SOURCE_SHA256
    manifest_path = Path(asset_root) / "desktop_build_manifest.json"
    if manifest_path.is_symlink():
        raise RuntimeError("Frozen build manifest is not a regular file.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_sha256") != SOURCE_SHA256 or not SOURCE_SHA256:
        raise RuntimeError("Frozen build manifest and embedded source fingerprint do not match.")
    if manifest.get("schema") != "rko-desktop-build-v1":
        raise RuntimeError("Frozen build manifest schema is unsupported.")
    policy_path = Path(asset_root) / "forward_policy_v5_addendum.json"
    if policy_path.is_symlink() or not policy_path.is_file():
        raise RuntimeError("Frozen build policy file is missing or unsafe.")
    policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    if manifest.get("policy_sha256") != policy_sha:
        raise RuntimeError("Frozen build policy and manifest do not match.")
    return manifest


class DesktopRuntime:
    def __init__(self, *, data_dir: Path, desktop_token: str, research_dir: Path | None = None,
                 db_path: Path | None = None, asset_root: Path | None = None,
                 forward_path: Path | None = None, restrict_data_dir: bool = True,
                 read_only_desktop: bool = False):
        self.data_dir = selected_directory(data_dir, restrict_existing=restrict_data_dir)
        self.asset_root = Path(asset_root or Path(__file__).resolve().parent).resolve()
        _desktop_build_manifest(self.asset_root)
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.data_dir / "paper.sqlite3"
        if not self.db_path.parent.exists():
            private_directory(self.db_path.parent)
        self.research_dir = (selected_directory(research_dir, restrict_existing=False) if research_dir else
            private_directory(self.data_dir / "research_data"))
        self.server: OwnedServer = create_server(self.db_path, port=0, desktop_token=desktop_token,
            research_root=self.research_dir, asset_root=self.asset_root, forward_path=forward_path,
            read_only_desktop=read_only_desktop)
        self.desktop_token = desktop_token
        self.read_only_desktop = bool(read_only_desktop)
        self.provider_attempts = 0
        if self.read_only_desktop:
            def disabled_provider(*_args, **_kwargs):
                self.provider_attempts += 1
                raise RuntimeError("Provider access is disabled in offline diagnostics.")
            self.server.forward_cohort.fetch_daily = disabled_provider
            self.server.forward_cohort.fetch_quotes = disabled_provider
        self._stopped = False

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def port(self) -> int:
        return int(self.server.address[1])

    def start(self, *, timeout: float = 5.0) -> None:
        self.server.start()
        endpoint = self.url + "/api/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            req = urllib.request.Request(endpoint, headers={"X-RKO-Desktop-Token": self.desktop_token})
            try:
                with opener.open(req, timeout=.5) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
            time.sleep(.05)
        self.stop()
        raise RuntimeError("The private loopback server did not become ready.") from last_error

    def has_open_or_pending_positions(self) -> bool:
        with self.server.lab.db() as db:
            row = db.execute("SELECT 1 FROM paper_v3 WHERE namespace='live:coingecko' AND qty>1e-12 LIMIT 1").fetchone()
            if row:
                return True
        with self.server.forward_cohort.db() as db:
            cid_row = self.server.forward_cohort._latest(db)
            if cid_row is None:
                return False
            return db.execute("SELECT 1 FROM accounts WHERE cohort_id=? AND (qty>1e-12 OR exit_requested=1) LIMIT 1",
                (cid_row["id"],)).fetchone() is not None

    def has_active_forward_session(self) -> bool:
        with self.server.forward_cohort.db() as db:
            row = self.server.forward_cohort._latest(db)
        return bool(row and row["session_status"] == "active" and row["state"] in {"starting", "running"})

    def close_warning(self) -> str | None:
        has_positions = self.has_open_or_pending_positions()
        active = self.has_active_forward_session()
        if not has_positions and not active:
            return None
        lines = ["Closing RKO stops this computer's polling and paper-session monitoring."]
        if active:
            if has_positions:
                lines.append("The active paper session is paused while the app is closed; its original deadline still advances.")
            else:
                lines.append("The session is active and flat: new entries stop while the app is closed; its original deadline still advances.")
        if has_positions:
            lines.append("Open holdings and pending exits remain saved. No simulated close will occur while the app is closed; reopen RKO for a fresh-quote exit.")
        lines.append("Nothing is sent to an exchange.")
        return "\n\n".join(lines)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.server.stop()


@contextlib.contextmanager
def locked_runtime(data_dir: Path, **kwargs):
    lock = InstanceLock(data_dir)
    lock.acquire()
    runtime = None
    try:
        runtime = DesktopRuntime(data_dir=data_dir, **kwargs)
        yield runtime
    finally:
        if runtime:
            runtime.stop()
        lock.close()
