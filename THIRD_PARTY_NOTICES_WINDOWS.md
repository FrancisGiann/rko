# Windows desktop third-party notices

The Windows x86_64 / Python 3.12 target is an unvalidated manual build configuration. No Windows GUI build or runtime test has been completed. This notice is not a binary distribution clearance.

The application source retains its existing project-license status. Notices staged by the builder include CPython (PSF), PyWebView 6.2.1 (BSD-3-Clause), Microsoft WebView2 Core/WinForms/loader (same-tag redistribution notice), Python.NET 3.0.5 (MIT), clr-loader 0.2.9 (MIT), pefile 2024.8.26 (MIT), pywin32-ctypes 0.2.3 (BSD-3-Clause), Bottle 0.13.4 (MIT), proxy-tools 0.1.0 (MIT), typing-extensions 4.16.0 (PSF-2.0), and the pinned PyInstaller/build-tool notices listed in `third_party/desktop`. Build-time packages are pinned in `requirements-windows.txt`; upstream source URLs and package scope are recorded in `DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md`.

The included license texts are from the pinned upstream source tags. The local PyWebView 6.2.1 Core DLL was checked against its upstream Git blob identity; no Windows binary has been built or runtime-tested. Completing the manual Windows diagnostic and review of the exact generated payload remain release gates. See `DESKTOP_DEPENDENCY_INVENTORY_WINDOWS.md` for scope and evidence. Do not infer that this notice licenses the RKO project itself.

Microsoft Edge WebView2 Runtime is a separate host prerequisite. RKO does not download or install it. The build selects the Edge WebView2 backend and excludes GTK, WebKit2GTK, Qt, MSHTML/Internet Explorer, legacy WebBrowserInterop DLLs, and market data. A missing WebView2 runtime should stop startup with an official Microsoft help link.
