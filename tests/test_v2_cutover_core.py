from __future__ import annotations

from dataclasses import dataclass
import sqlite3

import pytest

from memoryguard.cutover_v2 import ReadinessGate, RuntimeSnapshot, V2RuntimeFacade, snapshot_from_port
from memoryguard.system.manifest import ManifestError, ManifestManager, ManifestState


class Manifest:
    def __init__(self, state="V1_ACTIVE", generation=0, **extra):
        self.state = state
        self.generation = generation
        self.calls = 0
        self.extra = extra

    def current(self):
        self.calls += 1
        return {"state": self.state, "generation": self.generation, **self.extra}

    def transition(self, state, **kwargs):
        self.state = getattr(state, "value", state)
        self.generation += 1
        self.extra.update(kwargs)
        return self.current()

    def fail(self, **kwargs):
        return self.transition("V1_ACTIVE", **kwargs)


class Port:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def dispatch(self, surface, name, args, **kwargs):
        self.calls.append((surface, name, args, kwargs))
        return {"value": self.value}


class ContextPort(Port):
    supports_rule_mutation_context = True


def _facade(state):
    manifest = Manifest(state)
    legacy, v2 = Port("legacy"), Port("v2")
    return V2RuntimeFacade(manifest=manifest, legacy=legacy, v2=v2, workspace="fixture"), manifest, legacy, v2


def test_state_matrix_routes_once_and_ready_mutations_never_fallback():
    for state in ("V1_ACTIVE", "V2_BUILDING"):
        facade, manifest, legacy, v2 = _facade(state)
        result = facade.dispatch_mcp("memoryguard_memory_read", {"memory_id": "m"})
        assert result["code"] == "v2_upgrade_required"
        assert not legacy.calls and not v2.calls
        assert manifest.calls == 1

    facade, manifest, legacy, v2 = _facade("V2_READY")
    assert facade.dispatch_mcp("memoryguard_memory_read", {})["path"] == "v2"
    assert len(v2.calls) == 1 and not legacy.calls
    denied = facade.dispatch_mcp("memoryguard_memory_write", {})
    assert denied["code"] == "v2_not_active" and len(v2.calls) == 1 and not legacy.calls

    facade, manifest, legacy, v2 = _facade("V2_ACTIVE")
    facade.dispatch_mcp("memoryguard_memory_write", {})
    assert len(v2.calls) == 1 and not legacy.calls


def test_unknown_manifest_fails_closed_without_legacy():
    facade, manifest, legacy, v2 = _facade("FUTURE")
    result = facade.dispatch_mcp("memoryguard_memory_read", {})
    assert result["code"] == "v2_manifest_state_unavailable"
    assert not legacy.calls and not v2.calls


def test_snapshot_normalizes_state_values_and_preserves_v1_absent_generation():
    absent = RuntimeSnapshot.from_value({"state": "V1_ACTIVE"})
    assert absent.available and absent.generation == 0 and str(absent) == "V1_ACTIVE"
    enum_snapshot = RuntimeSnapshot.from_value({"state": "V2_ACTIVE", "generation": 2, "digests": {"x": 1}})
    assert enum_snapshot.state.value == "V2_ACTIVE"
    try:
        enum_snapshot.digests["x"] = 2
    except TypeError:
        pass
    else:
        raise AssertionError("snapshot digest metadata must be immutable")


def test_facade_state_snapshot_is_immutable_and_single_manifest_read():
    facade, manifest, _legacy, _v2 = _facade("V1_ACTIVE")
    snapshot = facade.state_snapshot()
    assert isinstance(snapshot, RuntimeSnapshot)
    assert snapshot.state.value == "V1_ACTIVE" and snapshot.generation == 0
    assert manifest.calls == 1


def test_absent_manifest_manager_defaults_to_v1_without_creating_database(tmp_path):
    manager = ManifestManager(tmp_path)
    snapshot = RuntimeSnapshot.from_value(manager.current())
    assert snapshot.state.value == "V1_ACTIVE" and snapshot.generation == 0
    assert not (tmp_path / ".memoryguard").exists()


def test_none_manifest_port_is_trusted_v1_without_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = RuntimeSnapshot.from_value(None)
    assert snapshot.available is False  # malformed value remains unavailable
    facade = V2RuntimeFacade(manifest=None)
    assert facade.state_snapshot().state.value == "V1_ACTIVE"
    assert snapshot_from_port(None).trusted and snapshot_from_port(None).generation == 0
    assert not (tmp_path / ".memoryguard").exists()


