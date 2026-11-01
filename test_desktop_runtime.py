from __future__ import annotations

import json
import os
import sqlite3
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from desktop_app import is_local_navigation, validate_external_url, write_csv_export
from desktop_runtime import DesktopRuntime, InstanceLock, default_data_directory
from forward_v5 import ForwardCohort
from server import create_server, desktop_token_matches

ROOT = Path(__file__).resolve().parent


def request(url: str, *, token: str | None = None, method: str = "GET", body: dict | None = None):
    headers = {}
    data = None
    if token:
        headers["X-RKO-Desktop-Token"] = token
    if body is not None:
        headers.update({"Content-Type": "application/json", "X-Crypto-Lab": "1"})
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.headers.get("Content-Type"), exc.read()
        finally:
            exc.close()


class DesktopPathAndLockTests(unittest.TestCase):
    def test_default_paths_are_platform_specific(self):
        home = Path("/home/tester")
        self.assertEqual(default_data_directory(platform="linux", environ={"XDG_DATA_HOME":"/private/data"}, home=home), Path("/private/data/RKO"))
        self.assertEqual(default_data_directory(platform="linux", environ={}, home=home), home / ".local/share/RKO")
        self.assertEqual(default_data_directory(platform="win32", environ={"LOCALAPPDATA":"C:/Users/a/AppData/Local"}, home=home), Path("C:/Users/a/AppData/Local/RKO"))

    def test_only_one_process_lock_per_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "private"
            first = InstanceLock(data)
            second = InstanceLock(data)
            first.acquire()
            with self.assertRaisesRegex(RuntimeError, "already open"):
                second.acquire()
            first.close()
            third = InstanceLock(data)
            third.acquire()
            third.close()

    def test_external_link_validation_is_https_only_and_rejects_credentials(self):
        self.assertEqual(validate_external_url("https://www.coingecko.com/en/api_terms"), "https://www.coingecko.com/en/api_terms")
        for value in ("http://example.com", "file:///etc/passwd", "https://user:pass@example.com/", "https://example.com:444", "javascript:alert(1)", " https://example.com"):
            with self.subTest(value=value):
                self.assertIsNone(validate_external_url(value))
        origin=("http","127.0.0.1",43210)
        self.assertTrue(is_local_navigation("http://127.0.0.1:43210/",origin))
        self.assertFalse(is_local_navigation("https://example.com/",origin))
        self.assertFalse(is_local_navigation("http://127.0.0.1:43211/",origin))
        self.assertFalse(is_local_navigation("http://[invalid/",origin))

    def test_csv_export_writer_is_bounded_and_writes_only_explicit_regular_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"SIMULATED-demo-BTC-USD.csv"
            self.assertEqual(write_csv_export(target,"side,price\nBUY,1.25\n"),target)
            self.assertEqual(target.read_text(),"side,price\nBUY,1.25\n")
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777,0o600)
            link=Path(tmp)/"alias.csv"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError,"regular CSV"):
                write_csv_export(link,"side\n")
            with self.assertRaisesRegex(ValueError,"25 MB"):
                write_csv_export(Path(tmp)/"large.csv","x"*(25_000_001))

    def test_desktop_token_rejects_non_ascii_header_values(self):
        self.assertTrue(desktop_token_matches("a"*32,"a"*32))
        self.assertFalse(desktop_token_matches("é"+"a"*31,"a"*32))
        self.assertFalse(desktop_token_matches(None,"a"*32))


