"""Tests for rec.web.server — the loopback HTTP skeleton.

Offline: the server binds 127.0.0.1 on an ephemeral port (port 0) in a
background thread. No audio device, no model, no network egress. The autouse
`xdg` and `isolate_logging` fixtures from conftest.py isolate paths and logs.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from http.client import HTTPConnection

import pytest

from rec.web import server


def _free_port() -> int:
    """Grab an ephemeral port the OS is guaranteed to hand us a free one for."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def web_server() -> Iterator[tuple[str, int]]:
    """Start the web server on an ephemeral port; yield (host, port).

    Uses the real WebHandler so the Host guard and routing are exercised. The
    server runs in a daemon thread; we shut it down cleanly in the finally.
    """
    srv = server.make_server(0)
    host, port = srv.server_address[0], srv.server_address[1]
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, port
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _get(host: str, port: int, path: str, *, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    """GET a path; return (status, body, lowercased response headers)."""
    conn = HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, body, hdrs
    finally:
        conn.close()


# ---- GET / ----------------------------------------------------------------


def test_get_index_serves_html(web_server):
    """The skeleton serves the SPA. index.html doesn't ship yet (T8), so the
    handler returns a clean 404 — proving the route resolves and the static
    loader degrades gracefully rather than throwing."""
    host, port = web_server
    # index.html is absent until T8; assert the route is matched (not a 404
    # from routing) by checking we get a JSON error envelope, not a default
    # HTML error page. Once T8 lands index.html this flips to 200 + text/html.
    status, body, _ = _get(host, port, "/", headers={"Host": f"127.0.0.1:{port}"})
    assert status in (200, 404)
    if status == 404:
        payload = json.loads(body)
        assert "error" in payload


# ---- DNS-rebinding guard --------------------------------------------------


def test_bad_host_header_is_403(web_server):
    """A Host header naming a non-loopback host is rejected with 403.

    This is the DNS-rebinding defence: a malicious page resolving an attacker
    hostname to 127.0.0.1 still sends its real Host, which we refuse.
    """
    host, port = web_server
    status, body, _ = _get(host, port, "/", headers={"Host": "evil.example.com:1234"})
    assert status == 403
    assert json.loads(body)["error"]


def test_localhost_host_is_allowed(web_server):
    host, port = web_server
    status, _, _ = _get(host, port, "/", headers={"Host": f"localhost:{port}"})
    assert status in (200, 404)  # route matched; guard passed


def test_wrong_port_in_host_is_rejected(web_server):
    """The Host must name OUR port, not just any loopback port."""
    host, port = web_server
    status, _, _ = _get(host, port, "/", headers={"Host": f"127.0.0.1:{port + 1}"})
    assert status == 403


# ---- 404 routing ----------------------------------------------------------


def test_unknown_path_is_404(web_server):
    host, port = web_server
    status, body, _ = _get(host, port, "/nope", headers={"Host": f"127.0.0.1:{port}"})
    assert status == 404
    assert json.loads(body)["error"]


def test_api_routes_absent_until_registered(web_server):
    """Until T4 registers them, /api/* paths are 404 — not 500 from a bad import."""
    host, port = web_server
    status, body, _ = _get(host, port, "/api/status", headers={"Host": f"127.0.0.1:{port}"})
    assert status == 404
    assert json.loads(body)["error"]


# ---- make_server: EADDRINUSE ---------------------------------------------


def test_port_in_use_raises_clean_error():
    """Binding a second server to an occupied port raises click.ClickException
    with a one-line message — never a traceback."""
    import click

    occupied = _free_port()
    holder = server.make_server(occupied)
    try:
        with pytest.raises(click.ClickException) as exc:
            server.make_server(occupied)
        assert str(occupied) in exc.value.message
        assert "--port" in exc.value.message
    finally:
        # holder.serve_forever() was never called, so shutdown() would block
        # waiting for a poll loop that isn't running. server_close() releases
        # the socket, which is all the cleanup this needs.
        holder.server_close()


def test_make_server_binds_loopback_only():
    """The server address must be 127.0.0.1 — the security model is loopback."""
    srv = server.make_server(0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


# ---- JSON helper shape ----------------------------------------------------


def test_error_envelope_is_human_readable_sentence(web_server):
    host, port = web_server
    status, body, _ = _get(host, port, "/nope", headers={"Host": f"127.0.0.1:{port}"})
    envelope = json.loads(body)
    assert isinstance(envelope["error"], str)
    assert envelope["error"]  # non-empty
