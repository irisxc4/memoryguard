from memoryguard.runtime_v2.governance_semantics import classify_governance_relation


LONG_WORKMODE_RULE = (
    "长期协作规则：不得为了迎合用户意见而附和。面对用户的判断、方案、指令或结论，"
    "先基于可得证据、风险、约束与真实目标进行客观分析；若不同意或条件不足，明确说明理由、"
    "证据边界、风险与可行替代方案。默认对所有编程、代码修改、重构、修复、评审、技术设计与"
    "依赖选择任务启用 Ponytail（full），并与 Caveman、RTK 一起作为主 Agent 和子 Agent 的默认工作方式。"
    "先理解实际调用链与约束，再按 YAGNI / 复用现有代码 / 标准库 / 原生能力 / 已安装依赖 / 最小可行改动"
    "的顺序选择方案；避免未请求的抽象、脚手架、依赖和冗余说明。Ponytail 不强用于非编码任务。"
    "安全、数据完整性、可访问性、项目 AGENTS 规则、用户明确需求与更高优先级指令始终优先；"
    "用户说“stop ponytail”或“normal mode”时当前会话停用。"
)
SHORT_WORKMODE_RULE = "全局默认使用 caveman 和 RTK，主 Agent 与所有子代理也默认遵循，除非用户明确要求关闭。"


def test_claim_subset_recovers_workmode_rule_hidden_by_unrelated_negation() -> None:
    relation = classify_governance_relation(LONG_WORKMODE_RULE, SHORT_WORKMODE_RULE)

    assert relation.kind == "update"
    assert relation.reason == "left_semantic_superset"
    assert relation.winner == "left"
    assert relation.mergeable is True


def test_positive_workmode_claim_contains_short_default() -> None:
    relation = classify_governance_relation(
        "默认启用 Ponytail full、Caveman 和 RTK，主 Agent 与子代理均遵循。",
        SHORT_WORKMODE_RULE,
    )

    assert relation.kind == "update"
    assert relation.reason == "left_semantic_superset"
    assert relation.winner == "left"


def test_claim_subset_requires_two_content_latin_anchors() -> None:
    relation = classify_governance_relation(
        "默认启用 Ponytail full、Caveman 和 RTK，主 Agent 与子代理均遵循。",
        "全局默认使用 Caveman，主 Agent 与子代理均遵循。",
    )

    assert relation.kind == "distinct"
    assert relation.mergeable is False


def test_opposite_workmode_polarity_never_merges() -> None:
    relation = classify_governance_relation(
        "默认启用 Ponytail full、Caveman 和 RTK，主 Agent 与子代理均遵循。",
        "禁止默认使用 Caveman 和 RTK。",
    )

    assert relation.mergeable is False


def test_non_user_exception_changes_keep_workmode_claims_distinct() -> None:
    relation = classify_governance_relation(
        "默认使用 Caveman 和 RTK，除非项目 foo。",
        "默认使用 Caveman 和 RTK，除非生产环境。",
    )

    assert relation.kind == "distinct"
    assert relation.mergeable is False


def test_subagent_spellings_and_case_normalize_for_claim_subset() -> None:
    relation = classify_governance_relation(
        "Default use Ponytail full, CAVEMAN and rtk. Main agent and sub-agents follow it.",
        "default use Caveman and RTK. Main Agent and subagents follow it.",
    )

    assert relation.kind == "update"
    assert relation.reason == "left_semantic_superset"
    assert relation.winner == "left"


def test_unrelated_or_unrelated_negation_stays_distinct() -> None:
    positive = classify_governance_relation(
        "默认启用 Ponytail full、Caveman 和 RTK，主 Agent 与子代理均遵循。",
        "保留发布前的加密备份。",
    )
    negative = classify_governance_relation(
        "默认启用 Ponytail full、Caveman 和 RTK，主 Agent 与子代理均遵循。",
        "不得删除未经授权的用户文件。",
    )

    assert positive.kind == "distinct"
    assert negative.kind == "distinct"


def test_canonical_relation_matrix_ignores_wrappers_not_constraints() -> None:
    cases = (
        ("Always use rtk for shell commands", "Use RTK for shell commands by default", "equivalent", ""),
        ("Always run topic tests for export", "Must run export topic tests", "equivalent", ""),
        ("Always use rtk for shell commands", "Use rtk for shell commands before commit", "update", "right"),
        ("Release policy: publish after approval", "Release policy: publish after manual approval", "update", "right"),
    )

    for left, right, kind, winner in cases:
        relation = classify_governance_relation(left, right)
        assert (relation.kind, relation.winner) == (kind, winner)
