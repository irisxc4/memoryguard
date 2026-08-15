"""Build the minimal Codex indexed-terminal cleanup hotfix.

The source is the already isolated post15 wheel. Only the lifecycle module and
host hook integration are replaced, so unrelated dirty-worktree changes cannot
enter the package.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import zipfile
from pathlib import Path

SOURCE_VERSION = "0.7.1.post15"
TARGET_VERSION = "0.7.1.post16"
SOURCE_DIR = "dist-hotfix-final20"
TARGET_DIR = "dist-hotfix-final21"
WHEEL_STEM = "agent_memguard"
WHEEL_TAG = "py3-none-any"
PATCH_PATHS = (
    "memoryguard/codex_mcp_lifecycle.py",
    "memoryguard/host_hooks.py",
)


def _digest(payload: bytes) -> str:
    raw = hashlib.sha256(payload).digest()
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


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


def build() -> Path:
    repo = Path(__file__).resolve().parents[1]
    source_wheel = repo / SOURCE_DIR / f"{WHEEL_STEM}-{SOURCE_VERSION}-{WHEEL_TAG}.whl"
    target_dir = repo / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_wheel = target_dir / f"{WHEEL_STEM}-{TARGET_VERSION}-{WHEEL_TAG}.whl"
    patches = {path: (repo / "src" / path).read_bytes() for path in PATCH_PATHS}
    if not source_wheel.is_file():
        raise SystemExit(f"source wheel missing: {source_wheel}")

    old_dist = f"{WHEEL_STEM}-{SOURCE_VERSION}.dist-info"
    new_dist = f"{WHEEL_STEM}-{TARGET_VERSION}.dist-info"
    record_name = f"{new_dist}/RECORD"
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []

    with zipfile.ZipFile(source_wheel, "r") as source:
        for info in source.infolist():
            old_name = info.filename
            if old_name == f"{old_dist}/RECORD":
                continue
            new_name = new_dist + old_name[len(old_dist):] if old_name.startswith(old_dist + "/") else old_name
            payload = source.read(old_name)
            if old_name in patches:
                payload = patches[old_name]
            elif old_name == f"{old_dist}/METADATA":
                text = payload.decode("utf-8")
                text, count = re.subn(
                    rf"(?m)^Version: {re.escape(SOURCE_VERSION)}(?=\r?$)",
                    f"Version: {TARGET_VERSION}",
                    text,
                    count=1,
                )
                if count != 1:
                    raise SystemExit("source wheel metadata version not found")
                payload = text.encode("utf-8")
            entries.append((_clone(info, new_name), payload))

    rows = [(info.filename, _digest(payload), str(len(payload))) for info, payload in entries]
    rows.append((record_name, "", ""))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    record_payload = buffer.getvalue().encode("utf-8")

    with zipfile.ZipFile(target_wheel, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for info, payload in entries:
            target.writestr(info, payload)
        record = zipfile.ZipInfo(record_name)
        record.compress_type = zipfile.ZIP_DEFLATED
        record.external_attr = 0o644 << 16
        target.writestr(record, record_payload)

    print(target_wheel)
    print(hashlib.sha256(target_wheel.read_bytes()).hexdigest())
    return target_wheel


if __name__ == "__main__":
    build()
