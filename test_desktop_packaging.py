import unittest
import tempfile
from pathlib import Path

from tools.bundle_policy import filter_toc, keep_binary_member, keep_data_member
from tools.build_windows import REQUIRED_WEBVIEW2_FILES, WINDOWS_LICENSE_FILES, validate_payload


class BundlePolicyTests(unittest.TestCase):
    def test_linux_toc_drops_host_shared_libraries_but_keeps_python_runtime(self):
        toc = [
            ("libgtk-3.so.0", "/usr/lib64/libgtk-3.so.0", "BINARY"),
            ("libgobject-2.0.so.0", "/usr/lib64/libgobject-2.0.so.0", "BINARY"),
            ("libcrypto.so.3", "/usr/lib64/libcrypto.so.3", "BINARY"),
            ("libpython3.14.so.1.0", "/usr/lib64/libpython3.14.so.1.0", "BINARY"),
            ("_cffi_backend.cpython-314-x86_64-linux-gnu.so", "/venv/_cffi_backend.so", "BINARY"),
            ("gi/_gi.cpython-314-x86_64-linux-gnu.so", "/system/gi/_gi.so", "BINARY"),
            ("cryptography/hazmat/bindings/_rust.abi3.so", "/venv/cryptography.so", "BINARY"),
            ("gio_modules/libgiognutls.so", "/usr/lib64/gio/libgiognutls.so", "BINARY"),
            (".libgnutls.so.30.hmac", "/usr/lib64/.libgnutls.so.30.hmac", "BINARY"),
        ]
        kept = [row[0] for row in filter_toc(toc, "binaries")]
        self.assertEqual(kept, [
            "libpython3.14.so.1.0",
            "_cffi_backend.cpython-314-x86_64-linux-gnu.so",
            "gi/_gi.cpython-314-x86_64-linux-gnu.so",
            "cryptography/hazmat/bindings/_rust.abi3.so",
        ])

    def test_linux_toc_keeps_app_assets_and_drops_os_theme_locale_typelib_data(self):
        toc = [
            ("index.html", "/stage/index.html", "DATA"),
            ("research_protocol_v5_exploratory.json", "/stage/protocol.json", "DATA"),
            ("gi_typelibs/Gtk-3.0.typelib", "/usr/lib64/Gtk.typelib", "DATA"),
            ("gi_typelibs/WebKit2-4.1.typelib", "/usr/lib64/WebKit2.typelib", "DATA"),
            ("share/icons/hicolor/index.theme", "/usr/share/icons/index.theme", "DATA"),
            ("share/locale/de/LC_MESSAGES/gtk30.mo", "/usr/share/locale/gtk.mo", "DATA"),
            ("gio_modules/giomodule.cache", "/usr/lib64/gio/giomodule.cache", "DATA"),
        ]
        kept = [row[0] for row in filter_toc(toc, "datas")]
        self.assertEqual(kept, ["index.html", "research_protocol_v5_exploratory.json"])

    def test_path_separators_are_normalized(self):
        self.assertFalse(keep_binary_member(r"gio_modules\libgnutls.so"))
        self.assertFalse(keep_data_member(r"share\icons\index.theme"))
        self.assertTrue(keep_data_member(r"assets\index.html"))

    def test_filter_kind_is_explicit(self):
        with self.assertRaises(ValueError):
            filter_toc([], "unknown")


class WindowsPayloadTests(unittest.TestCase):
    def make_valid_bundle(self, root):
        internal = root / "_internal"
        for relative in (
            "desktop_build_manifest.json", "index.html", "app.js",
            "forward_policy_v5_addendum.json",
            *(f"webview/{name}" for name in REQUIRED_WEBVIEW2_FILES),
            *(f"licenses/third_party/{name}" for name in WINDOWS_LICENSE_FILES),
        ):
            path = internal / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")
        (root / "RKO.exe").write_text("fixture", encoding="utf-8")

    def test_windows_payload_requires_edge_files_and_scoped_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "RKO"
            root.mkdir()
            self.make_valid_bundle(root)
            validate_payload(root)

    def test_windows_payload_rejects_legacy_interop_and_research_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "RKO"
            root.mkdir()
            self.make_valid_bundle(root)
            legacy = root / "_internal" / "webview" / "lib" / "WebBrowserInterop.x64.dll"
            legacy.write_text("fixture", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "legacy browser interop"):
                validate_payload(root)
            legacy.unlink()
            (root / "_internal" / "research_data").mkdir()
            with self.assertRaisesRegex(RuntimeError, "database, dataset, or research cache"):
                validate_payload(root)


if __name__ == "__main__":
    unittest.main()
