import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_app


class _Events:
    def __init__(self):
        self.loaded = threading.Event()


class _Window:
    uid = "diagnostic-test"

    def __init__(self):
        self.events = _Events()

    def evaluate_js(self, _script):
        raise AssertionError("The probe must not execute before GUI readiness.")


class _Runtime:
    read_only_desktop = True
    provider_attempts = 0


class _NavigationArgs:
    def __init__(self, uri):
        self.Uri = uri
        self.Cancel = False


class _NewWindowArgs:
    def __init__(self, uri):
        self.Uri = uri
        self.Handled = False


class CsvExportPortabilityTests(unittest.TestCase):
    def test_csv_export_succeeds_when_os_fchmod_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "SIMULATED-portable-export.csv"
            with patch.object(desktop_app.os, "fchmod", None, create=True):
                written = desktop_app.write_csv_export(target, "symbol,amount\nBTC-USD,1\n")
            self.assertEqual(written, target)
            self.assertEqual(target.read_text(encoding="utf-8"), "symbol,amount\nBTC-USD,1\n")


class DiagnosticTimeoutTests(unittest.TestCase):
    def test_diagnostic_exit_code_requires_successful_gui_report(self):
        self.assertEqual(desktop_app.diagnostic_report_exit_code(None), 1)
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "diagnostic.json"
            self.assertEqual(desktop_app.diagnostic_report_exit_code(report), 1)
            report.write_text("not-json", encoding="utf-8")
            self.assertEqual(desktop_app.diagnostic_report_exit_code(report), 1)
            report.write_text(json.dumps({"passed":False,"gui_ready":False}), encoding="utf-8")
            self.assertEqual(desktop_app.diagnostic_report_exit_code(report), 1)
            report.write_text(json.dumps({"passed":True,"gui_ready":False}), encoding="utf-8")
            self.assertEqual(desktop_app.diagnostic_report_exit_code(report), 1)
            report.write_text(json.dumps({"passed":True,"gui_ready":True}), encoding="utf-8")
            self.assertEqual(desktop_app.diagnostic_report_exit_code(report), 0)

    def test_edge_webview_navigation_and_new_window_guards_are_fail_closed(self):
        opened = []
        navigation, new_window = desktop_app.make_edge_navigation_guards(
            ("http", "127.0.0.1", 41000), open_external=opened.append)

        local = _NavigationArgs("http://127.0.0.1:41000/")
        navigation(None, local)
        self.assertFalse(local.Cancel)
        self.assertEqual(opened, [])

        remote = _NavigationArgs("https://example.com/path")
        navigation(None, remote)
        self.assertTrue(remote.Cancel)
        self.assertEqual(opened, ["https://example.com/path"])

        unsafe = _NavigationArgs("file:///etc/passwd")
        navigation(None, unsafe)
        self.assertTrue(unsafe.Cancel)
        self.assertEqual(len(opened), 1)

        popup = _NewWindowArgs("https://example.org/")
        new_window(None, popup)
        self.assertTrue(popup.Handled)
        self.assertEqual(opened[-1], "https://example.org/")

        unsafe_popup = _NewWindowArgs("javascript:alert(1)")
        new_window(None, unsafe_popup)
        self.assertTrue(unsafe_popup.Handled)
        self.assertEqual(len(opened), 2)

    def test_native_backend_never_selects_legacy_ie_and_webview2_check_fails_closed(self):
        self.assertEqual(desktop_app.native_gui_backend("win32"), "edgechromium")
        self.assertEqual(desktop_app.native_gui_backend("linux"), "gtk")
        self.assertTrue(desktop_app.require_webview2(lambda: True))
        self.assertFalse(desktop_app.require_webview2(lambda: False))
        self.assertFalse(desktop_app.require_webview2(lambda: (_ for _ in ()).throw(RuntimeError("missing"))))

    def test_js_bridge_exports_only_intended_public_methods(self):
        bridge = desktop_app.DesktopBridge()
        public_callables = {name for name in dir(bridge) if not name.startswith("_")
            and callable(getattr(bridge, name))}
        public_fields = {name for name in vars(bridge) if not name.startswith("_")}
        self.assertEqual(public_callables, {"open_external", "save_csv"})
        self.assertEqual(public_fields, set())
        self.assertTrue(hasattr(bridge, "_window"))
        self.assertFalse(hasattr(bridge, "window"))

    def test_diagnostic_url_redacts_query_fragment_and_credentials(self):
        safe = desktop_app.sanitize_diagnostic_url(
            "http://127.0.0.1:43123/api/state?X-RKO-Desktop-Token=secret#private")
        self.assertEqual(safe, {"scheme":"http", "host":"127.0.0.1", "path":"/api/state"})
        self.assertNotIn("secret", repr(safe))

    def test_missing_page_load_writes_timeout_report_and_requests_owned_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "diagnostic.json"
            shutdown = threading.Event()
            with patch.object(desktop_app, "DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS", 0.02), \
                patch.object(desktop_app, "_capture_webview_snapshot", return_value=False), \
                patch.object(desktop_app, "_request_diagnostic_shutdown", side_effect=lambda _window: shutdown.set()):
                window = _Window()
                desktop_app.start_diagnostic(window, _Runtime(), report,
                    guard_ready=threading.Event(), guard_error={"error":None}, timeout_seconds=0.02)
                self.assertTrue(shutdown.wait(2), "diagnostic worker did not request a bounded shutdown")
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(payload["passed"])
            self.assertFalse(payload["gui_ready"])
            self.assertEqual(payload["provider_attempts"], 0)
            self.assertIn("TimeoutError", payload["error"])
            self.assertFalse(payload["screenshot"]["captured"])


if __name__ == "__main__":
    unittest.main()
