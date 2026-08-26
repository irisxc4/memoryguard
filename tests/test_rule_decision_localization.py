"""Readable Chinese labels for governance decisions and risk signals."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.interactive import render_interactive_html  # noqa: E402


def test_rule_decision_actions_are_localized_and_groups_start_collapsed() -> None:
    html = render_interactive_html()

    assert "function ruleDecisionActionLabel" in html
    for label in (
        "删除/移除",
        "自动创建规则",
        "恢复",
        "自动写入",
        "规则已被更新替代",
        "撤销规则操作",
        "调整分类",
    ):
        assert label in html
    assert "未知操作" in html
    assert "group.action === 'delete'" not in html
    assert "高频 delete" not in html
    assert "rule-decision-group" in html
    assert "rule-decision-subgroup" in html
    assert "action code" in html
    assert "<details class=\"rule-decision-group\" ${collapsed ? '' : 'open'}>" not in html
    assert "items.length <= 3 ? 'open' : ''" not in html


def test_risk_signal_cards_use_plain_chinese_summary_and_hide_details() -> None:
    html = render_interactive_html()

    assert "function riskSeverityLabel" in html
    assert "function riskRuleLabel" in html
    assert "function riskDimensionLabel" in html
    assert "function riskSurfaceLabel" in html
    assert "function riskEvidenceSummary" in html
    assert "未知风险" in html
    assert "未知严重度" in html
    assert "未知维度" in html
    assert "未知来源" in html
    assert 'aria-expanded="${index === 0 ? \'true\' : \'false\'}"' not in html
    assert "style=\"display:${index === 0 ? 'block' : 'none'}\"" not in html
    assert "riskSeverityLabel(finding.severity, finding)" in html
    assert "riskRuleLabel(finding.rule_id" in html
    assert "riskEvidenceSummary(finding)" in html
    assert "<span class=\"key\">内部规则 code</span>" in html
    assert "<span class=\"key\">内部路径</span>" in html
    assert "<span class=\"key\">影响</span>" in html
    assert "<span class=\"key\">建议</span>" in html
