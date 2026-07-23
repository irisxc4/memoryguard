"""交互面板结构测试。

验证意图：神经图用于查看内容，治理动作统一进入治理台；其余治理视图必须复用同一套视觉语义。
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.interactive import render_interactive_html  # noqa: E402


def test_neuron_graph_uses_status_rail_for_node_detail() -> None:
    html = render_interactive_html()

    stage = html.index('id="neuron-stage"')
    rail = html.index('id="status-rail"')

    assert rail < stage
    assert "当前数据源映射" in html
    assert "原生记忆投影" in html
    assert "重构治理投影" in html
    assert "证据/萃取来源" in html
    assert "toggleProjectionSource" not in html
    assert "refreshNeuronGraph" in html
    assert "publishReconstructedMemory" in html
    assert "list_publish_targets" in html
    assert "choose_publish_target_path" not in html
    assert "Agent 原生记忆入口" in html
    assert "选择写回目标编号" not in html
    assert "rollbackNativeMemoryRelease" in html
    assert "showNativeReleaseArchive" not in html
    assert "发布存档" not in html
    assert "manifest_path" not in html
    assert "showRollbackModal" in html
    assert "confirmRollbackModal" in html
    assert "选择要恢复的版本编号" not in html
    assert "确认恢复" in html
    assert "await refreshNeuronGraph(native ?" in html
    assert "await refreshNeuronGraph('当前投影已删除" in html
    assert "await refreshNeuronGraph(`发布完成" in html
    assert "await refreshNeuronGraph(`回滚完成" in html
    assert "点击任意光点，在右侧查看可读内容" in html
    assert "selectedNeuronNode" in html
    assert "findNeuronByMemory" in html
    assert "focusNeuronNode" in html
    assert "自动纳入重构" in html
    assert ",'accept')" not in html
    assert "renderNeuronRailDetail" in html
    assert "selectNeuronByMemory" in html
    assert "cyInstance.animate({ center: { eles: cyNode }" in html
    assert "flashClass('pulse'" in html
    assert "删除/排除" in html
    assert "阅读语言" in html
    assert "setReaderLanguage('zh')" in html
    assert "displayTitle" in html
    assert "displayBody" in html
    assert "background-color': 'data(bg)'" in html
    assert "kindColor" in html
    assert "name: 'preset'" in html
    assert "neuronNodePositions" in html
    assert "接受候选" not in html
    assert "确认合并" not in html


def test_import_copy_treats_conversations_as_evidence_not_memory() -> None:
    html = render_interactive_html()

    assert "会话内容默认只作为证据/萃取来源" in html
    assert "不直接写入长期记忆" in html
    assert "会话解析为 MemoryRecord 写入 IR" not in html


def test_all_views_share_the_neural_visual_system() -> None:
    html = render_interactive_html()

    assert "--accent: #6ee7c4" in html
    assert 'class="brand-orb"' in html
    assert 'class="flow-canvas"' in html
    assert 'class="finding-item' in html
    assert 'class="plan-item' in html
    assert 'class="neuron-shell"' in html


def test_three_column_governance_shell_is_default_structure() -> None:
    html = render_interactive_html()

    assert 'class="app-shell"' in html
    assert 'class="sidebar"' in html
    assert 'class="main-wrapper"' in html
    assert 'class="status-rail"' in html
    assert "width: 224px" in html
    assert "width: 280px" in html
    assert "@media (max-width: 1024px)" in html
    assert "@media (max-width: 1100px)" not in html


def test_governance_flow_cards_navigate_to_real_queues() -> None:
    html = render_interactive_html()

    assert "flow-card cyan" in html
    assert "flow-card gray" in html
    assert "flow-card amber" in html
    assert "flow-card red" in html
    assert "switchGovernanceSub('recent_events')" in html
    assert "switchGovernanceSub('supersede')" in html
    assert "switchGovernanceSub('conflicts')" in html
    assert "switchGovernanceSub('quarantine')" in html


def test_agent_data_view_consumes_scopes_not_legacy_categories() -> None:
    html = render_interactive_html()

    assert "agentData.scopes" in html
    assert "const renderScope" in html
    assert "const renderCategory" in html
    assert "agentData && agentData.categories" not in html


def test_discovered_native_memory_rows_are_not_clickable_files() -> None:
    html = render_interactive_html()

    assert "const canOpen = !!f.root_id && f.authorized !== false && f.read_status !== 'discovered'" in html
    assert "仅发现，需先授权" in html
    assert "该条目只是发现结果，尚未授权为可读取来源" in html


def test_neuron_graph_selection_uses_rail_context_not_inner_panel() -> None:
    html = render_interactive_html()

    assert "neuron-detail-panel" not in html
    assert "进入治理台" in html
    assert "点击任意光点，在右侧查看可读内容" in html
    assert "displayBody(node) || '暂无正文内容'" in html
    assert "接受候选" not in html


def test_lifecycle_ui_matches_backend_enums_and_residual_split() -> None:
    html = render_interactive_html()

    assert "installed_no_data: '已安装无数据'" in html
    assert "data_only: '仅数据残留'" in html
    assert "uncertain: '待确认'" in html
    assert "agentCardsData.residuals" in html
    assert "残留与清理" in html
    assert "Agent 摘要" in html
    assert "const items = result.items || []" in html
