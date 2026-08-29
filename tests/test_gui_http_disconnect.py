from __future__ import annotations

import io
from pathlib import Path

import pytest

from memoryguard import gui


@pytest.mark.parametrize(
    "disconnect_error",
    [BrokenPipeError, ConnectionAbortedError, ConnectionResetError],
)
def test_localhost_post_silently_ends_when_error_response_client_disconnects(
    tmp_path: Path, monkeypatch, disconnect_error,
):
    """A browser abort must not trigger a second exception while writing 500."""
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(gui, "_find_free_port", lambda: 43124)
    monkeypatch.setattr(gui.http.server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(gui, "render_interactive_html", lambda: "<html></html>")
    monkeypatch.setattr(
        "memoryguard.security.generate_session_token", lambda: "test-token",
    )
    monkeypatch.setattr(
        gui, "_dispatch_gui_api_call",
        lambda *args: (_ for _ in ()).throw(RuntimeError("business failure")),
    )

    code, _url = gui.open_localhost_window(str(tmp_path), auto_open=False)
    assert code == 0

    handler = captured["handler"].__new__(captured["handler"])
    handler.path = "/api/get_audit"
    handler.headers = {"X-Session-Token": "test-token", "Content-Length": "0"}
    handler.rfile = io.BytesIO()

    class DisconnectedWriter:
        def write(self, _payload):
            raise disconnect_error("browser closed the request")

    handler.wfile = DisconnectedWriter()
    handler.send_response = lambda _status: None
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None

    # Before the fix this raises from the fallback 500 response itself.
    handler.do_POST()


def test_localhost_post_business_error_still_returns_unified_500(
    tmp_path: Path, monkeypatch,
):
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(gui, "_find_free_port", lambda: 43125)
    monkeypatch.setattr(gui.http.server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(gui, "render_interactive_html", lambda: "<html></html>")
    monkeypatch.setattr(
        "memoryguard.security.generate_session_token", lambda: "test-token",
    )
    monkeypatch.setattr(
        gui, "_dispatch_gui_api_call",
        lambda *args: (_ for _ in ()).throw(RuntimeError("business failure")),
    )

    code, _url = gui.open_localhost_window(str(tmp_path), auto_open=False)
    assert code == 0

    handler = captured["handler"].__new__(captured["handler"])
    handler.path = "/api/get_audit"
    handler.headers = {"X-Session-Token": "test-token", "Content-Length": "0"}
    handler.rfile = io.BytesIO()
    response_status = []
    handler.wfile = io.BytesIO()
    handler.send_response = response_status.append
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None

    handler.do_POST()

    assert response_status == [500]
    assert handler.wfile.getvalue() == (
        b'{"ok": false, "error": "gui_dispatch_failed"}'
    )
