"""Manual Windows x86_64 one-directory build; WebView2 is a host prerequisite."""
from __future__ import annotations

import os
from pathlib import Path
import webview

source = Path(os.environ["RKO_BUILD_SOURCE"]).resolve()
webview_package = Path(webview.__file__).resolve().parent
datas = []
for name in (
    "index.html", "app.js", "time_utils.js", "styles.css", "favicon.svg",
    "research_protocol_v4.json", "research_protocol_v5_exploratory.json", "research_protocol_jesse.json",
    "forward_policy_v5_addendum.json", "forward_policy_v5_addendum.json.sha256", "DATA_LICENSE_NOTICE.md",
    "THIRD_PARTY_NOTICES_WINDOWS.md", "PROJECT_LICENSE_STATUS.md", "DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md",
    "desktop_build_manifest.json",
):
    datas.append((str(source / name), "."))
for license_file in sorted((source / "third_party" / "desktop").iterdir()):
    if license_file.is_file() and not license_file.is_symlink():
        datas.append((str(license_file), "licenses/third_party"))

# Include only the pinned Edge WebView2 interop assemblies and x64 loader.
# Do not ship PyWebView's legacy WebBrowserInterop DLLs or a browser runtime.
for relative in (
    "lib/Microsoft.Web.WebView2.Core.dll",
    "lib/Microsoft.Web.WebView2.WinForms.dll",
    "lib/runtimes/win-x64/native/WebView2Loader.dll",
):
    datas.append((str(webview_package / relative), "webview/" + str(Path(relative).parent)))

a = Analysis(
    [str(source / "desktop_app.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "webview.platforms.winforms", "webview.platforms.edgechromium",
        "clr", "pythonnet", "clr_loader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "webview.platforms.cef",
        "webview.platforms.mshtml", "webview.platforms.gtk", "gi", "cairo", "cryptography",
        "cffi", "jinja2", "mako", "markupsafe", "setuptools",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="RKO", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name="RKO",
)
