"""MemoryGuard 交互式本地治理面板。

界面通过 pywebview JS API 调用本地 Python 后端，完成只读审计、
记忆神经图浏览、人工治理、修复计划、应用与撤销。
"""

from __future__ import annotations


def render_interactive_html() -> str:
    """返回自包含 CSS/JS 的交互式治理面板 HTML。"""
    return _HTML_TEMPLATE


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemoryGuard · 本地记忆治理</title>
<script src="cytoscape.min.js"></script>
<style>
:root {
  color-scheme: dark;
  --bg: #040b09;
  --bg-raised: #08120f;
  --panel: rgba(10, 25, 21, 0.86);
  --panel-solid: #0b1a16;
  --panel-bright: #10251f;
  --fg: #e4f5ef;
  --muted: #78988d;
  --faint: #48685e;
  --line: rgba(110, 231, 196, 0.16);
  --line-strong: rgba(110, 231, 196, 0.34);
  --accent: #6ee7c4;
  --accent-bright: #bcffeb;
  --accent-soft: rgba(110, 231, 196, 0.10);
  --red: #ff7d88;
  --orange: #e9bb64;
  --green: #6ee7c4;
  --blue: #6ee7c4;
  --purple: #9be8d4;
  --shadow: 0 24px 70px rgba(0, 0, 0, 0.32);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  position: relative; overflow: hidden;
  background:
    radial-gradient(circle at 14% 12%, rgba(48, 170, 133, 0.10), transparent 30rem),
    radial-gradient(circle at 84% 82%, rgba(78, 150, 125, 0.07), transparent 34rem),
    var(--bg);
  color: var(--fg);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  line-height: 1.55;
}
body::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .42;
  background-image:
    linear-gradient(var(--line) 1px, transparent 1px),
    linear-gradient(90deg, var(--line) 1px, transparent 1px);
  background-size: 56px 56px;
  mask-image: radial-gradient(circle at center, black 0, transparent 78%);
}
button, input, select { font: inherit; }
button:focus-visible, [role="tab"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
code {
  padding: .15em .4em; border: 1px solid var(--line); border-radius: 5px;
  background: rgba(110, 231, 196, .06); color: #b9d8ce; font-size: .88em;
  overflow-wrap: anywhere;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(110, 231, 196, .22); border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }

/* 三栏布局：侧栏 + 主工作区 + 状态栏 */
.app-shell { display: flex; height: 100%; overflow: hidden; }

/* 左侧导航 224px */
.sidebar {
  position: relative; z-index: 10; width: 224px; flex: none;
  display: flex; flex-direction: column; padding: 18px 0;
  border-right: 1px solid var(--line); background: rgba(4, 11, 9, .88);
  backdrop-filter: blur(20px);
}
.sidebar-brand { display: flex; align-items: center; gap: 12px; padding: 0 20px 18px; }
.brand-orb {
  position: relative; width: 28px; height: 28px; flex: none; border-radius: 50%;
  border: 1px solid rgba(188, 255, 235, .74);
  background: radial-gradient(circle at 38% 34%, #e4fff7 0 6%, var(--accent) 8% 19%, #163d33 44%, #07120f 72%);
  box-shadow: 0 0 20px rgba(110, 231, 196, .35), inset 0 0 12px rgba(110, 231, 196, .38);
}
.brand-orb::before, .brand-orb::after {
  content: ""; position: absolute; top: 50%; left: 50%; height: 1px;
  transform-origin: left; background: linear-gradient(90deg, var(--accent), transparent);
}
.brand-orb::before { width: 18px; transform: rotate(35deg); }
.brand-orb::after { width: 15px; transform: rotate(150deg); }
.brand-copy strong { display: block; font-size: 15px; letter-spacing: .06em; }
.brand-copy span { display: block; color: var(--muted); font-size: 10px; letter-spacing: .18em; text-transform: uppercase; }
.sidebar-nav { flex: 1; padding: 0 10px; overflow-y: auto; }
.nav-section-label { padding: 14px 10px 6px; color: var(--faint); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 9px; padding: 9px 10px; margin-bottom: 2px;
  color: var(--muted); cursor: pointer; font-size: 12px; border-radius: 8px; transition: all .16s ease;
}
.nav-item::before { content: ""; width: 5px; height: 5px; border: 1px solid var(--faint); border-radius: 50%; transition: all .16s ease; flex: none; }
.nav-item:hover { color: var(--fg); background: rgba(110, 231, 196, .04); }
.nav-item.active { color: var(--accent-bright); background: rgba(110, 231, 196, .10); }
.nav-item.active::before { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 10px var(--accent); }
.nav-item .count { min-width: 18px; text-align: right; color: var(--faint); font-size: 10px; flex: 1; }
.sidebar-footer { padding: 12px 20px 0; border-top: 1px solid var(--line); }
.reader-toggle { display: grid; gap: 8px; margin-bottom: 12px; }
.reader-toggle-label { color: var(--faint); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.reader-toggle-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.reader-toggle button { padding: 7px 8px; border: 1px solid var(--line); border-radius: 8px; background: rgba(110,231,196,.04); color: var(--muted); font-size: 11px; cursor: pointer; }
.reader-toggle button.active { color: var(--accent-bright); border-color: rgba(110,231,196,.42); background: rgba(110,231,196,.12); }
.local-badge { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 10px; }
.local-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }

/* 主工作区 */
.main-wrapper { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.topbar {
  position: relative; z-index: 9; min-height: 56px; padding: 0 24px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  border-bottom: 1px solid var(--line); background: rgba(4, 11, 9, .64); backdrop-filter: blur(14px);
}
.topbar-left { display: flex; align-items: center; gap: 16px; min-width: 0; }
.topbar-right { display: flex; align-items: center; gap: 10px; flex: none; }
.ws-path { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.health-badge {
  display: inline-flex; align-items: center; gap: 7px; padding: 5px 10px;
  color: var(--fg); border: 1px solid var(--line); border-radius: 999px;
  background: rgba(110, 231, 196, .06); font-size: 12px; font-weight: 600;
}
.health-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 10px currentColor; }
.btn {
  min-height: 34px; padding: 7px 13px; border: 1px solid var(--line-strong); border-radius: 8px;
  background: rgba(110, 231, 196, .04); color: var(--fg); cursor: pointer; font-size: 12px;
  transition: transform .16s ease, border-color .16s ease, background .16s ease, box-shadow .16s ease;
}
.btn:hover { transform: translateY(-1px); border-color: rgba(110, 231, 196, .62); background: rgba(110, 231, 196, .09); }
.btn:active { transform: translateY(0); }
.btn-primary { border-color: var(--accent); background: var(--accent); color: #062019; font-weight: 700; box-shadow: 0 0 18px rgba(110, 231, 196, .12); }
.btn-primary:hover { background: var(--accent-bright); box-shadow: 0 0 24px rgba(110, 231, 196, .22); }
.btn-danger { border-color: rgba(255, 125, 136, .48); color: var(--red); }
.btn-danger:hover { border-color: var(--red); background: rgba(255, 125, 136, .08); }
.btn-icon { min-width: 32px; padding: 5px 8px; }
.btn:disabled { opacity: .38; cursor: not-allowed; transform: none; }

.content {
  position: relative; z-index: 1; flex: 1; width: 100%;
  overflow: auto; padding: 24px 28px 38px;
}

/* 右侧状态栏 280px */
.status-rail {
  width: 280px; flex: none; padding: 20px 18px; overflow-y: auto;
  border-left: 1px solid var(--line); background: rgba(4, 11, 9, .72); backdrop-filter: blur(14px);
}
.status-rail h3 { margin-bottom: 14px; font-size: 10px; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
.status-item {
  display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 12px; margin-bottom: 7px;
  border: 1px solid var(--line); border-radius: 10px; background: rgba(110, 231, 196, .03);
  cursor: pointer; transition: border-color .16s ease, background .16s ease, transform .16s ease;
}
.status-item:hover { border-color: var(--line-strong); background: rgba(110, 231, 196, .07); transform: translateX(-2px); }
.status-item .status-label { color: var(--muted); font-size: 12px; }
.status-item .status-num { font-size: 18px; font-weight: 560; color: var(--accent-bright); }
.status-item.alert .status-num { color: var(--orange); }
.status-item.danger .status-num { color: var(--red); }
.status-item.zero .status-num { color: var(--faint); }
.status-rail .rail-link { display: block; margin-top: 12px; padding: 8px 12px; color: var(--muted); font-size: 11px; border: 1px solid var(--line); border-radius: 8px; text-align: center; cursor: pointer; transition: all .16s ease; }
.status-rail .rail-link:hover { color: var(--accent); border-color: var(--line-strong); }

/* 概念图：Governance Flow 事件卡 */
.flow-canvas { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.flow-card {
  position: relative; overflow: hidden; padding: 18px; min-height: 130px;
  border: 1px solid var(--line); border-radius: 14px; cursor: pointer;
  background: linear-gradient(145deg, rgba(15, 35, 29, .82), rgba(7, 18, 15, .78));
  transition: border-color .16s ease, transform .16s ease;
}
.flow-card:hover { transform: translateY(-2px); }
.flow-card::before { content: ""; position: absolute; top: 0; left: 0; width: 3px; height: 100%; }
.flow-card.cyan::before { background: var(--accent); box-shadow: 0 0 14px var(--accent); }
.flow-card.gray::before { background: var(--faint); }
.flow-card.amber::before { background: var(--orange); box-shadow: 0 0 14px var(--orange); }
.flow-card.red::before { background: var(--red); box-shadow: 0 0 14px var(--red); }
.flow-card .flow-kicker { font-size: 9px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
.flow-card.cyan .flow-kicker { color: var(--accent); }
.flow-card.amber .flow-kicker { color: var(--orange); }
.flow-card.red .flow-kicker { color: var(--red); }
.flow-card .flow-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.flow-card .flow-body { color: var(--muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.flow-card .flow-time { margin-top: 8px; color: var(--faint); font-size: 10px; }
.flow-card.empty { cursor: default; }
.flow-card.empty:hover { transform: none; }
.flow-arrow { display: flex; align-items: center; justify-content: center; color: var(--faint); font-size: 20px; }
.view-heading { margin-bottom: 18px; }
.eyebrow { color: var(--accent); font-size: 10px; letter-spacing: .18em; text-transform: uppercase; }
.view-heading h2 { margin-top: 4px; font-size: 24px; font-weight: 560; letter-spacing: -.03em; }
.view-heading p { max-width: 720px; margin-top: 5px; color: var(--muted); font-size: 13px; }
.card {
  position: relative; overflow: hidden; padding: 20px; margin-bottom: 14px;
  border: 1px solid var(--line); border-radius: 14px;
  background: linear-gradient(145deg, rgba(15, 35, 29, .82), rgba(7, 18, 15, .78));
  box-shadow: 0 18px 50px rgba(0, 0, 0, .10);
}
.card::before { content: ""; position: absolute; top: -2px; left: 22px; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px var(--accent); }
.card h2 { margin-bottom: 14px; font-size: 13px; font-weight: 650; letter-spacing: .04em; }
.card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.card-head h2 { margin: 0; }
.card-head p { color: var(--muted); font-size: 12px; }

/* 总览 */
.overview-hero { display: grid; grid-template-columns: minmax(220px, .75fr) minmax(0, 2fr); gap: 14px; margin-bottom: 14px; }
.health-orbit { min-height: 238px; display: grid; place-items: center; }
.health-ring {
  --health-angle: 0deg; position: relative; width: 164px; height: 164px; display: grid; place-items: center;
  border-radius: 50%; background: conic-gradient(var(--accent) var(--health-angle), rgba(110, 231, 196, .08) 0);
  box-shadow: 0 0 40px rgba(110, 231, 196, .08);
}
.health-ring::before { content: ""; position: absolute; inset: 10px; border-radius: 50%; background: radial-gradient(circle, #10251f, #07120f 72%); border: 1px solid var(--line); }
.health-ring::after { content: ""; position: absolute; inset: -8px; border: 1px dashed rgba(110, 231, 196, .20); border-radius: 50%; }
.health-ring-content { position: relative; z-index: 1; text-align: center; }
.health-ring-content strong { display: block; color: var(--accent-bright); font-size: 44px; font-weight: 530; line-height: 1; letter-spacing: -.05em; }
.health-ring-content span { color: var(--muted); font-size: 11px; letter-spacing: .14em; }
.metrics { display: grid; grid-template-columns: repeat(2, minmax(130px, 1fr)); gap: 10px; }
.metric {
  position: relative; min-height: 112px; padding: 18px; overflow: hidden;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(110, 231, 196, .035);
}
.metric::after { content: ""; position: absolute; right: -12px; bottom: -12px; width: 58px; height: 58px; border: 1px solid var(--line); border-radius: 50%; box-shadow: 0 0 24px rgba(110, 231, 196, .05); }
.metric .num { color: var(--accent-bright); font-size: 30px; font-weight: 530; line-height: 1.2; letter-spacing: -.04em; }
.metric .label, .metric > div:last-child { margin-top: 8px; color: var(--muted); font-size: 11px; }
.overview-grid { display: grid; grid-template-columns: 1.3fr .7fr; gap: 14px; }
.scan-list { display: grid; gap: 10px; }
.scan-row { display: flex; justify-content: space-between; gap: 16px; padding-bottom: 9px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; }
.scan-row:last-child { border-bottom: 0; padding-bottom: 0; }
.scan-row strong { color: var(--fg); font-weight: 500; text-align: right; }

/* 状态与列表 */
.chips { display: flex; flex-wrap: wrap; gap: 7px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px;
  border: 1px solid var(--line); border-radius: 999px; color: var(--muted); background: rgba(255,255,255,.018);
  font-size: 10px; line-height: 1.5;
}
.chip::before { content: ""; width: 4px; height: 4px; border-radius: 50%; background: currentColor; box-shadow: 0 0 7px currentColor; }
.chip-critical, .chip-high { color: var(--red); border-color: rgba(255,125,136,.28); }
.chip-medium { color: var(--orange); border-color: rgba(233,187,100,.28); }
.chip-low, .chip-confirmed { color: var(--accent); }
.chip-info, .chip-tentative { color: #9fc4b8; }
.modal-backdrop { position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center; padding: 22px; background: rgba(0,0,0,.58); backdrop-filter: blur(7px); }
.modal-card { width: min(720px, 96vw); max-height: min(720px, 88vh); overflow: auto; border: 1px solid var(--line-strong); border-radius: 18px; background: rgba(5, 18, 14, .98); box-shadow: 0 24px 80px rgba(0,0,0,.48); }
.modal-head { padding: 18px 20px 12px; border-bottom: 1px solid var(--line); }
.modal-head h3 { margin: 0 0 6px; font-size: 18px; }
.modal-head p { margin: 0; color: var(--muted); font-size: 12px; }
.modal-body { padding: 14px 20px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 20px 18px; border-top: 1px solid var(--line); }
.release-option { display: flex; gap: 12px; align-items: flex-start; padding: 12px 13px; margin-bottom: 9px; border: 1px solid var(--line); border-radius: 12px; cursor: pointer; background: rgba(255,255,255,.018); }
.release-option:hover { border-color: var(--line-strong); background: rgba(110,231,196,.045); }
.release-option input { margin-top: 3px; accent-color: var(--accent); }
.release-title { font-weight: 700; font-size: 13px; }
.release-meta { margin-top: 4px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.finding-item, .plan-item {
  position: relative; padding: 15px 16px 15px 22px; margin-bottom: 9px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(10, 26, 21, .70);
  transition: border-color .16s ease, background .16s ease, transform .16s ease;
}
.finding-item::before, .plan-item::before { content: ""; position: absolute; top: 21px; left: 10px; width: 4px; height: 4px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.finding-item:hover { transform: translateX(2px); border-color: var(--line-strong); background: rgba(16, 39, 32, .72); }
.finding-item.sev-high::before, .finding-item.sev-critical::before, .plan-item.failed::before { background: var(--red); box-shadow: 0 0 8px var(--red); }
.finding-item.sev-medium::before { background: var(--orange); box-shadow: 0 0 8px var(--orange); }
.finding-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.finding-rule { font-size: 12px; font-weight: 650; }
.finding-evidence { margin-top: 6px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.finding-detail { margin-top: 12px; padding: 12px 0 0; border-top: 1px solid var(--line); font-size: 12px; }
.row { display: flex; align-items: flex-start; gap: 10px; margin: 5px 0; }
.key { min-width: 72px; color: var(--muted); }
.finding-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.empty-state { min-height: 210px; display: grid; place-items: center; text-align: center; color: var(--muted); }
.empty-state .empty-orb { width: 44px; height: 44px; margin: 0 auto 14px; border: 1px solid var(--line-strong); border-radius: 50%; box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06); }

/* 对象表 */
.table-shell { overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 11px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .10em; text-transform: uppercase; }
tbody tr { transition: background .15s ease; }
tbody tr:hover { background: rgba(110, 231, 196, .035); }
tbody tr:last-child td { border-bottom: 0; }
.filter-bar { display: flex; gap: 8px; margin-bottom: 14px; }
.scope-tabs {
  display: flex; gap: 4px; padding: 4px; margin-bottom: 16px;
  border: 1px solid var(--line); border-radius: 10px;
  background: rgba(10, 26, 21, .50);
}
.scope-tab {
  flex: 1; padding: 8px 14px; border-radius: 7px; text-align: center;
  color: var(--muted); cursor: pointer; font-size: 12px; letter-spacing: .04em;
  transition: all .16s ease;
}
.scope-tab:hover { color: var(--fg); background: rgba(110, 231, 196, .05); }
.scope-tab.active {
  color: var(--accent-bright); background: rgba(110, 231, 196, .12);
  box-shadow: inset 0 0 0 1px rgba(110, 231, 196, .35);
}
.scope-tab .count { display: inline-block; min-width: 18px; margin-left: 6px;
  padding: 1px 6px; border-radius: 999px; background: rgba(110, 231, 196, .12);
  color: var(--accent); font-size: 10px; }
.surface-row {
  display: grid; grid-template-columns: auto 1fr auto auto auto; gap: 10px; align-items: center;
  padding: 10px 12px; margin-bottom: 6px;
  border: 1px solid var(--line); border-radius: 9px;
  background: rgba(10, 26, 21, .50); transition: border-color .15s ease;
}
.surface-row:hover { border-color: var(--line-strong); }
.surface-row .surface-icon { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.surface-row .surface-icon.found { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.surface-row .surface-icon.missing { background: var(--faint); }
.surface-row .surface-icon.unsupported { background: var(--orange); }
.surface-row .surface-icon.permission_denied { background: var(--red); }
.surface-row .surface-path { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.surface-row .surface-path code { font-size: 11px; }
.surface-row .surface-meta { font-size: 10px; color: var(--muted); letter-spacing: .04em; }
 /* v3.2 Agent 卡片 */
 .agent-cards {
   display: flex; gap: 8px; padding: 8px 0; margin-bottom: 16px;
   overflow-x: auto; flex-wrap: wrap;
 }
 .agent-card {
   flex: none; padding: 12px 16px; border: 1px solid var(--line); border-radius: 12px;
   background: rgba(10, 26, 21, .50); cursor: pointer;
   transition: all .16s ease; min-width: 120px;
 }
 .agent-card:hover { border-color: var(--line-strong); background: rgba(16, 39, 32, .60); }
 .agent-card.active {
   border-color: var(--accent); background: rgba(110, 231, 196, .12);
   box-shadow: 0 0 18px rgba(110, 231, 196, .15);
 }
 .agent-card .agent-name { font-size: 13px; font-weight: 600; color: var(--fg); }
 .agent-card .agent-meta { font-size: 10px; color: var(--muted); margin-top: 4px; }
 .agent-card .agent-badge {
   display: inline-block; padding: 1px 6px; border-radius: 999px;
   background: rgba(110, 231, 196, .12); color: var(--accent);
   font-size: 9px; margin-top: 4px;
 }
 .agent-card.add-card { border-style: dashed; color: var(--muted); }
.plan-item { cursor: default; }
.plan-item.verified::before, .plan-item.applied::before { background: var(--accent); }

/* 神经元画布 */
.neuron-shell {
  position: relative; min-height: calc(100vh - 120px); overflow: hidden;
  border: 1px solid var(--line); border-radius: 18px;
  background: radial-gradient(circle at 50% 48%, rgba(37, 104, 83, .14), transparent 38%), rgba(3, 10, 8, .74);
  box-shadow: var(--shadow), inset 0 0 80px rgba(0, 0, 0, .30);
}
.neuron-shell::before {
  content: ""; position: absolute; inset: 0; pointer-events: none; opacity: .28;
  background-image: radial-gradient(circle, rgba(110,231,196,.28) 1px, transparent 1.4px);
  background-size: 24px 24px; mask-image: radial-gradient(circle at center, black, transparent 82%);
}
.neuron-toolbar {
  position: absolute; z-index: 12; top: 16px; left: 18px; right: 18px;
  display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; pointer-events: none;
}
.neuron-title, .canvas-actions, .neuron-legend, .neuron-stats, .merge-dock { pointer-events: auto; }
.neuron-title { max-width: 390px; }
.neuron-title .eyebrow { display: block; margin-bottom: 5px; }
.neuron-title h2 { font-size: 18px; font-weight: 560; }
.neuron-title p { margin-top: 4px; color: var(--muted); font-size: 11px; }
.canvas-actions { display: flex; gap: 8px; }
.neuron-stage { position: relative; width: 100%; height: calc(100vh - 120px); min-height: 610px; }
.neuron-canvas { position: absolute; inset: 0; }
.neuron-stats {
  position: absolute; z-index: 11; left: 18px; bottom: 18px; max-width: calc(100% - 390px);
  display: flex; flex-wrap: wrap; gap: 8px; padding: 8px;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(4, 13, 10, .78); backdrop-filter: blur(14px);
}
.neuron-stat { min-width: 82px; padding: 6px 10px; border-right: 1px solid var(--line); }
.neuron-stat:last-child { border-right: 0; }
.neuron-stat strong { display: block; color: var(--accent-bright); font-size: 16px; font-weight: 550; }
.neuron-stat span { color: var(--muted); font-size: 9px; letter-spacing: .08em; }
.neuron-legend {
  position: absolute; z-index: 11; right: 18px; top: 82px; padding: 10px 12px;
  border: 1px solid var(--line); border-radius: 10px; background: rgba(4, 13, 10, .76); backdrop-filter: blur(12px);
  color: var(--muted); font-size: 10px;
}
.legend-item { display: flex; align-items: center; gap: 7px; margin: 5px 0; }
.legend-node { width: 7px; height: 7px; border: 1px solid var(--accent); border-radius: 50%; box-shadow: 0 0 7px rgba(110,231,196,.48); }
.legend-node.tentative { border-style: dashed; border-color: var(--orange); box-shadow: none; }
.legend-node.anchor { width: 4px; height: 4px; border: 0; background: rgba(110,231,196,.56); }
.merge-dock {
  position: absolute; z-index: 15; right: 18px; bottom: 18px; width: 310px; max-height: 220px; overflow: auto;
  padding: 12px; border: 1px solid var(--line); border-radius: 12px;
  background: rgba(4, 13, 10, .84); backdrop-filter: blur(16px); box-shadow: 0 18px 48px rgba(0,0,0,.28);
}
.merge-dock h3 { margin-bottom: 8px; font-size: 11px; font-weight: 650; }
.merge-item { padding: 9px 0; border-top: 1px solid var(--line); }
.merge-item:first-of-type { border-top: 0; }
.merge-copy { color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }
.merge-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 7px; }
.merge-score { color: var(--accent); font-size: 10px; }
.neuron-popover {
  position: absolute; z-index: 30; width: min(360px, calc(100% - 28px)); max-height: min(470px, calc(100% - 32px));
  overflow: auto; padding: 16px; border: 1px solid rgba(110,231,196,.42); border-radius: 14px;
  background: rgba(7, 22, 17, .94); backdrop-filter: blur(22px); box-shadow: 0 24px 64px rgba(0,0,0,.46), 0 0 30px rgba(110,231,196,.09);
  opacity: 0; visibility: hidden; transform: translate(-50%, calc(-100% - 42px)) scale(.96);
  transform-origin: bottom center; transition: opacity .16s ease, transform .16s ease, visibility .16s;
}
.neuron-popover.show { opacity: 1; visibility: visible; transform: translate(-50%, calc(-100% - 42px)) scale(1); }
.neuron-popover.below { transform-origin: top center; transform: translate(-50%, 42px) scale(.96); }
.neuron-popover.below.show { transform: translate(-50%, 42px) scale(1); }
.neuron-popover::after {
  content: ""; position: absolute; left: 50%; bottom: -6px; width: 11px; height: 11px;
  transform: translateX(-50%) rotate(45deg); border-right: 1px solid rgba(110,231,196,.42); border-bottom: 1px solid rgba(110,231,196,.42); background: #071611;
}
.neuron-popover.below::after { top: -6px; bottom: auto; border: 0; border-left: 1px solid rgba(110,231,196,.42); border-top: 1px solid rgba(110,231,196,.42); }
.neuron-detail-body { color: #d5ebe3; font-size: 12px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.popover-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.popover-head h3 { font-size: 15px; font-weight: 600; }
.popover-kicker { color: var(--accent); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.claim-list { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }
.claim-list h4 { margin-bottom: 7px; color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .08em; }
.claim-item { position: relative; padding: 7px 0 7px 11px; color: #b8d0c8; font-size: 11px; border-bottom: 1px solid rgba(110,231,196,.08); }
.claim-item::before { content: ""; position: absolute; top: 13px; left: 0; width: 4px; height: 4px; border-radius: 50%; background: var(--accent); }
.claim-item:last-child { border-bottom: 0; }

.loading { min-height: 320px; display: grid; place-items: center; color: var(--muted); font-size: 12px; }
.loading::before { content: ""; width: 34px; height: 34px; margin-right: 12px; border: 1px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: pulse-spin 1.2s linear infinite; }
@keyframes pulse-spin { to { transform: rotate(360deg); } }
.toast {
  position: fixed; z-index: 100; right: 22px; bottom: 22px; min-width: 220px; padding: 11px 14px;
  border: 1px solid var(--line-strong); border-radius: 10px; background: rgba(7,22,17,.94); box-shadow: var(--shadow);
  color: var(--fg); font-size: 12px; opacity: 0; transform: translateY(8px); pointer-events: none;
  transition: opacity .22s ease, transform .22s ease;
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast.success { border-color: rgba(110,231,196,.52); }
.toast.error { border-color: rgba(255,125,136,.55); }

@media (max-width: 1024px) {
  .status-rail { display: none; }
}
@media (max-width: 900px) {
  .sidebar { width: 64px; }
  .sidebar-brand .brand-copy, .nav-item .count, .nav-section-label, .sidebar-footer { display: none; }
  .nav-item { justify-content: center; }
  .ws-path { display: none; }
  .content { padding: 18px 16px 28px; }
  .overview-grid, .flow-canvas { grid-template-columns: 1fr; }
  .neuron-shell, .neuron-stage { min-height: 680px; height: calc(100vh - 170px); }
  .neuron-stats { max-width: calc(100% - 36px); bottom: 18px; right: 18px; }
  .merge-dock { right: 18px; bottom: 112px; }
}
@media (max-width: 620px) {
  .brand-copy span, .health-badge { display: none; }
  .topbar-right .btn { padding-inline: 10px; }
  .neuron-title p, .neuron-legend { display: none; }
  .canvas-actions { flex-direction: column; }
  .merge-dock { width: calc(100% - 36px); max-height: 150px; bottom: 130px; }
  .neuron-stat { min-width: 62px; }
}

/* v3 投影门控 + 数据源 + 变更记录 */
.projection-gate {
  display: grid; grid-template-columns: 80px 1fr; gap: 22px; align-items: start;
  padding: 28px;
}
.projection-gate .gate-orb {
  width: 56px; height: 56px; margin-top: 6px; border-radius: 50%;
  border: 1px dashed var(--line-strong);
  background: radial-gradient(circle, rgba(110,231,196,.08), transparent 70%);
  box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06);
  animation: gate-pulse 2.4s ease-in-out infinite;
}
@keyframes gate-pulse {
  0%, 100% { box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06); }
  50% { box-shadow: 0 0 36px rgba(110,231,196,.22), inset 0 0 22px rgba(110,231,196,.10); }
}
.gate-body h3 { font-size: 16px; font-weight: 580; margin-bottom: 8px; }
.gate-reason { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.gate-warning {
  margin: 14px 0; padding: 12px 14px; border: 1px solid rgba(233,187,100,.32);
  border-radius: 9px; background: rgba(233,187,100,.06); color: var(--fg);
  font-size: 12px; line-height: 1.7;
}
.gate-warning strong { color: var(--orange); }

.raw-file-list { margin-top: 10px; display: grid; gap: 4px; }
.raw-file-row {
  display: flex; align-items: center; gap: 10px; padding: 8px 11px;
  border: 1px solid var(--line); border-radius: 8px; background: rgba(10,26,21,.50);
  cursor: pointer; transition: border-color .15s ease, background .15s ease, transform .15s ease;
}
.raw-file-row:hover { transform: translateX(2px); border-color: var(--line-strong); background: rgba(16,39,32,.72); }
.raw-file-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.raw-file-path code { font-size: 11px; }

.raw-file-content {
  margin: 0; padding: 16px; border: 1px solid var(--line); border-radius: 10px;
  background: rgba(4,13,10,.78); color: #cce5dc; font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
  font-size: 12px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 70vh; overflow: auto;
}
.source-map-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
.source-map-table { width: 100%; border-collapse: collapse; min-width: 980px; }
.source-map-table th, .source-map-table td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 12px; }
.source-map-table th { color: var(--muted); font-weight: 700; background: rgba(12,34,27,.72); }
.source-map-table tr:last-child td { border-bottom: 0; }
.path-cell { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.muted-row { opacity: .58; }
</style>
</head>
<body>
<div class="app-shell">
  <!-- 左侧导航 224px -->
  <aside class="sidebar">
    <div class="sidebar-brand">
      <span class="brand-orb" aria-hidden="true"></span>
      <span class="brand-copy"><strong>MemoryGuard</strong><span>Local governance</span></span>
    </div>
    <nav class="sidebar-nav" role="tablist" aria-label="治理模块">
      <div class="nav-section-label">治理视图</div>
      <div class="nav-item active" role="tab" tabindex="0" data-tab="overview" onclick="switchTab('overview')">总览</div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="sources" onclick="switchTab('sources')">数据源<span class="count" id="sources-count"></span></div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="neurons" onclick="switchTab('neurons')">记忆核心<span class="count" id="neuron-count"></span></div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="findings" onclick="switchTab('findings')">风险信号<span class="count" id="findings-count"></span></div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="releases" onclick="switchTab('releases')">变更记录<span class="count" id="releases-count"></span></div>
      <div class="nav-section-label">操作</div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="governance" onclick="switchTab('governance')">治理台</div>
    </nav>
    <div class="sidebar-footer">
      <div class="reader-toggle">
        <div class="reader-toggle-label">阅读语言</div>
        <div class="reader-toggle-buttons">
          <button type="button" id="reader-zh" class="active" onclick="setReaderLanguage('zh')">中文</button>
          <button type="button" id="reader-original" onclick="setReaderLanguage('original')">原文</button>
        </div>
      </div>
      <span class="local-badge">Local only · No telemetry</span>
    </div>
  </aside>

  <!-- 主工作区 -->
  <div class="main-wrapper">
    <header class="topbar">
      <div class="topbar-left">
        <span class="ws-path" id="ws-path">正在连接本地工作区…</span>
      </div>
      <div class="topbar-right">
        <span class="health-badge" id="health-badge">健康度 --</span>
        <button class="btn btn-primary" type="button" onclick="runAudit()">重新扫描</button>
      </div>
    </header>
    <main class="content" id="content"><div class="loading">正在建立本地治理视图</div></main>
  </div>

  <!-- 右侧状态栏 280px -->
  <aside class="status-rail" id="status-rail">
    <h3>治理状态</h3>
    <div id="status-rail-content"><div class="loading" style="min-height:120px">连接中…</div></div>
  </aside>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
let state = { report: null, activeTab: 'overview', plans: [], changes: [], releases: [], lastPlan: null, governanceSnapshot: null };
let neuronGraph = null;
let projectionMode = localStorage.getItem('memoryguard.projectionMode') || 'native';
let cyInstance = null;
let selectedNeuronId = null;
let selectedNeuronNode = null;
let readerLanguage = localStorage.getItem('memoryguard.readerLanguage') || 'zh';
let sourcesScope = 'all';      // 数据源 sub-tab: 'all' | 'user' | 'project'
let discoveryResult = null;    // 缓存 discover_agents 结果
let activeAgentInstanceId = '';  // v3.2：当前选中的 Agent 卡片
let agentCardsData = null;     // v3.2：缓存 list_agents 结果
let dataPageMode = 'single_agent';  // v3.2：single_agent | multi_agent_shared_mcp
let governanceSubTab = 'recent_events';  // 治理台子视图

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function setReaderLanguage(language) {
  readerLanguage = language;
  localStorage.setItem('memoryguard.readerLanguage', language);
  document.getElementById('reader-zh')?.classList.toggle('active', language === 'zh');
  document.getElementById('reader-original')?.classList.toggle('active', language === 'original');
  renderStatusRail();
}

function displayTitle(item) {
  if (readerLanguage === 'zh') return item.title_zh || item.zh_title || item.title || item.memory_id || '';
  return item.original_title || item.title || item.title_zh || item.memory_id || '';
}

function displayBody(item) {
  if (readerLanguage === 'zh') return item.body_zh || item.zh_summary || item.body || item.body_preview || '';
  return item.original_body || item.body || item.body_zh || item.body_preview || '';
}

let MUTATION_METHODS = null;  // 从后端动态加载

async function getMutationMethods() {
  if (MUTATION_METHODS !== null) return MUTATION_METHODS;
  try {
    const registry = await callApiRaw('get_api_method_registry');
    MUTATION_METHODS = new Set(registry.mutation || []);
  } catch(e) {
    MUTATION_METHODS = new Set();
  }
  return MUTATION_METHODS;
}

let _sandboxMode = null;

async function isSandboxMode() {
  if (_sandboxMode !== null) return _sandboxMode;
  if (window.__MG_SANDBOX__ !== undefined) { _sandboxMode = window.__MG_SANDBOX__; return _sandboxMode; }
  try {
    const r = await callApiRaw('get_sandbox_status');
    _sandboxMode = r.sandbox;
  } catch(e) { _sandboxMode = false; }
  return _sandboxMode;
}

async function callApiRaw(method, ...args) {
  if (window.pywebview && window.pywebview.api) {
    return await window.pywebview.api[method](...args);
  }
  const headers = {'Content-Type': 'application/json'};
  if (window.__MG_SESSION__) headers['X-Session-Token'] = window.__MG_SESSION__;
  const resp = await fetch('/api/' + method, { method: 'POST', headers, body: JSON.stringify(args) });
  if (!resp.ok) {
    const errBody = await resp.json().catch(() => ({}));
    throw new Error(errBody.error || ('API ' + method + ' 返回 ' + resp.status));
  }
  return await resp.json();
}

async function callApi(method, ...args) {
  // pywebview 模式：通过 call_readonly / request_mutation 桥接
  if (window.pywebview && window.pywebview.api) {
    const mutMethods = await getMutationMethods();
    if (mutMethods.has(method)) {
      // 变更方法：走 request_mutation 桥接
      const result = await window.pywebview.api.request_mutation(method, args);
      if (result && result.deferred) {
        showToast('请求已提交，已尝试唤醒桌面执行器。如未弹出确认窗口，请手动运行 memoryguard desktop', 'info');
      }
      return result;
    }
    // 只读方法：走 call_readonly 桥接
    return await window.pywebview.api.call_readonly(method, args);
  }
  // localhost 模式
  const headers = {'Content-Type': 'application/json'};
  if (window.__MG_SESSION__) headers['X-Session-Token'] = window.__MG_SESSION__;
  const resp = await fetch('/api/' + method, { method: 'POST', headers, body: JSON.stringify(args) });
  if (!resp.ok) {
    const errBody = await resp.json().catch(() => ({}));
    throw new Error(errBody.error || ('API ' + method + ' 返回 ' + resp.status));
  }
  const result = await resp.json();
  if (result && result.deferred) {
    showToast('请求已提交，已尝试唤醒桌面执行器。如未弹出确认窗口，请手动运行 memoryguard desktop', 'info');
  }
  return result;
}

function waitForPywebview(timeoutMs) {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve(true);
    let elapsed = 0;
    const interval = setInterval(() => {
      elapsed += 100;
      if (window.pywebview && window.pywebview.api) { clearInterval(interval); resolve(true); }
      else if (elapsed >= timeoutMs) { clearInterval(interval); resolve(false); }
    }, 100);
  });
}

async function init() {
  const ready = await waitForPywebview(5000);
  if (!ready) { showToast('GUI 桥接未就绪，请稍后重试', 'error'); return; }
  try { state.report = await callApi('get_audit'); renderAll(); }
  catch (e) { showToast('扫描失败：' + e, 'error'); }
}

async function runAudit() {
  setContent('<div class="loading">正在重新扫描工作区</div>');
  try { state.report = await callApi('run_audit'); showToast('扫描完成', 'success'); renderAll(); }
  catch (e) { showToast('扫描失败：' + e, 'error'); }
}

function switchTab(tab) {
  state.activeTab = tab;
  if (tab !== 'neurons') {
    selectedNeuronId = null;
    selectedNeuronNode = null;
  }
  document.querySelectorAll('.nav-item').forEach(el => {
    const active = el.dataset.tab === tab;
    el.classList.toggle('active', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  renderStatusRail();
  renderContent();
}

document.querySelectorAll('.nav-item').forEach(el => el.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); switchTab(el.dataset.tab); }
}));

function setContent(html) { document.getElementById('content').innerHTML = html; }

function renderAll() {
  if (!state.report) return;
  const r = state.report;
  document.getElementById('ws-path').textContent = r.workspace;
  const badge = document.getElementById('health-badge');
  document.getElementById('reader-zh')?.classList.toggle('active', readerLanguage === 'zh');
  document.getElementById('reader-original')?.classList.toggle('active', readerLanguage === 'original');
  badge.textContent = '健康度 ' + Math.round(r.health_score) + '/100';
  badge.style.color = r.health_score >= 70 ? 'var(--accent)' : r.health_score >= 40 ? 'var(--orange)' : 'var(--red)';
  document.getElementById('findings-count').textContent = r.findings.length || '';
  document.getElementById('sources-count').textContent = '';
  document.getElementById('releases-count').textContent = state.releases ? state.releases.length : '';
  renderContent();
  loadGovernanceSnapshot();
}

async function loadGovernanceSnapshot() {
  try {
    state.governanceSnapshot = await callApi('get_governance_snapshot');
    renderStatusRail();
    if (state.activeTab === 'overview') renderOverview();
  } catch (e) { /* 静默失败，状态栏显示占位 */ }
}

function renderStatusRail() {
  const container = document.getElementById('status-rail-content');
  const title = document.querySelector('#status-rail h3');
  if (!container) return;
  if (state.activeTab === 'neurons' && selectedNeuronNode) {
    if (title) title.textContent = '节点详情';
    container.innerHTML = renderNeuronRailDetail(selectedNeuronNode);
    return;
  }
  if (title) title.textContent = state.activeTab === 'neurons' ? '神经图详情' : '治理状态';
  if (state.activeTab === 'neurons') {
    container.innerHTML = `<div class="status-item zero"><span class="status-label">当前选择</span><span class="status-num">—</span></div>
      <div class="rail-link" onclick="switchTab('governance')">进入治理台 →</div>`;
    return;
  }
  const snap = state.governanceSnapshot;
  if (!snap) {
    container.innerHTML = '<div class="loading" style="min-height:60px">连接中…</div>';
    return;
  }
  const activeCount = snap.status ? snap.status.active_count : 0;
  const conflictCount = snap.conflicts ? snap.conflicts.count : 0;
  const quarantineCount = snap.quarantine ? snap.quarantine.count : 0;
  const rollbackCount = snap.rollback_ready || 0;
  const conflictClass = conflictCount > 0 ? 'alert' : 'zero';
  const quarantineClass = quarantineCount > 0 ? 'danger' : 'zero';
  const rollbackClass = rollbackCount > 0 ? '' : 'zero';
  container.innerHTML = `
    <div class="status-item" onclick="switchTab('governance')">
      <span class="status-label">Active memories</span>
      <span class="status-num">${activeCount}</span>
    </div>
    <div class="status-item ${conflictClass}" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)">
      <span class="status-label">Conflicts</span>
      <span class="status-num">${conflictCount}</span>
    </div>
    <div class="status-item ${quarantineClass}" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('quarantine'),50)">
      <span class="status-label">Quarantined</span>
      <span class="status-num">${quarantineCount}</span>
    </div>
    <div class="status-item ${rollbackClass}" onclick="switchTab('releases')">
      <span class="status-label">Rollback ready</span>
      <span class="status-num">${rollbackCount}</span>
    </div>
    <div class="rail-link" onclick="switchTab('governance')">打开治理台 →</div>`;
}

function renderNeuronRailDetail(node) {
  const childCount = (neuronGraph && neuronGraph.nodes ? neuronGraph.nodes : []).filter(n => n.parent_id === node.id).length;
  const isAnchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
  const kindText = memoryKindLabel(node.kind || node.label || '');
  const title = isAnchor ? (displayTitle(node) || node.label || '未命名记忆') : (node.node_kind === 'topic' ? kindText : '记忆根节点');
  if (!isAnchor) {
    return `<div class="status-item zero"><span class="status-label">${escapeHtml(node.node_kind === 'topic' ? '主题' : '节点')}</span><span class="status-num">${childCount}</span></div>
      <div class="neuron-detail-body">${escapeHtml(node.node_kind === 'topic' ? `该主题下有 ${childCount} 个记忆节点。点击小光点查看具体内容。` : `当前投影共有 ${(neuronGraph.nodes || []).length} 个节点。`)}</div>
      <div class="rail-link" onclick="switchTab('governance')">进入治理台 →</div>`;
  }
  const members = (node.members || []).map(m => `<div class="raw-file-row" onclick="selectNeuronByMemory('${escapeHtml(m.memory_id || '')}')">
    <div><code>${escapeHtml(displayTitle(m) || m.memory_id || '')}</code><div class="surface-meta">${escapeHtml(displayBody(m) || m.body_preview || '')}</div></div>
    <span class="chip chip-info">${escapeHtml(memoryKindLabel(m.kind || ''))}</span>
  </div>`).join('');
  const related = (node.related || []).map(r => `<div class="raw-file-row" onclick="selectNeuronByMemory('${escapeHtml(r.memory_id || '')}')">
    <div><code>${escapeHtml(displayTitle(r) || r.memory_id || '')}</code><div class="surface-meta">${escapeHtml(displayBody(r) || r.body_preview || '')}</div></div>
    <span class="chip chip-medium">相关</span>
  </div>`).join('');
  const actionTarget = node.memory_id || node.id || '';
  return `<div class="popover-kicker">${escapeHtml(kindText)}</div>
    <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
    <div class="neuron-detail-body">${escapeHtml(displayBody(node) || '暂无正文内容')}</div>
    <div class="row"><span class="key">作用域</span><span>${escapeHtml(node.scope || 'project')}</span></div>
    <div class="row"><span class="key">置信度</span><span>${escapeHtml(String(node.confidence ?? '—'))}</span></div>
    <div class="row"><span class="key">完整性</span><span>${escapeHtml(node.completeness || '—')}</span></div>
    <div class="row"><span class="key">来源</span><span>${node.provenance_count || 0} 个来源证据</span></div>
    ${node.cluster_count ? `<div class="row"><span class="key">合并片段</span><span>${node.cluster_count} 条</span></div>` : ''}
    <div class="row"><span class="key">记录 ID</span><code style="overflow-wrap:anywhere">${escapeHtml(actionTarget)}</code></div>
    <div class="finding-actions" style="margin:12px 0 10px;display:flex;flex-wrap:wrap;gap:6px">
      <span class="chip chip-confirmed">自动纳入重构</span>
      <button class="btn btn-danger" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','exclude')">删除/排除</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','quarantine')">隔离</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','merge')">合并</button>
    </div>
    ${members ? `<div class="claim-list"><h4>合并片段</h4>${members}</div>` : ''}
    ${related ? `<div class="claim-list"><h4>相关联</h4>${related}</div>` : ''}`;
}

function renderContent() {
  switch (state.activeTab) {
    case 'overview': renderOverview(); break;
    case 'sources': renderSources(); break;
    case 'neurons': renderNeurons(); break;
    case 'findings': renderFindings(); break;
    case 'releases': renderReleases(); break;
    case 'governance': renderGovernance(); break;
  }
}

async function renderNeurons() {
  setContent('<div class="loading">正在读取神经图投影</div>');
  try { neuronGraph = await callApi('get_neuron_graph', projectionMode); renderNeuronGraph(); }
  catch (e) {
    showToast('神经图构建失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>神经图构建失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function kindColor(kind) {
  const colors = {
    fact: '#6ee7c4', preference: '#f6ad55', project: '#63b3ed', episode: '#fc8181', procedure: '#b794f4', correction: '#f687b3', workflow: '#b794f4', constraint: '#fbd38d'
  };
  return colors[kind] || '#6ee7c4';
}

function memoryKindLabel(kind) {
  const labels = {
    fact: '事实', preference: '偏好', project: '项目', episode: '事件', procedure: '流程', correction: '纠错',
    constraint: '约束', workflow: '流程', decision: '决策', context: '上下文', instruction: '指令', unknown: '未知'
  };
  return labels[kind] || kind || '未知';
}

function neuronHashUnit(value) {
  let hash = 2166136261;
  const text = String(value || '');
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 10000) / 10000;
}

function neuronNodePositions(nodes) {
  const positions = {};
  const children = {};
  nodes.forEach(node => {
    const parent = node.parent_id || '';
    if (!children[parent]) children[parent] = [];
    children[parent].push(node);
  });
  positions.main = { x: 0, y: 0 };
  const topics = (children.main || []).filter(n => n.node_kind === 'topic');
  topics.forEach((node, index) => {
    const angle = index * 2.399963 + neuronHashUnit(node.id) * .8;
    const radius = 230 + neuronHashUnit(node.id + ':r') * 110;
    positions[node.id] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  });
  nodes.forEach(node => {
    if (positions[node.id]) return;
    const parent = positions[node.parent_id || 'main'] || positions.main;
    const siblings = children[node.parent_id || ''] || [];
    const index = Math.max(0, siblings.findIndex(s => s.id === node.id));
    const angle = index * 2.399963 + neuronHashUnit(node.id) * 1.7;
    const radius = 70 + neuronHashUnit(node.id + ':leaf') * 260;
    positions[node.id] = {
      x: parent.x + Math.cos(angle) * radius + (neuronHashUnit(node.id + ':x') - .5) * 90,
      y: parent.y + Math.sin(angle) * radius + (neuronHashUnit(node.id + ':y') - .5) * 90,
    };
  });
  return positions;
}

function graphElements(graph) {
  // v3.1 §6.3：统一 v3 图契约
  // node: id / parent_id / label / node_kind / memory_id / kind / provenance_count
  // edge: id / source / target / edge_type
  const elements = [];
  const positions = neuronNodePositions(graph.nodes || []);
  for (const node of graph.nodes || []) {
    const root = node.node_kind === 'root';
    const anchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
    const cluster = node.node_kind === 'duplicate_cluster';
    // v3：用 provenance_count 替代旧 claim_count 决定大小
    const provCount = node.provenance_count || 0;
    const size = root ? 66 : cluster ? Math.max(15, Math.min(30, 12 + (node.cluster_count || 2) * 4)) : anchor ? 7 : Math.max(27, Math.min(54, 25 + provCount * 3.2));
    elements.push({ data: {
      id: node.id,
      label: anchor ? '' : String(node.node_kind === 'topic' ? memoryKindLabel(node.label) : (node.label || '')).slice(0, 18),
      kind: node.node_kind,
      memory_id: node.memory_id || '',
      record_kind: node.kind || '',
      cluster_count: node.cluster_count || 0,
      provenance_count: provCount,
      size,
      bg: node.bg || kindColor(node.kind || node.label || ''),
      opacity: 0.85,
    }, position: positions[node.id] || { x: 0, y: 0 }});
  }
  for (const edge of graph.edges || []) {
    elements.push({ data: {
      id: edge.id, source: edge.source, target: edge.target,
      etype: edge.edge_type, strength: 0.35,
    }});
  }
  return elements;
}

function renderMergeDock(suggestions) {
  // v3.1 §6.3：projection 不再返回 merge_suggestions（自动晋升凋亡已移除）
  // 重复候选在 Memory IR 的 duplicate_groups 中查看
  if (!suggestions || !suggestions.length) return '';
  return `<aside class="merge-dock" aria-label="重复候选">
    <h3>重复候选 · ${suggestions.length} 条</h3>
    ${suggestions.map(item => `<div class="merge-item">
      <div class="merge-copy">${escapeHtml(item.group_id || '')}</div>
      <div class="merge-actions"><span class="merge-score">${item.member_ids ? item.member_ids.length : 0} 条记录</span></div>
    </div>`).join('')}
  </aside>`;
}

function projectionModeLabel(mode) {
  return {
    native_memory_projection: '原生记忆投影',
    logical_reconstruction_projection: '逻辑重构投影',
    evidence_only: '证据/萃取来源'
  }[mode] || mode || '未知';
}

function sourceCategoryLabel(category) {
  return {
    native_memory: '原生记忆', project_memory: '项目记忆', knowledge_source: '知识文档',
    conversation_history: '会话历史', runtime_evidence: '运行证据', ignored_runtime_data: '忽略运行数据',
    control_surface: '控制面', skill_surface: '技能面'
  }[category] || category || '未知';
}

function renderProjectionSourceMap(sourceMap) {
  const entries = sourceMap?.entries || [];
  const summary = sourceMap?.summary || {};
  const rows = entries.length ? entries.map(renderProjectionSourceEntry).join('') : '<tr><td colspan="7" class="empty-note">暂无映射条目</td></tr>';
  return `<section class="card projection-source-map">
    <div class="card-head"><div><h2>当前数据源映射</h2><p>这里只读展示数据源页已勾选的 Agent / 项目 / 来源。勾选和取消请回到数据源页处理。</p></div>
      <div class="chips"><span class="chip chip-info">启用 ${summary.enabled || 0}/${summary.total || 0}</span><span class="chip chip-info">原生 ${summary.native_memory || 0}</span><span class="chip chip-info">逻辑 ${summary.logical_reconstruction || 0}</span><span class="chip chip-medium">证据 ${summary.evidence_only || 0}</span></div></div>
    <div class="source-map-table-wrap"><table class="source-map-table"><thead><tr><th>状态</th><th>投影</th><th>来源</th><th>Agent</th><th>项目/范围</th><th>策略</th><th>路径</th></tr></thead><tbody>${rows}</tbody></table></div>
  </section>`;
}

function renderProjectionSourceEntry(entry) {
  const eligible = entry.logical_eligible || entry.native_eligible;
  const mode = projectionModeLabel(entry.projection_mode);
  const path = entry.path || '';
  const project = entry.project_ref || (entry.scope === 'project' ? '当前项目' : entry.scope || '未知');
  return `<tr class="${entry.enabled ? '' : 'muted-row'}">
    <td><span class="chip ${entry.enabled ? 'chip-confirmed' : 'chip-medium'}">${entry.enabled ? '已勾选' : '未勾选'}</span></td>
    <td><span class="chip ${eligible ? 'chip-confirmed' : 'chip-medium'}">${escapeHtml(mode)}</span></td>
    <td><strong>${escapeHtml(entry.display_name || entry.surface_id || entry.root_id)}</strong><div class="surface-meta">${escapeHtml(sourceCategoryLabel(entry.source_category))}</div></td>
    <td>${escapeHtml(entry.agent_instance_id || '未绑定')}<div class="surface-meta">${escapeHtml(entry.surface_id || '—')}</div></td>
    <td>${escapeHtml(project)}</td>
    <td>${escapeHtml(entry.ingestion_policy || '')}</td>
    <td class="path-cell">${escapeHtml(path)}</td>
  </tr>`;
}

function projectionModeControls() {
  const nativeActive = projectionMode === 'native' ? 'btn-primary' : '';
  const reconstructedActive = projectionMode === 'reconstructed' ? 'btn-primary' : '';
  return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>投影模式</h2><p>原生投影查看当前真实记忆；重构治理投影用于自动治理、萃取并发布回原生记忆。</p></div></div>
    <div class="finding-actions">
      <button class="btn ${nativeActive}" type="button" onclick="switchProjectionMode('native')">原生记忆投影</button>
      <button class="btn ${reconstructedActive}" type="button" onclick="switchProjectionMode('reconstructed')">重构治理投影</button>
      <button class="btn" type="button" onclick="switchTab('sources')">去数据源页调整</button>
    </div></section>`;
}

async function switchProjectionMode(mode) {
  projectionMode = mode === 'native' ? 'native' : 'reconstructed';
  localStorage.setItem('memoryguard.projectionMode', projectionMode);
  await renderNeurons();
}

function renderNeuronGraph() {
  const graph = neuronGraph;
  // 顶部 7 项状态信息（v3.1 §6.1）：Agent 实例 / Profile / 规范版本 / Release / 接管状态 / 覆盖状态 / 是否漂移
  // 后端 meta 结构：{agent_instances: [...], instance_count, coverage, coverage_status, release_count, drifted}
  const meta = (graph && graph.meta) || {};
  const sourceMapPanel = renderProjectionSourceMap(graph?.source_map || {});
  const modeControls = projectionModeControls();
  const instances = meta.agent_instances || [];
  // 每个 agent_instance 一组 chip：产品名 + 接管状态 + 规范版本 + 记录数
  const instanceChips = instances.length ? instances.map(inst => {
    const takeoverClass = (inst.takeover_state === 'operational' || inst.takeover_state === 'runtime_verified') ? 'confirmed'
      : (inst.takeover_state === 'drifted' || inst.takeover_state === 'partial') ? 'high' : 'medium';
    return `<span class="chip chip-info" title="${escapeHtml(inst.instance_id)}">${escapeHtml(inst.product || 'agent')} · ${escapeHtml(inst.takeover_state || 'not_detected')}</span>
      <span class="chip chip-info">版本 · ${escapeHtml(inst.managed_version ? inst.managed_version.slice(0,8) : '—')}</span>
      <span class="chip chip-info">记录 · ${inst.record_count || 0}</span>`;
  }).join('') : '<span class="chip chip-info">Agent · 未发现</span>';
  const metaBar = `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>记忆核心状态</h2>
    <p>顶部 7 项状态信息（v3.1 §6.1）</p></div></div>
    <div class="chips">
      ${instanceChips}
      <span class="chip chip-info">实例数 · ${meta.instance_count || 0}</span>
      <span class="chip chip-info">Release · ${meta.release_count || 0}</span>
      <span class="chip chip-${meta.coverage_status === 'complete' ? 'confirmed' : 'medium'}">覆盖 · ${escapeHtml(meta.coverage_status || 'unknown')}</span>
      <span class="chip chip-${meta.drifted ? 'high' : 'confirmed'}">漂移 · ${meta.drifted ? '是' : '否'}</span>
    </div></section>`;
  // 未构建时显示门控
  if (!graph || graph.empty || !graph.nodes || !graph.nodes.length) {
    document.getElementById('neuron-count').textContent = '';
    const reason = graph && graph.reason ? graph.reason : 'not_built';
    const reasonText = {
      'not_built': '尚未构建投影。神经图是 Memory IR 的可视化投影，不是事实源。',
      'no_ir': 'Memory IR 为空，请先在数据源 tab 扫描来源。',
      'error': '投影读取失败。',
    }[reason] || '尚未构建投影。';
    setContent(`<div class="view-heading"><span class="eyebrow">Live cognition map</span><h2>记忆核心</h2>
      <p>神经图是 Memory IR 的可视化投影，不是事实源。删除后可从 IR + DecisionLog 完整重建。图上治理操作会写入 DecisionLog 并生成新规范版本。</p></div>
      ${metaBar}
      ${modeControls}
      ${sourceMapPanel}
      <section class="card projection-gate">
        <div class="gate-orb" aria-hidden="true"></div>
        <div class="gate-body">
          <h3>当前状态：未构建</h3>
          <p class="gate-reason">${escapeHtml(reasonText)}</p>
          <div class="gate-warning">
            <strong>${projectionMode === 'native' ? '原生投影读取当前真实记忆。' : '重构治理会自动萃取、合并和清理记忆。'}</strong><br>
            ${projectionMode === 'native' ? '此操作只生成当前原生记忆的可视化图。' : '确认发布时才会封存备份并写回原生记忆。'}
          </div>
          <div class="finding-actions">
            <button class="btn btn-primary" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '构建原生投影' : '构建重构投影'}</button>
          </div>
        </div>
      </section>`);
    return;
  }
  const stats = graph.stats || {};
  const publishActions = projectionMode === 'reconstructed' ? `<button class="btn btn-primary" type="button" onclick="publishReconstructedMemory()">确认发布</button><button class="btn" type="button" onclick="rollbackNativeMemoryRelease()">回滚发布</button>` : '';
  const suggestions = [];
  selectedNeuronId = null;
  selectedNeuronNode = null;
  renderStatusRail();
  document.getElementById('neuron-count').textContent = stats.node_count || '';
  setContent(`<div class="view-heading"><span class="eyebrow">Live cognition map</span><h2>记忆核心</h2>
    <p>点击任意光点，在右侧查看可读内容。滚轮缩放，拖拽探索；治理操作请到治理台处理。</p></div>
    ${metaBar}
    ${modeControls}
    ${sourceMapPanel}
    <section class="neuron-shell">
    <div class="neuron-toolbar">
      <div class="neuron-title"><span class="eyebrow">Cognition map</span><h2>可读神经图</h2>
        <p>点击节点查看记忆内容；接受、排除、隔离、合并等操作统一在治理台完成。</p></div>
      <div class="canvas-actions">
        <button class="btn" type="button" onclick="fitNeuronGraph()">重置视野</button>
        <button class="btn" type="button" onclick="deleteProjection()">删除当前投影</button>
        <button class="btn" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '重建原生投影' : '重建重构投影'}</button>
        ${publishActions}
      </div>
    </div>
    <div class="neuron-stage" id="neuron-stage">
      <div class="neuron-canvas" id="cy" aria-label="本地记忆神经图画布"></div>
      <div class="neuron-legend">
        <div class="legend-item"><span class="legend-node"></span>主题节点</div>
        <div class="legend-item"><span class="legend-node tentative"></span>记忆片段</div>
        <div class="legend-item"><span class="legend-node anchor"></span>原始记录</div>
      </div>
      <div class="neuron-stats">
        <div class="neuron-stat"><strong>${stats.claim_anchor_count || 0}</strong><span>记忆片段</span></div>
        <div class="neuron-stat"><strong>${stats.node_count || 0}</strong><span>总节点</span></div>
        <div class="neuron-stat"><strong>${stats.topic_count || 0}</strong><span>主题节点</span></div>
        <div class="neuron-stat"><strong>${stats.edge_count || 0}</strong><span>关系边</span></div>
        <div class="neuron-stat"><strong>${stats.provenance_total || 0}</strong><span>来源证据</span></div>
      </div>
      ${renderMergeDock(suggestions)}
      <aside class="neuron-popover" id="neuron-popover" role="dialog" aria-live="polite" aria-label="光点治理"></aside>
    </div>
  </section>`);

  if (typeof cytoscape === 'undefined') {
    document.getElementById('cy').innerHTML = '<div class="empty-state" style="color:var(--red)">本地 Cytoscape 资源加载失败</div>';
    return;
  }

  cyInstance = cytoscape({
    container: document.getElementById('cy'),
    elements: graphElements(graph),
    style: [
      { selector: 'node', style: {
        'width': 'data(size)', 'height': 'data(size)', 'background-color': 'data(bg)',
        'background-opacity': 'data(opacity)', 'border-width': 1.4, 'border-color': '#6ee7c4',
        'label': 'data(label)', 'color': '#cce5dc', 'font-size': 9.5,
        'font-family': 'Segoe UI, PingFang SC, sans-serif', 'font-weight': 500,
        'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 8,
        'text-outline-width': 2, 'text-outline-color': '#040b09', 'text-wrap': 'wrap', 'text-max-width': 92,
        'transition-property': 'border-width, border-color, background-color, opacity', 'transition-duration': '160ms',
      }},
      { selector: 'node[kind = "root"]', style: {
        'background-color': '#6ee7c4', 'background-opacity': .24, 'border-width': 2.5,
        'border-color': '#bcffeb', 'font-size': 11,
      }},
      { selector: 'node[kind = "claim_anchor"]', style: {
        'background-opacity': .68, 'border-width': 0, 'label': '',
      }},
      { selector: 'node[kind = "duplicate_cluster"]', style: {
        'background-opacity': .78, 'border-width': 1.8,
        'border-color': '#d8ffe9', 'label': '',
      }},
      { selector: 'node[status = "tentative"]', style: {
        'background-color': '#2b2a20', 'border-color': '#e9bb64', 'border-style': 'dashed',
      }},
      { selector: 'edge', style: {
        'width': 'mapData(strength, 0, 1, .45, 2.4)', 'line-color': '#6ee7c4', 'line-opacity': .18,
        'curve-style': 'unbundled-bezier', 'control-point-distances': 20, 'control-point-weights': .5,
        'target-arrow-shape': 'none', 'transition-property': 'line-opacity, width', 'transition-duration': '140ms',
      }},
      { selector: 'edge[etype = "related"]', style: { 'line-style': 'dashed', 'line-opacity': .12 }},
      { selector: '.neighborhood', style: { 'line-opacity': .62, 'width': 2.1 }},
      { selector: 'node.neighborhood', style: { 'border-color': '#bcffeb', 'border-width': 2.5 }},
      { selector: 'node:selected', style: {
        'border-width': 3, 'border-color': '#fff6c7', 'shadow-blur': 28,
        'shadow-color': '#fff3a3', 'shadow-opacity': .46,
      }},
      { selector: 'node.pulse', style: {
        'border-width': 4, 'border-color': '#ffffff', 'shadow-blur': 42,
        'shadow-color': '#ffffff', 'shadow-opacity': .62,
      }},
    ],
    layout: {
      name: 'preset', animate: true, animationDuration: 720, fit: true, padding: 86,
    },
    minZoom: .22, maxZoom: 3.6,
  });

  cyInstance.on('tap', 'node', event => selectNeuron(event.target.id()));
  cyInstance.on('mouseover', 'node', event => {
    const node = event.target;
    node.addClass('neighborhood');
    node.connectedEdges().addClass('neighborhood');
    node.neighborhood('node').addClass('neighborhood');
  });
  cyInstance.on('mouseout', 'node', () => cyInstance.elements().removeClass('neighborhood'));
  cyInstance.on('tap', event => { if (event.target === cyInstance) hideNeuronPopover(); });
  cyInstance.on('pan zoom resize', () => { if (selectedNeuronId) positionNeuronPopover(selectedNeuronId); });
  cyInstance.on('drag position', 'node', event => {
    if (selectedNeuronId === event.target.id()) positionNeuronPopover(selectedNeuronId);
  });
}

function fitNeuronGraph() {
  hideNeuronPopover();
  if (cyInstance) cyInstance.animate({ fit: { eles: cyInstance.elements(), padding: 72 }, duration: 340 });
}

function findNeuronByMemory(memoryId) {
  return (neuronGraph.nodes || []).find(item => item.memory_id === memoryId
    || (item.member_ids || []).includes(memoryId)
    || (item.members || []).some(member => member.memory_id === memoryId)
    || (item.related || []).some(related => related.memory_id === memoryId));
}

function focusNeuronNode(nodeId) {
  if (!cyInstance) return;
  const cyNode = cyInstance.getElementById(nodeId);
  if (!cyNode || !cyNode.length) return;
  cyInstance.animate({ center: { eles: cyNode }, zoom: Math.max(cyInstance.zoom(), 1.18) }, { duration: 420 });
  cyNode.flashClass('pulse', 900);
}

function selectNeuron(nodeId, focus = true) {
  const node = (neuronGraph.nodes || []).find(item => item.id === nodeId);
  const popover = document.getElementById('neuron-popover');
  if (!node) return;
  selectedNeuronId = nodeId;
  selectedNeuronNode = node;
  if (cyInstance) {
    cyInstance.elements().unselect();
    cyInstance.getElementById(nodeId).select();
  }
  if (popover) popover.classList.remove('show');
  renderStatusRail();
  if (focus) focusNeuronNode(nodeId);
}

function selectNeuronByMemory(memoryId) {
  const node = findNeuronByMemory(memoryId);
  if (!node) return showToast('未找到关联节点', 'error');
  selectNeuron(node.id, true);
}

async function neuronAction(nodeId, action) {
  // v3.1 §6.2：图上操作 → 追加 DecisionEvent → 生成新规范版本 → 投影重建
  let reason = '';
  if (action === 'exclude' || action === 'quarantine' || action === 'supersede') {
    reason = prompt(action + ' 的原因（必填）：') || '';
    if (!reason) return showToast('需填写原因', 'error');
  }
  showToast('正在写入 DecisionEvent…');
  try {
    const result = await callApi('neuron_decide', nodeId, action, reason, true);
    if (result.error) return showToast(result.error, 'error');
    showToast(`已写入决策，新规范版本：${result.memory_version || ''}`, 'success');
    neuronGraph = await callApi('get_neuron_graph'); renderNeuronGraph();
  } catch (e) { showToast('操作失败：' + e, 'error'); }
}

function positionNeuronPopover(nodeId) {
  const popover = document.getElementById('neuron-popover');
  const stage = document.getElementById('neuron-stage');
  if (!popover || !stage || !cyInstance) return;
  const element = cyInstance.getElementById(nodeId);
  if (!element || !element.length) return;
  const pos = element.renderedPosition();
  const half = Math.min(180, (stage.clientWidth - 28) / 2);
  const x = Math.max(half + 14, Math.min(stage.clientWidth - half - 14, pos.x));
  const needsBelow = pos.y < Math.min(popover.offsetHeight + 30, stage.clientHeight * .48);
  popover.classList.toggle('below', needsBelow);
  popover.style.left = x + 'px';
  popover.style.top = Math.max(14, Math.min(stage.clientHeight - 14, pos.y)) + 'px';
}

function hideNeuronPopover() {
  selectedNeuronId = null;
  selectedNeuronNode = null;
  const popover = document.getElementById('neuron-popover');
  if (popover) popover.classList.remove('show');
  if (cyInstance) cyInstance.elements().unselect();
  renderStatusRail();
}

async function doPromote(lightId) {
  showToast('v3 已移除自动晋升操作（神经图是纯投影）', 'error');
}

async function doDissolve(lightId) {
  showToast('v3 已移除自动凋亡操作（神经图是纯投影）', 'error');
}

async function doMerge(fromId, toId) {
  showToast('v3 已移除合并操作（神经图是纯投影）', 'error');
}

async function refreshNeuronGraph(message = '') {
  neuronGraph = await callApi('get_neuron_graph', projectionMode);
  renderNeuronGraph();
  if (message) showToast(message, 'success');
}

async function buildProjection() {
  const native = projectionMode === 'native';
  const message = native
    ? '构建原生记忆投影？\n\n· 读取数据源页已勾选的原生/项目记忆\n· 只生成当前真实记忆图\n· 不写入记忆文件\n\n继续？'
    : '构建重构治理投影？\n\n· 自动萃取、合并、清理已勾选来源\n· 生成可发布的新记忆结构\n· 确认发布时才会封存备份并写回\n\n继续？';
  if (!confirm(message)) return;
  setContent(`<div class="loading">正在构建${native ? '原生记忆' : '重构治理'}投影</div>`);
  try {
    const result = await callApi('build_projection', true, projectionMode);
    if (result.error) return showToast(result.error, 'error');
    await refreshNeuronGraph(native ? '原生投影构建完成' : '重构投影构建完成');
  } catch (e) { showToast('构建失败：' + e, 'error'); }
}

async function deleteProjection() {
  if (!confirm(`删除当前${projectionMode === 'native' ? '原生' : '重构'}投影？\n\n只删除投影文件，不删除原生记忆。`)) return;
  try {
    const result = await callApi('delete_projection', true, projectionMode);
    if (result.error) return showToast(result.error, 'error');
    await refreshNeuronGraph('当前投影已删除，可随时重建');
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function publishReconstructedMemory() {
  try {
    const data = await callApi('list_publish_targets');
    const targets = (data.targets || []).filter(t => t.is_agent_native_memory);
    if (!targets.length) {
      showToast('未解析到可写入的 Agent 原生记忆入口，请先在数据源页扫描并勾选原生记忆。', 'error');
      return;
    }
    if (targets.length === 1) {
      await publishToAgentNativeTarget(targets[0]);
      return;
    }
    showPublishTargetModal(targets);
  } catch (e) { showToast('发布失败：' + e, 'error'); }
}

async function publishToAgentNativeTarget(targetInfo) {
  const target = targetInfo.target_file;
  if (!confirm(`确认发布到 Agent 原生记忆入口？\n\nAgent：${targetInfo.agent_instance_id || '未知'}\n入口：${targetInfo.display_name || targetInfo.surface_id || ''}\n路径：${target}\n\n后台会执行：封存备份 → staged 写入 → 原子替换 → 校验。`)) return;
  const result = await callApi('publish_reconstructed_memory', target, true);
  if (!result.ok) return showToast((result.errors && result.errors[0]) || result.error || '发布失败', 'error');
  await refreshNeuronGraph(`发布完成：${result.release_id}，已写入 Agent 原生记忆入口`);
}

function showPublishTargetModal(targets) {
  closePublishTargetModal();
  const rows = targets.map((t, i) => `<label class="release-option">
    <input type="radio" name="publish-target" value="${i}" ${i === 0 ? 'checked' : ''}>
    <span><div class="release-title">${escapeHtml(t.display_name || t.surface_id || t.root_id)}</div><div class="release-meta">Agent：${escapeHtml(t.agent_instance_id || '未知')} · ${escapeHtml(t.target_file || '')}</div></span>
  </label>`).join('');
  const modal = document.createElement('div');
  modal.id = 'publish-target-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="选择 Agent 原生记忆入口">
    <div class="modal-head"><h3>选择 Agent 原生记忆入口</h3><p>只列出已解析到的 agent-managed 原生记忆入口。</p></div>
    <div class="modal-body">${rows}</div>
    <div class="modal-actions"><button class="btn" type="button" onclick="closePublishTargetModal()">取消</button><button class="btn btn-primary" type="button" onclick="confirmPublishTargetModal()">确认发布</button></div>
  </div>`;
  modal.__targets = targets;
  modal.addEventListener('click', (event) => { if (event.target === modal) closePublishTargetModal(); });
  document.body.appendChild(modal);
}

function closePublishTargetModal() {
  const modal = document.getElementById('publish-target-modal');
  if (modal) modal.remove();
}

async function confirmPublishTargetModal() {
  const modal = document.getElementById('publish-target-modal');
  const selected = document.querySelector('input[name="publish-target"]:checked');
  if (!modal || !selected) return showToast('请选择 Agent 原生记忆入口', 'error');
  const targetInfo = modal.__targets[Number(selected.value)];
  closePublishTargetModal();
  if (targetInfo) await publishToAgentNativeTarget(targetInfo);
}

function formatReleaseVersionList(releases) {
  return (releases || []).slice(0, 20).map((r, i) => `${i + 1}. ${r.release_id} · ${r.rollback_reason || r.status || ''} · ${r.created_at || ''}`).join('\n');
}

async function rollbackNativeMemoryRelease() {
  try {
    const data = await callApi('list_native_memory_releases');
    const allReleases = data.releases || [];
    const releases = allReleases.filter(r => r.can_rollback);
    if (!releases.length) {
      const versions = allReleases.length ? '\n\n版本状态：\n' + formatReleaseVersionList(allReleases) : '\n\n暂无成功发布的版本记录。';
      alert('没有可恢复的已发布版本。' + versions);
      return;
    }
    showRollbackModal(releases.slice(0, 20));
  } catch (e) { showToast('读取回滚版本失败：' + e, 'error'); }
}

function showRollbackModal(releases) {
  closeRollbackModal();
  const rows = releases.map((r, i) => `<label class="release-option">
    <input type="radio" name="rollback-release" value="${escapeHtml(r.release_id)}" ${i === 0 ? 'checked' : ''}>
    <span><div class="release-title">${escapeHtml(r.release_id)}</div><div class="release-meta">${escapeHtml(r.created_at || '')}</div></span>
  </label>`).join('');
  const modal = document.createElement('div');
  modal.id = 'rollback-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="选择恢复版本">
    <div class="modal-head"><h3>选择要恢复的版本</h3><p>只列出当前可安全恢复的已发布版本。</p></div>
    <div class="modal-body">${rows}</div>
    <div class="modal-actions"><button class="btn" type="button" onclick="closeRollbackModal()">取消</button><button class="btn btn-primary" type="button" onclick="confirmRollbackModal()">确认恢复</button></div>
  </div>`;
  modal.addEventListener('click', (event) => { if (event.target === modal) closeRollbackModal(); });
  document.body.appendChild(modal);
}

function closeRollbackModal() {
  const modal = document.getElementById('rollback-modal');
  if (modal) modal.remove();
}

async function confirmRollbackModal() {
  const selected = document.querySelector('input[name="rollback-release"]:checked');
  if (!selected) return showToast('请选择要恢复的版本', 'error');
  const releaseId = selected.value;
  try {
    const result = await callApi('rollback_native_memory_release', releaseId, false, true);
    if (!result.ok) return showToast((result.errors && result.errors[0]) || result.error || '回滚失败', 'error');
    closeRollbackModal();
    await refreshNeuronGraph(`回滚完成：${releaseId}`);
  } catch (e) { showToast('回滚失败：' + e, 'error'); }
}

async function reExtract() {
  // 兼容旧调用：转发到 buildProjection
  await buildProjection();
}

function renderOverview() {
  const report = state.report;
  const snap = state.governanceSnapshot;

  // 空状态：没有记忆写入事件
  if (!snap || !snap.has_events) {
    setContent(`<div class="view-heading"><span class="eyebrow">Governance Flow</span><h2>总览</h2>
      <p>概念图式的治理流控制台。新写入 -> 覆盖 / 冲突 / 隔离，实时展示真实事件。</p></div>
      <section class="card empty-state"><div><div class="empty-orb"></div>
        <p>尚无记忆写入事件</p>
        <p style="margin-top:6px;font-size:11px">连接本地 Agent 或导入示例工作区以查看治理流</p>
      </div></section>
      <div class="flow-canvas">
        <div class="flow-card empty cyan"><div class="flow-kicker">新写入</div><div class="flow-title">等待事件</div><div class="flow-body">Agent 写入记忆后，事件将出现在这里。</div></div>
        <div class="flow-card empty gray"><div class="flow-kicker">覆盖</div><div class="flow-title">等待事件</div><div class="flow-body">auto_supersede 决策将出现在这里。</div></div>
        <div class="flow-card empty amber"><div class="flow-kicker">冲突</div><div class="flow-title">等待事件</div><div class="flow-body">运行期冲突将出现在这里。</div></div>
        <div class="flow-card empty red"><div class="flow-kicker">隔离</div><div class="flow-title">等待事件</div><div class="flow-body">隔离项将出现在这里。</div></div>
      </div>`);
    return;
  }

  // 四张事件卡
  const evt = snap.latest_event;
  const sup = snap.latest_supersede;
  const conf = snap.conflicts;
  const quar = snap.quarantine;

  const evtCard = evt ? `<div class="flow-card cyan" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('recent_events'),50)">
    <div class="flow-kicker">最新记忆写入</div>
    <div class="flow-title">${escapeHtml(evt.agent_instance_id || 'unknown')}</div>
    <div class="flow-body">${escapeHtml(evt.raw_content_preview || '(无内容)')}${evt.auto_actions && evt.auto_actions.length ? '<br>自动处理：' + evt.auto_actions.map(a => escapeHtml(a.action)).join(', ') : ''}</div>
    <div class="flow-time">${escapeHtml(evt.created_at || '')}</div>
  </div>` : `<div class="flow-card empty cyan"><div class="flow-kicker">新写入</div><div class="flow-title">暂无事件</div></div>`;

  const supCard = sup ? `<div class="flow-card gray" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('supersede'),50)">
    <div class="flow-kicker">被覆盖的旧记忆</div>
    <div class="flow-title">auto_supersede</div>
    <div class="flow-body">新：${escapeHtml((sup.new_content_preview || '').slice(0, 80))}<br>旧：${escapeHtml((sup.old_content_preview || '').slice(0, 80))}<br>原因：${escapeHtml(sup.reason || '')}</div>
    <div class="flow-time">${escapeHtml(sup.created_at || '')}</div>
  </div>` : `<div class="flow-card empty gray"><div class="flow-kicker">覆盖</div><div class="flow-title">暂无覆盖</div></div>`;

  const confCard = conf && conf.count > 0 ? `<div class="flow-card amber" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)">
    <div class="flow-kicker">未解决冲突</div>
    <div class="flow-title">${conf.count} 组冲突</div>
    <div class="flow-body">${escapeHtml(conf.first_reason || '点击查看冲突队列')}</div>
  </div>` : `<div class="flow-card empty amber"><div class="flow-kicker">冲突</div><div class="flow-title">无未解决冲突</div></div>`;

  const quarCard = quar && quar.count > 0 ? `<div class="flow-card red" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('quarantine'),50)">
    <div class="flow-kicker">隔离项</div>
    <div class="flow-title">${quar.count} 条隔离</div>
    <div class="flow-body">${quar.items && quar.items.length ? '模式：' + escapeHtml(quar.items[0].detected_pattern || '') + ' · ' + escapeHtml(quar.items[0].masked_preview || '') : '点击查看隔离队列'}</div>
  </div>` : `<div class="flow-card empty red"><div class="flow-kicker">隔离</div><div class="flow-title">无隔离项</div></div>`;

  // 健康分摘要（保留原有信息但缩小为次要）
  const summary = report.summary;
  const health = Math.max(0, Math.min(100, Number(report.health_score || 0)));
  const severity = Object.entries(summary.finding_count_by_severity || {})
    .map(([name, count]) => `<span class="chip chip-${escapeHtml(name)}">${escapeHtml(name)} · ${count}</span>`).join('');
  const invisible = summary.invisible_count > 0 ? `<section class="card"><div class="card-head"><div><h2>不可见范围</h2><p>治理边界之外的对象会明确显示，不会静默忽略。</p></div></div>
    ${report.invisible.map(item => `<div class="finding-evidence">${escapeHtml(item.path)} · ${escapeHtml(item.reason)}</div>`).join('')}</section>` : '';

  setContent(`<div class="view-heading"><span class="eyebrow">Governance Flow</span><h2>总览</h2>
    <p>概念图式的治理流控制台。新写入 -> 覆盖 / 冲突 / 隔离，实时展示真实事件。</p></div>
    <div class="flow-canvas">${evtCard}${supCard}${confCard}${quarCard}</div>
    <div class="overview-grid">
      <section class="card"><div class="card-head"><div><h2>风险频谱</h2><p>仅保留有决策价值的严重度信号</p></div></div><div class="chips">${severity || '<span class="chip chip-low">当前未发现风险</span>'}</div></section>
      <section class="card"><div class="card-head"><div><h2>健康度</h2></div></div><div class="scan-list">
        <div class="scan-row"><span>健康分</span><strong style="color:${health >= 70 ? 'var(--accent)' : health >= 40 ? 'var(--orange)' : 'var(--red)'}">${Math.round(health)}/100</strong></div>
        <div class="scan-row"><span>已识别对象</span><strong>${summary.object_count}</strong></div>
        <div class="scan-row"><span>风险信号</span><strong>${report.findings.length}</strong></div>
        <div class="scan-row"><span>生成时间</span><strong>${escapeHtml(report.generated_at)}</strong></div>
      </div></section>
    </div>${invisible}`);
}

function renderFindings() {
  const report = state.report;
  if (!report.findings.length) {
    setContent('<div class="view-heading"><span class="eyebrow">Risk signals</span><h2>风险信号</h2></div><div class="card empty-state"><div><div class="empty-orb"></div><p>没有发现需要处理的风险信号。</p></div></div>');
    return;
  }
  const items = report.findings.map(finding => `<article class="finding-item sev-${escapeHtml(finding.severity)}" onclick="toggleFinding('${escapeHtml(finding.id)}')">
    <div class="finding-header"><span class="finding-rule"><span class="chip chip-${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span> ${escapeHtml(finding.rule_id)}</span>
      ${finding.fixable ? '<span style="color:var(--accent);font-size:10px">可生成变更</span>' : ''}</div>
    <div class="finding-evidence">${escapeHtml(finding.evidence)}</div>
    <div class="finding-detail" id="detail-${escapeHtml(finding.id)}" style="display:none">
      <div class="row"><span class="key">维度</span><span>${escapeHtml(finding.dimension)}</span></div>
      <div class="row"><span class="key">表面</span><span>${escapeHtml(finding.surface)}</span></div>
      <div class="row"><span class="key">位置</span><code>${escapeHtml(finding.location.path)}:${finding.location.span[0]}</code></div>
      <div class="row"><span class="key">影响</span><span>${escapeHtml(finding.impact)}</span></div>
      <div class="row"><span class="key">建议</span><span>${escapeHtml(finding.suggestion)}</span></div>
      <div class="row"><span class="key">置信度</span><span>${(finding.confidence * 100).toFixed(0)}%</span></div>
      <div class="finding-actions">${finding.fixable ? `<button class="btn btn-primary" type="button" onclick="event.stopPropagation();generatePlan('${escapeHtml(finding.id)}')">生成修复计划</button>` : ''}</div>
    </div></article>`).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Risk signals</span><h2>风险信号</h2><p>沿着证据链查看问题，不隐藏扫描盲区。</p></div>${items}`);
}

function toggleFinding(id) {
  const element = document.getElementById('detail-' + id);
  if (element) element.style.display = element.style.display === 'none' ? 'block' : 'none';
}

async function generatePlan(findingId) {
  showToast('正在生成修复计划…');
  try {
    const result = await callApi('generate_plan', findingId);
    if (result.error) return showToast(result.error, 'error');
    state.plans.push(result.plan);
    document.getElementById('releases-count').textContent = (state.releases ? state.releases.length : 0) + state.plans.length;
    showToast('修复计划已生成', 'success'); switchTab('releases');
  } catch (e) { showToast('生成失败：' + e, 'error'); }
}

async function applyPlan(planId) {
  showToast('正在应用并验证…');
  try {
    const result = await callApi('apply_plan', planId);
    if (result.error) return showToast(result.error, 'error');
    const plan = state.plans.find(item => item.plan_id === planId);
    if (plan) plan.change = result.change;
    showToast(result.change.status === 'verified' ? '变更已通过验证' : '变更未通过验证', result.change.status === 'verified' ? 'success' : 'error');
    state.report = await callApi('get_audit'); renderAll();
  } catch (e) { showToast('应用失败：' + e, 'error'); }
}

async function undoChange(changeId, planId) {
  showToast('正在撤销变更…');
  try {
    const result = await callApi('undo_change', changeId);
    if (result.error) return showToast(result.error, 'error');
    const plan = state.plans.find(item => item.plan_id === planId);
    if (plan) plan.change = null;
    state.report = await callApi('get_audit'); showToast('变更已撤销', 'success'); renderAll();
  } catch (e) { showToast('撤销失败：' + e, 'error'); }
}

// ===========================================================================
// v3 数据源 tab：SourceRoot 列表 + 原始记忆按 agent 分组 + 文件内容查看
// spec §7.1 / §7.2 SourceApi + get_raw_memory + get_source_file_content
// ===========================================================================

async function renderSources() {
  setContent('<div class="loading">正在加载数据页</div>');
  try {
    // v3.2：先加载 Agent 卡片
    agentCardsData = await callApi('list_agents');
    const agents = agentCardsData.agents || [];
    // 默认选中第一个 Agent
    if (!activeAgentInstanceId && agents.length > 0) {
      activeAgentInstanceId = agents[0].instance_id;
    }
    // 加载选中 Agent 的数据 + 来源列表 + 覆盖率
    const [agentData, sourcesResult, rawResult] = await Promise.all([
      activeAgentInstanceId ? callApi('get_agent_data', activeAgentInstanceId) : Promise.resolve(null),
      callApi('list_sources'),
      callApi('get_raw_memory'),
    ]);
    renderSourcesView(sourcesResult, rawResult, agentData);
  } catch (e) {
    showToast('数据源加载失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>数据源加载失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function selectAgentCard(instanceId) {
  activeAgentInstanceId = instanceId;
  renderSources();
}

function renderSourcesView(sourcesResult, rawResult, agentData) {
  const sources = sourcesResult.sources || [];
  const cov = rawResult.coverage || {};
  document.getElementById('sources-count').textContent = sources.length || '';

  // v3.2 Agent 卡片
  const agents = (agentCardsData && agentCardsData.agents) || [];
  const residuals = (agentCardsData && agentCardsData.residuals) || [];
  const lifecycleLabels = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
  const lifecycleChips = { installed: 'confirmed', installed_no_data: 'info', data_only: 'medium', uncertain: 'info', ignored: 'low', not_detected: 'low' };
  const agentCardsHtml = agents.length ? agents.map(a => {
    const isActive = a.instance_id === activeAgentInstanceId;
    const lifecycle = a.lifecycle_state || 'uncertain';
    return `<div class="agent-card ${isActive ? 'active' : ''}" onclick="selectAgentCard('${escapeHtml(a.instance_id)}')">
      <div class="agent-name">${escapeHtml(a.product)}</div>
      <div class="agent-meta">${a.found_surface_count}/${a.surface_count} 表面 · 私有 ${a.private_data_surface_count || 0} · 共享 ${a.shared_surface_count || 0} · ${a.bound_source_count} 来源</div>
      <div class="agent-badge">${escapeHtml(a.target_capability || 'export_only')}</div>
      <span class="chip chip-${lifecycleChips[lifecycle] || 'info'}">${escapeHtml(lifecycleLabels[lifecycle] || lifecycle)}</span>
    </div>`;
  }).join('') : '<div class="agent-card" style="cursor:default"><div class="agent-meta">未发现已安装 Agent，点击"检测本机 Agent"</div></div>';
  const residualCardsHtml = residuals.length ? residuals.map(r => {
    const lifecycle = r.lifecycle_state || 'uncertain';
    return `<div class="agent-card" onclick="showResidualCleanup('${escapeHtml(r.instance_id)}')">
      <div class="agent-name">${escapeHtml(r.product)}</div>
      <div class="agent-meta">私有残留 ${r.private_data_surface_count || 0} · 共享表面 ${r.shared_surface_count || 0}</div>
      <span class="chip chip-${lifecycleChips[lifecycle] || 'medium'}">${escapeHtml(lifecycleLabels[lifecycle] || lifecycle)}</span>
    </div>`;
  }).join('') : '<div class="agent-card" style="cursor:default"><div class="agent-meta">无私有残留数据。</div></div>';
  const addCards = `<div class="agent-card add-card" onclick="addSourceDialog()"><div class="agent-name">+ 手动来源</div></div>
    <div class="agent-card add-card" onclick="importBundleDialog()"><div class="agent-name">+ 外部 MCP</div></div>`;

  const catLabels = {
    native_memory: '原生记忆', control_surface: '控制面', skill_surface: 'Skill 表面',
    conversation_history: '会话历史', runtime_evidence: '运行证据', knowledge_source: '知识来源',
    unknown: '其他', project_memory: '项目记忆',
  };
  const scopeLabels = {user: '全局/用户', project: '项目', unknown: '未归属'};
  const renderFiles = (files) => `<div class="raw-file-list">
    ${(files || []).map(f => {
      const canOpen = !!f.root_id && f.authorized !== false && f.read_status !== 'discovered';
      const safeRoot = escapeHtml(f.root_id || '');
      const safePath = escapeHtml(f.relative_path || '').replaceAll("'", "\\'");
      const clickAttr = canOpen ? ` onclick="viewSourceFile('${safeRoot}','${safePath}')"` : '';
      const statusText = canOpen ? (f.read_status || '') : '仅发现，需先授权';
      return `<div class="raw-file-row"${clickAttr} style="${canOpen ? '' : 'cursor:default;opacity:.72'}">
        <span class="raw-file-path"><code>${escapeHtml(f.relative_path || '').replaceAll('\\', '/')}</code></span>
        <span class="chip chip-${canOpen && f.read_status === 'read' ? 'confirmed' : 'medium'}">${escapeHtml(statusText)}</span>
        <span style="color:var(--faint);font-size:10px">${escapeHtml(f.media_type || '')}</span>
      </div>`;
    }).join('')}
  </div>`;
  const renderCategory = (cat) => {
    const label = catLabels[cat.category] || cat.category || 'unknown';
    const files = cat.files || [];
    return `<div style="margin-bottom:14px">
      <div class="finding-header"><span class="finding-rule">${escapeHtml(label)}</span>
        <span class="chip chip-info">${files.length} 个文件</span></div>
      ${renderFiles(files)}
    </div>`;
  };
  const renderScope = (scopeObj) => {
    const scope = scopeObj.scope || 'unknown';
    const label = scopeLabels[scope] || scope;
    const directCategories = (scopeObj.categories || []).map(renderCategory).join('');
    const projectHtml = (scopeObj.projects || []).map(proj => `<details class="scope-block" style="margin:12px 0 0 12px">
      <summary class="finding-header" style="cursor:pointer"><span class="finding-rule">${escapeHtml(label)} · ${escapeHtml(proj.project_ref || '未命名项目')}</span>
        <span class="chip chip-info">${escapeHtml(proj.scope_source || scopeObj.scope_source || '')}</span></summary>
      ${(proj.categories || []).map(renderCategory).join('') || '<div class="empty-state"><p>该项目下暂无可显示文件。</p></div>'}
    </details>`).join('');
    return `<details class="scope-block" style="margin-bottom:18px" ${scope === 'user' ? 'open' : ''}>
      <summary class="finding-header" style="cursor:pointer"><span class="finding-rule">${escapeHtml(label)}</span>
        <span class="chip chip-info">${escapeHtml(scopeObj.scope_source || '')}</span></summary>
      ${directCategories}${projectHtml || (!directCategories ? '<div class="empty-state"><p>该作用域暂无可显示文件。</p></div>' : '')}
    </details>`;
  };
  const agentDataHtml = agentData ? ((agentData.scopes || []).map(renderScope).join('') || '<div class="empty-state"><div class="empty-orb"></div><p>该 Agent 暂无已发现数据。</p></div>') : '<div class="empty-state"><div class="empty-orb"></div><p>选中一个 Agent 卡片查看其数据。</p></div>';

  const covCard = `<section class="card">
    <div class="card-head"><div><h2>覆盖率账本</h2><p>证明扫描完整性，unaccounted_count 必须为 0</p></div></div>
    <div class="chips">
      <span class="chip chip-info">candidates · ${cov.candidate_count || 0}</span>
      <span class="chip chip-confirmed">read · ${cov.read || 0}</span>
      <span class="chip chip-medium">unsupported · ${cov.unsupported || 0}</span>
      <span class="chip chip-high">unreadable · ${cov.unreadable || 0}</span>
      <span class="chip chip-medium">skipped · ${cov.skipped_by_policy || 0}</span>
      <span class="chip chip-${(cov.unaccounted_count || 0) === 0 ? 'confirmed' : 'high'}">unaccounted · ${cov.unaccounted_count || 0}</span>
      <span class="chip chip-${cov.coverage_status === 'complete' ? 'confirmed' : 'medium'}">${escapeHtml(cov.coverage_status || 'unknown')}</span>
    </div>
  </section>`;

  const agentInfo = agentData && agentData.agent ? agentData.agent : null;
  setContent(`<div class="view-heading"><span class="eyebrow">Sources</span><h2>数据源</h2>
    <p>顶部选择 Agent，下方查看其数据。全局/项目可折叠展开。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 摘要</h2>
      <p>${agents.length} 个已安装 · ${residuals.length} 个残留候选 · 点击卡片切换数据视图</p></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="discoverAgents()">检测本机 Agent</button>
        <button class="btn" type="button" onclick="addSourceDialog()">手工添加</button>
        <button class="btn" type="button" onclick="importBundleDialog()">导入导出包</button>
      </div></div>
      <div class="agent-cards">${agentCardsHtml}${addCards}</div></section>
    ${residuals.length ? `<details class="card" style="margin-bottom:16px">
      <summary class="card-head" style="cursor:pointer"><div><h2>残留与清理</h2>
        <p>${residuals.length} 个候选 · 点击展开查看</p></div></summary>
      <div class="agent-cards" style="padding:16px">${residualCardsHtml}</div>
    </details>` : ''}
    <section class="card"><div class="card-head"><div><h2>${agentInfo ? escapeHtml(agentInfo.product) + ' 数据视图' : 'Agent 数据视图'}</h2>
      <p>${agentData ? agentData.total_files + ' 个文件，' + agentData.category_count + ' 个类别' : '选中 Agent 后显示数据'}</p></div>
      ${agentInfo ? `<div class="finding-actions"><button class="btn" type="button" onclick="selectAgentInstance('${escapeHtml(agentInfo.instance_id)}')">勾选授权</button>
        <button class="btn btn-primary" type="button" onclick="enterMultiAgentMode()">进入多 Agent 共享 MCP 模式</button></div>` : ''}</div>
      ${agentDataHtml}</section>
    ${covCard}`);
}

async function enterMultiAgentMode() {
  dataPageMode = 'multi_agent_shared_mcp';
  showToast('进入多 Agent 共享 MCP 模式，请选择 Agent');
  await renderMultiAgentBinding();
}

async function renderMultiAgentBinding() {
  setContent('<div class="loading">正在加载 Agent 列表与已有绑定…</div>');
  try {
    const [agentsResult, bindingsResult] = await Promise.all([
      callApi('list_agents'),
      callApi('list_bindings'),
    ]);
    showMultiAgentBinding(agentsResult, bindingsResult);
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function showMultiAgentBinding(agentsResult, bindingsResult) {
  const agents = (agentsResult && agentsResult.agents) || [];
  const existingBindings = (bindingsResult && bindingsResult.bindings) || [];
  if (!agents.length) {
    setContent(`<div class="view-heading"><span class="eyebrow">Multi-agent</span><h2>多 Agent 共享 MCP 模式</h2></div>
      <div class="card empty-state"><div><div class="empty-orb"></div><p>未发现 Agent。请先在数据源 tab 检测本机 Agent 或手工添加来源。</p></div></div>
      <div class="finding-actions"><button class="btn" type="button" onclick="renderSources()">← 返回数据源</button></div>`);
    return;
  }
  // 已有 active binding 的 agent 默认勾选
  const boundAgentSet = new Set(
    existingBindings.filter(b => b.status === 'active').map(b => b.agent_instance_id)
  );
  const agentRowsHtml = agents.map(a => {
    const bound = boundAgentSet.has(a.instance_id);
    return `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto;align-items:center">
      <input type="checkbox" data-agent-id="${escapeHtml(a.instance_id)}" ${bound ? 'checked' : ''}>
      <div>
        <div class="finding-rule">${escapeHtml(a.product)}</div>
        <div class="surface-meta">${a.found_surface_count}/${a.surface_count} 表面 · ${a.bound_source_count} 来源</div>
      </div>
      ${bound ? '<span class="chip chip-confirmed">已绑定</span>' : '<span class="chip chip-info">未绑定</span>'}
    </label>`;
  }).join('');

  // 已有共享组分组展示
  const groupMap = new Map();
  existingBindings.forEach(b => {
    if (b.status !== 'active') return;
    if (!groupMap.has(b.share_group_id)) groupMap.set(b.share_group_id, []);
    groupMap.get(b.share_group_id).push(b);
  });
  const groupsHtml = groupMap.size ? Array.from(groupMap.entries()).map(([gid, binds]) => `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">共享组 ${escapeHtml(gid.slice(0, 16))}</span>
      <span class="chip chip-confirmed">${binds.length} 个 Agent</span>
    </div>
    <div class="finding-evidence">${binds.map(b => escapeHtml(b.agent_instance_id)).join(' · ')}</div>
    <div class="finding-actions">
      <button class="btn" type="button" onclick="previewSharedGroup('${escapeHtml(gid)}')">查看共享组预览</button>
    </div>
  </article>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>暂无共享组。勾选 Agent 后创建。</p></div>';

  setContent(`<div class="view-heading"><span class="eyebrow">Multi-agent shared MCP</span><h2>多 Agent 共享 MCP 模式</h2>
    <p>勾选多个 Agent，创建共享组绑定，所有 Agent 通过 MemoryGuard MCP 共享同一组记忆。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 列表</h2>
      <p>勾选要加入共享组的 Agent。已有绑定的 Agent 默认勾选。</p></div></div>
      <div class="raw-file-list">${agentRowsHtml}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="createSharedBinding()">创建共享组绑定</button>
        <button class="btn" type="button" onclick="exitMultiAgentMode()">退出多 Agent 模式</button>
      </div>
    </section>
    <section class="card"><div class="card-head"><div><h2>已有共享组</h2></div></div>
      ${groupsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
      </div>
    </section>`);
}

async function createSharedBinding() {
  const checks = document.querySelectorAll('input[type=checkbox][data-agent-id]:checked');
  const agentIds = Array.from(checks).map(c => c.dataset.agentId);
  if (agentIds.length < 2) return showToast('多 Agent 共享组至少需要选择 2 个 Agent', 'error');
  if (!confirm(`确认创建共享组绑定？\n\n· ${agentIds.length} 个 Agent 将通过 MemoryGuard MCP 共享同一组记忆\n· 每个 Agent 的原生记忆模式默认为 observed\n· 可在共享组预览中查看绑定详情`)) return;
  showToast('正在创建绑定…');
  try {
    const result = await callApi('bind_agents_to_shared_group', agentIds);
    if (result.error) return showToast(result.error, 'error');
    showToast(`已创建共享组，绑定 ${agentIds.length} 个 Agent`, 'success');
    showSharedGroupPreview(result.share_group_id, result.preview);
  } catch (e) { showToast('创建失败：' + e, 'error'); }
}

async function previewSharedGroup(groupId) {
  showToast('正在加载共享组预览…');
  try {
    const result = await callApi('get_shared_group_preview', groupId);
    if (result.error) return showToast(result.error, 'error');
    showSharedGroupPreview(groupId, result);
  } catch (e) { showToast('加载失败：' + e, 'error'); }
}

function showSharedGroupPreview(groupId, preview) {
  const bindings = (preview && preview.bindings) || [];
  const memStatus = (preview && preview.memory_status) || {};
  const bindingsHtml = bindings.length ? bindings.map(b => `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">${escapeHtml(b.agent_instance_id)}</span>
      <span class="chip chip-${b.status === 'active' ? 'confirmed' : 'medium'}">${escapeHtml(b.status || '')}</span>
    </div>
    <div class="row"><span class="key">binding_id</span><code>${escapeHtml(b.binding_id || '')}</code></div>
    <div class="row"><span class="key">native_mode</span><span>${escapeHtml(b.native_memory_mode || '')}</span></div>
    <div class="row"><span class="key">mcp_server</span><span>${escapeHtml(b.mcp_server_name || '')}</span></div>
    <div class="row"><span class="key">redirect_paths</span><span>${(b.redirect_paths || []).length} 个</span></div>
  </article>`).join('') : '<div class="empty-state"><p>无绑定</p></div>';
  setContent(`<div class="view-heading"><span class="eyebrow">Shared group preview</span><h2>共享组预览</h2>
    <p>共享组 <code>${escapeHtml(groupId)}</code> · ${preview.agent_count || 0} 个 Agent</p></div>
    <section class="card"><div class="card-head"><div><h2>状态摘要</h2></div></div>
      <div class="chips">
        <span class="chip chip-info">agents · ${preview.agent_count || 0}</span>
        <span class="chip chip-confirmed">auto_writes · ${preview.auto_write_count || 0}</span>
        <span class="chip chip-medium">auto_decisions · ${preview.auto_decision_count || 0}</span>
        <span class="chip chip-high">conflicts · ${preview.conflict_count || 0}</span>
        <span class="chip chip-high">quarantine · ${preview.quarantine_count || 0}</span>
      </div>
    </section>
    <section class="card"><div class="card-head"><div><h2>绑定详情</h2></div></div>
      ${bindingsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="renderMultiAgentBinding()">← 返回多 Agent 模式</button>
      </div>
    </section>`);
}

async function exitMultiAgentMode() {
  dataPageMode = 'single_agent';
  showToast('已退回单 Agent 模式');
  renderSources();
}


async function discoverAgents() {
  showToast('正在检测本机 Agent…');
  try {
    const result = await callApi('discover_agents');
    if (result.error) return showToast(result.error, 'error');
    discoveryResult = result;
    showDiscoveryResult(result);
  } catch (e) {
    showToast('检测失败：' + e, 'error');
  }
}

function showDiscoveryResult(result) {
  const instances = result.instances || [];
  const ledger = result.discovery_ledger || {};

  // 聚合所有 surface 按 scope 分组：user / project
  const SCOPE_LABEL = { user: '全局/用户', project: '项目' };
  const scopeOrder = ['user', 'project'];
  const surfacesByScope = { user: [], project: [] };
  instances.forEach(inst => {
    (inst.surfaces || []).forEach(s => {
      const scope = s.scope || 'project';
      if (!surfacesByScope[scope]) surfacesByScope[scope] = [];
      surfacesByScope[scope].push({ ...s, product: inst.product, instance_id: inst.instance_id });
    });
  });

  const surfaceRowHtml = (s) => {
    const state = s.status || 'unknown';
    const role = s.surface_role || '';
    const cat = (s.category || '').replace(/_/g, ' ');
    return `<div class="surface-row" title="${escapeHtml(s.surface_id || '')}">
      <span class="surface-icon ${escapeHtml(state)}"></span>
      <div class="surface-path">
        <code>${escapeHtml(s.resolved_path || s.path_template || '')}</code>
        <div class="surface-meta">${escapeHtml(s.product || '')} · ${escapeHtml(s.surface_id || '')} · ${escapeHtml(role)}</div>
      </div>
      <span class="chip chip-${state==='found'?'confirmed':state==='missing'?'medium':state==='unsupported'?'medium':'info'}">${escapeHtml(state)}</span>
      <span class="chip chip-info">${escapeHtml(cat)}</span>
    </div>`;
  };

  const scopeSectionsHtml = scopeOrder.map(scope => {
    const list = surfacesByScope[scope] || [];
    const foundCount = list.filter(s => s.status === 'found').length;
    return `<details class="card" style="margin-bottom:12px">
      <summary class="card-head" style="cursor:pointer"><div><h2>${SCOPE_LABEL[scope] || scope}</h2>
        <p>${foundCount} / ${list.length} 个表面已发现 · 点击展开</p></div></summary>
      ${list.length ? list.map(surfaceRowHtml).join('') : '<div class="empty-state" style="min-height:80px"><p>此层级无候选表面</p></div>'}
    </details>`;
  }).join('');

  // Agent 实例摘要
  const LIFECYCLE_LABEL = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
  const LIFECYCLE_CHIP = { installed: 'confirmed', installed_no_data: 'info', data_only: 'medium', uncertain: 'info', ignored: 'low', not_detected: 'low' };
  const SUPPORT_CHIP = { A: 'confirmed', B: 'info', C: 'medium', D: 'high' };
  const instancesHtml = instances.length ? instances.map(inst => {
    const foundCount = (inst.surfaces || []).filter(s => s.status === 'found').length;
    const totalCount = (inst.surfaces || []).length;
    const lifecycle = inst.lifecycle_state || inst.install_state || 'pending';
    const lifecycleLabel = LIFECYCLE_LABEL[lifecycle] || lifecycle;
    const lifecycleChip = LIFECYCLE_CHIP[lifecycle] || 'info';
    const supportLevel = inst.support_level || '';
    const supportChip = SUPPORT_CHIP[supportLevel] || 'info';
    return `<article class="plan-item verified">
      <div class="finding-header">
        <span class="finding-rule">${escapeHtml(inst.product)}</span>
        <span class="chip chip-confirmed">${foundCount}/${totalCount} 表面</span>
        <span class="chip chip-${lifecycleChip}">${escapeHtml(lifecycleLabel)}</span>
        ${supportLevel ? `<span class="chip chip-${supportChip}">支持 ${escapeHtml(supportLevel)}</span>` : ''}
        <span class="chip chip-info">${escapeHtml(inst.target_capability || 'export_only')}</span>
      </div>
      <div class="row"><span class="key">profile</span><code>${escapeHtml(inst.profile_id || '')}</code></div>
      <div class="row"><span class="key">platform</span><span>${escapeHtml(inst.platform || '')} · ${escapeHtml(inst.host_id || '')}</span></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="selectAgentInstance('${escapeHtml(inst.instance_id)}')">勾选授权</button>
        <button class="btn" type="button" onclick="showResidualCleanup('${escapeHtml(inst.instance_id)}')">残留与清理</button>
      </div>
    </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>未检测到任何已安装 Agent。可手工添加文件/文件夹。</p></div>';

  setContent(`<div class="view-heading"><span class="eyebrow">Discovery</span><h2>本机 Agent 检测</h2>
    <p>有限候选发现：只检测 Profile 声明的固定路径，不递归扫描用户主目录，候选阶段不读取正文。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 摘要</h2><p>${instances.length} 个 Agent · 点击操作</p></div></div>
      ${instancesHtml}</section>
    ${scopeSectionsHtml}
    <section class="card"><div class="card-head"><div><h2>发现账本</h2>
      <p>所有已知表面 100% 进入账本，unaccounted 必须为 0</p></div></div>
      <div class="chips">
        <span class="chip chip-confirmed">found · ${ledger.found || 0}</span>
        <span class="chip chip-medium">missing · ${ledger.missing || 0}</span>
        <span class="chip chip-high">unsupported · ${ledger.unsupported || 0}</span>
        <span class="chip chip-${(ledger.unaccounted_count || 0) === 0 ? 'confirmed' : 'high'}">unaccounted · ${ledger.unaccounted_count || 0}</span>
        <span class="chip chip-info">total · ${ledger.surface_count || 0}</span>
      </div></section>
    <div class="finding-actions" style="margin-top:14px">
      <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
    </div>`);
}

async function selectAgentInstance(instanceId) {
  showToast('正在加载分类勾选树…');
  try {
    const result = await callApi('get_selection_tree', instanceId);
    if (result.error) return showToast(result.error, 'error');
    showSelectionTree(instanceId, result);
  } catch (e) { showToast('加载失败：' + e, 'error'); }
}

async function showResidualCleanup(instanceId) {
  setContent('<div class="loading">正在加载残留数据…</div>');
  showToast('正在加载残留数据…');
  try {
    const result = await callApi('get_residual_cleanup', instanceId);
    if (result.error) {
      setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2></div>
        <div class="card empty-state"><div><div class="empty-orb"></div>
        <p>加载失败：${escapeHtml(result.error)}</p></div></div>
        <div class="finding-actions"><button class="btn" type="button" onclick="discoverAgents()">返回检测</button></div>`);
      showToast(result.error, 'error');
      return;
    }
    const items = result.items || [];
    const candidateId = result.candidate_id || '';
    const productName = result.product || instanceId;
    const lifecycleLabel = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
    const lifecycle = lifecycleLabel[result.lifecycle_state] || result.lifecycle_state || '';
    const installEvHtml = (result.install_evidence || []).map(e => `<div class="row"><span class="key">${escapeHtml(e.probe_type)}</span><span style="color:${e.found ? 'var(--accent)' : 'var(--danger)'}">${e.found ? '命中' : '未命中'}: ${escapeHtml(e.detail || '')}</span></div>`).join('');
    const dataEvHtml = (result.data_evidence || []).map(e => `<div class="row"><span class="key">${escapeHtml(e.dir_path || '')}</span><span>${e.exists ? `${e.file_count} 文件` : '不存在'}</span></div>`).join('');
    const itemsHtml = items.length ? items.map((it, idx) => {
      const preview = it.archive_preview || {};
      const previewOk = preview.ok !== false;
      const safeIdx = idx;
      return `<article class="plan-item" data-candidate-id="${escapeHtml(candidateId)}" data-item-path="${escapeHtml(it.path || '')}" data-instance-id="${escapeHtml(instanceId)}">
        <div class="finding-header">
          <span class="finding-rule">${escapeHtml(it.path || '')}</span>
          <span class="chip chip-info">${escapeHtml(it.residual_type || '')}</span>
        </div>
        <div class="finding-evidence">${escapeHtml(it.description || '')}</div>
        ${preview.error ? `<div style="color:var(--danger);font-size:11px;margin-top:4px">归档预检失败：${escapeHtml(preview.error)}</div>` : previewOk ? `<div style="color:var(--accent);font-size:11px;margin-top:4px">可归档（dry-run 通过）</div>` : ''}
        <div class="finding-actions" style="margin-top:8px">
          <button class="btn btn-primary" type="button" onclick="archiveResidualByIdx(${safeIdx})">归档此项</button>
          <button class="btn" type="button" onclick="openResidualFolderByIdx(${safeIdx})">打开文件夹</button>
        </div>
      </article>`;
    }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>无残留数据。</p></div>';
    const archivesHtml = (result.archives || []).slice(0, 10).map((a, idx) => `<div class="plan-item" data-archive-id="${escapeHtml(a.archive_id || '')}" data-instance-id="${escapeHtml(instanceId)}">
      <div class="finding-header">
        <span class="finding-rule">${escapeHtml(a.product || '')} · ${escapeHtml(a.original_path || '')}</span>
        <span class="chip chip-info">${escapeHtml(a.archive_id || '')}</span>
      </div>
      <div class="finding-evidence">归档原因: ${escapeHtml(a.reason || '')} · 归档时间: ${escapeHtml(a.archived_at || '')}</div>
      <div class="finding-actions" style="margin-top:8px">
        <button class="btn" type="button" onclick="restoreArchivedByIdx(${idx})">恢复</button>
        <button class="btn" type="button" style="border-color:var(--danger);color:var(--danger)" onclick="deleteArchivedByIdx(${idx})">永久删除</button>
      </div>
    </div>`).join('');
    setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2>
      <p><strong>${escapeHtml(productName)}</strong> · ${escapeHtml(lifecycle)} · candidate: <code>${escapeHtml(candidateId)}</code></p></div>
      <section class="card"><div class="card-head"><div><h2>安装证据</h2><p>${(result.install_evidence || []).length} 条探针</p></div></div>
        ${installEvHtml || '<div class="empty-state"><p>无安装探针配置。</p></div>'}</section>
      <section class="card"><div class="card-head"><div><h2>数据残留</h2><p>${items.length} 个残留项 · 可归档或打开文件夹手动处理</p></div></div>
        ${itemsHtml}</section>
      ${dataEvHtml ? `<section class="card"><div class="card-head"><div><h2>数据证据明细</h2></div></div>${dataEvHtml}</section>` : ''}
      ${archivesHtml ? `<section class="card"><div class="card-head"><div><h2>归档历史</h2><p>可恢复或永久删除</p></div></div>${archivesHtml}</section>` : ''}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="discoverAgents()">返回检测</button>
      </div>`);
  } catch (e) {
    setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2></div>
      <div class="card empty-state"><div><div class="empty-orb"></div>
      <p>加载异常：${escapeHtml(String(e))}</p></div></div>
      <div class="finding-actions"><button class="btn" type="button" onclick="discoverAgents()">返回检测</button></div>`);
    showToast('加载失败：' + e, 'error');
  }
}

async function archiveResidualByIdx(idx) {
  const articles = document.querySelectorAll('.plan-item[data-candidate-id]');
  const el = articles[idx];
  if (!el) return showToast('未找到残留项', 'error');
  const candidateId = el.getAttribute('data-candidate-id');
  const dirPath = el.getAttribute('data-item-path');
  const instanceId = el.getAttribute('data-instance-id');
  if (!candidateId || !dirPath) return showToast('缺少归档参数', 'error');
  if (!confirm('确认归档此目录到 MemoryGuard 可恢复归档区？\n' + dirPath)) return;
  showToast('正在归档…');
  try {
    const result = await callApi('archive_agent_dir', '', dirPath, '', candidateId, false);
    if (result.error) return showToast('归档失败：' + result.error, 'error');
    showToast('归档成功，可从归档历史恢复');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('归档失败：' + e, 'error');
  }
}

async function openResidualFolderByIdx(idx) {
  const articles = document.querySelectorAll('.plan-item[data-candidate-id]');
  const el = articles[idx];
  if (!el) return showToast('未找到残留项', 'error');
  const candidateId = el.getAttribute('data-candidate-id');
  const dirPath = el.getAttribute('data-item-path');
  if (!candidateId || !dirPath) return showToast('缺少打开参数', 'error');
  try {
    const result = await callApi('open_agent_folder', dirPath, candidateId);
    if (result.error) return showToast('打开失败：' + (result.reason || result.error), 'error');
    showToast('已打开文件夹，请在系统文件管理器中手动处理', 'info');
  } catch (e) {
    showToast('打开失败：' + e, 'error');
  }
}

async function restoreArchivedByIdx(idx) {
  const items = document.querySelectorAll('.plan-item[data-archive-id]');
  const el = items[idx];
  if (!el) return showToast('未找到归档项', 'error');
  const archiveId = el.getAttribute('data-archive-id');
  const instanceId = el.getAttribute('data-instance-id');
  if (!archiveId) return showToast('缺少归档 ID', 'error');
  if (!confirm('确认恢复此归档到原路径？\n' + archiveId)) return;
  showToast('正在恢复…');
  try {
    const result = await callApi('restore_archived_agent', archiveId);
    if (result.error) return showToast('恢复失败：' + result.error, 'error');
    showToast('恢复成功');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('恢复失败：' + e, 'error');
  }
}

async function deleteArchivedByIdx(idx) {
  const items = document.querySelectorAll('.plan-item[data-archive-id]');
  const el = items[idx];
  if (!el) return showToast('未找到归档项', 'error');
  const archiveId = el.getAttribute('data-archive-id');
  const instanceId = el.getAttribute('data-instance-id');
  if (!archiveId) return showToast('缺少归档 ID', 'error');
  if (!confirm('永久删除此归档？此操作不可恢复！\n' + archiveId)) return;
  showToast('正在删除…');
  try {
    const result = await callApi('delete_archived_agent', archiveId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast('删除失败：' + (result.reason || result.error), 'error');
    showToast('已永久删除');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('删除失败：' + e, 'error');
  }
}

function showSelectionTree(instanceId, tree) {
  const scopes = tree.scopes || [];
  const scopeTabs = `
    <div class="scope-tabs">
      <div class="scope-tab active" data-scope="all" onclick="filterSelectionScope('all')">全部</div>
      <div class="scope-tab" data-scope="user" onclick="filterSelectionScope('user')">全局/用户</div>
      <div class="scope-tab" data-scope="project" onclick="filterSelectionScope('project')">项目</div>
      <div class="scope-tab" data-scope="unknown" onclick="filterSelectionScope('unknown')">未归属</div>
    </div>`;
  const scopeLabels = { user: '全局/用户', project: '项目', unknown: '未归属' };
  const scopeSourceLabels = { profile_declared: 'Profile声明', project_resolver: '项目解析器', fallback: '默认' };
  let treeHtml = '';
  for (const scopeObj of scopes) {
    const scope = scopeObj.scope;
    const scopeLabel = scopeLabels[scope] || scope;
    const scopeSourceLabel = scopeSourceLabels[scopeObj.scope_source] || scopeObj.scope_source || '';
    if (scope === 'project' && scopeObj.projects) {
      for (const proj of scopeObj.projects) {
        treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}" data-project="${escapeHtml(proj.project_ref || '')}">
          <div class="finding-header" style="margin:14px 0 8px">
            <span class="finding-rule">${scopeLabel} · ${escapeHtml(proj.project_ref || '')}</span>
            <span class="chip chip-info">${escapeHtml(proj.scope_source || scopeObj.scope_source || '')}</span>
          </div>`;
        for (const cat of (proj.categories || [])) {
          treeHtml += renderSelectionCategory(cat, scope, proj.project_ref);
        }
        treeHtml += `</div>`;
      }
    } else {
      treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}">
        <div class="finding-header" style="margin:14px 0 8px">
          <span class="finding-rule">${scopeLabel}</span>
          <span class="chip chip-info">${escapeHtml(scopeSourceLabel)}</span>
        </div>`;
      for (const cat of (scopeObj.categories || [])) {
        treeHtml += renderSelectionCategory(cat, scope);
      }
      treeHtml += `</div>`;
    }
  }
  setContent(`<div class="view-heading"><span class="eyebrow">Selection</span><h2>分类勾选授权</h2>
    <p>勾选要纳入治理的表面。当前阶段：已发现。完整治理流程：已发现 -> 已授权扫描 -> 已备份 -> 已纳入治理 -> 已生成受管记忆 -> 已发布 -> 已验证生效。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>授权摘要</h2>
        <p>instance: <code>${escapeHtml(instanceId)}</code></p></div></div>
      <div class="row"><span class="key">ownership</span><span>原生记忆 → agent_managed；普通文档 → external_read_only</span></div>
      <div class="row"><span class="key">backup</span><span>仅原生记忆会做基线备份，普通文档不整库复制</span></div>
    </section>
    <section class="card"><div class="card-head"><div><h2>作用域树</h2></div></div>
      ${scopeTabs}
      ${treeHtml}
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="confirmSelection('${escapeHtml(instanceId)}')">确认授权</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>`);
}

function renderSelectionCategory(cat, scope, projectRef) {
  projectRef = projectRef || '';
  const catLabels = {
    native_memory: '原生记忆', control_surface: '控制面', skill_surface: 'Skill 表面',
    conversation_history: '会话历史', runtime_evidence: '运行证据', knowledge_source: '知识来源',
    unknown: '其他', project_memory: '项目记忆',
  };
  const scopeTags = { user: '全局', project: '项目', unknown: '未归属' };
  const catLabel = catLabels[cat.category] || cat.category;
  const files = (cat.files || []).map(f => {
    const checked = f.default_selected ? 'checked' : '';
    const persisted = f.saved_selected === true ? '已保存勾选' : f.saved_selected === false ? '已保存取消' : '默认建议';
    const persistedChip = f.saved_selected === true ? 'confirmed' : f.saved_selected === false ? 'medium' : 'info';
    const fScope = f.scope || scope;
    const scopeSource = f.scope_source || '';
    const fProjectRef = f.project_ref || projectRef;
    const discoveryId = f.discovery_object_id || '';
    const confidence = f.confidence != null ? (f.confidence * 100).toFixed(0) + '%' : '';
    const reason = f.default_reason || '';
    const scopeTag = scopeTags[fScope] || fScope;
    const metaParts = [scopeTag, catLabel];
    if (reason) metaParts.push(escapeHtml(reason));
    if (confidence) metaParts.push('置信度 ' + confidence);
    return `<label class="raw-file-row" style="cursor:pointer">
      <input type="checkbox" data-cat="${escapeHtml(cat.category)}" data-path="${escapeHtml(f.path)}" data-scope="${escapeHtml(fScope)}" data-scope-source="${escapeHtml(scopeSource)}" data-project-ref="${escapeHtml(fProjectRef)}" data-discovery-object-id="${escapeHtml(discoveryId)}" ${checked}>
      <span class="raw-file-path">
        <code>${escapeHtml(f.path)}</code>
        <div class="surface-meta">${metaParts.join(' · ')}</div>
      </span>
      <span class="chip chip-${persistedChip}">${escapeHtml(persisted)}</span>
      <span class="chip chip-${f.ingestion_policy === 'import_verbatim' ? 'confirmed' : 'info'}">${escapeHtml(f.ingestion_policy || '')}</span>
    </label>`;
  }).join('');
  return `<div style="margin-bottom:14px">
    <div class="finding-header"><span class="finding-rule">${escapeHtml(catLabel)}</span>
      <span class="chip chip-info">${(cat.files || []).length} 个文件</span></div>
    <div class="raw-file-list">${files}</div>
  </div>`;
}

function filterSelectionScope(scope) {
  document.querySelectorAll('.scope-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.scope === scope);
  });
  document.querySelectorAll('.selection-group').forEach(g => {
    if (scope === 'all') {
      g.style.display = '';
    } else {
      g.style.display = g.dataset.scope === scope ? '' : 'none';
    }
  });
}

async function confirmSelection(instanceId) {
  const checks = document.querySelectorAll('input[type=checkbox][data-cat]:checked');
  const selected = Array.from(checks).map(c => ({
    category: c.dataset.cat,
    path: c.dataset.path,
    scope: c.dataset.scope || 'project',
    scope_source: c.dataset.scopeSource || 'fallback',
    project_ref: c.dataset.projectRef || '',
    discovery_object_id: c.dataset.discoveryObjectId || ''
  }));
  if (!selected.length) return showToast('请至少勾选一个文件', 'error');
  showToast('正在写入 SelectionManifest…');
  try {
    const result = await callApi('commit_selection', instanceId, selected, true);
    if (result.error) return showToast(result.error, 'error');
    showToast(`已授权 ${selected.length} 个文件，可去神经图构建投影`, 'success');
    renderSources();
  } catch (e) { showToast('授权失败：' + e, 'error'); }
}

async function addSourceDialog() {
  // 使用系统目录选择器（pywebview）或文件选择器
  const result = await callApi('pick_path');
  if (!result || result.error || !result.path) {
    if (result && result.error && result.error !== 'cancelled') showToast(result.error, 'error');
    return;
  }
  const path = result.path;
  const isDir = result.is_directory;
  const name = prompt('显示名称（可留空）：', '') || '';
  showToast('正在添加来源…');
  try {
    const addResult = await callApi('add_source', path, isDir ? 'directory' : 'file', name, true);
    if (addResult.error) return showToast(addResult.error, 'error');
    showToast('来源已添加', 'success');
    renderSources();
  } catch (e) { showToast('添加失败：' + e, 'error'); }
}

async function importBundleDialog() {
  const result = await callApi('pick_path', true);
  if (!result || result.error || !result.path) {
    if (result && result.error && result.error !== 'cancelled') showToast(result.error, 'error');
    return;
  }
  const path = result.path;
  showToast('正在检测导出包…');
  try {
    const preview = await callApi('preview_import', path);
    if (preview.error) {
      showToast(preview.error, 'error');
      return;
    }
    showImportPreview(path, preview);
  } catch (e) {
    showToast('检测失败：' + e, 'error');
  }
}

async function importBundlePrompt() {
  // 兼容旧调用：转发到 importBundleDialog
  await importBundleDialog();
}

function showImportPreview(path, preview) {
  const inv = preview.inventory || {};
  const invText = Object.keys(inv).map(k => `· ${k}: ${JSON.stringify(inv[k]).slice(0, 200)}`).join('\n');
  setContent(`<div class="view-heading"><span class="eyebrow">Import preview</span><h2>离线导出包预览</h2>
    <p>检测到的 provider 和 inventory。会话内容默认只作为证据/萃取来源，不直接写入长期记忆。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>检测结果</h2></div>
        <span class="chip chip-confirmed">${escapeHtml(preview.provider || 'unknown')}</span></div>
      <div class="row"><span class="key">path</span><code style="overflow-wrap:anywhere">${escapeHtml(path)}</code></div>
      <div class="row"><span class="key">confidence</span><span>${(preview.confidence || 0) * 100}%</span></div>
      <div class="row"><span class="key">notes</span><span>${escapeHtml(preview.notes || '')}</span></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Inventory</h2><p>检测到的会话/文件清单</p></div></div>
      <pre class="raw-file-content">${escapeHtml(invText || '(empty)')}</pre>
    </section>
    <section class="card">
      <div class="gate-warning">
        <strong>会话不会默认写入 Memory IR。</strong><br>
        原始文件不会被修改；需要像文档一样先萃取候选，用户接受后才进入长期记忆。
      </div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="confirmImport('${escapeHtml(path).replaceAll("'", "\\'")}')">确认导入</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>`);
}

async function confirmImport(path) {
  if (!confirm('确认解析此导出包？\n· 会话只作为证据/萃取来源\n· 不直接写入长期记忆\n· 原始文件不被修改')) return;
  showToast('正在解析…');
  try {
    const result = await callApi('create_import', path, true);
    if (result.error) {
      showToast(result.error, 'error');
      return;
    }
    showToast(`解析完成：${result.conversation_count} 个会话，未直接写入长期记忆`, 'success');
    renderSources();
  } catch (e) {
    showToast('导入失败：' + e, 'error');
  }
}

async function addSourcePrompt() {
  // 兼容旧调用：转发到 addSourceDialog
  await addSourceDialog();
}

async function removeSource(rootId) {
  if (!confirm('移除来源 ' + rootId + '？\n配置会被删除，原始文件不会被改动。')) return;
  try {
    const result = await callApi('remove_source', rootId, true);
    if (result.error) return showToast(result.error, 'error');
    showToast('来源已移除', 'success');
    renderSources();
  } catch (e) { showToast('移除失败：' + e, 'error'); }
}

async function viewSourceFile(rootId, relativePath) {
  if (!rootId) return showToast('该条目只是发现结果，尚未授权为可读取来源', 'info');
  try {
    const result = await callApi('get_source_file_content', rootId, relativePath);
    if (result.error) {
      showToast(result.error === 'file not found' ? '文件已不存在，请刷新数据源' : result.error, 'error');
      if (result.error === 'file not found' || result.error === 'source root not found') renderSources();
      return;
    }
    const escaped = escapeHtml(result.content);
    const lines = result.content.split('\n').length;
    const safePath = escapeHtml(relativePath).replaceAll('\\', '/').replaceAll("'", "\\'");
    setContent(`<div class="view-heading"><span class="eyebrow">Raw memory</span><h2>${escapeHtml(result.display_name)} · ${escapeHtml(relativePath).replaceAll('\\', '/')}</h2>
      <p>原始记忆文件，只读查看。size=${result.size} bytes · lines=${lines}</p></div>
      <section class="card"><div class="card-head"><div>
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
        <button class="btn btn-primary" type="button" onclick="extractSourceFile('${escapeHtml(rootId)}','${safePath}')">萃取为实用记忆</button>
      </div></div>
      <pre class="raw-file-content">${escaped}</pre></section>`);
  } catch (e) { showToast('读取失败：' + e, 'error'); }
}

async function extractSourceFile(rootId, relativePath) {
  showToast('正在萃取预览…');
  try {
    const result = await callApi('extract_preview', rootId, relativePath);
    if (result.error) return showToast(result.error, 'error');
    showExtractPreview(rootId, relativePath, result);
  } catch (e) { showToast('萃取失败：' + e, 'error'); }
}

function showExtractPreview(rootId, relativePath, result) {
  const candidates = result.candidates || [];
  const riskLabel = { high: '高风险', medium: '中风险', low: '低风险' };
  const riskChip = { high: 'high', medium: 'medium', low: 'confirmed' };
  const itemsHtml = candidates.length ? candidates.map((c, i) => `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto;align-items:flex-start">
      <input type="checkbox" data-candidate-id="${escapeHtml(c.candidate_id)}" checked>
      <div>
        <div class="finding-rule">#${i + 1} · ${escapeHtml(c.kind || '')}</div>
        <div class="finding-evidence" style="white-space:pre-wrap;margin-top:4px">${escapeHtml(c.preview || c.body || '').slice(0, 300)}</div>
      </div>
      <span class="chip chip-${riskChip[c.risk_level] || 'info'}">${escapeHtml(riskLabel[c.risk_level] || c.risk_level)}</span>
    </label>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>未识别到可萃取的记忆片段。</p></div>';
  const safePath = escapeHtml(relativePath).replaceAll('\\', '/').replaceAll("'", "\\'");
  const extractId = escapeHtml(result.extract_id || '');
  setContent(`<div class="view-heading"><span class="eyebrow">Extract preview</span><h2>萃取候选预览</h2>
    <p>从 <code>${escapeHtml(relativePath).replaceAll('\\', '/')}</code> 提取出 ${result.total || 0} 条候选记忆。请审阅后选择接受哪些。</p></div>
    <section class="card"><div class="card-head"><div><h2>候选列表</h2>
      <p>勾选要接受的候选，取消勾选将丢弃。风险等级标注敏感内容或低置信度。</p></div></div>
      <div class="raw-file-list">${itemsHtml}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="acceptExtractCandidates('${extractId}','${escapeHtml(rootId)}','${safePath}')">接受选中</button>
        <button class="btn btn-danger" type="button" onclick="discardExtractCandidates('${escapeHtml(rootId)}','${safePath}')">全部丢弃</button>
      </div>
    </section>`);
}

async function acceptExtractCandidates(extractId, rootId, relativePath) {
  const checks = document.querySelectorAll('input[type=checkbox][data-candidate-id]:checked');
  const candidateIds = Array.from(checks).map(c => c.dataset.candidateId);
  if (!candidateIds.length) return showToast('请至少勾选一个候选', 'error');
  if (!confirm('确认接受 ' + candidateIds.length + ' 条候选记忆并写入共享记忆？')) return;
  showToast('正在写入记忆…');
  try {
    const result = await callApi('accept_candidates', extractId, candidateIds);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已接受 ' + result.total + ' 条记忆', 'success');
    showAcceptResult(rootId, relativePath, result);
  } catch (e) { showToast('写入失败：' + e, 'error'); }
}

function discardExtractCandidates(rootId, relativePath) {
  showToast('已丢弃全部候选，staging 文件将在 24 小时后自动清理', 'success');
  viewSourceFile(rootId, relativePath);
}

function showAcceptResult(rootId, relativePath, result) {
  const accepted = result.accepted || [];
  const itemsHtml = accepted.length ? accepted.map((m, i) => `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">#${i + 1} · ${escapeHtml(m.kind || '')}</span>
      <span class="chip chip-${m.status === 'active' ? 'confirmed' : 'medium'}">${escapeHtml(m.status || '')}</span>
    </div>
    <div class="row"><span class="key">memory_id</span><code>${escapeHtml(m.memory_id || '')}</code></div>
  </article>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>无写入记录。</p></div>';
  const safePath = escapeHtml(relativePath).replaceAll('\\', '/').replaceAll("'", "\\'");
  setContent(`<div class="view-heading"><span class="eyebrow">Accept</span><h2>写入结果</h2>
    <p>已接受 ${result.total || 0} 条记忆，写入共享组 <code>${escapeHtml(result.share_group_id || 'default')}</code>。</p></div>
    <section class="card"><div class="card-head"><div><h2>写入的记忆</h2></div></div>
      ${itemsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="viewSourceFile('${escapeHtml(rootId)}','${safePath}')">← 返回源文件</button>
        <button class="btn btn-primary" type="button" onclick="renderSources()">返回数据源</button>
      </div>
    </section>`);
}

// ===========================================================================
// v3 变更记录 tab：BuildPlan + ReleaseChange + apply/verify/rollback
// spec §7.1 / §7.2 MemoryApi
// ===========================================================================

async function renderReleases() {
  setContent('<div class="loading">正在读取变更记录</div>');
  try {
    // v3.1 §8.4：用 list_history 替代 list_releases，包含 rule_change + memory_release + warnings
    const historyResult = await callApi('list_history');
    state.releases = (historyResult.items || []).filter(it => it.record_type === 'memory_release');
    state.ruleChanges = (historyResult.items || []).filter(it => it.record_type === 'rule_change');
    state.historyWarnings = historyResult.warnings || [];
    document.getElementById('releases-count').textContent = historyResult.items ? historyResult.items.length : 0;
    renderReleasesView();
  } catch (e) {
    showToast('变更记录加载失败：' + e, 'error');
    state.releases = [];
    state.ruleChanges = [];
    state.historyWarnings = [];
    renderReleasesView();
  }
}

function renderReleasesView() {
  const releases = state.releases || [];
  // 规则级修复（spec §11.1 保留 v2.1）
  const plans = state.plans || [];
  // v3.1 §8.4：state.ruleChanges 来自旧 changes/ 的兼容读取
  const ruleChanges = state.ruleChanges || [];
  const warnings = state.historyWarnings || [];

  const warningsHtml = warnings.length ? `<section class="card">
    <div class="card-head"><div><h2>历史解析警告</h2>
      <p>${warnings.length} 条历史记录无法解析，单条坏记录不阻断页面</p></div></div>
    ${warnings.map(w => `<div class="raw-file-row" style="cursor:default">
      <span class="raw-file-path"><code>${escapeHtml(w.file_name)}</code></span>
      <span class="chip chip-high">${escapeHtml(w.reason)}</span>
      ${w.missing_keys && w.missing_keys.length ? `<span class="chip chip-medium">missing: ${escapeHtml(w.missing_keys.join(', '))}</span>` : ''}
    </div>`).join('')}
  </section>` : '';

  const plansHtml = plans.length ? plans.map(plan => {
    const status = plan.change ? plan.change.status : 'pending';
    const statusClass = status === 'verified' ? 'confirmed' : status === 'failed' ? 'high' : 'info';
    return `<article class="plan-item ${escapeHtml(status)}"><div class="finding-header">
      <span class="finding-rule">修复计划 ${escapeHtml(plan.plan_id.slice(-8))}</span><span class="chip chip-${statusClass}">${escapeHtml(status)}</span></div>
      <div class="finding-evidence">处理 ${plan.finding_ids.length} 个风险信号 · ${plan.patches.length} 个补丁 · 风险 ${escapeHtml(plan.risk_level)}</div>
      <div class="finding-actions">
        ${status === 'pending' ? `<button class="btn btn-primary" type="button" onclick="applyPlan('${escapeHtml(plan.plan_id)}')">应用并验证</button>` : ''}
        ${status === 'verified' || status === 'applied' ? `<button class="btn btn-danger" type="button" onclick="undoChange('${escapeHtml(plan.change.change_id)}','${escapeHtml(plan.plan_id)}')">撤销变更</button>` : ''}
      </div></article>`;
  }).join('') : (ruleChanges.length ? ruleChanges.map(rc => `<article class="plan-item">
    <div class="finding-header">
      <span class="finding-rule">规则修复 ${escapeHtml((rc.event_id || '').slice(-8))}</span>
      <span class="chip chip-info">rule_change · schema ${escapeHtml(rc.schema_version || '2.1')}</span>
    </div>
    <div class="row"><span class="key">plan_id</span><code>${escapeHtml(rc.plan_id || '')}</code></div>
    <div class="row"><span class="key">applied_at</span><span>${escapeHtml(rc.applied_at || '')}</span></div>
    <div class="row"><span class="key">status</span><span>${escapeHtml(rc.status || '')}</span></div>
  </article>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>没有规则级修复记录。</p></div>');

  const releasesHtml = releases.length ? releases.map(r => {
    const status = r.status || 'unknown';
    const statusClass = status === 'verified' ? 'confirmed' : status === 'rolled_back' ? 'medium' : status === 'failed' ? 'high' : 'info';
    return `<article class="plan-item ${escapeHtml(status)}">
      <div class="finding-header">
        <span class="finding-rule">Release ${escapeHtml((r.event_id || '').slice(-8))}</span>
        <span class="chip chip-${statusClass}">${escapeHtml(status)}</span>
      </div>
      <div class="row"><span class="key">build_id</span><code>${escapeHtml((r.build_id || '').slice(-12))}</code></div>
      <div class="row"><span class="key">profile</span><span>${escapeHtml(r.target_profile || '')}</span></div>
      <div class="row"><span class="key">applied_at</span><span>${escapeHtml(r.applied_at || '')}</span></div>
      <div class="row"><span class="key">changed</span><span>${r.changed_count || 0} 个文件</span></div>
      <div class="finding-actions">
        ${status === 'applied' || status === 'verified' ? `<button class="btn" type="button" onclick="verifyRelease('${escapeHtml(r.event_id)}')">重新验证</button>
        <button class="btn btn-danger" type="button" onclick="rollbackRelease('${escapeHtml(r.event_id)}')">回滚</button>` : ''}
      </div>
    </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>尚无发布记录。先生成构建计划。</p></div>';

  setContent(`<div class="view-heading"><span class="eyebrow">Releases</span><h2>变更记录</h2>
    <p>两类变更闭环统一时间线。规则级修复（Finding→Plan→Apply→Undo）和发布事务（BuildPlan→Apply→Verify→Rollback）。</p></div>
    ${warningsHtml}
    <section class="card"><div class="card-head"><div><h2>规则级修复</h2><p>对单个 Finding 生成最小补丁，备份后应用，重扫验证，可撤销</p></div></div>
      ${plansHtml}</section>
    <section class="card"><div class="card-head"><div><h2>发布事务</h2><p>从 Memory IR 生成 BuildPlan，展示完整 Diff 后由用户批准</p></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="createBuildPlan()">生成构建计划</button>
      </div></div>
      <div class="gate-warning" style="margin-top:0">
        <strong>发布会完整替换受管目标文件。</strong>回滚以 ReleaseChange 为单位，每次回滚前会备份当前目标状态。
      </div>
    </section>
    <section class="card"><div class="card-head"><div><h2>发布历史</h2><p>${releases.length} 条 memory_release 记录</p></div></div>
      ${releasesHtml}</section>`);
}

async function createBuildPlan() {
  const targetPath = prompt('目标路径（留空使用默认 .memoryguard/memory-target）：', '') || '';
  showToast('正在生成构建计划…');
  try {
    const plan = await callApi('create_build_plan', targetPath);
    if (plan.error) return showToast(plan.error, 'error');
    state.lastPlan = plan;
    showBuildPlanDetail(plan);
  } catch (e) { showToast('生成失败：' + e, 'error'); }
}

function showBuildPlanDetail(plan) {
  const manifest = plan.manifest || {};
  const diff = plan.diff_preview || {};
  setContent(`<div class="view-heading"><span class="eyebrow">Build plan</span><h2>构建计划 ${escapeHtml((plan.plan_id || '').slice(-8))}</h2>
    <p>请审阅完整 Diff 和覆盖率状态后批准应用。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>完整性</h2><p>BuildManifest 五个完整性条件</p></div>
        <span class="chip chip-${plan.integrity_ok ? 'confirmed' : 'high'}">${plan.integrity_ok ? 'integrity OK' : 'integrity FAIL'}</span></div>
      <div class="row"><span class="key">coverage</span><span>${escapeHtml(plan.coverage_status || '')}</span></div>
      <div class="row"><span class="key">target_profile</span><span>${escapeHtml(plan.target_profile || '')}</span></div>
      <div class="row"><span class="key">published</span><span>${manifest.published_record_count || 0} 条</span></div>
      <div class="row"><span class="key">unaccounted</span><span>${manifest.unaccounted_record_count || 0} 条</span></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Diff 预览</h2><p>受管目标文件变更摘要</p></div></div>
      <pre class="raw-file-content">${escapeHtml(JSON.stringify(diff, null, 2))}</pre>
    </section>
    <section class="card">
      <div class="gate-warning">
        <strong>应用后会完整替换受管目标文件。</strong>系统会先备份当前目标状态，再原子切换，最后复扫验证。
      </div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="applyBuildPlan('${escapeHtml(plan.plan_id)}')">批准并应用</button>
        <button class="btn" type="button" onclick="renderReleases()">取消</button>
      </div>
    </section>`);
}

async function applyBuildPlan(planId) {
  if (!confirm('确认应用构建计划？\n\n· 会先备份当前受管目标文件\n· 原子切换为新内容\n· 复扫验证\n· 可通过回滚恢复')) return;
  showToast('正在应用并验证…');
  try {
    const result = await callApi('apply_build', planId, true);
    if (result.error) return showToast(result.error, 'error');
    showToast('发布已应用并验证', 'success');
    await renderReleases();
  } catch (e) { showToast('应用失败：' + e, 'error'); }
}

async function verifyRelease(releaseId) {
  showToast('正在重新验证…');
  try {
    const result = await callApi('verify_release', releaseId);
    if (result.error) return showToast(result.error, 'error');
    const ok = result.rescan_match && result.hashes_match;
    showToast(ok ? '验证通过' : '验证失败', ok ? 'success' : 'error');
  } catch (e) { showToast('验证失败：' + e, 'error'); }
}

async function rollbackRelease(releaseId) {
  if (!confirm('回滚此发布？\n\n· 会先备份当前目标状态\n· 恢复为应用前的内容\n· 复扫验证')) return;
  showToast('正在回滚…');
  try {
    const result = await callApi('rollback_release', releaseId, true);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已回滚', 'success');
    await renderReleases();
  } catch (e) { showToast('回滚失败：' + e, 'error'); }
}

// ===========================================================================
// 治理台 tab：最近写入 / 覆盖记录 / 冲突队列 / 隔离队列 / 版本回滚
// ===========================================================================

function renderGovernance() {
  const tabs = [
    { id: 'recent_events', label: '最近写入' },
    { id: 'supersede', label: '覆盖记录' },
    { id: 'conflicts', label: '冲突队列' },
    { id: 'quarantine', label: '隔离队列' },
    { id: 'rollback', label: '版本回滚' },
  ];
  const tabsHtml = tabs.map(t =>
    `<div class="scope-tab ${t.id === governanceSubTab ? 'active' : ''}" onclick="switchGovernanceSub('${t.id}')">${t.label}</div>`
  ).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Governance</span><h2>治理台</h2>
    <p>记忆治理操作台：最近写入、覆盖记录、冲突队列、隔离队列、版本回滚。</p></div>
    <div class="scope-tabs">${tabsHtml}</div>
    <div id="governance-content"><div class="loading">正在加载</div></div>`);
  renderGovernanceSub();
}

function switchGovernanceSub(subTab) {
  governanceSubTab = subTab;
  renderGovernance();
}

function renderGovernanceSub() {
  switch (governanceSubTab) {
    case 'recent_events': renderRecentEvents(); break;
    case 'supersede': renderSupersedeChain(); break;
    case 'conflicts': renderConflictQueue(); break;
    case 'quarantine': renderQuarantine(); break;
    case 'rollback': renderRollback(); break;
  }
}

async function renderRecentEvents() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取最近写入</div>';
  try {
    const result = await callApi('get_recent_events');
    if (result.error) return showToast(result.error, 'error');
    const events = result.events || [];
    if (!events.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无自动写入事件。</p></div></div>';
      return;
    }
    const items = events.map(e => {
      const preview = escapeHtml((e.raw_content || '').slice(0, 100));
      const actions = (e.auto_actions || []).map(a => `<span class="chip chip-info">${escapeHtml(a.action || a.type || 'auto')}</span>`).join('');
      return `<article class="plan-item" onclick="toggleEventDetail('${escapeHtml(e.event_id)}')">
        <div class="finding-header">
          <span class="finding-rule">${escapeHtml(e.agent_instance_id || 'unknown')}</span>
          <span class="chip chip-info">${escapeHtml(e.created_at || '')}</span>
        </div>
        <div class="finding-evidence">${preview}${(e.raw_content || '').length > 100 ? '…' : ''}</div>
        ${actions ? `<div class="chips" style="margin-top:6px">${actions}</div>` : ''}
        <div class="finding-detail" id="event-detail-${escapeHtml(e.event_id)}" style="display:none">
          <div class="row"><span class="key">event_id</span><code>${escapeHtml(e.event_id || '')}</code></div>
          <div class="row"><span class="key">agent</span><span>${escapeHtml(e.agent_instance_id || '')}</span></div>
          <div class="row"><span class="key">group</span><span>${escapeHtml(e.share_group_id || '')}</span></div>
          <div class="row"><span class="key">时间</span><span>${escapeHtml(e.created_at || '')}</span></div>
          <div class="row"><span class="key">完整内容</span></div>
          <pre class="raw-file-content" style="max-height:300px">${escapeHtml(e.raw_content || '')}</pre>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>最近自动写入</h2>
      <p>最近 ${events.length} 条 MemoryEvent（最多 50 条）</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

function toggleEventDetail(eventId) {
  const el = document.getElementById('event-detail-' + eventId);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function renderSupersedeChain() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取覆盖记录</div>';
  try {
    const result = await callApi('get_supersede_decisions');
    if (result.error) return showToast(result.error, 'error');
    const decisions = result.decisions || [];
    if (!decisions.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无自动覆盖记录。</p></div></div>';
      return;
    }
    const items = decisions.map(d => `<article class="plan-item">
      <div class="finding-header">
        <span class="finding-rule">覆盖 · ${escapeHtml(d.created_at || '')}</span>
        <span class="chip chip-medium">auto_supersede</span>
      </div>
      <div class="row" style="margin-top:8px"><span class="key">新记忆</span><code>${escapeHtml(d.new_memory_id || '')}</code></div>
      <div class="finding-evidence" style="margin-left:82px">${escapeHtml(d.new_content_preview || '(无内容)')}</div>
      <div class="row" style="margin-top:8px"><span class="key">旧记忆</span><code>${escapeHtml(d.old_memory_id || '')}</code></div>
      <div class="finding-evidence" style="margin-left:82px;color:var(--faint)">${escapeHtml(d.old_content_preview || '(无内容)')}</div>
      <div class="row" style="margin-top:8px"><span class="key">原因</span><span>${escapeHtml(d.reason || '')}</span></div>
    </article>`).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>自动覆盖记录</h2>
      <p>共 ${decisions.length} 条 auto_supersede 决策。新记忆 -> 旧记忆覆盖链。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function renderConflictQueue() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取冲突队列</div>';
  try {
    const [conflictsResult, memResult] = await Promise.all([
      callApi('get_conflicts'),
      callApi('list_memory'),
    ]);
    if (conflictsResult.error) return showToast(conflictsResult.error, 'error');
    const conflicts = (conflictsResult.conflicts || []).filter(c => c.status === 'unresolved');
    if (!conflicts.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无未解决冲突。</p></div></div>';
      return;
    }
    const records = {};
    (memResult.records || []).forEach(r => { records[r.memory_id] = r; });
    const items = conflicts.map(c => {
      const members = (c.member_ids || []).map(mid => {
        const rec = records[mid];
        const preview = rec ? escapeHtml((rec.body || '').slice(0, 100)) : '(记录不存在)';
        return `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto;align-items:center">
          <input type="radio" name="conflict-${escapeHtml(c.group_id)}" value="${escapeHtml(mid)}">
          <div>
            <code>${escapeHtml(mid)}</code>
            <div class="surface-meta">${preview}</div>
          </div>
          <span class="chip chip-${rec ? 'confirmed' : 'high'}">${rec ? escapeHtml(rec.status || 'active') : 'missing'}</span>
        </label>`;
      }).join('');
      return `<article class="plan-item">
        <div class="finding-header">
          <span class="finding-rule">冲突组 ${escapeHtml((c.group_id || '').slice(0, 16))}</span>
          <span class="chip chip-high">${escapeHtml(c.status || 'unresolved')}</span>
        </div>
        <div class="finding-evidence" style="margin-top:6px">原因：${escapeHtml(c.reason || '')}</div>
        <div class="row" style="margin-top:6px"><span class="key">创建时间</span><span>${escapeHtml(c.created_at || '')}</span></div>
        <div class="raw-file-list" style="margin-top:10px">${members}</div>
        <div class="finding-actions" style="margin-top:10px">
          <button class="btn btn-primary" type="button" onclick="resolveConflict('${escapeHtml(c.group_id)}')">保留选中并解决</button>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>冲突队列</h2>
      <p>共 ${conflicts.length} 个未解决冲突组。选择保留哪条，其余将被软删除。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function resolveConflict(groupId) {
  const selected = document.querySelector(`input[name="conflict-${groupId}"]:checked`);
  if (!selected) return showToast('请先选择要保留的记忆', 'error');
  const keepId = selected.value;
  if (!confirm('确认解决冲突？\n\n· 保留记忆：' + keepId + '\n· 其他成员将被软删除')) return;
  showToast('正在解决冲突…');
  try {
    const result = await callApi('resolve_conflict', groupId, keepId);
    if (result.error) return showToast(result.error, 'error');
    showToast('冲突已解决', 'success');
    renderConflictQueue();
  } catch (e) { showToast('解决失败：' + e, 'error'); }
}

async function renderQuarantine() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取隔离队列</div>';
  try {
    const result = await callApi('get_quarantine');
    if (result.error) return showToast(result.error, 'error');
    const entries = (result.quarantine || []).filter(e => !e.released);
    if (!entries.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>隔离队列为空。</p></div></div>';
      return;
    }
    const items = entries.map(e => {
      return `<article class="plan-item">
        <div class="finding-header">
          <span class="finding-rule">隔离 ${escapeHtml((e.quarantine_id || '').slice(0, 16))}</span>
          <span class="chip chip-high">quarantined</span>
        </div>
        <div class="row"><span class="key">memory_id</span><code>${escapeHtml(e.memory_id || '')}</code></div>
        <div class="row"><span class="key">原内容</span><span style="font-family:monospace">${escapeHtml(e.masked_preview || '••••')}</span></div>
        <div class="row"><span class="key">原因</span><span>${escapeHtml(e.reason || '')}</span></div>
        <div class="row"><span class="key">匹配模式</span><code>${escapeHtml(e.detected_pattern || '')}</code></div>
        <div class="row"><span class="key">隔离时间</span><span>${escapeHtml(e.quarantined_at || '')}</span></div>
        <div class="finding-actions" style="margin-top:10px">
          <button class="btn btn-primary" type="button" onclick="releaseQuarantine('${escapeHtml(e.quarantine_id)}')">释放</button>
          <button class="btn btn-danger" type="button" onclick="deleteQuarantine('${escapeHtml(e.quarantine_id)}')">永久删除</button>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>隔离队列</h2>
      <p>共 ${entries.length} 条未释放隔离记忆。敏感内容已脱敏显示。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function releaseQuarantine(quarantineId) {
  if (!confirm('释放此隔离记忆？\n\n· 记忆将恢复为 active 状态\n· 请确认内容安全')) return;
  showToast('正在释放…');
  try {
    const result = await callApi('release_quarantine', quarantineId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已释放', 'success');
    renderQuarantine();
  } catch (e) { showToast('释放失败：' + e, 'error'); }
}

async function deleteQuarantine(quarantineId) {
  if (!confirm('永久删除此隔离记忆？\n\n· 记忆将被标记为 deleted\n· 此操作不可撤销')) return;
  showToast('正在删除…');
  try {
    const result = await callApi('delete_quarantine', quarantineId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已删除', 'success');
    renderQuarantine();
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function renderRollback() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取版本历史</div>';
  try {
    const result = await callApi('list_memory_versions');
    if (result.error) return showToast(result.error, 'error');
    const versions = (result.versions || []).slice().sort((a, b) =>
      (b.created_at || '').localeCompare(a.created_at || ''));
    if (!versions.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无版本记录。</p></div></div>';
      return;
    }
    const items = versions.map(v => `<article class="plan-item">
      <div class="finding-header">
        <span class="finding-rule">版本 ${escapeHtml((v.version_id || '').slice(0, 16))}</span>
        <span class="chip chip-info">${escapeHtml(v.created_at || '')}</span>
      </div>
      <div class="row"><span class="key">version_id</span><code>${escapeHtml(v.version_id || '')}</code></div>
      <div class="row"><span class="key">原因</span><span>${escapeHtml(v.reason || '(无)')}</span></div>
      <div class="row"><span class="key">记录数</span><span>${v.record_count || 0} 条（active: ${v.active_count || 0}）</span></div>
      <div class="finding-actions" style="margin-top:10px">
        <button class="btn btn-danger" type="button" onclick="rollbackToVersion('${escapeHtml(v.version_id)}')">回滚到此版本</button>
      </div>
    </article>`).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>版本回滚</h2>
      <p>共 ${versions.length} 个版本。回滚前会自动创建当前状态快照。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function rollbackToVersion(versionId) {
  if (!confirm('确认回滚到版本 ' + versionId.slice(0, 16) + '？\n\n· 会先备份当前状态为新版本\n· 恢复为该版本的 records 和 decisions\n· 可通过新版本再次回滚')) return;
  showToast('正在回滚…');
  try {
    const result = await callApi('rollback_memory', versionId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已回滚到目标版本', 'success');
    renderRollback();
  } catch (e) { showToast('回滚失败：' + e, 'error'); }
}

function showToast(message, type) {
  const toast = document.getElementById('toast');
  toast.textContent = message; toast.className = 'toast show ' + (type || '');
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.className = 'toast', 2600);
}

window.addEventListener('pywebviewready', init);
setTimeout(() => { if (!state.report && window.pywebview && window.pywebview.api) init(); }, 2000);
</script>
</body>
</html>"""
