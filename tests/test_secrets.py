import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import MemoryKind, MemoryRecord
from memoryguard.secrets import detect_secrets, redact_secrets


def test_redact_incomplete_pem_without_end() -> None:
    text = (
        "Deploy notes:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7incompletebase64line\n"
        "more secret material without end marker"
    )
    redacted, labels = redact_secrets(text)

    assert "MIIEpAIBAAKCAQEA7incompletebase64line" not in redacted
    assert "more secret material without end marker" not in redacted
    assert "[REDACTED:private_key]" in redacted
    assert "private_key" in labels
    assert "Deploy notes:" in redacted


def test_short_named_secret_uses_shared_quarantine_threshold() -> None:
    text = "temporary api_key=do-not-leak"

    matches = detect_secrets(text)
    redacted, labels = redact_secrets(text)

    assert any(match["label"] == "generic_secret" for match in matches)
    assert "do-not-leak" not in redacted
    assert "generic_secret" in labels


def test_normalize_or_save_does_not_persist_plaintext_secret_in_ir(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m-secret",
        kind=MemoryKind.FACT,
        title="API key note",
        body=f"Remember: {secret}",
        original_title="API key note",
        original_body=f"Remember: {secret}",
    )], snapshot_id="snap")
    normalizer = MemoryNormalizer(workspace)
    normalizer.save(ir)

    saved = json.loads((workspace / ".memoryguard" / "ir" / "current.json").read_text(encoding="utf-8"))
    blob = json.dumps(saved, ensure_ascii=False)

    assert secret not in blob
    assert "[REDACTED:" in blob
