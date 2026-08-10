"""Loopback HTTP server for ``rec web``.

Built on stdlib ``http.server.ThreadingHTTPServer`` (no new runtime deps). The
handler routes ``GET``/``POST`` to the read-only and mutating endpoints; this
module owns the skeleton (loopback bind, the DNS-rebinding Host guard, the
quiet access log, JSON helpers, and static-file serving). Endpoint bodies live
here too and are kept small — they delegate to :mod:`rec.session`,
:mod:`rec.recorder`, :mod:`rec.index`, and :mod:`rec.web.jobs`.

Security model: bind ``127.0.0.1`` only, and reject any request whose ``Host``
header is not ``127.0.0.1:<port>`` or ``localhost:<port>``. That one check
closes the DNS-rebinding hole (a malicious page resolving a hostname to
loopback to read your transcripts) without touching ``CORS`` or origin checks.

Access log discipline: the global file handler runs at DEBUG, so the access
log prints method, path, and status only — never a query string, never a body.
"""

from __future__ import annotations

import errno
import json
import urllib.parse
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

import click

from .. import session as session_mod
from ..log import get_logger

log = get_logger(__name__)

DEFAULT_PORT = 7717
LOOPBACK_HOST = "127.0.0.1"

# Session ids are always YYYY-MM-DD_HH-MM-SS (see session.new_session_id).
# Validating every id from a URL against this exact shape blocks path traversal
# before it reaches session_dir(); reject with 400, never sanitise-and-continue.
# The pattern lives in session.py (single source of truth) and is reused here.

# Map an incoming path to a handler. Static routes are exact; the dynamic
# session routes carry a <id> placeholder resolved at dispatch time. Populated
# by _build_routes() once at import time and shared by all handler instances.
# (Endpoints are registered incrementally as this module grows; the skeleton
# only needs GET / and the 404 fallback.)


class _RecWebServer(ThreadingHTTPServer):
    """Threading server with daemon threads so it dies with the process."""

    daemon_threads = True
    allow_reuse_address = True


