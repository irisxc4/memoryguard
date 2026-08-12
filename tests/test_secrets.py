"""Secret detection and V2 native extraction quarantine tests."""

from __future__ import annotations

from memoryguard.content import ContentStore
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.secrets import detect_secrets, redact_secrets

from _publish_helpers import native_context


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


def test_native_extraction_never_accepts_plaintext_secret(tmp_path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    source = tmp_path / "memory.md"
    source.write_text(f"# API key note\n\nRemember: {secret}\n", encoding="utf-8")
    ContentStore(tmp_path).upsert_source_connector(
        source_id="selected-secret-file",
        provider="test",
        source_type="selected_file",
        external_root_key=str(source.resolve()),
        workspace_id=str(tmp_path.resolve()),
        enabled=True,
    )

    service = NativeExtractionEnrichmentService(tmp_path)
    context = native_context(tmp_path)
    preview = service.extract({"source_path": str(source)}, context=context)

    assert preview["candidates"]
    candidate = preview["candidates"][0]
    assert candidate["secret_redacted"] is True
    assert secret not in candidate["preview"]

    accepted = service.accept(
        {
            "extract_id": preview["extract_id"],
            "candidate_ids": [candidate["candidate_id"]],
        },
        context=context,
    )
    assert accepted["total"] == 1

    memory = MemoryAtomStore(tmp_path, readonly=True)
    atoms = memory.list_atoms(
        scope={
            "workspace_id": str(tmp_path.resolve()),
            "share_group_id": "group-test",
            "agent_instance_id": "agent-test",
            "project_ref": str(tmp_path.resolve()),
            "provider": "test",
            "runtime_role": "test",
        },
        include_building=True,
    )
    assert atoms
    assert all(secret not in atom.body for atom in atoms)
