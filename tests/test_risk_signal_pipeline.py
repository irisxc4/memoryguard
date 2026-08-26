"""Contract checks for the reference-audit blocker -> risk-card pipeline."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.interactive import render_interactive_html  # noqa: E402


def _slice_function(html: str, start: str, end: str) -> str:
    begin = html.index(start)
    return html[begin:html.index(end, begin)]


def test_reference_audit_shape_preserves_readable_fields_into_risk_cards() -> None:
    """Exercise the JS contract statically, without requiring a browser runtime."""
    html = render_interactive_html()
    normalize = _slice_function(html, "function normalizeAuditReport", "function apiErrorMessage")
    formatters = _slice_function(html, "function riskSeverityLabel", "function renderFindings")
    render = _slice_function(html, "function renderFindings", "function toggleFinding")
    backend_shape = {
        "title_zh": "架构不可读",
        "title": "架构不可读",
        "severity_label": "高风险",
        "type_label": "架构不可读",
        "dimension_label": "数据完整性",
        "surface_label": "存储",
        "summary": "规则定义表无法读取",
        "evidence_summary": "规则定义表无法读取",
    }

    assert "const blockerData = blocker && typeof blocker === 'object' ? {...blocker} : {};" in normalize
    assert "...blockerData," in normalize
    contract = normalize + formatters + render
    for field in backend_shape:
        assert field in contract
    assert "riskSeverityLabel(finding.severity, finding)" in render
    assert "riskRuleLabel(finding.rule_id, finding)" in render
    assert "riskDimensionLabel(finding.dimension, finding)" in render
    assert "riskSurfaceLabel(finding.surface, finding)" in render
    assert "riskEvidenceSummary(finding)" in render
    assert "内部规则 code" in render
    assert "内部路径" in render