def test_explicit_snapshot_is_single_read_and_mapping_snapshot_is_rejected():
    facade, manifest, legacy, v2 = _facade("V2_ACTIVE")
    snap = RuntimeSnapshot.from_value({"state": "V2_ACTIVE", "generation": 7})
    result = facade.dispatch_mcp("memoryguard_memory_read", {}, snapshot=snap)
    assert result["generation"] == 7 and result["state"] == "V2_ACTIVE"
    assert manifest.calls == 0 and len(v2.calls) == 1
    denied = facade.dispatch_mcp("memoryguard_memory_read", {}, snapshot={"state": "V2_ACTIVE", "generation": 8})
    assert denied["code"] == "invalid_runtime_snapshot" and manifest.calls == 0 and len(v2.calls) == 1
    untrusted = RuntimeSnapshot("V2_ACTIVE", 9)
    assert facade.dispatch_mcp("memoryguard_memory_read", {}, snapshot=untrusted)["code"] == "invalid_runtime_snapshot"


def test_readiness_gate_unknown_and_activation_rollback_generation():
    evidence = {
        "metrics": {
            "loss": 0, "orphan": 0, "outbox": {"pending": 0, "failed": 0},
            "scope": 0, "binding": 0, "leak": 0,
            "mandatory_equivalence": True, "recall_v2": 3, "recall_v1": 3,
            "tokens_v2": 3, "tokens_v1": 4,
        },
        "source_digest": "s", "target_digest": "t", "manifest_digest": "m",
        "checkpoints": {"phase": "ok"}, "validator_passed": True,
    }
    gate = ReadinessGate(evidence=evidence)
    result = gate.evaluate()
    assert result.ready
    manifest = Manifest("V2_READY", 4)
    gate.activate(manifest, result, expected_generation=4)
    assert manifest.state == "V2_ACTIVE" and manifest.generation == 5
    before = manifest.generation
    gate.rollback(manifest, expected_generation=before)
    assert manifest.state == "V1_ACTIVE" and manifest.generation == before + 1
    blocked = ReadinessGate({**evidence, "metrics": {**evidence["metrics"], "unknown": -1}}).evaluate()
    assert not blocked.ready and "unknown_metric" in blocked.failures


def test_readiness_generation_rejects_bool_instead_of_coercing_to_one():
    evidence = {
        "metrics": {
            "loss": 0, "orphan": 0, "outbox": {"pending": 0, "failed": 0},
            "scope": 0, "binding": 0, "leak": 0,
            "mandatory_equivalence": True, "recall_v2": 3, "recall_v1": 3,
            "tokens_v2": 3, "tokens_v1": 4,
        },
        "source_digest": "s", "target_digest": "t", "manifest_digest": "m",
        "checkpoints": {"phase": "ok"}, "validator_passed": True,
        "generation": True,
    }
    result = ReadinessGate(evidence=evidence).evaluate()
    assert not result.ready and "unknown_metric" in result.failures


def test_non_rule_context_is_never_dropped_or_retried():
    manifest = Manifest("V2_ACTIVE")
    legacy, v2 = Port("legacy"), ContextPort("v2")
    facade = V2RuntimeFacade(manifest=manifest, legacy=legacy, v2=v2)
    result = facade.dispatch_mcp("memoryguard_memory_write", {}, context={"agent": "a"})
    assert result["path"] == "v2" and len(v2.calls) == 1 and v2.calls[0][2 + 1]["context"] == {"agent": "a"}
    assert not legacy.calls

    class NoContext(Port):
        supports_rule_mutation_context = False
    no_context = NoContext("v2")
    denied = V2RuntimeFacade(manifest=Manifest("V2_ACTIVE"), legacy=legacy, v2=no_context).dispatch_mcp(
        "memoryguard_memory_write", {}, context={"agent": "a"}
    )
    assert denied["code"] == "v2_context_capability_required" and not no_context.calls


def test_snapshot_factory_trust_and_generation_fail_closed():
    assert not RuntimeSnapshot("V1_ACTIVE", 0).trusted
    assert RuntimeSnapshot.from_value({"state": "V1_ACTIVE"}).trusted
    assert RuntimeSnapshot.unavailable().trusted
    assert RuntimeSnapshot.from_value({"state": "V2_ACTIVE", "generation": 1.5}).available is False
    assert RuntimeSnapshot.from_value({"state": "V2_ACTIVE", "generation": "1"}).available is False
    nested = {"evidence": {"items": [1]}}
    snapshot = RuntimeSnapshot.from_value({"state": "V1_ACTIVE", "digests": nested})
    nested["evidence"]["items"].append(2)
    assert tuple(snapshot.digests["evidence"]["items"]) == (1,)
    class SlotEvidence:
        __slots__ = ("value",)
        def __init__(self):
            self.value = {"items": [1]}
    slot = SlotEvidence()
    slot_snapshot = RuntimeSnapshot.from_value({"state": "V1_ACTIVE", "raw": slot})
    slot.value["items"].append(2)
    assert tuple(slot_snapshot.raw["raw"]["value"]["items"]) == (1,)


def test_existing_empty_manifest_database_is_corrupt_not_v1(tmp_path):
    manager = ManifestManager(tmp_path)
    manager.layout.ensure_dirs()
    manager.db_path.touch()
    with pytest.raises(ManifestError):
        manager.current()


