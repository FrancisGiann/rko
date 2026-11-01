#!/usr/bin/env python3
"""Local-only RKO research app. Python 3.10+ standard library, no dependencies."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import Lab
from forward_v5 import ForwardCohort
from research_jobs import ResearchJobs, research_state
from research_v5_view import HistoricalV5Summary

ROOT = Path(__file__).resolve().parent
_SERVER_CONTROL_INIT = threading.Lock()


def desktop_token_matches(supplied: object, expected: object) -> bool:
    """Reject malformed header strings before constant-time ASCII comparison."""
    if not isinstance(supplied, str) or not isinstance(expected, str) or not supplied or not supplied.isascii():
        return False
    try:
        return hmac.compare_digest(supplied, expected)
    except (TypeError, ValueError):
        return False


def handler_for(lab: Lab, research_jobs: ResearchJobs | None = None,
                forward_cohort: ForwardCohort | None = None,
                historical_v5: HistoricalV5Summary | None = None,
                desktop_token: str | None = None,
                static_root: Path | None = None,
                read_only_desktop: bool = False):
    research_jobs = research_jobs or ResearchJobs()
    historical_v5 = historical_v5 or HistoricalV5Summary()
    static_root = Path(static_root or ROOT)
    class Handler(BaseHTTPRequestHandler):
        server_version = "RKO/5.3"

        def allowed_host(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def allowed_api(self, path: str) -> bool:
            if desktop_token is None or not path.startswith("/api/"):
                return True
            supplied = self.headers.get("X-RKO-Desktop-Token", "")
            return desktop_token_matches(supplied, desktop_token)

        def send_bytes(self, code: int, body: bytes, mime: str, *, download: str | None = None):
            self.send_response(code)
            for key, value in {"Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                               "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}.items():
                self.send_header(key, value)
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json(self, code: int, value: object):
            self.send_bytes(code, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self.allowed_host():
                return self.json(403, {"error": "Unexpected local Host header."})
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path.startswith("/api/") and not self.allowed_api(parsed.path):
                return self.json(403, {"error": "Desktop authorization is required."})
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                if parsed.path == "/api/health":
                    return self.json(200, {"ok": True, "simulated_only": True, "job": lab.job,
                        "historical_research_job": research_jobs.snapshot()})
                if parsed.path == "/api/research/v4":
                    return self.json(200, research_state(research_jobs.data_root, research_jobs))
                if parsed.path == "/api/research/v5":
                    return self.json(200, historical_v5.state())
                if parsed.path == "/api/forward/v5":
                    if forward_cohort is None:
                        return self.json(503, {"error": "Forward cohort service is unavailable."})
                    return self.json(200, forward_cohort.state())
                if parsed.path == "/api/forward/v5/export.csv":
                    if forward_cohort is None:
                        return self.json(503, {"error": "Forward cohort service is unavailable."})
                    return self.send_bytes(200, forward_cohort.export_csv().encode(), "text/csv; charset=utf-8",
                        download="SIMULATED-forward-v5.csv")
                if parsed.path in {"/api/state", "/api/export.csv"}:
                    mode, symbol = query.get("mode", ["live"])[0], query.get("symbol", ["BTC-USD"])[0]
                    lab.namespace(mode, symbol)
                    if parsed.path == "/api/state":
                        return self.json(200, lab.state(mode, symbol))
                    return self.send_bytes(200, lab.export(mode, symbol).encode(), "text/csv; charset=utf-8", download=f"SIMULATED-{mode}-{symbol}.csv")
                asset = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/time_utils.js": "time_utils.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}.get(parsed.path)
                if asset is None:
                    return self.json(404, {"error": "Not found."})
                mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}[Path(asset).suffix]
                self.send_bytes(200, (static_root / asset).read_bytes(), mime + "; charset=utf-8")
            except ValueError as exc:
                self.json(400, {"error": str(exc)})
            except Exception:
                self.json(500, {"error": "Local data could not be read. Check the server terminal."})
                raise

        def do_POST(self):
            if not self.allowed_host():
                return self.json(403, {"error": "Unexpected local Host header."})
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/api/") and not self.allowed_api(path):
                return self.json(403, {"error": "Desktop authorization is required."})
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if (origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.json(403, {"error": "Cross-origin changes are blocked."})
            if self.headers.get("X-Crypto-Lab") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.json(403, {"error": "Local JSON control header required."})
            if path not in {"/api/control", "/api/research/v4", "/api/forward/v5"}:
                return self.json(404, {"error": "Not found."})
            if not hasattr(self.server, "_control_lock"):
                with _SERVER_CONTROL_INIT:
                    if not hasattr(self.server, "_control_lock"):
                        self.server._control_lock = threading.RLock()
                        self.server._closing = False
            with self.server._control_lock:
                if self.server._closing:
                    return self.json(503, {"error": "The desktop app is closing; new controls are blocked."})
                if read_only_desktop:
                    return self.json(403, {"error": "This offline diagnostic instance is read-only."})
                self._handle_post(path)

        def _handle_post(self, path: str):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 4096:
                    return self.json(413, {"error": "JSON body must be between 1 and 4096 bytes."})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Control body must be a JSON object.")
                if path == "/api/research/v4":
                    job = research_jobs.start(body.get("action"))
                    return self.json(202, {"ok": True, "job": job})
                if path == "/api/forward/v5":
                    if forward_cohort is None:
                        return self.json(503, {"error": "Forward cohort service is unavailable."})
                    action = body.get("action")
                    if not isinstance(action, str):
                        raise ValueError("Forward action must be a string.")
                    state = forward_cohort.control(action, confirm=body.get("confirm"),
                        duration_minutes=body.get("duration_minutes"), expiry_policy=body.get("expiry_policy"),
                        take_profit_pct=body.get("take_profit_pct"), stop_loss_pct=body.get("stop_loss_pct"))
                    return self.json(202 if action in {"start", "resume"} else 200, {"ok": True, "state": state})
                mode, symbol, action = body.get("mode"), body.get("symbol"), body.get("action")
                if not all(isinstance(v, str) for v in (mode, symbol, action)):
                    raise ValueError("Mode, symbol and action must be strings.")
                if action == "reset" and body.get("confirm") != "RESET":
                    raise ValueError("Reset requires explicit confirmation.")
                message = lab.control(mode, symbol, action)
                self.json(200, {"ok": True, "message": message})
            except (ValueError, UnicodeDecodeError) as exc:
                self.json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.json(409, {"error": str(exc)})
            except Exception:
                self.json(500, {"error": "Local action failed; the transaction was rolled back. Check the terminal."})
                raise

    return Handler


def create_server(db_path: Path, *, port: int = 0, desktop_token: str | None = None,
                  research_root: Path | None = None, asset_root: Path | None = None,
                  forward_path: Path | None = None, read_only_desktop: bool = False):
    """Construct an owned loopback server; port=0 asks the OS for a free port."""
    data_path = Path(db_path)
    data_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if asset_root is None:
        asset_root = ROOT
    if research_root is None:
        from archive_data import DATA_ROOT
        research_root = DATA_ROOT.parent
    research_root = Path(research_root)
    v4_root = research_root / "v4"
    research_jobs = ResearchJobs(v4_root)
    forward_path = Path(forward_path) if forward_path else data_path.parent / "forward_v5" / (data_path.stem + "_cohort.sqlite3")
    forward_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lab = Lab(data_path)
    historical_v5 = HistoricalV5Summary(
        studies=research_root / "v5" / "studies",
        protocol_path=Path(asset_root) / "research_protocol_v5_exploratory.json",
        time_audit_path=research_root / "jesse" / "time-in-market-metric-audit.json")
    forward_cohort = ForwardCohort(forward_path)
    handler = handler_for(lab, research_jobs, forward_cohort, historical_v5,
        desktop_token=desktop_token, static_root=asset_root, read_only_desktop=read_only_desktop)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except BaseException:
        lab.stop.set()
        research_jobs.stop()
        forward_cohort.stop()
        raise
    # HTTP handlers are not allowed to keep a native window alive during
    # shutdown. Mutating controls are serialized/rejected by _control_lock;
    # read-only requests are bounded and their daemon threads die with the
    # owned process. The legacy CLI below keeps its own daemon policy.
    server.daemon_threads = True
    server.block_on_close = False
    server._control_lock = threading.RLock()
    server._closing = False
    return OwnedServer(server, lab, research_jobs, forward_cohort)


class OwnedServer:
    """Lifecycle wrapper for exactly one server and the workers it created."""
    def __init__(self, server, lab, research_jobs, forward_cohort):
        self.server, self.lab, self.research_jobs, self.forward_cohort = server, lab, research_jobs, forward_cohort
        self.thread = None
        self._started = False
        self._stopped = False
        self._lifecycle_lock = threading.Lock()

    @property
    def address(self):
        return self.server.server_address

    @property
    def url(self):
        return f"http://127.0.0.1:{self.address[1]}"

    def start(self):
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("Owned server has already stopped.")
            if self._started:
                raise RuntimeError("Owned server is already running.")
            self.thread = threading.Thread(target=self.server.serve_forever,
                kwargs={"poll_interval": .2}, name="rko-desktop-http", daemon=True)
            self.thread.start()
            self._started = True
        shutdown_event = getattr(self.server, "_BaseServer__is_shut_down", None)
        if shutdown_event is not None:
            deadline = time.monotonic() + 2
            while shutdown_event.is_set() and time.monotonic() < deadline and self.thread.is_alive():
                time.sleep(.005)
            if shutdown_event.is_set():
                self.stop()
                raise RuntimeError("Owned HTTP server did not enter its serve loop.")

    def stop(self):
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
        # Serialize with any control that already began. Later mutations see
        # _closing and are rejected before reaching the paper engines.
        with self.server._control_lock:
            self.server._closing = True
        self.lab.stop.set()
        if self.forward_cohort is not None:
            self.forward_cohort.stop()
        self.research_jobs.stop()
        if self._started and self.thread and self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description="RKO local research and simulated crypto accounts; no exchange orders.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CRYPTO_LAB_PORT", "8765")))
    default_db = ROOT / "data" / "crypto_lab_v5" / "paper.sqlite3"
    parser.add_argument("--db", type=Path, default=default_db)
    parser.add_argument("--legacy-poll", action="store_true", help="Opt in to a 60-second legacy safety-exit poll. It only services explicitly requested exits; it never fetches history or creates signals.")
    parser.add_argument("--no-poll", action="store_true", help="Compatibility alias that disables the legacy price poller.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    if args.no_poll and args.legacy_poll:
        parser.error("--legacy-poll and --no-poll cannot be used together")
    legacy_db = (ROOT / "data" / "crypto_lab.sqlite3").resolve()
    if args.db.resolve() == legacy_db:
        parser.error("The original V3 database is preserved. Choose a new --db path; the default uses data/crypto_lab_v5/paper.sqlite3.")
    database_existed = args.db.exists()
    if args.db.resolve() == default_db.resolve() and not args.db.parent.exists():
        args.db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(args.db.parent, 0o700)
        except OSError:
            pass
    lab = Lab(args.db)
    if not database_existed and args.db.exists():
        try:
            os.chmod(args.db, 0o600)
        except OSError:
            pass
    research_jobs = ResearchJobs()
    forward_dir = args.db.parent / "forward_v5"
    if not forward_dir.exists():
        forward_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(forward_dir, 0o700)
        except OSError:
            pass
    forward_path = forward_dir / (args.db.stem + "_cohort.sqlite3")
    forward_cohort = ForwardCohort(forward_path)
    historical_v5 = HistoricalV5Summary()
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
        handler_for(lab, research_jobs, forward_cohort, historical_v5))
    server.daemon_threads = True
    if args.legacy_poll and not args.no_poll:
        threading.Thread(target=lab.poll, name="public-poller", daemon=True).start()
    print(f"RKO: http://127.0.0.1:{args.port}", flush=True)
    print(f"SIMULATED ONLY · legacy SQLite: {args.db} · separate V5 forward SQLite: {forward_path}", flush=True)
    legacy_poll_status = "on every 60 seconds for requested exits only" if args.legacy_poll and not args.no_poll else "off"
    print(f"Legacy auto-poll is {legacy_poll_status}. V5 polling starts only after explicit cohort Start.", flush=True)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        print("Stopping RKO.")
    finally:
        lab.stop.set()
        research_jobs.stop()
        forward_cohort.stop()
        server.server_close()


if __name__ == "__main__":
    main()
