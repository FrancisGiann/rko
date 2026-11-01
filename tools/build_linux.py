#!/usr/bin/env python3
"""Build an unsigned Linux x86_64 one-directory bundle in an isolated staging tree."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_HASH_FILES = (
    "forward_v5.py", "market.py", "walkforward_v5.py", "server.py", "engine.py", "research.py",
)
PACKAGE_HASH_FILES = (
    "tools/build_linux.py", "tools/rko_desktop_linux.spec", "tools/bundle_policy.py",
    "requirements-linux.lock", "desktop_app.py", "desktop_runtime.py", "app.js", "index.html",
    "styles.css", "time_utils.js", "THIRD_PARTY_NOTICES.md", "DESKTOP_DEPENDENCY_INVENTORY.md",
)
PYTHON_FILES = (
    "desktop_app.py", "desktop_runtime.py", "desktop_build_info.py", "server.py", "engine.py",
    "forward_v5.py", "market.py", "research.py", "archive_data.py", "research_jobs.py",
    "research_v5_view.py", "walkforward.py", "walkforward_v5.py", "walkforward_jesse.py",
)
DATA_FILES = (
    "index.html", "app.js", "time_utils.js", "styles.css", "favicon.svg",
    "research_protocol_v4.json", "research_protocol_v5_exploratory.json",
    "research_protocol_jesse.json",
    "forward_policy_v5_addendum.json",
    "forward_policy_v5_addendum.json.sha256", "DATA_LICENSE_NOTICE.md",
    "THIRD_PARTY_NOTICES.md", "PROJECT_LICENSE_STATUS.md", "DESKTOP_DEPENDENCY_INVENTORY.md",
)
LICENSE_DIR = Path("third_party") / "desktop"
EXPECTED = {"pywebview": "6.2.1", "pyinstaller": "6.22.3", "proxy-tools": "0.1.0",
    "bottle": "0.13.4", "typing-extensions": "4.16.0", "altgraph": "0.17.5",
    "packaging": "26.3", "pyinstaller-hooks-contrib": "2026.9", "setuptools": "84.2.1"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_environment() -> None:
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SystemExit("This reviewed builder currently supports Linux x86_64 only.")
    for name, version in EXPECTED.items():
        actual = importlib.metadata.version(name)
        if actual != version:
            raise SystemExit(f"Build dependency {name} is {actual}; required exact reviewed version is {version}.")
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2  # noqa: F401


def stage_source(stage: Path) -> dict:
    stage.mkdir(parents=True, exist_ok=False)
    for name in (*PYTHON_FILES, *DATA_FILES):
        source = ROOT / name
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"Required build input is missing or unsafe: {name}")
        shutil.copyfile(source, stage / name)
    license_source = ROOT / LICENSE_DIR
    license_target = stage / LICENSE_DIR
    license_target.mkdir(parents=True)
    for source in license_source.iterdir():
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, license_target / source.name)
    source_hashes = {name: sha(ROOT / name) for name in RUNTIME_HASH_FILES}
    policy_hash = sha(ROOT / "forward_policy_v5_addendum.json")
    package_hashes = {name: sha(ROOT / name) for name in PACKAGE_HASH_FILES}
    for license_file in sorted((ROOT / LICENSE_DIR).iterdir()):
        if license_file.is_file() and not license_file.is_symlink():
            package_hashes[license_file.relative_to(ROOT).as_posix()] = sha(license_file)
    build_id = hashlib.sha256(json.dumps({"source_sha256": source_hashes, "policy_sha256": policy_hash,
        "package_sha256":package_hashes},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"schema":"rko-desktop-build-v1", "build_id":build_id,
        "built_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "target":"linux-x86_64", "python":platform.python_version(),
        "source_sha256":source_hashes, "policy_sha256":policy_hash,
        "package_sha256":package_hashes,
        "qt_included":False, "market_data_bundled":False, "saved_studies_bundled":False}
    (stage / "desktop_build_info.py").write_text(
        '"""Generated and verified for this frozen local build."""\nSOURCE_SHA256 = ' +
        repr(source_hashes) + "\n", encoding="utf-8")
    (stage / "desktop_build_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build(out: Path, python: str) -> dict:
    verify_environment()
    out = Path(out).expanduser().resolve()
    if out.name != "RKO":
        raise SystemExit("The reviewed one-dir executable bundle name is fixed as RKO.")
    if out.exists():
        raise SystemExit(f"Refusing to overwrite existing build output: {out}")
    dist_dir = out.parent
    dist_dir.mkdir(parents=True, exist_ok=True)
    work = dist_dir / "work"
    if work.exists():
        raise SystemExit(f"Refusing to overwrite existing PyInstaller work directory: {work}")
    stage = dist_dir / "source"
    if stage.exists():
        raise SystemExit(f"Refusing to overwrite existing build staging directory: {stage}")
    manifest = stage_source(stage)
    env = dict(os.environ)
    env["RKO_BUILD_SOURCE"] = str(stage)
    spec = ROOT / "tools" / "rko_desktop_linux.spec"
    command = [python, "-m", "PyInstaller", "--clean", "--noconfirm", "--log-level=WARN",
        "--distpath", str(dist_dir), "--workpath", str(work), str(spec)]
    try:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        built = out
        if not built.is_dir() or not (built / "RKO").is_file():
            raise RuntimeError("PyInstaller completed without the expected RKO one-dir executable.")
        if not (built / "_internal" / "desktop_build_manifest.json").is_file():
            raise RuntimeError("Frozen one-dir bundle is missing its build manifest.")
        validate_payload(built)
        return manifest | {"bundle_path":str(built), "pyinstaller_version":EXPECTED["pyinstaller"],
            "pywebview_version":EXPECTED["pywebview"]}
    except BaseException:
        if out.exists():
            shutil.rmtree(out)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


def validate_payload(bundle: Path) -> None:
    internal = bundle / "_internal"
    for required in ("RKO", "_internal/desktop_build_manifest.json", "_internal/index.html",
        "_internal/app.js", "_internal/forward_policy_v5_addendum.json",
        "_internal/research_protocol_jesse.json", "_internal/libpython3.14.so.1.0",
        "_internal/gi/_gi.cpython-314-x86_64-linux-gnu.so",
        "_internal/gi/_gi_cairo.cpython-314-x86_64-linux-gnu.so",
        "_internal/DESKTOP_DEPENDENCY_INVENTORY.md",
        "_internal/licenses/third_party/Python-LICENSE.txt",
        "_internal/licenses/third_party/PyGObject-LGPL-2.1.txt",
        "_internal/licenses/third_party/PyCairo-LGPL-2.1.txt",
        "_internal/licenses/third_party/PyCairo-MPL-1.1.txt"):
        if not (bundle / required).is_file():
            raise RuntimeError(f"Frozen package is missing required runtime content: {required}")
    forbidden_dirs = (internal / "share", internal / "gio_modules", internal / "gi_typelibs")
    if any(path.exists() for path in forbidden_dirs):
        raise RuntimeError("Frozen package unexpectedly contains host theme, plugin, or typelib data.")
    if (internal / "cryptography").exists() or (internal / "markupsafe").exists():
        raise RuntimeError("Frozen package unexpectedly contains excluded optional cryptography/template packages.")
    system_libs = [path.name for path in internal.iterdir() if path.is_file() and path.name.startswith("lib")
        and ".so" in path.name and not path.name.startswith("libpython")]
    if system_libs:
        raise RuntimeError("Frozen package unexpectedly contains host shared libraries: " + ", ".join(system_libs[:8]))
    banned_suffixes = {".sqlite", ".sqlite3", ".db", ".csv"}
    banned_names = [path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()
        and (path.suffix.lower() in banned_suffixes or path.name.lower().endswith((".zip", ".parquet")))
        and path.name != "base_library.zip"]
    if banned_names:
        raise RuntimeError("Frozen package unexpectedly contains user databases or datasets: " + ", ".join(banned_names[:8]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "linux-x86_64" / "RKO")
    parser.add_argument("--python", default=str(ROOT / ".venv-linux" / "bin" / "python"))
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = build(args.out, args.python)
    if args.receipt:
        path = args.receipt.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