class WebHandler(BaseHTTPRequestHandler):
    """Routes requests to endpoint handlers; enforces the loopback Host guard."""

    # Quiet the default stderr access log — we print our own one-liner that
    # deliberately omits query strings and bodies.
    server_version = "rec-web/1.0"
    sys_version = ""

    # The protocol version stays at 1.1 so we can send Content-Length and keep
    # connections alive; we always send a body or Content-Length: 0.
    protocol_version = "HTTP/1.1"

    # Drop a connection that stalls mid-request so a client that opens a socket
    # and sits on it can't hold a worker thread indefinitely. 30s is generous
    # for a loopback client and longer than any legal request here takes.
    timeout = 30

    # ---- request entry points --------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        self._dispatch("POST")

    # ---- routing ---------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                self._send_error(HTTPStatus.FORBIDDEN, "Host not allowed.")
                return
            if method == "POST" and not self._csrf_ok():
                self._send_error(HTTPStatus.FORBIDDEN, "Missing or invalid request header.")
                return
            path = urllib.parse.urlsplit(self.path).path
            handler, params = _route(method, path)
            if handler is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            handler(self, **params)
        except _ApiError as e:
            self._send_error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected mid-response (common for media streams:
            # Safari's <audio> closes the connection once it has buffered
            # enough). This is normal — log quietly, never as an error, and
            # crucially don't try to send a 500 to the now-dead socket.
            log.debug("client disconnected during %s %s", method, urllib.parse.urlsplit(self.path).path)
        except Exception:  # pragma: no cover — defensive, must not leak a traceback
            # Log the path only — never self.path, which may carry a query
            # string (e.g. a search term) we deliberately keep out of the log.
            log.exception("unhandled error serving %s %s", method, urllib.parse.urlsplit(self.path).path)
            try:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Something went wrong.")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The socket died AND something else went wrong — nothing we
                # can tell the client. Don't let this mask the original error
                # or cascade into socketserver's noisy traceback.
                pass

    # ---- DNS-rebinding guard --------------------------------------------

    def _host_ok(self) -> bool:
        """True if the Host header names loopback on our port.

        Rejects DNS-rebinding attacks where a remote page resolves a hostname
        to 127.0.0.1 and pokes the local server. We accept only the literal
        loopback hostnames on the port we're actually bound to.
        """
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]  # type: ignore[attr-defined]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    # ---- CSRF guard -----------------------------------------------------

    # The Host guard authenticates the *address*, not the *origin*: a malicious
    # page the user visits while `rec web` runs can still POST to our loopback
    # URL (the browser sets Host automatically, which the guard accepts). To
    # block that, every mutating request must carry a custom header. Custom
    # headers force a CORS preflight, and this server grants none, so a
    # cross-origin POST never gets off the ground. Our SPA is same-origin and
    # adds the header itself.
    REQUIRED_HEADER = "X-Requested-With"
    REQUIRED_HEADER_VALUE = "rec-web"

    def _csrf_ok(self) -> bool:
        """True if the request is same-origin (carries our custom header)."""
        return self.headers.get(self.REQUIRED_HEADER) == self.REQUIRED_HEADER_VALUE

    # ---- response helpers ------------------------------------------------

    def _send_json(self, status: int, obj: object) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # Cap request bodies so a client (or a CSRF page) can't exhaust memory by
    # declaring a huge Content-Length. 16 KiB is far more than any of our
    # endpoints need (the biggest legal body is a tiny JSON object).
    MAX_BODY_BYTES = 16 * 1024

    def _read_json(self) -> dict:
        """Read and parse a JSON object body; raise _ApiError on malformed/oversized input."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as e:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.") from e
        if length < 0:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length.")
        if length == 0:
            return {}
        if length > self.MAX_BODY_BYTES:
            raise _ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large."
            )
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "Request body is not valid JSON.") from e
        if not isinstance(obj, dict):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        return obj

    # ---- quiet, redacted access log -------------------------------------

    # BaseHTTPRequestHandler writes a default stderr access line; we override
    # log_request to emit our own quiet one-liner instead — method, path (no
    # query string), and the real status code passed in by send_response.
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:  # noqa: ARG002
        path_only = urllib.parse.urlsplit(self.path).path
        log.info("%s %s %s", self.command, path_only, code)


class _ApiError(Exception):
    """An error the dispatcher maps to an HTTP status + one-line message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---- static index ---------------------------------------------------------


def _get_index(h: WebHandler) -> None:
    """Serve the single-page app from package data."""
    html = _load_static("index.html")
    if html is None:
        raise _ApiError(HTTPStatus.NOT_FOUND, "Web UI is not installed in this build.")
    # Deny framing entirely: the page has Start/Stop recording controls, and a
    # clickjacking overlay over them could trick a user into triggering capture.
    # The Host guard stops DNS rebinding but not iframe embedding on 127.0.0.1.
    h._send_bytes(
        HTTPStatus.OK,
        html,
        "text/html; charset=utf-8",
        extra_headers=[
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "frame-ancestors 'none'"),
        ],
    )


def _load_static(name: str) -> bytes | None:
    """Read a file from rec.web/static, or None if absent (not yet added to the wheel)."""
    try:
        return (resources.files("rec.web") / "static" / name).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return None


# ---- routing table ---------------------------------------------------------


# A route handler takes the handler instance plus any captured path params.
RouteHandler = Callable[..., None]


def _build_routes() -> dict[tuple[str, str], tuple[RouteHandler, list[str]]]:
    """Return {(method, pattern): (handler, param_names)}.

    Patterns use a ``{name}`` placeholder for the single dynamic segment we
    support (the session id). More complex routing isn't worth a framework.

    The /api/* routes are registered incrementally as their handlers land
    (T4 read-only, T5 audio, T7 mutating). The skeleton ships GET / only.
    """
    routes: dict[tuple[str, str], tuple[RouteHandler, list[str]]] = {
        ("GET", "/"): (_get_index, []),
    }
    # Late import so this module stays importable before api.py exists.
    try:
        from . import api  # noqa: F401
    except ImportError:
        api = None  # type: ignore[assignment]
    if api is not None:
        api.register(routes)
    return routes


