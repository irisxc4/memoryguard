from __future__ import annotations

from pathlib import Path

import memoryguard.runtime_v2.canonical_claims as canonical_claims
from memoryguard.runtime_v2.canonical_claims import (
    claims_related,
    compose_canonical_bodies,
    topic_affinity,
)
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.governance_semantics import classify_governance_relation
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer


def test_composes_bullets_and_deduplicates_equivalent_claims() -> None:
    result = compose_canonical_bodies(
        [
            "- Always use RTK for shell commands.\n[2] Use RTK for shell commands by default.",
        ]
    )

    assert len(result.claims) == 1
    assert result.rejected_conflicts == ()
    assert result.body.startswith("- ")
    assert "- - " not in result.body


def test_more_specific_update_wins() -> None:
    result = compose_canonical_bodies(
        ["Use rtk", "Always use rtk for shell commands before every command."]
    )

    assert result.claims == ("Always use rtk for shell commands before every command.",)


def test_join_separator_and_conjunction_paraphrases_stay_one_claim() -> None:
    """Classifier update/equivalent must compose even when raw ASCII anchors miss.

    Compound identifiers and CJK conjunctions tokenize differently in the
    composer, so incremental compose of a rewritten bullet body against a
    join-separator paraphrase used to reject the second form as unrelated.
    """

    conjunction = "默认使用 alpha 和 beta"
    short_join = "默认用 alpha/beta"
    extended_join = "默认使用 alpha/beta，作为默认工具"
    rendered = f"- {conjunction}"

    assert claims_related(conjunction, short_join)
    assert claims_related(conjunction, extended_join)
    assert claims_related(rendered, short_join)
    assert claims_related(rendered, extended_join)

    for bodies in (
        [conjunction, short_join, extended_join],
        [rendered, short_join],
        [rendered, extended_join],
        [extended_join, rendered],
        [short_join, rendered],
    ):
        result = compose_canonical_bodies(bodies)
        assert result.rejected_unrelated == ()
        assert result.rejected_conflicts == ()
        assert len(result.claims) == 1


def test_unpunctuated_cjk_short_long_topic_uses_structural_affinity() -> None:
    short = "通用工具偏好短句"
    long = "通用工具偏好短句路径统一并包含详细例外"
    result = compose_canonical_bodies([short, long])

    assert topic_affinity(short, long) >= 0.32
    assert claims_related(short, long)
    assert result.claims == (long,)
    assert result.rejected_unrelated == ()


def test_same_theme_complementary_claims_are_rendered_together() -> None:
    result = compose_canonical_bodies(
        [
            "Use rtk for shell commands in CI",
            "Use rtk for shell commands when review starts",
        ]
    )

    assert len(result.claims) == 2
    assert result.body == "- Use rtk for shell commands in CI\n- Use rtk for shell commands when review starts"


def test_unrelated_claim_is_rejected_and_not_composed() -> None:
    result = compose_canonical_bodies(
        ["Always use rtk for shell commands", "Keep encrypted backups for releases"]
    )

    assert result.claims == ("Always use rtk for shell commands",)
    assert result.rejected_unrelated == ("Keep encrypted backups for releases",)
    assert "backups" not in result.body


def test_distinct_safety_constraints_with_negation_can_coexist() -> None:
    result = compose_canonical_bodies(
        [
            "清理时不得误删用户文件",
            "清理时不删除未授权文件",
        ]
    )

    assert len(result.claims) == 2
    assert result.rejected_conflicts == ()
    assert result.rejected_unrelated == ()


def test_direct_opposite_predicate_is_isolated() -> None:
    result = compose_canonical_bodies(
        ["不要删除用户文件", "必须删除用户文件"]
    )

    assert result.claims == ()
    assert set(result.rejected_conflicts) == {
        "不要删除用户文件",
        "必须删除用户文件",
    }


def test_chinese_topic_anchor_composes_without_rtk_specificity() -> None:
    result = compose_canonical_bodies(
        [
            "数据库：查询前检查索引",
            "数据库：写入后记录索引变更",
        ]
    )

    assert len(result.claims) == 2
    assert result.rejected_unrelated == ()
    assert "数据库" in result.body


def test_same_heading_composes_complementary_test_strategy_tails() -> None:
    result = compose_canonical_bodies(
        [
            "测试策略：无代码改动不要重复全量测试",
            "测试策略：优先运行与改动相关的定向测试",
        ]
    )

    assert len(result.claims) == 2
    assert result.rejected_conflicts == ()
    assert result.rejected_unrelated == ()


def test_semantic_filter_constants_do_not_encode_business_domains() -> None:
    forbidden = {
        "测试", "清理", "发布", "备份", "caveman", "luna", "rtk",
        "test", "cleanup", "publish", "backup",
    }
    for name in ("_GENERIC_HAN", "_GENERIC_LATIN", "_GENERIC_HEADING"):
        values = getattr(canonical_claims, name)
        assert not any(
            item.casefold() in {term.casefold() for term in forbidden}
            for item in values
        )


def test_same_heading_rejects_publish_strategy_conflict() -> None:
    result = compose_canonical_bodies(
        [
            "发布策略：不要自动发布",
            "发布策略：每次必须自动发布",
        ]
    )

    assert result.claims == ()
    assert set(result.rejected_conflicts) == {
        "发布策略：不要自动发布",
        "发布策略：每次必须自动发布",
    }


