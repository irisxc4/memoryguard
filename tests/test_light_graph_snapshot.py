"""Light graph snapshot keeps every parent-child branch connected to main."""
from memoryguard.light_graph import KnowledgeClaim, LightGraphManager, LightNode


def test_snapshot_synthesizes_missing_parent_edges() -> None:
    graph = LightGraphManager()
    graph._nodes["cat-x"] = LightNode(
        light_id="cat-x", parent_id="main", label="cat-x",
        node_kind="topic", status="confirmed",
    )
    graph._nodes["leaf-x"] = LightNode(
        light_id="leaf-x", parent_id="cat-x", label="leaf-x",
        node_kind="claim_anchor", anchor_claim_id=1, status="confirmed",
    )
    graph._claims[1] = KnowledgeClaim(
        id=1, display_label="leaf-x", body="x", memory_type="cat-x",
        source="memory.md", light_id="leaf-x", status="active",
    )

    snapshot = graph.build_live_snapshot()
    edges = {(edge["from"], edge["to"]) for edge in snapshot["edges"]}

    assert ("main", "cat-x") in edges
    assert ("cat-x", "leaf-x") in edges