def test_ready_and_active_never_construct_legacy_and_surface_ports_receive_cas():
    class SurfacePort:
        supports_rule_mutation_context = True

        def __init__(self):
            self.calls = []

        def dispatch_mcp(self, name, args, *, context=None, generation=None, mutation=None):
            self.calls.append(("mcp", name, args, context, generation, mutation))
            return {"ok": True}

        def dispatch_gui(self, name, args, *, context=None, generation=None, mutation=None):
            self.calls.append(("gui", name, args, context, generation, mutation))
            return {"ok": True}

        def dispatch_cli(self, name, args, *, context=None, generation=None, mutation=None):
            self.calls.append(("cli", name, args, context, generation, mutation))
            return {"ok": True}

    class ExplodingLegacy:
        def dispatch(self, *args, **kwargs):
            raise AssertionError("retired legacy dispatch must not run")

        def bootstrap_hook(self, *args, **kwargs):
            raise AssertionError("retired legacy hook must not run")

    port = SurfacePort()
    legacy = ExplodingLegacy()
    facade = V2RuntimeFacade(
        manifest=Manifest("V2_READY", 8),
        legacy=legacy,
        legacy_port=legacy,
        hook_legacy=legacy,
        legacy_adapter=legacy,
        v2=port,
    )
    assert facade.dispatch_mcp("memoryguard_memory_read", {}, context={"agent": "a"})["path"] == "v2"
    assert facade.dispatch_gui("lock_memory", {}, mutation=False)["code"] == "v2_not_active"
    assert facade.dispatch_cli("audit", {}, context={"agent": "a"})["path"] == "v2"
    assert facade.bootstrap_hook("session_start", {})["code"] == "bootstrap_failed"
    assert port.calls[0][4] == 8 and port.calls[0][3] == {"agent": "a"}
    assert port.calls[1][5] is False


@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING"])
def test_retired_states_never_invoke_exploding_legacy_or_hook(state):
    class ExplodingLegacy:
        def dispatch(self, *args, **kwargs):
            raise AssertionError("retired legacy dispatch must not run")

        def bootstrap_hook(self, *args, **kwargs):
            raise AssertionError("retired legacy hook must not run")

    legacy = ExplodingLegacy()
    facade = V2RuntimeFacade(
        manifest=Manifest(state),
        legacy=legacy,
        hook_legacy=legacy,
        v2=Port("v2"),
    )
    assert facade.dispatch_mcp("memoryguard_memory_read", {})["code"] == "v2_upgrade_required"
    assert facade.dispatch_gui("get_memory", {})["code"] == "v2_upgrade_required"
    assert facade.dispatch_cli("audit", {})["code"] == "v2_upgrade_required"
    assert facade.bootstrap_hook("session_start", {})["code"] == "v2_upgrade_required"


def test_manifest_transition_commit_cas_and_rollback_audit(tmp_path):
    manager = ManifestManager(tmp_path)
    manager.begin(migration_id="cas-batch")
    manager.record_checkpoint({"phase": "build"}, migration_id="cas-batch")
    ready = manager.transition(
        ManifestState.V2_READY,
        migration_id="cas-batch",
        source_digest="source", target_digest="target", manifest_digest="manifest",
        digests={"validator_passed": True, "checkpoints": {"phase": "build"}},
    )
    with pytest.raises(ManifestError):
        manager.transition(ManifestState.V2_ACTIVE, expected_generation=ready.generation - 1)
    rolled = manager.fail(error="operator requested rollback", expected_generation=ready.generation)
    assert rolled.state is ManifestState.V1_ACTIVE
    assert rolled.source_digest == "source" and rolled.target_digest == "target"
    assert rolled.manifest_digest == "manifest"
    assert rolled.checkpoints["phase"] == "build"
    assert rolled.errors["reason"] == "operator requested rollback"
    assert rolled.errors["generation"] == ready.generation


def test_readiness_unknown_columns_block_and_foreign_result_cannot_activate():
    evidence = {
        "metrics": {
            "loss": 0, "orphan": 0, "outbox": {"pending": 0}, "scope": 0,
            "binding": 0, "leak": 0, "mandatory_equivalence": True,
            "recall_v2": 2, "recall_v1": 2, "tokens_v2": 2, "tokens_v1": 3,
            "unknown_columns": 1,
        },
        "source_digest": "s", "target_digest": "t", "manifest_digest": "m",
        "checkpoints": {"phase": "ok"}, "validator_passed": True,
    }
    gate = ReadinessGate(evidence=evidence)
    blocked = gate.evaluate()
    assert not blocked.ready and "unknown_metric" in blocked.failures
    clean = {**evidence, "metrics": {k: v for k, v in evidence["metrics"].items() if k != "unknown_columns"}}
    first = ReadinessGate(clean)
    result = first.evaluate()
    with pytest.raises(Exception):
        ReadinessGate(clean).activate(Manifest("V2_READY", 0), result, expected_generation=0)