def test_non_conflict_claims_are_conserved_across_component_selection() -> None:
    result = compose_canonical_bodies(
        [
            "发布策略：自动发布",
            "发布策略：自动发布。备份策略：每次必须加密备份",
        ]
    )

    assert any("发布策略" in claim for claim in result.claims)
    assert result.rejected_unrelated == ("备份策略：每次必须加密备份",)
    assert set(result.claims) | set(result.rejected_unrelated) == {
        "发布策略：自动发布",
        "发布策略：自动发布。",
        "备份策略：每次必须加密备份",
    } or set(result.claims) | set(result.rejected_unrelated) == {
        "发布策略：自动发布。",
        "备份策略：每次必须加密备份",
    }


def test_opposite_polarity_is_rejected_before_additive_merge() -> None:
    result = compose_canonical_bodies(
        ["Use rtk for shell commands", "Never use rtk for shell commands"]
    )

    assert result.claims == ()
    assert set(result.rejected_conflicts) == {
        "Use rtk for shell commands",
        "Never use rtk for shell commands",
    }
    assert "Use rtk" not in result.body
    assert "Never use" not in result.body


def test_delegation_guardrails_are_not_false_conflicts() -> None:
    result = compose_canonical_bodies(
        [
            "Delegate only when necessary; keep the number small and close every delegated task before delivery.",
            "Do not repeatedly delegate or review without new risk evidence.",
        ]
    )

    assert result.rejected_conflicts == ()


def test_sol_luna_grok_delegation_guardrails_are_distinct_claims() -> None:
    sol_rule = (
        "Sol 负责规划、风险判断与验收；明确执行用 Luna xhigh；"
        "复杂跨模块或高不确定性实现用 Grok Build MCP 的 Grok 4.6 xhigh；"
        "禁止无新增风险证据的反复委派和审核。"
    )
    delegation_rule = (
        "用户不希望小型/局部任务被拆成大量并行子任务，造成多个 Codex 对话长期显示处理中。"
        "后续优先直接处理；确需委派时减少数量，并在交付前等待完成或明确收口子任务状态。"
    )

    relation = classify_governance_relation(sol_rule, delegation_rule)
    result = compose_canonical_bodies([sol_rule, delegation_rule])

    assert relation.kind == "distinct"
    assert result.rejected_conflicts == ()


def test_organizer_keeps_sol_luna_grok_guardrails_out_of_conflict(tmp_path: Path) -> None:
    """The real write path must not tombstone compatible delegation rules."""

    group = "sol-luna-grok-guardrails"
    memory = MemoryAtomStore(tmp_path)
    organizer = V2MemoryOrganizer(
        tmp_path,
        group,
        memory_store=memory,
        governance=GovernanceV2(tmp_path, memory_store=memory),
    )
    peer_body = (
        "用户不希望小型/局部任务被拆成大量并行子任务，造成多个 Codex 对话长期显示处理中。"
        "后续优先直接处理；确需委派时减少数量，并在交付前等待完成或明确收口子任务状态。"
    )
    target_body = (
        "Sol 负责规划、风险判断与验收；明确执行用 Luna xhigh；"
        "复杂跨模块或高不确定性实现用 Grok Build MCP 的 Grok 4.6 xhigh；"
        "禁止无新增风险证据的反复委派和审核。"
    )

    def write(body: str, event_id: str) -> dict:
        return organizer.write(
            {
                "body": body,
                "kind": "procedure",
                "event_id": event_id,
                "agent_instance_id": "agent-a",
                "share_group_id": group,
                "project_ref": "project-a",
                "provider": "codex",
                "runtime_role": "root",
                "visibility": "active",
                "injection_policy": "always",
            }
        )

    peer = write(peer_body, "event-peer")
    target = write(target_body, "event-target")

    assert peer["ok"] is True
    assert target["ok"] is True
    assert target["mutation_kind"] != "conflicted"
    assert target["status"] != "conflicted"
    assert not any("conflict_group_id" in action for action in target["actions"])
    peer_atom = memory.get_atom(peer["memory_id"], scope=organizer.scope, include_building=True)
    assert peer_atom is not None
    assert peer_atom.status != "deleted"
    assert peer_atom.metadata.get("conflict_group_id") is None


def test_order_and_recomposition_are_idempotent() -> None:
    bodies = [
        "Use rtk for shell commands when review starts",
        "Use rtk for shell commands in CI",
    ]
    first = compose_canonical_bodies(bodies)
    second = compose_canonical_bodies(list(reversed(bodies)))
    replay = compose_canonical_bodies([first.body])

    assert first.body == second.body == replay.body
    assert first.claims == second.claims == replay.claims
    assert replay.changed is False
    assert "- - " not in replay.body


def test_chinese_and_english_sentence_delimiters_become_atomic_claims() -> None:
    result = compose_canonical_bodies(
        ["[1] 始终使用 rtk 执行命令；[2] Always run tests after changes.\n• Keep logs"]
    )

    assert result.claims
    assert all(not claim.startswith(("[1]", "[2]", "•")) for claim in result.claims)
    assert all(not line.startswith("- [") for line in result.body.splitlines())
