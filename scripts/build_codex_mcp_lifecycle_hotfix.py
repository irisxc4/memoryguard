"""Build a minimal Codex lifecycle hotfix wheel from the last clean wheel.

Only the explicitly allow-listed Codex compatibility modules and distribution
metadata are replaced. This prevents unrelated dirty-worktree changes from
leaking into the Hook-only compatibility package.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path


SOURCE_VERSION = "0.7.1.post8"
TARGET_VERSION = "0.7.1.post9"
SOURCE_DIR = "dist-hotfix-final13"
TARGET_DIR = "dist-hotfix-final14"
WHEEL_STEM = "agent_memguard"
WHEEL_TAG = "py3-none-any"
PATCH_PATHS = (
    "memoryguard/cli.py",
    "memoryguard/codex_hook_trust.py",
    "memoryguard/codex_mcp_lifecycle.py",
    "memoryguard/codex_subagent_reconcile.py",
    "memoryguard/host_hook_executor.py",
    "memoryguard/host_hooks.py",
    "memoryguard/mcp_server.py",
    "memoryguard/provider_adapters.py",
)


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _clone_info(source: zipfile.ZipInfo, filename: str) -> zipfile.ZipInfo:
    target = zipfile.ZipInfo(filename=filename, date_time=source.date_time)
    target.compress_type = zipfile.ZIP_DEFLATED
    target.comment = source.comment
    target.extra = source.extra
    target.create_system = source.create_system
    target.create_version = source.create_version
    target.extract_version = source.extract_version
    target.flag_bits = source.flag_bits
    target.internal_attr = source.internal_attr
    target.external_attr = source.external_attr
    return target


def build() -> Path:
    repo = Path(__file__).resolve().parents[1]
    source_wheel = (
        repo
        / SOURCE_DIR
        / f"{WHEEL_STEM}-{SOURCE_VERSION}-{WHEEL_TAG}.whl"
    )
    patch_files = {path: repo / "src" / path for path in PATCH_PATHS}
    target_dir = repo / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_wheel = target_dir / f"{WHEEL_STEM}-{TARGET_VERSION}-{WHEEL_TAG}.whl"

    if not source_wheel.is_file():
        raise SystemExit(f"source wheel missing: {source_wheel}")
    missing = [str(path) for path in patch_files.values() if not path.is_file()]
    if missing:
        raise SystemExit("hotfix module missing: " + ", ".join(missing))

    old_dist_info = f"{WHEEL_STEM}-{SOURCE_VERSION}.dist-info"
    new_dist_info = f"{WHEEL_STEM}-{TARGET_VERSION}.dist-info"
    record_name = f"{new_dist_info}/RECORD"
    patch_payloads = {
        path: source_path.read_bytes() for path, source_path in patch_files.items()
    }
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    patched_names: set[str] = set()

    with zipfile.ZipFile(source_wheel, "r") as source:
        for info in source.infolist():
            old_name = info.filename
            if old_name == f"{old_dist_info}/RECORD":
                continue
            new_name = (
                new_dist_info + old_name[len(old_dist_info) :]
                if old_name.startswith(old_dist_info + "/")
                else old_name
            )
            payload = source.read(old_name)
            if old_name in patch_payloads:
                payload = patch_payloads[old_name]
                patched_names.add(old_name)
            elif old_name == f"{old_dist_info}/METADATA":
                text = payload.decode("utf-8")
                text, count = re.subn(
                    rf"(?m)^Version: {re.escape(SOURCE_VERSION)}(?=\r?$)",
                    f"Version: {TARGET_VERSION}",
                    text,
                    count=1,
                )
                if count != 1:
                    raise SystemExit("source wheel metadata version was not found")
                payload = text.encode("utf-8")
            entries.append((_clone_info(info, new_name), payload))

    # New compatibility modules may not exist in the source wheel. Add only
    # the explicit allow-list entries, with stable metadata, after copying the
    # clean wheel. Nothing else from the dirty worktree can leak into output.
    for path in sorted(set(patch_payloads) - patched_names):
        info = zipfile.ZipInfo(path, date_time=(2026, 8, 15, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        entries.append((info, patch_payloads[path]))

    records: list[tuple[str, str, str]] = []
    for info, payload in entries:
        records.append((info.filename, _record_digest(payload), str(len(payload))))
    records.append((record_name, "", ""))
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    writer.writerows(records)
    record_payload = record_buffer.getvalue().encode("utf-8")

    with zipfile.ZipFile(target_wheel, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info, payload in entries:
            target.writestr(info, payload)
        record_info = zipfile.ZipInfo(record_name)
        record_info.compress_type = zipfile.ZIP_DEFLATED
        record_info.external_attr = 0o644 << 16
        target.writestr(record_info, record_payload)

    print(target_wheel)
    print(hashlib.sha256(target_wheel.read_bytes()).hexdigest())
    return target_wheel


if __name__ == "__main__":
    build()
