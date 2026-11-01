"""Unsigned local GTK/WebKit desktop shell for RKO paper research."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import stat
import urllib.parse
import webbrowser
from pathlib import Path

from desktop_runtime import DesktopRuntime, InstanceLock, default_data_directory


def asset_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


def validate_external_url(url: object) -> str | None:
    if not isinstance(url, str) or len(url) > 2048 or url != url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443):
            return None
        if any(ord(ch) < 32 for ch in url):
            return None
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return None


def is_local_navigation(url: object, expected_origin: tuple[str, str | None, int | None]) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme, parsed.hostname, parsed.port) == expected_origin
    except ValueError:
        return False


def sanitize_diagnostic_url(url: object) -> dict[str, str | None]:
    """Keep only non-secret request location fields for local diagnostic reports."""
    if not isinstance(url, str):
        return {"scheme":None,"host":None,"path":None}
    try:
        parsed = urllib.parse.urlsplit(url)
        return {"scheme":parsed.scheme[:12], "host":(parsed.hostname or "")[:200] or None,
            "path":parsed.path[:240] or "/"}
    except ValueError:
        return {"scheme":None,"host":None,"path":None}


def make_edge_navigation_guards(expected_origin, open_external=None):
    """Create fail-closed Edge WebView2 callbacks for navigation/new-window events."""
    open_external = open_external or webbrowser.open_new_tab

    def open_safe_external(url):
        safe_url = validate_external_url(url)
        if safe_url:
            try:
                open_external(safe_url)
            except Exception:
                pass

    def navigation_starting(_sender, args):
        try:
            uri = str(args.Uri)
        except Exception:
            args.Cancel = True
            return
        if not is_local_navigation(uri, expected_origin):
            args.Cancel = True
            open_safe_external(uri)

    def new_window_requested(_sender, args):
        args.Handled = True
        try:
            uri = str(args.Uri)
        except Exception:
            return
        open_safe_external(uri)

    return navigation_starting, new_window_requested


def native_gui_backend(platform_name: str | None = None) -> str:
    platform_name = platform_name or sys.platform
    return "edgechromium" if platform_name.startswith("win") else "gtk"


def require_webview2(check_runtime) -> bool:
    """Fail closed instead of allowing PyWebView's legacy MSHTML fallback."""
    try:
        return bool(check_runtime())
    except Exception:
        return False


class ExternalLinks:
    def open_external(self, url: str) -> bool:
        safe_url = validate_external_url(url)
        if not safe_url:
            return False
        return bool(webbrowser.open_new_tab(safe_url))


CSV_NAME = re.compile(r"SIMULATED-[A-Za-z0-9_-]{1,80}\.csv\Z")
MAX_CSV_BYTES = 25_000_000
DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS = 30
MAX_DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS = 45


def diagnostic_report_exit_code(report_path: Path | None) -> int:
    """Treat only a complete, passing GUI report as a successful diagnostic."""
    if report_path is None:
        return 1
    try:
        import json
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 1
    return 0 if isinstance(report, dict) and report.get("passed") is True and report.get("gui_ready") is True else 1


