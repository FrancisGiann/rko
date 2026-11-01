"""PyInstaller one-directory build; native GTK/WebKitGTK stays a system runtime."""
from __future__ import annotations

import os
import sys
from pathlib import Path

source = Path(os.environ["RKO_BUILD_SOURCE"]).resolve()
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
from tools.bundle_policy import filter_toc

datas = []
for name in (
    "index.html", "app.js", "time_utils.js", "styles.css", "favicon.svg",
    "research_protocol_v4.json", "research_protocol_v5_exploratory.json",
    "research_protocol_jesse.json", "forward_policy_v5_addendum.json",
    "forward_policy_v5_addendum.json.sha256", "DATA_LICENSE_NOTICE.md",
    "THIRD_PARTY_NOTICES.md", "PROJECT_LICENSE_STATUS.md", "DESKTOP_DEPENDENCY_INVENTORY.md",
    "desktop_build_manifest.json",
):
    datas.append((str(source / name), "."))
for license_file in sorted((source / "third_party" / "desktop").iterdir()):
    if license_file.is_file() and not license_file.is_symlink():
        datas.append((str(license_file), "licenses/third_party"))

a = Analysis(
    [str(source / "desktop_app.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=["webview.platforms.gtk", "gi", "gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.WebKit2"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "clr", "pythonnet", "win32com",
        "cryptography", "cffi", "jinja2", "mako", "markupsafe", "setuptools"],
    noarchive=False,
    optimize=1,
)
## Use the host Fedora GTK/WebKit runtime. Keep the embedded Python runtime and
## Python extensions while omitting duplicate OS libraries, desktop themes, and
## locale packs automatically pulled in by GI discovery hooks.
a.binaries = filter_toc(a.binaries, "binaries")
a.datas = filter_toc(a.datas, "datas")
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="RKO", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
    name="RKO",
)
