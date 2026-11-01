# RKO V5.3 Windows dependency inventory

This is a reviewed build configuration for a future manual Windows x86_64 runner, not a tested Windows application or distributable binary. No Windows runner, Edge WebView2 runtime, or native GUI validation has been completed. The build requires Windows x86_64 and Python 3.12 and refuses other targets.

## Intended bundled components

| Component | Pin / source | Current notice state |
| --- | --- | --- |
| CPython runtime and standard library | Python 3.12, Windows x86_64 | PSF notice is staged; exact runner build to be recorded in its build manifest. |
| PyWebView | 6.2.1 | BSD-3-Clause notice staged. |
| Bottle / proxy-tools / typing-extensions | 0.13.4 / 0.1.0 / 4.16.0 | MIT / MIT / PSF-2.0 notices staged. |
| WebView2 Core, WinForms, and x64 loader files | PyWebView 6.2.1 wheel; local Core DLL (649,800 bytes, SHA-256 `8219a3135436c147f8eb02fb831e99596103eebc18b6d7b794ca93f4c67018a2`) matches the upstream tag's Git blob `9b1964676eff82e29ee9c07244fac0577e027eef` | The same-tag PyWebView license covers Core, WinForms, and WebView2Loader; notice included. The separately installed Evergreen Runtime is not bundled. |
| Python.NET / clr-loader | 3.0.5 / 0.2.9 | Exact upstream tagged MIT notices included. |
| pefile / pywin32-ctypes | 2024.8.26 / 0.2.3 | Exact upstream tagged MIT / BSD-3-Clause notices included. |
| PyInstaller / hooks / altgraph / packaging / setuptools / other runtime packages | Exact pins in `requirements-windows.txt` | Corresponding available notices are staged in `third_party/desktop` and copied into the package. |

The manual builder verifies exact package versions, ships only the Edge WebView2 interop DLLs and x64 loader from the PyWebView package, and excludes the legacy `WebBrowserInterop` DLLs, GTK, Qt, and MSHTML backend. Microsoft Edge WebView2 Runtime is a host prerequisite. RKO never downloads or installs it. A missing runtime is reported to the user with Microsoft's official installation page; no admin operation or automatic installer is used. See `THIRD_PARTY_NOTICES_WINDOWS.md` and the named files in `third_party/desktop` for full notices and source URLs.

## Redistribution gate

Do not distribute a Windows binary until the manual Windows CI workflow has built it on a legitimate Windows x86_64 / Python 3.12 runner, the embedded WebView2 runtime diagnostic has passed with provider access disabled, and the package contents/notices have been reviewed. License texts are included for the pinned dependencies; this source review does not validate the resulting binary or replace applicable terms. The workflow only uploads a redacted diagnostic report and sanitized build manifest, never the executable.

The project itself has no project-wide open-source license selected. This inventory does not grant one. The public app remains paper-only, has no bundled market data or saved research, and does not contain credentials or a user database.

## Pinned notice sources

- [Python.NET 3.0.5 MIT](https://raw.githubusercontent.com/pythonnet/pythonnet/v3.0.5/LICENSE)
- [clr-loader 0.2.9 MIT](https://raw.githubusercontent.com/pythonnet/clr-loader/v0.2.9/LICENSE)
- [pefile 2024.8.26 MIT](https://raw.githubusercontent.com/erocarrera/pefile/v2024.8.26/LICENSE)
- [pywin32-ctypes 0.2.3 BSD-3-Clause](https://raw.githubusercontent.com/enthought/pywin32-ctypes/v0.2.3/LICENSE.txt)
- [PyWebView 6.2.1's Microsoft WebView2 redistribution notice](https://raw.githubusercontent.com/r0x0r/pywebview/6.2.1/webview/lib/Microsoft.Web.WebView2.LICENSE.md)
- [Microsoft WebView2 Runtime distribution documentation](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)