def write_csv_export(path: Path, content: str) -> Path:
    if not isinstance(content, str):
        raise ValueError("CSV export content must be text.")
    raw = content.encode("utf-8")
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV export exceeds the local 25 MB safety limit.")
    target = Path(os.path.abspath(Path(path).expanduser()))
    if target.suffix.lower() != ".csv" or target.is_symlink():
        raise ValueError("Choose a regular CSV file path.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("The chosen export target is not a regular file.")
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            try:
                fchmod(fd, 0o600)
            except OSError:
                pass
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("CSV export write did not make progress.")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


class DesktopBridge(ExternalLinks):
    def __init__(self):
        # PyWebView recursively exports public attributes from js_api objects.
        # Keep the native Window reference private so no GUI/native methods or
        # blocking properties become reachable from page JavaScript.
        self._window = None

    def save_csv(self, filename: str, content: str) -> bool:
        if not isinstance(filename, str) or not CSV_NAME.fullmatch(filename):
            return False
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CSV_BYTES:
            return False
        if self._window is None:
            return False
        import webview
        selected = self._window.create_file_dialog(webview.FileDialog.SAVE,
            save_filename=filename, file_types=("CSV files (*.csv)",))
        if not selected:
            return False
        path = selected[0] if isinstance(selected, (list, tuple)) else selected
        write_csv_export(Path(path), content)
        return True


def _install_navigation_guard(window, window_uid: str) -> None:
    """Block cross-origin navigation before the native bridge can leave the app origin."""
    expected_url = urllib.parse.urlsplit(window.real_url)
    expected = (expected_url.scheme, expected_url.hostname, expected_url.port)
    if sys.platform.startswith("win"):
        from webview.platforms import winforms
        form = winforms.BrowserView.instances.get(window_uid)
        if form is None or getattr(form, "webview", None) is None:
            raise RuntimeError("The expected Edge WebView2 control is unavailable; refusing an unguarded native bridge.")
        def install_edge_callbacks():
            core = getattr(form.webview, "CoreWebView2", None)
            if core is None:
                raise RuntimeError("Edge WebView2 did not initialize; refusing an unguarded native bridge.")
            navigation_starting, new_window_requested = make_edge_navigation_guards(expected)
            form.webview.NavigationStarting += navigation_starting
            core.NewWindowRequested += new_window_requested
            form._rko_navigation_guards = (navigation_starting, new_window_requested)
        form.Invoke(winforms.Func[winforms.Type](install_edge_callbacks))
        return

    from webview.platforms.gtk import BrowserView, webkit
    browser = BrowserView.instances.get(window_uid)
    if browser is None:
        raise RuntimeError("The expected GTK WebView instance is unavailable; refusing an unguarded native bridge.")
    if getattr(browser, "_rko_guard_installed", False):
        return

    def guard(_view, decision, _decision_type):
        if not isinstance(decision, webkit.NavigationPolicyDecision):
            return False
        try:
            request_uri = decision.get_navigation_action().get_request().get_uri()
        except (TypeError, ValueError, AttributeError):
            decision.ignore()
            return True
        if not is_local_navigation(request_uri, expected):
            decision.ignore()
            return True
        return False

    # connect_after follows PyWebView's policy handler: top-level off-origin
    # navigation is denied even when its handler chose no action.
    browser.webview.connect_after("decide-policy", guard)
    browser._rko_guard_installed = True


def _open_positions_warning(runtime: DesktopRuntime) -> str | None:
    return runtime.close_warning()


def _show_windows_webview_error() -> None:
    message = ("RKO could not start its embedded browser. Microsoft Edge WebView2 Runtime is required.\n\n"
        "Check the runtime through Microsoft's official page or your IT support: "
        "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
        "RKO will not download or install it automatically.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "RKO · Paper Research", 0x10)
    except Exception:
        logging.getLogger("rko.desktop").error("RKO requires Microsoft Edge WebView2 Runtime.")


def run_app(*, data_dir: Path, research_dir: Path | None = None, db_path: Path | None = None,
            diagnostic: bool = False, diagnostic_report: Path | None = None,
            diagnostic_timeout_seconds: int = DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS) -> int:
    import webview

    if sys.platform.startswith("win"):
        try:
            from webview.platforms import winforms
            available = require_webview2(winforms._is_chromium)
        except Exception:
            available = False
        if not available:
            _show_windows_webview_error()
            return 1

    data_dir = Path(data_dir).expanduser().resolve()
    lock = InstanceLock(data_dir, restrict_existing=db_path is None)
    lock.acquire()
    runtime = None
    window = None
    import threading
    diagnostic_guard_ready = threading.Event()
    diagnostic_guard_error = {"error": None}
    diagnostic_network = {"requests":[], "responses":[]}
    diagnostic_network_lock = threading.Lock()
    try:
        runtime = DesktopRuntime(data_dir=data_dir, desktop_token=webview.token,
            research_dir=research_dir, db_path=db_path, asset_root=asset_directory(),
            restrict_data_dir=db_path is None, read_only_desktop=diagnostic)
        runtime.start()
        query = "?desktop=1"
        if diagnostic:
            query += "&diagnostic=1"
        url = f"{runtime.url}/{query}#forward-cohort"
        bridge = DesktopBridge()
        window = webview.create_window("RKO · Paper Research", url,
            js_api=bridge, width=1440, height=900, min_size=(760, 640),
            resizable=True, confirm_close=False, background_color="#f5f4ef")
        bridge._window = window

        def on_close_requested():
            try:
                warning = _open_positions_warning(runtime)
            except Exception:
                logging.getLogger("rko.desktop").exception("Could not verify paper-session state before exit")
                return False
            if warning is None:
                return True
            try:
                approved = window.create_confirmation_dialog("Close RKO?", warning)
            except Exception:
                # If confirmation cannot be presented, keep the app running.
                return False
            return bool(approved)

        window.events.closing += on_close_requested

        def on_loaded():
            try:
                _install_navigation_guard(window, window.uid)
            except Exception as exc:
                logging.getLogger("rko.desktop").exception("Failed to install native navigation guard")
                diagnostic_guard_error["error"] = f"Navigation guard failed: {type(exc).__name__}"
                if sys.platform.startswith("win"):
                    _show_windows_webview_error()
                if not diagnostic:
                    window.destroy()
            finally:
                if diagnostic:
                    diagnostic_guard_ready.set()

        window.events.loaded += on_loaded
        if diagnostic:
            def on_request(request):
                item = sanitize_diagnostic_url(getattr(request, "url", None)) | {
                    "method":str(getattr(request, "method", ""))[:12]}
                with diagnostic_network_lock:
                    diagnostic_network["requests"].append(item)

            def on_response(response):
                item = sanitize_diagnostic_url(getattr(response, "url", None)) | {
                    "status":getattr(response, "status_code", None)}
                with diagnostic_network_lock:
                    diagnostic_network["responses"].append(item)

            window.events.request_sent += on_request
            window.events.response_received += on_response
            # Start before the native UI loop. If no WebKit page-load event arrives,
            # the diagnostic still writes a bounded timeout report and quits
            # its owned loop instead of waiting forever.
            start_diagnostic(window, runtime, diagnostic_report,
                guard_ready=diagnostic_guard_ready, guard_error=diagnostic_guard_error,
                network_observations=diagnostic_network, network_lock=diagnostic_network_lock,
                timeout_seconds=diagnostic_timeout_seconds)
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        backend = native_gui_backend()
        try:
            webview.start(gui=backend, debug=False, http_server=False, private_mode=True,
                storage_path=str(data_dir / "webview"))
        except Exception:
            if sys.platform.startswith("win"):
                _show_windows_webview_error()
                return 1
            raise
        return diagnostic_report_exit_code(diagnostic_report) if diagnostic else 0
    finally:
        if runtime:
            runtime.stop()
        lock.close()


def start_diagnostic(window, runtime: DesktopRuntime, report_path: Path | None, *, guard_ready,
                     guard_error: dict[str, str | None], network_observations=None, network_lock=None,
                     timeout_seconds: int = DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS) -> None:
    """Run an offline, read-only embedded-WebKit smoke test; never starts a cohort."""
    import json
    import threading

    def run():
        result = {"schema": "rko-desktop-diagnostic-v1", "read_only_mode":runtime.read_only_desktop,
            "gui_ready":False, "provider_attempts":runtime.provider_attempts}
        try:
            if not guard_ready.wait(timeout_seconds):
                raise TimeoutError(f"Embedded WebKit did not report page-load readiness within {timeout_seconds} seconds.")
            if guard_error.get("error"):
                raise RuntimeError(guard_error["error"])
            if not window.events.loaded.is_set():
                raise RuntimeError("Navigation guard became ready before the WebView loaded.")
            result["gui_ready"] = True
            probe = window.evaluate_js("window.__rkoDesktopProbe ? window.__rkoDesktopProbe() : null")
            if isinstance(probe, str):
                probe = json.loads(probe)
            result["webview"] = probe
            result["url_has_token"] = bool(probe.get("url_has_token")) if isinstance(probe, dict) else None
            forward_state = runtime.server.forward_cohort.state()
            result["forward_state"] = {"state":forward_state.get("state"),
                "session_status":forward_state.get("session_status"),
                "totals":forward_state.get("totals"), "qa_fixture":forward_state.get("qa_fixture")}
            result["provider_attempts"] = runtime.provider_attempts
            result["process_shutdown_requested"] = True
            expected_routes = {"/api/health", "/api/state?mode=demo&symbol=BTC-USD", "/api/research/v4",
                "/api/research/v5", "/api/forward/v5", "/api/export.csv?mode=demo&symbol=BTC-USD",
                "/api/forward/v5/export.csv"}
            results = probe.get("results", {}) if isinstance(probe, dict) else {}
            route_results = {key:value for key,value in results.items() if key.startswith("/api/")}
            valid = bool(runtime.read_only_desktop and probe and set(route_results) == expected_routes and
                result["url_has_token"] is False and probe.get("forward_ready") is True and
                probe.get("hash") == "#forward-cohort" and
                probe.get("active_nav") == "location" and
                all(isinstance(v,dict) and v.get("unauthorized_status") == 403 and v.get("authorized_status") == 200
                    for v in route_results.values()) and
                results.get("post_control",{}).get("unauthorized_status") == 403 and
                results.get("post_control",{}).get("authorized_status") == 403 and
                result["forward_state"].get("session_status") == "not_started" and
                result["provider_attempts"] == 0 and
                result["forward_state"].get("totals",{}).get("all_forward_fills") == 0)
            result["passed"] = valid
            if not valid:
                result["error"] = "A local diagnostic assertion failed."
        except Exception as exc:
            result["passed"] = False
            result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        if network_observations is not None:
            if network_lock is None:
                result["webview_network"] = network_observations
            else:
                with network_lock:
                    result["webview_network"] = {key:list(value)[:100]
                        for key,value in network_observations.items()}
        if report_path:
            path = Path(report_path).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            screenshot = path.with_suffix(".png")
            if _capture_webview_snapshot(window, screenshot):
                import hashlib
                result["screenshot"] = {"file":screenshot.name,
                    "sha256":hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                    "source":"app-owned WebKitGTK WebView.get_snapshot; offline isolated diagnostic"}
            else:
                reason = ("Windows WebView2 screenshot capture is not implemented." if sys.platform.startswith("win")
                    else "WebKitGTK snapshot API unavailable or failed.")
                result["screenshot"] = {"captured":False,"reason":reason}
            path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        # The diagnostic process owns this GUI loop. Schedule both native
        # window destruction and application quit on GTK's main context; a
        # bare Window.destroy can leave Gio.Application.run blocked when the
        # browser never created its WebView.
        _request_diagnostic_shutdown(window)

    threading.Thread(target=run, name="rko-desktop-diagnostic", daemon=True).start()


def _request_diagnostic_shutdown(window) -> None:
    if sys.platform.startswith("win"):
        try:
            from webview.platforms.winforms import BrowserView, WinForms, destroy_window
            window_uid = getattr(window, "uid", None)
            if window_uid in BrowserView.instances:
                destroy_window(window_uid)
            WinForms.Application.Exit()
            return
        except Exception:
            try:
                window.destroy()
            except Exception:
                pass
            return
    try:
        from webview.platforms.gtk import BrowserView, glib, _app
        window_uid = getattr(window, "uid", None)

        def shutdown():
            browser = BrowserView.instances.get(window_uid)
            if browser is not None:
                try:
                    browser.destroy()
                except Exception:
                    pass
            else:
                try:
                    window.destroy()
                except Exception:
                    pass
            if _app is not None:
                _app.quit()
            return False

        glib.idle_add(shutdown)
    except Exception:
        try:
            window.destroy()
        except Exception:
            pass


def _capture_webview_snapshot(window, path: Path) -> bool:
    """Capture only the owned WebKit visible region through GTK's async API."""
    import threading
    if sys.platform.startswith("win"):
        return False
    try:
        from webview.platforms.gtk import BrowserView, glib, webkit
        browser = BrowserView.instances.get(window.uid)
        if browser is None or not hasattr(browser.webview, "get_snapshot"):
            return False
        done = threading.Event()
        outcome = {"ok":False}

        def finished(view, async_result, _user_data=None):
            try:
                surface = view.get_snapshot_finish(async_result)
                surface.write_to_png(str(path))
                outcome["ok"] = True
            except Exception:
                outcome["ok"] = False
            finally:
                done.set()

        def begin():
            browser.webview.get_snapshot(webkit.SnapshotRegion.VISIBLE,
                webkit.SnapshotOptions.NONE, None, finished, None)
            return False

        glib.idle_add(begin)
        return done.wait(5) and bool(outcome["ok"])
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RKO local paper research desktop; simulated only, no exchange orders.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--data-dir", type=Path, help="Use this persistent application data directory (no copying is performed).")
    group.add_argument("--db", type=Path, help="Use this exact existing/new SQLite database path; never copied or migrated automatically.")
    parser.add_argument("--research-dir", type=Path, help="Explicit directory containing compatible saved research; no automatic scan/import.")
    parser.add_argument("--diagnostic", action="store_true", help="Open an offline read-only WebKit diagnostic window and close after checks; never starts a cohort.")
    parser.add_argument("--diagnostic-report", type=Path, help="Write a redacted local diagnostic JSON report.")
    parser.add_argument("--diagnostic-timeout-seconds", type=int, default=DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS,
        help="Bound the diagnostic page-load wait (1-45 seconds; default 30).")
    args = parser.parse_args(argv)
    if args.diagnostic:
        if not args.data_dir or args.db or args.research_dir or not args.diagnostic_report:
            parser.error("--diagnostic requires an empty --data-dir and --diagnostic-report; it cannot use --db or --research-dir.")
        diag_dir = args.data_dir.expanduser().resolve()
        if diag_dir.exists() and any(diag_dir.iterdir()):
            parser.error("--diagnostic requires an empty isolated data directory; existing files are never reused.")
        if not 1 <= args.diagnostic_timeout_seconds <= MAX_DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS:
            parser.error("--diagnostic-timeout-seconds must be between 1 and 45.")
    elif args.diagnostic_timeout_seconds != DIAGNOSTIC_GUI_READY_TIMEOUT_SECONDS:
        parser.error("--diagnostic-timeout-seconds is only valid with --diagnostic.")
    if args.db:
        db = args.db.expanduser().resolve()
        chosen_data = db.parent
    else:
        chosen_data = args.data_dir or default_data_directory()
        db = None
    try:
        return run_app(data_dir=chosen_data, research_dir=args.research_dir, db_path=db,
            diagnostic=args.diagnostic, diagnostic_report=args.diagnostic_report,
            diagnostic_timeout_seconds=args.diagnostic_timeout_seconds)
    except RuntimeError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
