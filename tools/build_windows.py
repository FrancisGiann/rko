#!/usr/bin/env python3
"""Build an unsigned Windows x86_64 one-directory RKO package (manual runner only)."""
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
PYTHON_FILES = (
    "desktop_app.py", "desktop_runtime.py", "desktop_build_info.py", "server.py", "engine.py",
    "forward_v5.py", "market.py", "research.py", "archive_data.py", "research_jobs.py",
    "research_v5_view.py", "walkforward.py", "walkforward_v5.py", "walkforward_jesse.py",
)
DATA_FILES = (
    "index.html", "app.js", "time_utils.js", "styles.css", "favicon.svg",
    "research_protocol_v4.json", "research_protocol_v5_exploratory.json", "research_protocol_jesse.json",
    "forward_policy_v5_addendum.json", "forward_policy_v5_addendum.json.sha256",
    "DATA_LICENSE_NOTICE.md", "THIRD_PARTY_NOTICES_WINDOWS.md", "PROJECT_LICENSE_STATUS.md",
    "DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md",
)
LICENSE_DIR = Path("third_party") / "desktop"
WINDOWS_LICENSE_FILES = (
    "Python-LICENSE.txt", "altgraph-LICENSE", "bottle-LICENSE",
    "packaging-LICENSE.APACHE", "packaging-LICENSE.BSD", "proxy_tools-LICENSE.txt",
    "pyinstaller-COPYING.txt", "pyinstaller_hooks_contrib-LICENSE", "pywebview-LICENSE",
    "setuptools-LICENSE", "typing_extensions-LICENSE", "pythonnet-LICENSE",
    "clr-loader-LICENSE", "pefile-LICENSE", "pywin32-ctypes-LICENSE",
    "Microsoft-WebView2-LICENSE.md",
)
REQUIRED_WEBVIEW2_FILES = (
    "lib/Microsoft.Web.WebView2.Core.dll",
    "lib/Microsoft.Web.WebView2.WinForms.dll",
    "lib/runtimes/win-x64/native/WebView2Loader.dll",
)
EXPECTED = {
    "pywebview":"6.2.1", "pyinstaller":"6.22.3", "proxy-tools":"0.1.0", "bottle":"0.13.4",
    "typing-extensions":"4.16.0", "altgraph":"0.17.5", "packaging":"26.3",
    "pyinstaller-hooks-contrib":"2026.9", "setuptools":"84.2.1", "pythonnet":"3.0.5",
    "clr-loader":"0.2.9", "pefile":"2024.8.26", "pywin32-ctypes":"0.2.3",
}
PACKAGE_HASH_FILES = (
    "tools/build_windows.py", "tools/rko_desktop_windows.spec", "requirements-windows.txt",
    "desktop_app.py", "desktop_runtime.py", "app.js", "index.html", "styles.css", "time_utils.js",
    "THIRD_PARTY_NOTICES_WINDOWS.md", "DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_environment() -> None:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise SystemExit("This reviewed builder requires a Windows x86_64 runner.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("The manual Windows build is pinned to Python 3.12.")
    for name, version in EXPECTED.items():
        actual = importlib.metadata.version(name)
        if actual != version:
            raise SystemExit(f"Build dependency {name} is {actual}; required exact version is {version}.")


def stage_source(stage: Path) -> dict:
    stage.mkdir(parents=True, exist_ok=False)
    for name in (*PYTHON_FILES, *DATA_FILES):
        source = ROOT / name
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"Required Windows build input is missing or unsafe: {name}")
        shutil.copyfile(source, stage / name)
    license_source = ROOT / LICENSE_DIR
    license_target = stage / LICENSE_DIR
    license_target.mkdir(parents=True)
    for name in WINDOWS_LICENSE_FILES:
        source = license_source / name
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"Required Windows dependency notice is missing or unsafe: {source.relative_to(ROOT)}")
        shutil.copyfile(source, license_target / name)

    source_hashes = {name:sha(ROOT / name) for name in RUNTIME_HASH_FILES}
    policy_hash = sha(ROOT / "forward_policy_v5_addendum.json")
    package_hashes = {name:sha(ROOT / name) for name in PACKAGE_HASH_FILES}
    for name in WINDOWS_LICENSE_FILES:
        license_file = license_source / name
        package_hashes[license_file.relative_to(ROOT).as_posix()] = sha(license_file)
    identity = {"target":"windows-x86_64", "python":platform.python_version(),
        "source_sha256":source_hashes, "policy_sha256":policy_hash, "package_sha256":package_hashes}
    build_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"schema":"rko-desktop-build-v1", "build_id":build_id,
        "built_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "target":"windows-x86_64", "python":platform.python_version(),
        "source_sha256":source_hashes, "policy_sha256":policy_hash, "package_sha256":package_hashes,
        "qt_included":False, "market_data_bundled":False, "saved_studies_bundled":False}
    (stage / "desktop_build_info.py").write_text(
        '"""Generated and verified for this frozen local build."""\nSOURCE_SHA256 = ' +
        repr(source_hashes) + "\n", encoding="utf-8", newline="\n")
    (stage / "desktop_build_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return manifest


def validate_payload(bundle: Path) -> None:
    internal = bundle / "_internal"
    required = (
        bundle / "RKO.exe", internal / "desktop_build_manifest.json", internal / "index.html",
        internal / "app.js", internal / "forward_policy_v5_addendum.json",
        *(internal / "webview" / relative for relative in REQUIRED_WEBVIEW2_FILES),
        *(internal / "licenses" / "third_party" / name for name in WINDOWS_LICENSE_FILES),
    )
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"Frozen Windows package is missing required content: {path.relative_to(bundle)}")
    banned_suffixes = {".sqlite", ".sqlite3", ".db", ".csv"}
    banned = [p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()
        and (p.suffix.lower() in banned_suffixes or p.suffix.lower() in {".parquet"})
        and p.name != "base_library.zip"]
    if banned or any(p.name == "research_data" for p in bundle.rglob("research_data")):
        raise RuntimeError("Frozen Windows package contains a database, dataset, or research cache.")
    legacy = [p.relative_to(bundle).as_posix() for p in bundle.rglob("WebBrowserInterop.*.dll") if p.is_file()]
    if legacy:
        raise RuntimeError("Frozen Windows package unexpectedly includes legacy browser interop DLLs.")


def build(out: Path) -> dict:
    verify_environment()
    out = Path(out).resolve()
    if out.name != "RKO":
        raise SystemExit("The reviewed one-directory Windows bundle name is fixed as RKO.")
    if out.exists():
        raise SystemExit(f"Refusing to overwrite existing Windows build output: {out}")
    parent = out.parent
    parent.mkdir(parents=True, exist_ok=True)
    work, stage = parent / "work", parent / "source"
    if work.exists() or stage.exists():
        raise SystemExit("Refusing to overwrite existing Windows build work/staging directories.")
    manifest = stage_source(stage)
    env = dict(os.environ)
    env["RKO_BUILD_SOURCE"] = str(stage)
    spec = ROOT / "tools" / "rko_desktop_windows.spec"
    command = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--log-level=WARN",
        "--distpath", str(parent), "--workpath", str(work), str(spec)]
    try:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        validate_payload(out)
        return manifest | {"bundle_path":str(out), "pyinstaller_version":EXPECTED["pyinstaller"],
            "pywebview_version":EXPECTED["pywebview"]}
    except BaseException:
        if out.exists():
            shutil.rmtree(out)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "windows-x86_64" / "RKO")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    receipt = build(args.out)
    if args.receipt:
        path = args.receipt.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
