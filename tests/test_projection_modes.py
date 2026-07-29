from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR
from memoryguard.projection import ProjectionBuilder


def test_projection_builder_stores_native_and_reconstructed_separately(tmp_path) -> None:
    ir = MemoryIR(records=[], snapshot_id="snap")
    native = ProjectionBuilder(tmp_path, "native", scope_key="agent-a")
    reconstructed = ProjectionBuilder(tmp_path, "reconstructed", scope_key="agent-a")

    native.save(native.build(ir, meta={"name": "native"}))
    reconstructed.save(reconstructed.build(ir, meta={"name": "reconstructed"}))

    native_path = tmp_path / ".memoryguard" / "projections" / "native" / "agent-a.json"
    reconstructed_path = tmp_path / ".memoryguard" / "projections" / "reconstructed" / "agent-a.json"
    assert native_path.exists()
    assert reconstructed_path.exists()
    assert native.load().meta["projection_mode"] == "native"
    assert reconstructed.load().meta["projection_mode"] == "reconstructed"

    native.delete()

    assert not native_path.exists()
    assert reconstructed_path.exists()
    assert native.get_or_empty()["empty"] is True


def test_scoped_projection_does_not_fallback_to_legacy_global(tmp_path) -> None:
    legacy_path = tmp_path / ".memoryguard" / "projections" / "reconstructed.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        '{"snapshot_id":"old","built_at":"","nodes":[{"id":"main"}],"edges":[],"stats":{},"meta":{}}',
        encoding="utf-8",
    )
    builder = ProjectionBuilder(tmp_path, "reconstructed", scope_key="agent-b")

    # 无 scoped 文件时不得回退混显旧全局图
    assert builder.get_or_empty()["empty"] is True

    ir = MemoryIR(records=[], snapshot_id="new")
    builder.save(builder.build(ir))

    assert builder.get_or_empty().get("empty") is not True
    assert (tmp_path / ".memoryguard" / "projections" / "reconstructed" / "agent-b.json").exists()


def test_projection_builder_without_scope_key_refuses_load(tmp_path) -> None:
    legacy = tmp_path / ".memoryguard" / "projections" / "reconstructed.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"snapshot_id":"x","nodes":[],"edges":[],"meta":{}}', encoding="utf-8")
    builder = ProjectionBuilder(tmp_path, "reconstructed")
    assert builder.load() is None
    assert builder.get_or_empty()["empty"] is True