def _route(method: str, path: str) -> tuple[RouteHandler | None, dict[str, str]]:
    """Resolve a (method, path) to its handler and captured params.

    Returns (None, {}) for an unmatched path or wrong method. A session id
    captured from the URL is validated here so no handler ever receives an
    untrusted id.
    """
    for (m, pattern), (handler, names) in _ROUTES.items():
        if m != method:
            continue
        params, ok = _match(pattern, path)
        if not ok:
            continue
        # Validate any captured 'id' (session id) before it reaches a handler.
        if "id" in params and not session_mod.is_valid_session_id(params["id"]):
            raise _ApiError(
                HTTPStatus.BAD_REQUEST, "Invalid session id."
            )
        # Only pass the named params the handler expects.
        return handler, {n: params[n] for n in names}
    return None, {}


def _match(pattern: str, path: str) -> tuple[dict[str, str], bool]:
    """Match a ``{name}``-pattern against a path; return (params, ok)."""
    if "{" not in pattern:
        return {}, path == pattern
    # Split into literal segments and placeholders, e.g. /api/sessions/{id}/audio/{stream}
    seg_pat = pattern.split("/")
    seg_path = path.split("/")
    if len(seg_pat) != len(seg_path):
        return {}, False
    params: dict[str, str] = {}
    for sp, sp_path in zip(seg_pat, seg_path):
        if sp.startswith("{") and sp.endswith("}"):
            params[sp[1:-1]] = urllib.parse.unquote(sp_path)
        elif sp != sp_path:
            return {}, False
    return params, True


# Built once at import time, after the handler functions above exist.
_ROUTES = _build_routes()


# ---- entry point ----------------------------------------------------------


def make_server(port: int, handler_cls: type[BaseHTTPRequestHandler] = WebHandler) -> _RecWebServer:
    """Construct the loopback server. Raises click.ClickException on EADDRINUSE.

    Bound to 127.0.0.1 only; the host is not configurable. Caller handles
    browser-open and serve_forever().
    """
    try:
        return _RecWebServer((LOOPBACK_HOST, port), handler_cls)
    except OSError as e:
        # Distinguish the two common bind failures with one clean line each,
        # never a traceback. EADDRINUSE (48 on macOS, 98 on Linux) is the usual
        # one; EACCES (e.g. --port 80 without privileges) needs a different hint.
        if e.errno == errno.EADDRINUSE:
            raise click.ClickException(
                f"Port {port} is already in use. Try another with --port, "
                f"or close the other process holding it."
            ) from e
        if e.errno == getattr(errno, "EACCES", 13):
            raise click.ClickException(
                f"Permission denied to bind port {port}. Ports below 1024 need "
                f"privileges — try a higher port with --port."
            ) from e
        raise click.ClickException(f"Could not start the web server: {e}") from e


def serve(port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    """Start the loopback server. Blocks until interrupted.

    Sets the logging command context to 'web' (the global file handler still
    runs at DEBUG), opens a browser tab unless suppressed, and prints the URL
    once so a headless/SSH user can copy it.
    """
    from .. import log as log_mod

    log_mod.set_command_context("web")
    server = make_server(port)
    bound_port = server.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/"
    log.info("rec web serving on %s", url)
    # Echo to stdout (not the log) so the user sees the URL plainly.
    print(f"rec web: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover — browser open is best-effort
            log.warning("could not open a browser automatically")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        # Cancel queued transcription jobs and stop the worker pool so a Ctrl+C
        # while a Whisper run is in flight doesn't hang the process on the
        # non-daemon executor thread. An in-flight job still runs to completion
        # (cooperative cancellation is a bigger change); tell the user why.
        from . import jobs

        if _has_active_job(jobs):
            print("rec web: waiting for in-flight transcription to finish…")
        jobs.registry.shutdown()


def _has_active_job(jobs_mod) -> bool:
    """True if the global registry has any queued or running job."""
    registry = jobs_mod.registry
    with registry._lock:  # noqa: SLF001 (introspect the singleton once at shutdown)
        return any(
            j.state in (jobs_mod.JobState.queued, jobs_mod.JobState.running)
            for j in registry._jobs.values()  # noqa: SLF001
        )
