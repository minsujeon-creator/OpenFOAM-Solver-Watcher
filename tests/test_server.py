from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from email.message import Message
import http.client
import io
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from unittest import TestCase
from unittest.mock import patch
import urllib.error
import urllib.request

from tests.helpers import TemporaryCase
from watcher.server import MAX_BODY_BYTES, SECURITY_HEADERS, create_server, main


@dataclass(frozen=True)
class Response:
    status: int
    headers: Message
    body: bytes


class BlockingCollector:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._state_lock = Lock()
        self.active_calls = 0
        self.maximum_active_calls = 0
        self.calls: list[str] = []

    def snapshot(self) -> dict[str, object]:
        return self._call("snapshot", block=True)

    def series(self, series_id: str, limit: int) -> dict[str, object]:
        del series_id, limit
        return self._call("series")

    def update_config(self, payload: object) -> dict[str, object]:
        del payload
        return self._call("update_config")

    def _call(self, name: str, *, block: bool = False) -> dict[str, object]:
        with self._state_lock:
            self.active_calls += 1
            self.maximum_active_calls = max(
                self.maximum_active_calls,
                self.active_calls,
            )
            self.calls.append(name)
        try:
            if block:
                self.entered.set()
                if not self.release.wait(timeout=2):
                    raise RuntimeError("test collector was not released")
            return {"name": name, "version": 1}
        finally:
            with self._state_lock:
                self.active_calls -= 1


