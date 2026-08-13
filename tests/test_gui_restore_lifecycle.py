from __future__ import annotations

import sys
import threading
import types

from memoryguard import gui


class _Event:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in tuple(self.handlers):
            handler()


class _Events:
    def __init__(self) -> None:
        self.minimized = _Event()
        self.restored = _Event()
        self.resized = _Event()
        self.closed = _Event()


class _Window:
    def __init__(self) -> None:
        self.events = _Events()
        self.loaded = []

    def load_url(self, url: str) -> None:
        self.loaded.append(url)


class _Control:
    def __init__(self) -> None:
        self.Visible = True
        self.InvokeRequired = False
        self.actions = []

    def BringToFront(self) -> None:
        self.actions.append("front")

    def Invalidate(self) -> None:
        self.actions.append("invalidate")

    def Update(self) -> None:
        self.actions.append("update")

    def Refresh(self) -> None:
        self.actions.append("refresh")


class _Native:
    def __init__(self) -> None:
        self.webview = _Control()
        self.actions = []

    def Invalidate(self) -> None:
        self.actions.append("invalidate")

    def Update(self) -> None:
        self.actions.append("update")


class _ImmediateTimer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.daemon = False

    def start(self) -> None:
        self.callback()


class _QueuedTimer(_ImmediateTimer):
    pending = []

    def start(self) -> None:
        self.pending.append(self.callback)

    @classmethod
    def run_next(cls) -> None:
        cls.pending.pop(0)()


def test_windows_restore_reloads_only_after_minimize(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "nt")
    monkeypatch.setattr(gui.threading, "Timer", lambda _delay, callback: _ImmediateTimer(callback))
    window = _Window()
    window.native = _Native()

    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")

    window.events.restored.fire()
    assert window.loaded == []
    window.events.minimized.fire()
    assert window.native.webview.Visible is False
    window.events.restored.fire()
    assert window.loaded == ["http://127.0.0.1:43123/"]
    assert window.native.webview.Visible is True
    assert window.native.webview.actions == ["front", "invalidate", "update", "refresh"]
    assert window.native.actions == ["invalidate", "update"]
    window.events.restored.fire()
    assert window.loaded == ["http://127.0.0.1:43123/"]


def test_restore_recovery_is_not_installed_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "posix")
    window = _Window()

    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")

    assert window.events.minimized.handlers == []
    assert window.events.restored.handlers == []
    assert window.events.resized.handlers == []


def test_stale_restore_timer_cannot_show_renderer_after_second_minimize(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "nt")
    _QueuedTimer.pending = []
    monkeypatch.setattr(gui.threading, "Timer", lambda _delay, callback: _QueuedTimer(callback))
    window = _Window()
    window.native = _Native()
    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")

    window.events.minimized.fire()
    window.events.restored.fire()
    assert len(_QueuedTimer.pending) == 1
    window.events.minimized.fire()
    _QueuedTimer.run_next()

    assert window.loaded == []
    assert window.native.webview.Visible is False
    window.events.restored.fire()
    _QueuedTimer.run_next()
    assert window.loaded == ["http://127.0.0.1:43123/"]
    assert window.native.webview.Visible is True


def test_concurrent_restore_and_resize_schedule_one_reload(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "nt")
    _QueuedTimer.pending = []
    monkeypatch.setattr(gui.threading, "Timer", lambda _delay, callback: _QueuedTimer(callback))
    window = _Window()
    window.native = _Native()
    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")
    window.events.minimized.fire()

    threads = [
        threading.Thread(target=window.events.restored.fire),
        threading.Thread(target=window.events.resized.fire),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_QueuedTimer.pending) == 1
    _QueuedTimer.run_next()
    assert window.loaded == ["http://127.0.0.1:43123/"]


def test_close_invalidates_pending_restore(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "nt")
    _QueuedTimer.pending = []
    monkeypatch.setattr(gui.threading, "Timer", lambda _delay, callback: _QueuedTimer(callback))
    window = _Window()
    window.native = _Native()
    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")

    window.events.minimized.fire()
    window.events.restored.fire()
    window.events.closed.fire()
    _QueuedTimer.run_next()

    assert window.loaded == []
    assert window.native.webview.Visible is False


def test_renderer_visibility_uses_control_invoke_when_required(monkeypatch) -> None:
    monkeypatch.setattr(gui.os, "name", "nt")
    monkeypatch.setattr(gui.threading, "Timer", lambda _delay, callback: _ImmediateTimer(callback))
    monkeypatch.setitem(sys.modules, "System", types.SimpleNamespace(Action=lambda callback: callback))
    window = _Window()
    window.native = _Native()
    control = window.native.webview
    control.InvokeRequired = True
    invoked = []

    def invoke(callback) -> None:
        invoked.append(True)
        callback()

    control.Invoke = invoke
    gui._install_windows_restore_recovery(window, "http://127.0.0.1:43123/")
    window.events.minimized.fire()
    window.events.restored.fire()

    assert invoked == [True, True]
    assert window.loaded == ["http://127.0.0.1:43123/"]