class DesktopLoopbackSecurityTests(unittest.TestCase):
    def test_default_research_root_does_not_duplicate_v4_component(self):
        import archive_data
        with tempfile.TemporaryDirectory() as tmp:
            owned=create_server(Path(tmp)/"paper.sqlite3",desktop_token="f"*32,asset_root=ROOT)
            try:
                self.assertEqual(owned.research_jobs.data_root,archive_data.DATA_ROOT)
            finally:
                owned.stop()

    def test_owned_server_can_stop_before_start_and_repeatedly(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned=create_server(Path(tmp)/"paper.sqlite3",desktop_token="z"*32,
                research_root=Path(tmp)/"research",asset_root=ROOT)
            owned.stop()
            owned.stop()
            self.assertFalse(owned.thread)

    def test_ephemeral_loopback_readiness_and_all_private_routes_require_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "a" * 32
            runtime = DesktopRuntime(data_dir=root / "data", desktop_token=token,
                asset_root=ROOT, research_dir=root / "research")
            try:
                runtime.start()
                self.assertEqual(runtime.port, runtime.server.address[1])
                self.assertEqual(runtime.server.address[0], "127.0.0.1")
                routes = [
                    "/api/health", "/api/state?mode=demo&symbol=BTC-USD",
                    "/api/research/v4", "/api/research/v5", "/api/forward/v5",
                    "/api/export.csv?mode=demo&symbol=BTC-USD", "/api/forward/v5/export.csv",
                ]
                for path in routes:
                    with self.subTest(path=path):
                        self.assertEqual(request(runtime.url + path)[0], 403)
                        status, content_type, body = request(runtime.url + path, token=token)
                        self.assertEqual(status, 200)
                        self.assertTrue(content_type)
                        self.assertNotIn(token.encode(), body)
                self.assertEqual(request(runtime.url + "/", token=None)[0], 200)
                self.assertEqual(request(runtime.url + "/api/health", token="b" * 32)[0], 403)
                post = {"mode":"demo", "symbol":"BTC-USD", "action":"invalid"}
                self.assertEqual(request(runtime.url + "/api/control", method="POST", body=post)[0], 403)
                status, _, _ = request(runtime.url + "/api/control", token=token, method="POST", body=post)
                self.assertEqual(status, 400)
                self.assertIsNone(runtime.close_warning())
                report = runtime.server.forward_cohort.state()
                self.assertEqual(report["session_status"], "not_started")
                self.assertEqual(report["totals"]["all_forward_fills"], 0)
            finally:
                runtime.stop()

    def test_port_collision_does_not_take_over_existing_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = create_server(Path(tmp) / "first.sqlite3", port=0, desktop_token="x" * 32,
                research_root=Path(tmp) / "research", asset_root=ROOT)
            try:
                occupied = first.address[1]
                # Binding the second listener to the occupied port must fail;
                # the runtime never searches for or terminates that process.
                with self.assertRaises(OSError):
                    create_server(Path(tmp) / "second.sqlite3", port=occupied, desktop_token="y" * 32,
                        research_root=Path(tmp) / "research2", asset_root=ROOT)
            finally:
                first.server.server_close()
                first.lab.stop.set()
                first.research_jobs.stop()
                first.forward_cohort.stop()

    def test_owned_shutdown_does_not_wait_on_a_stuck_read_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned = create_server(Path(tmp) / "paper.sqlite3", port=0, desktop_token="h" * 32,
                research_root=Path(tmp) / "research", asset_root=ROOT)
            entered, release = threading.Event(), threading.Event()
            handler = owned.server.RequestHandlerClass
            original = handler.do_GET

            def blocked_read(request_handler):
                if request_handler.path == "/":
                    entered.set()
                    release.wait(5)
                return original(request_handler)

            handler.do_GET = blocked_read
            connection = None
            try:
                self.assertTrue(owned.server.daemon_threads)
                self.assertFalse(owned.server.block_on_close)
                owned.start()
                connection = socket.create_connection(owned.address, timeout=2)
                connection.sendall(f"GET / HTTP/1.0\r\nHost: 127.0.0.1:{owned.address[1]}\r\n\r\n".encode())
                self.assertTrue(entered.wait(2), "test read handler did not block")
                began = time.monotonic()
                owned.stop()
                self.assertLess(time.monotonic() - began, 1.0,
                    "owned shutdown must not wait for a hung read-only HTTP handler")
            finally:
                release.set()
                if connection is not None:
                    connection.close()
                handler.do_GET = original
                owned.stop()

    def test_shutdown_preserves_unfilled_pending_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = DesktopRuntime(data_dir=root / "data", desktop_token="c" * 32,
                research_dir=root / "research", asset_root=ROOT)
            runtime.start()
            cohort = runtime.server.forward_cohort
            quote_attempts = []
            cohort.fetch_quotes = lambda: quote_attempts.append("called")
            with cohort.db() as db:
                row = cohort._latest(db)
                db.execute("INSERT INTO accounts(cohort_id,symbol,strategy,initial,cash,qty,basis,peak_nlv,exit_requested,exit_reason,exit_priority,exit_decided_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["id"],"BTC-USD","passive",10_000,0,1,100,10_000,1,"take_profit",20,1234567890,1234567890))
                db.execute("UPDATE cohorts SET state='protection_pending',session_status='paused' WHERE id=?", (row["id"],))
            self.assertTrue(runtime.has_open_or_pending_positions())
            self.assertIn("No simulated close", runtime.close_warning())
            runtime.stop()
            self.assertEqual(quote_attempts, [])
            with sqlite3.connect(root / "data" / "forward_v5" / "paper_cohort.sqlite3") as db:
                saved = db.execute("SELECT qty,exit_requested,exit_decided_at FROM accounts WHERE symbol='BTC-USD' AND strategy='passive'").fetchone()
            self.assertEqual(saved, (1.0, 1, 1234567890.0))
            with sqlite3.connect(root / "data" / "forward_v5" / "paper_cohort.sqlite3") as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fills").fetchone()[0], 0)

    def test_offline_diagnostic_server_rejects_all_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=DesktopRuntime(data_dir=Path(tmp)/"data",desktop_token="d"*32,
                research_dir=Path(tmp)/"research",asset_root=ROOT,read_only_desktop=True)
            try:
                runtime.start()
                response=request(runtime.url+"/api/forward/v5",token="d"*32,method="POST",
                    body={"action":"start","confirm":"START","duration_minutes":1,"expiry_policy":"CLOSE"})
                self.assertEqual(response[0],403)
                self.assertEqual(runtime.server.forward_cohort.state()["session_status"],"not_started")
                self.assertEqual(runtime.provider_attempts,0)
            finally:
                runtime.stop()

    def test_closing_gate_rejects_a_late_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=DesktopRuntime(data_dir=Path(tmp)/"data",desktop_token="g"*32,
                research_dir=Path(tmp)/"research",asset_root=ROOT)
            runtime.start()
            runtime.server.server._closing=True
            response=request(runtime.url+"/api/forward/v5",token="g"*32,method="POST",
                body={"action":"start","confirm":"START","duration_minutes":1,"expiry_policy":"CLOSE"})
            self.assertEqual(response[0],503)
            self.assertEqual(runtime.server.forward_cohort.state()["session_status"],"not_started")
            runtime.stop()

    def test_shutdown_warning_describes_active_flat_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=DesktopRuntime(data_dir=Path(tmp)/"data",desktop_token="e"*32,
                research_dir=Path(tmp)/"research",asset_root=ROOT)
            deadline=time.time()+3600
            try:
                runtime.start()
                runtime.server.forward_cohort.fetch_quotes=lambda: (_ for _ in ()).throw(RuntimeError("disabled test feed"))
                runtime.server.forward_cohort.fetch_daily=lambda: (_ for _ in ()).throw(RuntimeError("disabled test feed"))
                with runtime.server.forward_cohort.db() as db:
                    row=runtime.server.forward_cohort._latest(db)
                    db.execute("UPDATE cohorts SET state='running',session_status='active',session_deadline_at=? WHERE id=?",
                        (deadline,row["id"]))
                warning=runtime.close_warning()
                self.assertIn("active and flat",warning)
                self.assertIn("deadline still advances",warning)
                self.assertNotIn("Open holdings",warning)
            finally:
                runtime.stop()
            with runtime.server.forward_cohort.db() as db:
                row=runtime.server.forward_cohort._latest(db)
                self.assertEqual(row["session_status"],"paused")
                self.assertEqual(row["session_deadline_at"],deadline)


class FrozenFingerprintTests(unittest.TestCase):
    def test_frozen_forward_config_uses_embedded_fingerprint_and_rejects_missing(self):
        import desktop_build_info
        from forward_v5 import ForwardCohort
        with patch.object(desktop_build_info, "SOURCE_SHA256", {"forward_v5.py":"a" * 64}), \
             patch("forward_v5.sys.frozen", True, create=True):
            self.assertEqual(ForwardCohort._config()["implementation_sha256"], {"forward_v5.py":"a" * 64})
        with patch.object(desktop_build_info, "SOURCE_SHA256", {}), \
             patch("forward_v5.sys.frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "fingerprint is invalid"):
                ForwardCohort._config()

    def test_frozen_manifest_rejects_source_drift(self):
        from desktop_runtime import _desktop_build_manifest
        import desktop_build_info
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "desktop_build_manifest.json").write_text(json.dumps({
                "schema":"rko-desktop-build-v1", "source_sha256":{"server.py":"a" * 64}}))
            with patch.object(desktop_build_info, "SOURCE_SHA256", {"server.py":"b" * 64}), \
                 patch("desktop_runtime.sys.frozen", True, create=True):
                with self.assertRaisesRegex(RuntimeError, "do not match"):
                    _desktop_build_manifest(root)


if __name__ == "__main__":
    unittest.main()