class ServerTests(TestCase):
    def setUp(self) -> None:
        self.case = TemporaryCase()
        self.case.__enter__()
        self.static_directory = TemporaryDirectory()
        self.static_root = Path(self.static_directory.name)
        (self.static_root / "index.html").write_text(
            "<!doctype html><title>Watcher</title>",
            encoding="utf-8",
        )
        (self.static_root / "app.css").write_text(
            "body { color: white; }",
            encoding="utf-8",
        )
        self.static_patch = patch("watcher.server.STATIC_ROOT", self.static_root)
        self.static_patch.start()
        self.server = create_server(self.case.path, 0)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.static_patch.stop()
        self.static_directory.cleanup()
        self.case.__exit__()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return Response(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            try:
                return Response(error.code, error.headers, error.read())
            finally:
                error.close()

    def json_request(self, path: str, **kwargs: object) -> tuple[Response, object]:
        response = self.request(path, **kwargs)
        return response, json.loads(response.body)

    def authorized_headers(self) -> dict[str, str]:
        _, session = self.json_request("/api/session")
        assert isinstance(session, dict)
        return {
            "Origin": self.base_url,
            "Content-Type": "application/json",
            "X-Watcher-Token": str(session["token"]),
        }

    def test_binds_only_to_ipv4_loopback(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_health_has_exact_security_headers_and_no_cors(self) -> None:
        response, payload = self.json_request("/api/health")

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsNone(response.headers["Access-Control-Allow-Origin"])
        for name, value in SECURITY_HEADERS.items():
            with self.subTest(name=name):
                self.assertEqual(response.headers[name], value)

    def test_session_token_contains_32_random_bytes_and_is_per_server(self) -> None:
        response, session = self.json_request("/api/session")
        assert isinstance(session, dict)
        token = str(session["token"])
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

        other = create_server(self.case.path, 0)
        try:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(len(decoded), 32)
            self.assertNotEqual(token, other.session_token)
        finally:
            other.server_close()

    def test_rejects_host_outside_exact_actual_port_allowlist(self) -> None:
        for host in (
            "example.test",
            "127.0.0.1",
            f"127.0.0.1:{self.port + 1}",
            f"LOCALHOST:{self.port}",
        ):
            with self.subTest(host=host):
                response, payload = self.json_request(
                    "/api/health",
                    headers={"Host": host},
                )
                self.assertEqual(response.status, 403)
                self.assertEqual(payload, {"error": "forbidden_host"})

        response = self.request(
            "/api/health",
            headers={"Host": f"localhost:{self.port}"},
        )
        self.assertEqual(response.status, 200)

    def test_config_post_requires_exact_allowed_origin_json_and_token(self) -> None:
        payload = json.dumps({"version": 1}).encode("utf-8")
        valid = self.authorized_headers()
        invalid_headers = (
            {},
            {**valid, "Origin": f"http://localhost:{self.port + 1}"},
            {**valid, "Origin": f"HTTP://127.0.0.1:{self.port}"},
            {**valid, "X-Watcher-Token": valid["X-Watcher-Token"] + "x"},
            {**valid, "Content-Type": "text/plain"},
            {**valid, "Content-Type": "application/json; charset=utf-8"},
        )
        for headers in invalid_headers:
            with self.subTest(headers=headers):
                response = self.request(
                    "/api/config",
                    method="POST",
                    body=payload,
                    headers=headers,
                )
                self.assertEqual(response.status, 403 if (
                    headers.get("Origin") not in {
                        self.base_url,
                        f"http://localhost:{self.port}",
                    }
                    or headers.get("X-Watcher-Token")
                    != valid["X-Watcher-Token"]
                ) else 415)

        response, configuration = self.json_request(
            "/api/config",
            method="POST",
            body=payload,
            headers=valid,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(configuration["version"], 1)  # type: ignore[index]
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_config_body_limit_checks_declared_and_read_size(self) -> None:
        headers = self.authorized_headers()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("POST", "/api/config")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        raw_response = connection.getresponse()
        response = Response(
            raw_response.status,
            raw_response.headers,
            raw_response.read(),
        )
        connection.close()
        payload = json.loads(response.body)
        self.assertEqual(response.status, 413)
        self.assertEqual(payload, {"error": "payload_too_large"})

        at_limit, payload = self.json_request(
            "/api/config",
            method="POST",
            body=b" " * MAX_BODY_BYTES,
            headers=headers,
        )
        self.assertEqual(at_limit.status, 400)
        self.assertEqual(payload, {"error": "malformed_json"})
        self.assertFalse((self.case.path / ".foam-watcher.json").exists())

    def test_content_length_rejects_non_ascii_and_implausibly_long_values(self) -> None:
        headers = self.authorized_headers()
        malformed = ("+1", "-1", "1x", "²", "9" * 5_000)
        for value in malformed:
            with self.subTest(value=value[:20]):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    self.port,
                    timeout=2,
                )
                connection.putrequest("POST", "/api/config")
                for name, header_value in headers.items():
                    connection.putheader(name, header_value)
                connection.putheader("Content-Length", value)
                connection.endheaders()
                raw = connection.getresponse()
                body = raw.read()
                connection.close()

                self.assertEqual(raw.status, 400)
                self.assertEqual(json.loads(body), {"error": "invalid_content_length"})
                self.assertLess(len(body), 100)
                self.assertNotIn(b"Traceback", body)

    def test_truncated_config_body_times_out_with_safe_json(self) -> None:
        headers = self.authorized_headers()
        with patch("watcher.server.CONNECTION_TIMEOUT_SECONDS", 0.1):
            connection = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            connection.settimeout(2)
            request = (
                "POST /api/config HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Origin: {headers['Origin']}\r\n"
                "Content-Type: application/json\r\n"
                f"X-Watcher-Token: {headers['X-Watcher-Token']}\r\n"
                "Content-Length: 10\r\n"
                "\r\n"
            ).encode("ascii") + b"{}"
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(4_096)
                if not chunk:
                    break
                response += chunk
            connection.close()

        status_line, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b" 408 ", status_line)
        self.assertEqual(json.loads(body), {"error": "request_timeout"})
        self.assertNotIn(b"Traceback", body)

    def test_collector_calls_from_concurrent_handlers_are_serialized(self) -> None:
        collector = BlockingCollector()
        self.server.collector = collector
        responses: list[Response] = []

        snapshot = Thread(
            target=lambda: responses.append(self.request("/api/snapshot")),
            daemon=True,
        )
        snapshot.start()
        self.assertTrue(collector.entered.wait(timeout=2))

        series = Thread(
            target=lambda: responses.append(self.request("/api/series?id=x")),
            daemon=True,
        )
        config = Thread(
            target=lambda: responses.append(
                self.request(
                    "/api/config",
                    method="POST",
                    body=b'{"version":1}',
                    headers=self.authorized_headers(),
                )
            ),
            daemon=True,
        )
        series.start()
        config.start()
        with self.server.active_handlers_condition:
            handlers_started = self.server.active_handlers_condition.wait_for(
                lambda: self.server.active_handler_count >= 3,
                timeout=2,
            )

        self.assertTrue(handlers_started)
        self.assertEqual(collector.calls, ["snapshot"])
        self.assertEqual(collector.maximum_active_calls, 1)
        collector.release.set()
        for thread in (snapshot, series, config):
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(response.status for response in responses), [200, 200, 200])
        self.assertEqual(
            sorted(collector.calls),
            ["series", "snapshot", "update_config"],
        )
        self.assertEqual(collector.maximum_active_calls, 1)

    def test_handler_cap_rejects_overload_and_releases_slot(self) -> None:
        collector = BlockingCollector()
        with patch("watcher.server.MAX_CONCURRENT_HANDLERS", 1):
            server = create_server(self.case.path, 0)
        server.collector = collector
        port = server.server_address[1]
        base_url = f"http://127.0.0.1:{port}"
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        first_response: list[Response] = []

        def request_at(path: str) -> Response:
            request = urllib.request.Request(base_url + path)
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return Response(response.status, response.headers, response.read())
            except urllib.error.HTTPError as error:
                try:
                    return Response(error.code, error.headers, error.read())
                finally:
                    error.close()

        first = Thread(
            target=lambda: first_response.append(request_at("/api/snapshot")),
            daemon=True,
        )
        first.start()
        self.assertTrue(collector.entered.wait(timeout=2))
        try:
            overloaded = request_at("/api/health")
            self.assertEqual(overloaded.status, 503)
            self.assertEqual(
                json.loads(overloaded.body),
                {"error": "server_busy"},
            )

            collector.release.set()
            first.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertEqual(first_response[0].status, 200)

            released = request_at("/api/health")
            self.assertEqual(released.status, 200)
        finally:
            collector.release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_malformed_and_invalid_config_errors_are_safe_json(self) -> None:
        headers = self.authorized_headers()
        cases = (
            (b"{broken", "malformed_json"),
            (b'{"version":2}', "invalid_configuration"),
            (b'{"version":1,"selectedSeries":["missing"]}', "invalid_configuration"),
        )
        for body, error_name in cases:
            with self.subTest(body=body):
                response, payload = self.json_request(
                    "/api/config",
                    method="POST",
                    body=body,
                    headers=headers,
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(payload, {"error": error_name})
                self.assertNotIn(b"Traceback", response.body)
                self.assertNotIn(str(self.case.path).encode(), response.body)

    def test_snapshot_and_series_errors_use_correct_statuses(self) -> None:
        snapshot_response, snapshot = self.json_request("/api/snapshot")
        self.assertEqual(snapshot_response.status, 200)
        self.assertIsInstance(snapshot, dict)

        cases = (
            ("/api/series", 400, "missing_series_id"),
            ("/api/series?id=unknown", 404, "unknown_series"),
            ("/api/series?id=unknown&limit=0", 400, "invalid_limit"),
            ("/api/series?id=unknown&limit=abc", 400, "invalid_limit"),
            ("/api/series?id=one&id=two", 400, "invalid_query"),
        )
        for path, status, error_name in cases:
            with self.subTest(path=path):
                response, payload = self.json_request(path)
                self.assertEqual(response.status, status)
                self.assertEqual(payload, {"error": error_name})
                self.assertNotIn(b"Traceback", response.body)

    def test_rejects_traversal_after_decoding_and_canonical_resolution(self) -> None:
        for path in (
            "/..%2f..%2fetc%2fpasswd",
            "/%2e%2e/outside.txt",
            "/..%5coutside.txt",
        ):
            with self.subTest(path=path):
                response, payload = self.json_request(path)
                self.assertEqual(response.status, 403)
                self.assertEqual(payload, {"error": "forbidden_path"})

    def test_static_files_only_come_from_resolved_static_root_with_cache_policy(self) -> None:
        index = self.request("/")
        asset = self.request("/app.css")
        missing = self.request("/missing.js")

        self.assertEqual(index.status, 200)
        self.assertEqual(index.headers["Content-Type"], "text/html")
        self.assertEqual(index.headers["Cache-Control"], "no-store")
        self.assertEqual(asset.status, 200)
        self.assertEqual(asset.headers["Cache-Control"], "public, max-age=300")
        self.assertEqual(missing.status, 404)

    def test_unknown_routes_and_unsupported_methods_are_safe(self) -> None:
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                response = self.request("/api/health", method=method)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.headers["Allow"], "GET, POST")
                self.assertNotIn(b"Traceback", response.body)

        response, payload = self.json_request("/api/unknown")
        self.assertEqual(response.status, 404)
        self.assertEqual(payload, {"error": "not_found"})

        response, payload = self.json_request(
            "/api/health",
            method="PUT",
            headers={"Host": "example.test"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(payload, {"error": "forbidden_host"})


class CliTests(TestCase):
    def test_rejects_unsupported_python_before_server_creation(self) -> None:
        stderr = io.StringIO()
        with patch("watcher.server.sys.version_info", (3, 9, 9)):
            with redirect_stderr(stderr):
                result = main([])

        self.assertEqual(result, 2)
        self.assertIn("Python 3.10", stderr.getvalue())

    def test_rejects_non_case_and_has_no_host_option(self) -> None:
        with TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main(["--case", directory])

        self.assertEqual(result, 2)
        self.assertIn("OpenFOAM case", stderr.getvalue())
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--host", "0.0.0.0"])

    def test_requires_control_dict_even_when_case_markers_exist(self) -> None:
        class InterruptingServer:
            server_address = ("127.0.0.1", 8765)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        with TemporaryCase() as case:
            stderr = io.StringIO()
            with (
                patch(
                    "watcher.server.create_server",
                    return_value=InterruptingServer(),
                ),
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["--case", str(case.path)])

        self.assertEqual(result, 2)
        self.assertIn("controlDict", stderr.getvalue())

    def test_accepts_time_only_cases_with_robust_numeric_names(self) -> None:
        class InterruptingServer:
            server_address = ("127.0.0.1", 8765)

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                pass

        for time_name in ("0", "1e-06", ".5", "-2.0E+3"):
            with self.subTest(time_name=time_name):
                with TemporaryDirectory() as directory:
                    case = Path(directory)
                    (case / "system").mkdir()
                    (case / "system" / "controlDict").write_text(
                        "application simpleFoam;\n",
                        encoding="utf-8",
                    )
                    (case / time_name).mkdir()
                    with (
                        patch(
                            "watcher.server.create_server",
                            return_value=InterruptingServer(),
                        ),
                        redirect_stdout(io.StringIO()),
                    ):
                        result = main(["--case", str(case)])
                self.assertEqual(result, 0)

        for invalid_name in ("nan", "1.2.3", "0.old"):
            with self.subTest(invalid_name=invalid_name):
                with TemporaryDirectory() as directory:
                    case = Path(directory)
                    (case / "system").mkdir()
                    (case / "system" / "controlDict").write_text(
                        "application simpleFoam;\n",
                        encoding="utf-8",
                    )
                    (case / invalid_name).mkdir()
                    with redirect_stderr(io.StringIO()):
                        result = main(["--case", str(case)])
                self.assertEqual(result, 2)

    def test_prints_url_and_ssh_command_and_handles_ctrl_c(self) -> None:
        class InterruptingServer:
            server_address = ("127.0.0.1", 8765)

            def __init__(self) -> None:
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        fake_server = InterruptingServer()
        output = io.StringIO()
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            with (
                patch("watcher.server.create_server", return_value=fake_server),
                patch("watcher.server.socket.gethostname", return_value="solver-host"),
                patch("watcher.server.getpass.getuser", return_value="analyst"),
                redirect_stdout(output),
            ):
                result = main(["--case", str(case.path), "--port", "8765"])

        self.assertEqual(result, 0)
        self.assertTrue(fake_server.closed)
        self.assertIn("http://127.0.0.1:8765", output.getvalue())
        self.assertIn(
            "ssh -L 8765:127.0.0.1:8765 analyst@solver-host",
            output.getvalue(),
        )

    def test_occupied_port_distinguishes_watcher_from_other_service(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            watcher = create_server(case.path, 0)
            thread = Thread(target=watcher.serve_forever, daemon=True)
            thread.start()
            occupied_port = watcher.server_address[1]
            stderr = io.StringIO()
            try:
                with redirect_stderr(stderr):
                    result = main(["--case", str(case.path), "--port", str(occupied_port)])
            finally:
                watcher.shutdown()
                watcher.server_close()
                thread.join(timeout=2)

        self.assertEqual(result, 2)
        self.assertIn("already running", stderr.getvalue())
        self.assertIn(f"http://127.0.0.1:{occupied_port}", stderr.getvalue())

        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            occupied = socket.socket()
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            occupied_port = occupied.getsockname()[1]
            stderr = io.StringIO()
            try:
                with redirect_stderr(stderr):
                    result = main(["--case", str(case.path), "--port", str(occupied_port)])
            finally:
                occupied.close()

        self.assertEqual(result, 2)
        self.assertIn("not the watcher", stderr.getvalue())
        self.assertIn("Try --port", stderr.getvalue())
