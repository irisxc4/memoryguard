"""交互面板结构测试。

验证意图：神经图用于查看内容，治理动作统一进入治理台；其余治理视图必须复用同一套视觉语义。
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.interactive import render_interactive_html  # noqa: E402


def test_neuron_graph_uses_status_rail_for_node_detail() -> None:
    html = render_interactive_html()

    assert '<link rel="icon" type="image/png" href="memoryguard-icon.png">' in html
    stage = html.index('id="neuron-stage"')
    rail = html.index('id="status-rail"')

    assert rail < stage
    assert "当前数据源映射" in html
    assert "原生记忆投影" in html
    assert "重构治理投影" in html
    assert "证据/萃取来源" in html
    assert "toggleProjectionSource" not in html
    assert "refreshNeuronGraph" in html
    assert "publishReconstructedMemory" not in html
    assert "publishToAgentNativeTarget" not in html
    assert "choose_publish_target_path" not in html
    assert "原生记忆文件保持只读" in html
    assert "选择写回目标编号" not in html
    assert "rollbackNativeMemoryRelease" not in html
    assert "showNativeReleaseArchive" not in html
    assert "发布存档" not in html
    assert "manifest_path" not in html
    assert "showRollbackModal" not in html
    assert "confirmRollbackModal" not in html
    assert "选择要恢复的版本编号" not in html
    assert "rollback_native_memory_release" not in html
    assert "await refreshNeuronGraph(shared ?" in html
    assert "await refreshNeuronGraph('当前投影已删除" in html
    assert "已写入 Agent 原生记忆入口" not in html
    assert "renderNeuronMetaBar" in html
    assert "${groupLabel} ·" in html
    assert "成员 · 无绑定" in html
    assert "点击任意光点，在右侧查看可读内容" in html
    assert "规则与习惯可直接在图内治理" in html
    assert "selectedNeuronNode" in html
    assert "findNeuronByMemory" in html
    assert "focusNeuronNode" in html
    assert "自动纳入重构" in html
    assert ",'accept')" not in html
    assert "renderNeuronRailDetail" in html
    assert "selectNeuronByMemory" in html
    assert "cyInstance.animate({ center: { eles: cyNode }" in html
    assert "flashClass('pulse'" in html
    assert "startNeuronSignalPulses" in html
    assert "pickNeuronSignalPath" in html
    assert "edge.signal" in html
    assert "mapData(strength, 0, 1, 2.4, 4.8)" in html
    assert "mapData(strength, 0, 1, 1.5, 3.0)" in html
    assert "相关连线（点击跳转）" in html
    assert "突触末梢（点击跳转）" in html
    assert "selectNeuronByMemory" in html
    assert "同源跨类型" in html
    assert "相似关联" in html
    assert "legend-edge shared" in html
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
    assert "同源突触" in html
    assert "记忆末梢" in html
    assert "来源/类型主题" in html
    assert "node.derivation" in html
    assert 'edge[etype = "duplicate"]' in html
    assert "构建重构投影（含 LLM 整理）" in html
    assert "重构治理投影用于自动治理、萃取并发布回原生记忆" not in html
    assert "共享图直接读取 SharedMemoryStore" in html


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
    assert "displayBody(node) || node.body || '暂无正文内容'" in html
    assert "相关连线（点击跳转）" in html
    assert "接受候选" not in html


def test_lifecycle_ui_matches_backend_enums_and_residual_split() -> None:
    html = render_interactive_html()

    assert "installed_no_data: '已安装无数据'" in html
    assert "data_only: '原生数据待接入'" in html
    assert "uncertain: '待确认'" in html
    assert "agentCardsData.residuals" in html
    assert "残留与清理" in html
    assert "Agent 摘要" in html
    assert "已接入" in html
    assert "已发现 · 待接入" in html
    assert "新建个人记忆层" in html
    assert "可接入 MemoryGuard 层" in html
    assert "const items = result.items || []" in html


def test_multi_agent_ui_keeps_existing_personal_and_shared_groups_selectable() -> None:
    html = render_interactive_html()

    assert "callApi('list_share_groups')" in html
    assert "showMultiAgentBinding(agentsResult, bindingsResult, hooksResult, groupsResult)" in html
    assert "data-existing-group-agent" in html
    assert "bindSelectedExistingGroup" in html
    assert "绑定已有记忆组" in html
    assert "已有记忆组" in html
    assert "当前没有绑定 Agent；仍可重新接入" in html


def test_discovery_ui_exposes_existing_groups_without_requiring_native_memory() -> None:
    html = render_interactive_html()

    assert "const [result, groupsResult, bindingsResult] = await Promise.all" in html
    assert "showDiscoveryResult(result, groupsResult, bindingsResult)" in html
    assert "data-existing-group-agent" in html
    assert "接入已有记忆组" in html
    assert "新建个人记忆层" in html
    assert "导入原生记忆（可选）" in html
    assert "无原生记忆不影响接入" in html
    assert "管理已有记忆组" in html


def test_discovery_ui_discloses_profile_bounded_market_coverage() -> None:
    html = render_interactive_html()

    assert "known_profile_count" in html
    assert "未登记的新产品不会被猜测扫描" in html
    assert "外部 Profile 或手工来源接入" in html


def test_gui_normalizes_v2_audit_and_redacted_path_values() -> None:
    """V2 receipts and redacted path descriptors must not break the browser shell."""
    html = render_interactive_html()

    assert "function normalizeAuditReport" in html
    assert "Number.isFinite" in html
    assert "function guiPathText" in html
    assert "function guiPathLabel" in html
    assert "source_root_id" in html
    assert "source_root_id: c.dataset.sourceRootId" in html
    assert "(f.path || '').split(/[/\\\\]/)" not in html
    assert "const p = escapeHtml(f.path || '').replaceAll" not in html


def test_governance_actions_use_choices_and_explicit_scope() -> None:
    """治理写操作不能让用户手填路径/ID/原因，也不能隐式落到 default 组。"""
    html = render_interactive_html()

    assert "prompt(" not in html
    assert "createBuildPlan" not in html
    assert "showBuildTargetModal" not in html
    assert "生成构建计划" not in html
    assert "发布事务" not in html
    assert "完整替换受管目标文件" not in html
    assert "pickDecisionReason('takeover')" in html
    assert "list_share_groups" in html
    assert "selectGovernanceGroup" in html
    assert "callApi('get_recent_events', activeShareGroupId)" in html
    assert "callApi('get_supersede_decisions', activeShareGroupId)" in html
    assert "callApi('get_conflicts', activeShareGroupId)" in html
    assert "callApi('get_quarantine', activeShareGroupId)" in html
    assert "callApi('list_memory_versions', activeShareGroupId)" in html


def test_pywebview_bridge_prefers_dispatch_and_keeps_safe_legacy_fallback() -> None:
    html = render_interactive_html()

    assert "typeof bridge.dispatch_api === 'function'" in html
    assert "raw = await bridge.dispatch_api(method, args)" in html
    assert "typeof bridge.call_readonly === 'function'" in html
    assert "typeof bridge.request_mutation === 'function'" in html
    assert "await bridge.request_mutation(method, args)" in html
    assert "await bridge.call_readonly(method, args)" in html


def test_neuron_graph_uses_edge_bound_signal_particles() -> None:
    html = render_interactive_html()

    assert 'id="neuron-particles"' in html
    assert "animateNeuronEdgeParticle" in html
    assert "animateNeuronPathParticle" in html
    assert "raw * edges.length" in html
    assert "renderedPosition()" in html
    assert "control-point-distances" in html
    assert "Math.atan2" in html
    assert "isSignalNeuronEdge" in html
    assert "virtual_index: 0.46" in html
    assert "underlay-opacity" in html
    assert "const particle = document.createElement('span')" in html
    assert "const offsetX = cyRect.left - layerRect.left" in html
    assert "'overlay-opacity': .82" not in html
    assert "'overlay-opacity': .76" not in html
    assert "'overlay-opacity': .7" not in html
    assert "'line-opacity': 1, 'width': 'mapData(strength, 0, 1, 3.6, 6.4)'" not in html
    assert "neuron-edge-particle::after" not in html
    assert "neuron-edge-particle-core" in html
    assert "neuron-edge-particle-trail" in html
    assert "'shape': 'ellipse'" in html
    assert "round-rectangle" not in html
    assert "粒子层异常不能影响 Cytoscape 边/节点脉冲" in html
    assert "const starters = cy.nodes().filter" in html
    assert "const leafEdge = outgoers.find" in html
    assert "collectNeuronSignalPaths" in html
    assert "One continuous light travels branch -> leaf" in html
    assert "Always launch 5–8 concurrent full-path pulses" in html
    assert "const delay = index * 160" in html
    assert "underlay-opacity': 0" in html
    assert "Math.random()" not in html[html.index("function buildNeuronSignalPath"):html.index("function fitNeuronGraph")]
    assert "const initialWave = setTimeout(fireWave, 360)" in html
    assert "neuron-particle-travel" not in html


def test_risk_signals_offer_agent_handoff_without_blind_auto_fix() -> None:
    """不可自动修复的风险也必须有明确处理出口，并要求修复后复扫。"""
    html = render_interactive_html()

    assert "复制全部风险给 Agent" in html
    assert "复制给 Agent 处理" in html
    assert "copyFindingForAgent" in html
    assert "copyAllFindingsForAgent" in html
    assert "完成后重新扫描验证" in html
    assert "不要修改 MemoryGuard 的来源文件" in html
    assert "aria-expanded=\"${index === 0 ? 'true' : 'false'}\"" in html
    assert "index === 0 ? '收起详情' : '展开详情'" in html
    assert "element.closest('.finding-item')" in html


def test_reader_language_has_explicit_chinese_and_english_modes() -> None:
    """语言开关应表达显示语言，而不是含义不明的“原文”。"""
    html = render_interactive_html()

    assert "setReaderLanguage('auto')" in html
    assert "setReaderLanguage('zh')" in html
    assert "setReaderLanguage('en')" in html
    assert 'id="reader-original"' not in html
    assert ">原文</button>" not in html
    assert "无英文版本时显示来源原文" in html


def test_takeover_success_is_not_relabelled_as_refresh_failure() -> None:
    """正式接管提交成功后，投影刷新失败只能作为刷新警告。"""
    html = render_interactive_html()

    assert "正式接管已确认" in html
    assert "正式接管已完成，但神经图刷新失败" in html
    assert "await refreshNeuronGraph(`正式接管完成" not in html


def test_personal_and_shared_memory_layers_are_distinct_and_reachable() -> None:
    """个人层不能混进共享组解散区，且切换、安装和图谱入口都必须可达。"""
    html = render_interactive_html()

    assert "function memoryGroupKind(groupId)" in html
    assert "个人记忆层" in html
    assert "共享记忆层" in html
    assert "viewMemoryLayer" in html
    assert "installMemoryGroupMcp" in html
    assert "activeBindings = existingBindings.filter(b => b.status === 'active')" in html
    assert "b && b.group_kind === 'shared'" in html
    assert "确认启用该 Agent 的个人记忆层并安装全局 MCP" in html
    assert "MCP 未完整安装" in html
    assert "选择个人或共享记忆层" in html


def test_memory_layer_lifecycle_actions_are_explicit_and_safe() -> None:
    html = render_interactive_html()

    assert "showMemorySourceMap" in html
    assert "exportMemoryGroup" in html
    assert "clearMemoryGroup" in html
    assert "archiveMemoryGroup" in html
    assert "系统会先自动导出可恢复 ZIP" in html
    assert "保留 binding、MCP 配置和空数据库" in html
    assert "现有 MCP 将因无活动 binding 而拒绝读写" in html
    assert "原生文件不变" in html


def test_history_ui_routes_result_types_and_exposes_export() -> None:
    html = render_interactive_html()

    assert "r.matched_summary || r.summary" in html
    assert "r.can_timeline && anchor" in html
    assert 'data-mg-action="history-read-session"' in html
    assert 'data-session-id="${escapeHtml(r.session_id)}"' in html
    assert "exportHistorySession" in html
    assert "callApi('export_history', [sessionId], historyScope())" in html


def test_history_ui_requires_real_agent_and_refreshes_on_agent_switch() -> None:
    html = render_interactive_html()

    assert "agent_instance_id: activeAgentInstanceId || ''" in html
    assert "if (!activeAgentInstanceId)" in html
    assert "if (state.activeTab === 'history') renderHistory()" in html


def test_history_ui_hides_cross_owner_delete_action() -> None:
    html = render_interactive_html()

    assert "const canDelete = !!activeAgentInstanceId && owner === activeAgentInstanceId;" in html
    assert "仅会话 owner 可删除" in html
    assert "${canDelete ? `<button class=\"btn\" data-mg-action=\"history-delete\"" in html
    assert "当前共享组成员可查，仅 owner 可删" in html
    assert "meta.project_status === 'unknown' ? ' · 未识别项目'" not in html
    assert "meta.project_status === 'removed' ? ' · 路径已移除' : ''" in html


def test_data_sources_render_as_folder_tree() -> None:
    html = render_interactive_html()

    assert "const buildFileTree" in html
    assert 'class="folder-group"' in html
    assert 'class="folder-row"' in html
    assert "${count} 个文件" in html
    assert '<div class="folder-children">${renderFileTree(child, depth + 1)}</div>' in html


def test_history_projects_render_as_collapsible_folders() -> None:
    html = render_interactive_html()

    assert 'class="card history-project-group folder-group"' in html
    assert "${sessionCount} 个会话" in html
    assert '<div class="folder-children">${agents}</div>' in html


def test_rule_habits_render_as_collapsible_folders() -> None:
    html = render_interactive_html()

    assert '<details class="folder-group" open style="--folder-depth:0">' in html
    assert '<summary class="folder-row"><span class="folder-caret" aria-hidden="true"></span><span class="folder-name">${label}</span><span class="folder-count">${cards.length} 条</span></summary>' in html
    assert '<div class="folder-children">${cards.join(\'\')}</div>' in html
    assert "const mergedHtml = mergedCount ? `<span class=\"chip chip-info\">已合并 ${mergedCount} 条旧记忆</span>` : '';" in html


def test_neuron_drag_carries_descendant_branches() -> None:
    html = render_interactive_html()

    assert "const collectNeuronSubtree = (rootId) => {" in html
    assert "if (!dragRoot.selected()) dragRoot.select();" in html
    assert "const dragNodeIds = new Set([dragRoot.id()]);" in html
    assert "collectNeuronSubtree(dragRoot.id()).forEach(id => dragNodeIds.add(id));" in html
    assert "dragNodeIds.forEach(nodeId => {" in html


def test_neuron_graph_bridges_missing_parent_edges() -> None:
    html = render_interactive_html()

    assert "const edgeKeys = new Set();" in html
    assert "parent-bridge:" in html
    assert "主光点到分类不会悬空" in html


def test_projection_build_ui_closes_start_cancel_and_error_states() -> None:
    html = render_interactive_html()

    assert "let activeBuildRunId = '';" in html
    assert '<button class="btn" type="button" disabled>正在创建任务…</button>' in html
    assert "pointer-events:none\">正在创建任务…" in html
    assert "phase: 'cancelling', message: '正在取消…'" in html
    assert "await restoreNeuronAfterBuild(apiErrorMessage(result || {}, '取消失败'), true)" in html
    assert "await restoreNeuronAfterBuild('构建已取消', false)" in html
    assert "{ id: 'engine', label: '引擎' }" in html
    assert "{ id: 'enrich', label: '整理' }" in html
