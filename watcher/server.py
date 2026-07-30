from __future__ import annotations

import argparse
import getpass
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import shlex
import socket
import sys
from threading import BoundedSemaphore, Condition, RLock
from typing import Mapping, NoReturn, Sequence, cast
from urllib.parse import parse_qs, unquote, urlsplit
import urllib.error
import urllib.request

from watcher.persistence import ConfigValidationError, UnsafeConfigPath
from watcher.snapshot import WatcherCollector


MAX_BODY_BYTES = 65_536
MAX_CONCURRENT_HANDLERS = 32
CONNECTION_TIMEOUT_SECONDS = 5.0
STATIC_ROOT = (Path(__file__).resolve().parent.parent / "static").resolve()
SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_HEX_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ASCII_DECIMAL = re.compile(r"[0-9]+")
_TIME_DIRECTORY = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_MAX_CONTENT_LENGTH_DIGITS = 20


class _WatcherHandler(BaseHTTPRequestHandler):
    server_version = "OpenFOAMWatcher/0.1"
    sys_version = ""

    def do_GET(self) -> None:
        if not self._valid_host():
            self._json_response(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        parsed = self._parsed_target()
        if parsed is None:
            return
        path, query = parsed
        if path == "/api/health":
            self._json_response(HTTPStatus.OK, {"ok": True})
        elif path == "/api/session":
            self._json_response(
                HTTPStatus.OK,
                {"token": self._watcher_server.session_token},
            )
        elif path == "/api/snapshot":
            try:
                with self._watcher_server.collector_lock:
                    snapshot = self._watcher_server.collector.snapshot()
            except Exception:
                self._json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "snapshot_unavailable"},
                )
            else:
                self._json_response(HTTPStatus.OK, snapshot)
        elif path == "/api/series":
            self._serve_series(query)
        elif path.startswith("/api/"):
            self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        if not self._valid_host():
            self._json_response(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        parsed = self._parsed_target()
        if parsed is None:
            return
        path, query = parsed
        if path != "/api/config" or query:
            self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._valid_write_authorization():
            self._discard_small_declared_body()
            self._json_response(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if self.headers.get("Content-Type") != "application/json":
            self._discard_small_declared_body()
            self._json_response(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "unsupported_media_type"},
            )
            return
        body = self._read_request_body()
        if body is None:
            return
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "malformed_json"},
            )
            return
        try:
            with self._watcher_server.collector_lock:
                configuration = self._watcher_server.collector.update_config(payload)
        except (ConfigValidationError, KeyError, ValueError):
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_configuration"},
            )
        except UnsafeConfigPath:
            self._json_response(
                HTTPStatus.CONFLICT,
                {"error": "configuration_write_refused"},
            )
        except OSError:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "configuration_write_failed"},
            )
        except Exception:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "configuration_unavailable"},
            )
        else:
            self._json_response(HTTPStatus.OK, configuration)

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def do_CONNECT(self) -> None:
        self._method_not_allowed()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._method_not_allowed()
        else:
            self._json_response(code, {"error": "request_failed"})

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    @property
    def _watcher_server(self) -> "_ServerState":
        return cast("_ServerState", self.server)

    def _valid_host(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        return len(hosts) == 1 and hosts[0] in self._watcher_server.allowed_hosts

    def _parsed_target(self) -> tuple[str, str] | None:
        try:
            split = urlsplit(self.path)
            raw_path = split.path
            _validate_percent_escapes(raw_path)
            path = unquote(raw_path, encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError):
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid_target"})
            return None
        return path, split.query

    def _valid_write_authorization(self) -> bool:
        origins = self.headers.get_all("Origin", [])
        tokens = self.headers.get_all("X-Watcher-Token", [])
        return (
            len(origins) == 1
            and origins[0] in self._watcher_server.allowed_origins
            and len(tokens) == 1
            and hmac.compare_digest(tokens[0], self._watcher_server.session_token)
        )

    def _read_request_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "unsupported_transfer_encoding"},
            )
            return None
        declared, error = self._declared_content_length()
        if error is not None:
            self._json_response(
                (
                    HTTPStatus.LENGTH_REQUIRED
                    if error == "content_length_required"
                    else HTTPStatus.BAD_REQUEST
                ),
                {"error": error},
            )
            return None
        assert declared is not None
        if declared > MAX_BODY_BYTES:
            self._json_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "payload_too_large"},
            )
            return None
        try:
            body = self.rfile.read(declared)
        except OSError:
            self._json_response(
                HTTPStatus.REQUEST_TIMEOUT,
                {"error": "request_timeout"},
            )
            return None
        if len(body) != declared:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "incomplete_body"},
            )
            return None
        if len(body) > MAX_BODY_BYTES:
            self._json_response(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "payload_too_large"},
            )
            return None
        return body

    def _discard_small_declared_body(self) -> None:
        length, error = self._declared_content_length()
        if error is not None or length is None or length > MAX_BODY_BYTES:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.1)
            self.rfile.read(length)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _declared_content_length(self) -> tuple[int | None, str | None]:
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            return None, "content_length_required"
        if len(lengths) != 1:
            return None, "invalid_content_length"
        value = lengths[0]
        if (
            not value
            or len(value) > _MAX_CONTENT_LENGTH_DIGITS
            or _ASCII_DECIMAL.fullmatch(value) is None
        ):
            return None, "invalid_content_length"
        return int(value), None

    def _serve_series(self, query: str) -> None:
        try:
            parameters = parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid_query"})
            return
        if set(parameters) - {"id", "limit"} or any(
            len(values) != 1 for values in parameters.values()
        ):
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid_query"})
            return
        series_ids = parameters.get("id")
        if series_ids is None or not series_ids[0]:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "missing_series_id"},
            )
            return
        limit = 2_000
        if "limit" in parameters:
            raw_limit = parameters["limit"][0]
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 0
            if str(limit) != raw_limit or limit <= 0:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_limit"},
                )
                return
        try:
            with self._watcher_server.collector_lock:
                series = self._watcher_server.collector.series(series_ids[0], limit)
        except KeyError:
            self._json_response(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_series"},
            )
        except ValueError:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_limit"},
            )
        except Exception:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "series_unavailable"},
            )
        else:
            self._json_response(HTTPStatus.OK, series)

    def _serve_static(self, decoded_path: str) -> None:
        root = self._watcher_server.static_root
        relative = decoded_path.lstrip("/\\") or "index.html"
        try:
            target = (root / relative).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            self._json_response(HTTPStatus.FORBIDDEN, {"error": "forbidden_path"})
            return
        if not target.is_file():
            self._json_response(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            content = target.read_bytes()
        except OSError:
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "asset_unavailable"},
            )
            return
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._byte_response(
            HTTPStatus.OK,
            content,
            content_type=media_type,
            cache_control=(
                "no-store"
                if target.name == "index.html"
                else "public, max-age=300"
            ),
        )

    def _method_not_allowed(self) -> None:
        if not self._valid_host():
            self._json_response(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        self._json_response(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method_not_allowed"},
            extra_headers={"Allow": "GET, POST"},
        )

    def _json_response(
        self,
        status: int,
        payload: object,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            content = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            content = b'{"error":"response_unavailable"}'
        self._byte_response(
            status,
            content,
            content_type="application/json",
            cache_control="no-store",
            extra_headers=extra_headers,
        )

    def _byte_response(
        self,
        status: int,
        content: bytes,
        *,
        content_type: str,
        cache_control: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Connection", "close")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass


class _ServerState(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    collector: WatcherCollector
    collector_lock: RLock
    session_token: str
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    static_root: Path
    active_handlers_condition: Condition
    active_handler_count: int

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
    ) -> None:
        self.collector_lock = RLock()
        self._handler_slots = BoundedSemaphore(MAX_CONCURRENT_HANDLERS)
        self.active_handlers_condition = Condition()
        self.active_handler_count = 0
        super().__init__(server_address, request_handler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(CONNECTION_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        if not self._handler_slots.acquire(blocking=False):
            self._reject_overload(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        with self.active_handlers_condition:
            self.active_handler_count += 1
            self.active_handlers_condition.notify_all()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.active_handlers_condition:
                self.active_handler_count -= 1
                self.active_handlers_condition.notify_all()
            self._handler_slots.release()

    def _reject_overload(self, request: socket.socket) -> None:
        body = b'{"error":"server_busy"}'
        headers = [
            "HTTP/1.0 503 Service Unavailable",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        headers.extend(f"{name}: {value}" for name, value in SECURITY_HEADERS.items())
        response = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
        try:
            self._drain_overload_headers(request)
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    @staticmethod
    def _drain_overload_headers(request: socket.socket) -> None:
        request.settimeout(0.1)
        received = b""
        while len(received) < 8_192 and b"\r\n\r\n" not in received:
            chunk = request.recv(min(1_024, 8_192 - len(received)))
            if not chunk:
                return
            received += chunk


def create_server(
    case_dir: Path,
    port: int,
    explicit_log: Path | None = None,
) -> ThreadingHTTPServer:
    collector = WatcherCollector(Path(case_dir), explicit_log=explicit_log)
    server = _ServerState(("127.0.0.1", port), _WatcherHandler)
    state = cast(_ServerState, server)
    actual_port = server.server_address[1]
    state.collector = collector
    state.session_token = secrets.token_urlsafe(32)
    state.allowed_hosts = frozenset(
        {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
    )
    state.allowed_origins = frozenset(
        {
            f"http://127.0.0.1:{actual_port}",
            f"http://localhost:{actual_port}",
        }
    )
    state.static_root = STATIC_ROOT.resolve()
    return server


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("foam-watch requires Python 3.10 or newer.", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        prog="foam-watch",
        description="Serve the OpenFOAM Solver Watcher on IPv4 loopback.",
    )
    parser.add_argument("--case", type=Path, default=Path.cwd())
    parser.add_argument("--log", type=Path)
    parser.add_argument("--port", type=_cli_port, default=8765)
    arguments = parser.parse_args(argv)

    try:
        case_dir = arguments.case.resolve(strict=True)
    except OSError:
        print(f"Not an OpenFOAM case: {arguments.case}", file=sys.stderr)
        return 2
    if not _is_openfoam_case(case_dir):
        print(
            "Not an OpenFOAM case (expected system/controlDict and constant/ "
            f"or a numeric time directory): {case_dir}",
            file=sys.stderr,
        )
        return 2

    explicit_log: Path | None = None
    if arguments.log is not None:
        candidate = (
            arguments.log
            if arguments.log.is_absolute()
            else case_dir / arguments.log
        )
        try:
            explicit_log = candidate.resolve(strict=True)
            explicit_log.relative_to(case_dir)
        except (OSError, ValueError):
            print(
                f"Log path must be an existing file inside the case: {arguments.log}",
                file=sys.stderr,
            )
            return 2
        if not explicit_log.is_file():
            print(
                f"Log path must be an existing file inside the case: {arguments.log}",
                file=sys.stderr,
            )
            return 2

    try:
        server = create_server(case_dir, arguments.port, explicit_log)
    except OSError:
        if _watcher_on_port(arguments.port):
            print(
                "OpenFOAM Solver Watcher is already running at "
                f"http://127.0.0.1:{arguments.port}",
                file=sys.stderr,
            )
        else:
            suggested = _next_available_port(arguments.port)
            suffix = (
                f" Try --port {suggested}."
                if suggested is not None
                else " No nearby loopback port is available."
            )
            print(
                f"Port {arguments.port} is occupied by a service that is not "
                f"the watcher.{suffix}",
                file=sys.stderr,
            )
        return 2

    actual_port = server.server_address[1]
    host = socket.gethostname()
    user = getpass.getuser()
    print(f"OpenFOAM Solver Watcher: http://127.0.0.1:{actual_port}")
    print(
        "SSH tunnel: "
        f"ssh -L {actual_port}:127.0.0.1:{actual_port} "
        f"{shlex.quote(user)}@{shlex.quote(host)}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nOpenFOAM Solver Watcher stopped.")
    finally:
        server.server_close()
    return 0


def _cli_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _is_openfoam_case(case_dir: Path) -> bool:
    if not (
        case_dir.is_dir()
        and (case_dir / "system" / "controlDict").is_file()
    ):
        return False
    if (case_dir / "constant").is_dir():
        return True
    try:
        return any(
            child.is_dir() and _TIME_DIRECTORY.fullmatch(child.name) is not None
            for child in case_dir.iterdir()
        )
    except OSError:
        return False


def _watcher_on_port(port: int) -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            payload = json.loads(response.read())
            server_header = response.headers.get("Server", "")
    except (
        OSError,
        urllib.error.URLError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        return False
    return (
        response.status == HTTPStatus.OK
        and payload == {"ok": True}
        and server_header.startswith("OpenFOAMWatcher/")
    )


def _next_available_port(port: int) -> int | None:
    for candidate in range(port + 1, min(port + 101, 65_536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    return None


def _validate_percent_escapes(path: str) -> None:
    index = 0
    while True:
        index = path.find("%", index)
        if index < 0:
            return
        if _HEX_ESCAPE.fullmatch(path[index : index + 3]) is None:
            raise ValueError("invalid percent escape")
        index += 3


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"unsupported JSON constant: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
