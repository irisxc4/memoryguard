from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR
from memoryguard.projection import ProjectionBuilder


def test_projection_builder_stores_native_and_reconstructed_separately(tmp_path) -> None:
    ir = MemoryIR(records=[], snapshot_id="snap")
    native = ProjectionBuilder(tmp_path, "native")
    reconstructed = ProjectionBuilder(tmp_path, "reconstructed")

    native.save(native.build(ir, meta={"name": "native"}))
    reconstructed.save(reconstructed.build(ir, meta={"name": "reconstructed"}))

    native_path = tmp_path / ".memoryguard" / "projections" / "native.json"
    reconstructed_path = tmp_path / ".memoryguard" / "projections" / "reconstructed.json"
    assert native_path.exists()
    assert reconstructed_path.exists()
    assert native.load().meta["projection_mode"] == "native"
    assert reconstructed.load().meta["projection_mode"] == "reconstructed"

    native.delete()

    assert not native_path.exists()
    assert reconstructed_path.exists()
    assert native.get_or_empty()["empty"] is True


def test_reconstructed_delete_blocks_legacy_fallback_until_rebuild(tmp_path) -> None:
    legacy_path = tmp_path / ".memoryguard" / "projections" / "neuron.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text('{"snapshot_id":"old","built_at":"","nodes":[],"edges":[],"stats":{},"meta":{}}', encoding="utf-8")
    builder = ProjectionBuilder(tmp_path, "reconstructed")

    assert builder.get_or_empty().get("empty") is not True

    builder.delete()

    assert builder.get_or_empty()["empty"] is True
    assert not legacy_path.exists()

    ir = MemoryIR(records=[], snapshot_id="new")
    builder.save(builder.build(ir))

    assert builder.get_or_empty().get("empty") is not True
