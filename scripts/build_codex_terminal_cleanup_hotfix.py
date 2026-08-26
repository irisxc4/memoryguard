"""Build the minimal Codex subagent-rollout reconciliation hotfix.

The preferred source is the clean post17 wheel. If that wheel has already been
cleaned up locally, rebuild the same baseline from the currently installed
post17 distribution, excluding generated scripts, bytecode, installer receipts,
and direct_url metadata. Only the Codex subagent reconciler and host-hook
integration are replaced from the dirty worktree.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata
import io
import os
from pathlib import Path
import re
import time
import zipfile

SOURCE_VERSION = "0.7.1.post17"
TARGET_VERSION = "0.7.1.post18"
SOURCE_DIR = "dist-hotfix-complete-final"
TARGET_DIR = "dist-hotfix-subagent-rollout-post18"
WHEEL_STEM = "agent_memguard"
WHEEL_TAG = "py3-none-any"
PATCH_PATHS = (
    "memoryguard/codex_subagent_reconcile.py",
    "memoryguard/host_hooks.py",
)
_DIST_INFO_KEEP = {
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
}


def _digest(payload: bytes) -> str:
    raw = hashlib.sha256(payload).digest()
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _zip_info(filename: str, *, timestamp: float | None = None) -> zipfile.ZipInfo:
    stamp = time.localtime(timestamp or time.time())[:6]
    info = zipfile.ZipInfo(filename=filename, date_time=stamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def _clone(info: zipfile.ZipInfo, filename: str) -> zipfile.ZipInfo:
    target = zipfile.ZipInfo(filename=filename, date_time=info.date_time)
    target.compress_type = zipfile.ZIP_DEFLATED
    target.comment = info.comment
    target.extra = info.extra
    target.create_system = info.create_system
    target.create_version = info.create_version
    target.extract_version = info.extract_version
    target.flag_bits = info.flag_bits
    target.internal_attr = info.internal_attr
    target.external_attr = info.external_attr
    return target


def _rewrite_metadata(payload: bytes) -> bytes:
    text = payload.decode("utf-8")
    text, count = re.subn(
        rf"(?m)^Version: {re.escape(SOURCE_VERSION)}(?=\r?$)",
        f"Version: {TARGET_VERSION}",
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit("source distribution metadata version not found")
    return text.encode("utf-8")


def _entries_from_wheel(source_wheel: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    old_dist = f"{WHEEL_STEM}-{SOURCE_VERSION}.dist-info"
    new_dist = f"{WHEEL_STEM}-{TARGET_VERSION}.dist-info"
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(source_wheel, "r") as source:
        for info in source.infolist():
            old_name = info.filename
            if old_name == f"{old_dist}/RECORD":
                continue
            new_name = (
                new_dist + old_name[len(old_dist) :]
                if old_name.startswith(old_dist + "/")
                else old_name
            )
            payload = source.read(old_name)
            if old_name == f"{old_dist}/METADATA":
                payload = _rewrite_metadata(payload)
            entries.append((_clone(info, new_name), payload))
    return entries


def _entries_from_installed() -> list[tuple[zipfile.ZipInfo, bytes]]:
    distribution = importlib.metadata.distribution("agent-memguard")
    if distribution.version != SOURCE_VERSION:
        raise SystemExit(
            f"installed fallback must be {SOURCE_VERSION}, got {distribution.version}"
        )
    old_dist = f"{WHEEL_STEM}-{SOURCE_VERSION}.dist-info"
    new_dist = f"{WHEEL_STEM}-{TARGET_VERSION}.dist-info"
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    for package_path in distribution.files or ():
        name = str(package_path).replace("\\", "/")
        if name.startswith("../") or "__pycache__" in name or name.endswith(".pyc"):
            continue
        include = name.startswith("memoryguard/")
        if name.startswith(old_dist + "/"):
            rel = name[len(old_dist) + 1 :]
            include = rel in _DIST_INFO_KEEP or rel.startswith("licenses/")
        if not include:
            continue
        source_path = Path(distribution.locate_file(package_path))
        if not source_path.is_file():
            continue
        new_name = (
            new_dist + name[len(old_dist) :]
            if name.startswith(old_dist + "/")
            else name
        )
        payload = source_path.read_bytes()
        if name == f"{old_dist}/METADATA":
            payload = _rewrite_metadata(payload)
        entries.append(
            (_zip_info(new_name, timestamp=source_path.stat().st_mtime), payload)
        )
    required = {
        "memoryguard/__init__.py",
        "memoryguard/host_hooks.py",
        f"{new_dist}/METADATA",
        f"{new_dist}/WHEEL",
    }
    present = {info.filename for info, _payload in entries}
    missing = sorted(required - present)
    if missing:
        raise SystemExit(f"installed fallback missing required files: {missing}")
    return entries


def build() -> Path:
    repo = Path(__file__).resolve().parents[1]
    source_wheel = repo / SOURCE_DIR / f"{WHEEL_STEM}-{SOURCE_VERSION}-{WHEEL_TAG}.whl"
    target_dir = repo / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_wheel = target_dir / f"{WHEEL_STEM}-{TARGET_VERSION}-{WHEEL_TAG}.whl"
    patches = {path: (repo / "src" / path).read_bytes() for path in PATCH_PATHS}

    entries = (
        _entries_from_wheel(source_wheel)
        if source_wheel.is_file()
        else _entries_from_installed()
    )
    by_name = {info.filename: (info, payload) for info, payload in entries}
    for path, payload in patches.items():
        if path not in by_name:
            raise SystemExit(f"baseline distribution missing patch target: {path}")
        info, _old = by_name[path]
        by_name[path] = (info, payload)

    new_dist = f"{WHEEL_STEM}-{TARGET_VERSION}.dist-info"
    record_name = f"{new_dist}/RECORD"
    final_entries = [by_name[name] for name in sorted(by_name)]
    rows = [
        (info.filename, _digest(payload), str(len(payload)))
        for info, payload in final_entries
    ]
    rows.append((record_name, "", ""))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    record_payload = buffer.getvalue().encode("utf-8")

    with zipfile.ZipFile(target_wheel, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info, payload in final_entries:
            target.writestr(info, payload)
        target.writestr(_zip_info(record_name), record_payload)

    print(target_wheel)
    print(hashlib.sha256(target_wheel.read_bytes()).hexdigest())
    return target_wheel


if __name__ == "__main__":
    build()
