# RKO V5.3 desktop dependency inventory

This inventory describes the Linux x86_64 one-directory package. It is tested on Fedora KDE 44; it is not a universal Linux compatibility claim.

## Bundled runtime

| Component | Version / provenance | License notice |
| --- | --- | --- |
| CPython runtime and standard library | 3.14.8, Fedora x86_64 build | PSF license and incorporated-software acknowledgements in `third_party/desktop/Python-LICENSE.txt` |
| PyWebView | 6.2.1, PyPI wheel SHA-256 `9d07275f53894ab4d5e2e0e996227193e7187dec276d9b624dccbce029216b46` | BSD-3-Clause |
| PyGObject bindings | 3.56.3, Fedora `python3-gobject-base`; CPython 3.14 ABI | LGPL-2.1-or-later |
| Pycairo bindings | 1.28.0, Fedora `python3-cairo`; CPython 3.14 ABI | LGPL-2.1-only or MPL-1.1 |
| Bottle | 0.13.4, PyPI wheel SHA-256 `045684fbd2764eac9cdeb824861d1551d113e8b683d8d26e296898d3dd99a12e` | MIT |
| proxy-tools | 0.1.0, PyPI wheel SHA-256 `ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010` | MIT |
| typing-extensions | 4.16.0, PyPI wheel SHA-256 `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` | PSF-2.0 |

The corresponding exact notices are shipped in `third_party/desktop`. PyGObject and Pycairo include Python bindings/extensions only; the GTK/WebKit shared libraries and introspection typelibs are resolved from the host distribution.

## Host runtime requirements

The target needs GTK 3, WebKit2GTK 4.1, GObject introspection/GI typelibs for GTK 3 and WebKit2 4.1, and their distribution-provided GLib, Cairo, X11 or Wayland, font, and desktop services. The build host reported GTK 3.24.52 and WebKit2GTK 2.54.1. It deliberately excludes system shared-library copies, GIO modules, typelibs, icon themes, and locale trees from the bundle. Do not run with sandbox-disabling flags or environment overrides.

## Build-only tools

`requirements-linux.lock` pins PyInstaller 6.22.3, pyinstaller-hooks-contrib 2026.9, altgraph 0.17.5, packaging 26.3, setuptools 84.2.1, plus the runtime packages above. Each PyPI artifact is hash-pinned. The PyInstaller exception and build-tool license files are included in `third_party/desktop`; these tools are not intended as Python application imports.

No market history, normalized data, saved study results, user database, credentials, Qt, or Windows GUI runtime is bundled.
