"""Cross-process workspace governance lock.

The lock is an opaque filesystem synchronization primitive.  Its file path is
stable, but the file contents are never used to carry governance state.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO


class GovernanceLockError(RuntimeError):
    """The governance lock could not be acquired or released safely."""


class GovernanceLockTimeout(GovernanceLockError, TimeoutError):
    """The governance lock was contended until its deadline."""


class _LockState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.owner_thread_id: int | None = None
        self.depth = 0
        self.acquiring_thread_id: int | None = None
        self.handle: BinaryIO | None = None


_STATES_GUARD = threading.Lock()
_STATES: dict[str, _LockState] = {}


def _state_for(path: Path) -> _LockState:
    key = os.path.normcase(str(path))
    with _STATES_GUARD:
        state = _STATES.get(key)
        if state is None:
            state = _LockState()
            _STATES[key] = state
        return state


def _is_contention(exc: OSError) -> bool:
    """Recognize only OS-level lock contention as retryable."""
    if isinstance(exc, BlockingIOError):
        return True
    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
        return True
    # Windows msvcrt.locking commonly reports sharing/lock contention through
    # Win32 errors instead of a portable errno value.
    return getattr(exc, "winerror", None) in {32, 33, 36}


class WorkspaceGovernanceLock:
    """A re-entrant, fail-closed lock for one workspace.

    The same resolved workspace path is coordinated across threads in this
    process and across processes through ``flock`` on POSIX or
    ``msvcrt.locking`` on Windows.  Re-entry is allowed only by the owning
    thread; other threads wait and never bypass the OS lock.
    """

    DEFAULT_TIMEOUT = 5.0
    DEFAULT_POLL_INTERVAL = 0.05

    def __init__(
        self,
        workspace: str | Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        if timeout < 0:
            raise ValueError("governance lock timeout must be >= 0")
        if poll_interval <= 0:
            raise ValueError("governance lock poll_interval must be > 0")
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / ".memoryguard" / "governance.lock"
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._state = _state_for(self.path)

    def __enter__(self) -> "WorkspaceGovernanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.release()
        return False

    def acquire(self) -> "WorkspaceGovernanceLock":
        thread_id = threading.get_ident()
        deadline = time.monotonic() + self.timeout

        with self._state.condition:
            if self._state.owner_thread_id == thread_id:
                self._state.depth += 1
                return self
            while (
                self._state.owner_thread_id is not None
                or self._state.acquiring_thread_id is not None
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error()
                self._state.condition.wait(min(self.poll_interval, remaining))
            self._state.acquiring_thread_id = thread_id

        handle: BinaryIO | None = None
        try:
            handle = self._open_handle()
            while True:
                try:
                    self._try_os_lock(handle)
                    break
                except OSError as exc:
                    if not _is_contention(exc):
                        raise GovernanceLockError(
                            f"failed to acquire governance lock {self.path}: {exc}"
                        ) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_error() from exc
                    time.sleep(min(self.poll_interval, remaining))

            with self._state.condition:
                self._state.handle = handle
                self._state.owner_thread_id = thread_id
                self._state.depth = 1
                self._state.acquiring_thread_id = None
                self._state.condition.notify_all()
            return self
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            with self._state.condition:
                if self._state.acquiring_thread_id == thread_id:
                    self._state.acquiring_thread_id = None
                    self._state.condition.notify_all()
            raise

    def release(self) -> None:
        thread_id = threading.get_ident()
        with self._state.condition:
            if self._state.owner_thread_id != thread_id:
                raise GovernanceLockError(
                    f"governance lock {self.path} is not owned by this thread"
                )
            if self._state.depth > 1:
                self._state.depth -= 1
                return
            handle = self._state.handle
            self._state.handle = None
            self._state.owner_thread_id = None
            self._state.depth = 0
            try:
                if handle is not None:
                    self._unlock_os(handle)
            except OSError as exc:
                raise GovernanceLockError(
                    f"failed to release governance lock {self.path}: {exc}"
                ) from exc
            finally:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                self._state.condition.notify_all()

    def _timeout_error(self) -> GovernanceLockTimeout:
        return GovernanceLockTimeout(
            f"timed out after {self.timeout:.3f}s acquiring governance lock {self.path}"
        )

    def _open_handle(self) -> BinaryIO:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            # msvcrt.locking requires a byte range.  This opaque NUL sentinel
            # is not a governance payload and is never read as application data.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            return handle
        except OSError as exc:
            raise GovernanceLockError(
                f"failed to open governance lock {self.path}: {exc}"
            ) from exc

    @staticmethod
    def _try_os_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        try:
            import fcntl
        except ImportError as exc:
            raise GovernanceLockError(
                "POSIX governance locking is unavailable on this platform"
            ) from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_os(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        try:
            import fcntl
        except ImportError as exc:
            raise GovernanceLockError(
                "POSIX governance locking is unavailable on this platform"
            ) from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def governance_lock(
    workspace: str | Path,
    *,
    timeout: float = WorkspaceGovernanceLock.DEFAULT_TIMEOUT,
    poll_interval: float = WorkspaceGovernanceLock.DEFAULT_POLL_INTERVAL,
) -> WorkspaceGovernanceLock:
    """Return the public workspace lock context manager."""
    return WorkspaceGovernanceLock(
        workspace, timeout=timeout, poll_interval=poll_interval,
    )


__all__ = [
    "GovernanceLockError",
    "GovernanceLockTimeout",
    "WorkspaceGovernanceLock",
    "governance_lock",
]
