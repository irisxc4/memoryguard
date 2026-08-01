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
<link rel="icon" type="image/png" href="memoryguard-icon.png">
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
.reader-toggle-buttons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
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
.finding-toggle { color: var(--accent); font-size: 11px; white-space: nowrap; }
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
.scope-select {
  width: 100%; padding: 10px 12px; border: 1px solid var(--line-strong); border-radius: 10px;
  color: var(--fg); background: rgba(7, 24, 18, .96); font: inherit; outline: none;
}
.scope-select:focus { box-shadow: 0 0 0 2px rgba(110,231,196,.16); }
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
.knowledge-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px;
}
.memory-card { margin: 10px 0; padding: 13px 14px; border: 1px solid rgba(110,231,196,.16); border-radius: 10px; background: rgba(3,20,15,.46); }
.memory-card-top { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.memory-card p { margin:8px 0; color:var(--muted); white-space:pre-wrap; }
.muted { color:var(--faint); font-size:11px; }
.raw-content { max-height:420px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--fg); font:12px/1.6 var(--mono, monospace); }
.knowledge-card {
  position: relative; min-width: 0; padding: 15px; overflow: hidden;
  border: 1px solid var(--line); border-radius: 12px;
  background: radial-gradient(circle at 16px 16px, rgba(110,231,196,.12), transparent 46px), rgba(10,26,21,.52);
}
.knowledge-card::before {
  content: ""; position: absolute; top: 15px; left: 14px; width: 7px; height: 7px;
  border-radius: 50%; background: var(--accent); box-shadow: 0 0 11px var(--accent);
}
.knowledge-card.missing::before { background: var(--red); box-shadow: 0 0 11px var(--red); }
.knowledge-title { padding-left: 17px; font-size: 13px; font-weight: 650; color: var(--fg); }
.knowledge-path {
  margin: 10px 0; padding: 8px 10px; overflow: hidden; text-overflow: ellipsis;
  border: 1px solid rgba(110,231,196,.10); border-radius: 8px;
  background: rgba(3,10,8,.46); color: var(--muted); white-space: nowrap;
}
.knowledge-path code { font-size: 10px; }
.knowledge-files { margin-top: 10px; }
.knowledge-files summary { cursor: pointer; color: var(--muted); font-size: 11px; }
.knowledge-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.plan-item { cursor: default; }
.plan-item.verified::before, .plan-item.applied::before { background: var(--accent); }

/* 神经元画布 */
.neuron-shell {
  position: relative; min-height: calc(100vh - 120px); overflow: hidden;
  border: 1px solid var(--line); border-radius: 18px;
  background: radial-gradient(circle at 50% 48%, rgba(37, 104, 83, .14), transparent 38%), rgba(3, 10, 8, .74);
  box-shadow: var(--shadow), inset 0 0 80px rgba(0, 0, 0, .30);
  animation: neuron-shell-breath 8s ease-in-out infinite;
  isolation: isolate;
  overflow: hidden;
}
.neuron-shell::before {
  content: ""; position: absolute; inset: 0; pointer-events: none; opacity: .28;
  background-image: radial-gradient(circle, rgba(110,231,196,.28) 1px, transparent 1.4px);
  background-size: 24px 24px; mask-image: radial-gradient(circle at center, black, transparent 82%);
  animation: neuron-grid-drift 18s linear infinite;
}
.neuron-shell::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(90deg, transparent 0%, rgba(110,231,196,.14) 48%, transparent 100%);
  mix-blend-mode: screen; opacity: .24;
  transform: skewX(-28deg) translateX(-62%);
  animation: neuron-scanline 7.8s linear infinite;
}
.neuron-stage {
  position: relative; width: 100%; height: calc(100vh - 120px); min-height: 610px;
  overflow: hidden;
}
.neuron-stage::before {
  content: ""; position: absolute; inset: -20%; pointer-events: none; z-index: 1;
  border-radius: 50%;
  border: 1px solid rgba(188, 255, 235, .12);
  background:
    radial-gradient(circle at 28% 22%, rgba(110,231,196,.19), transparent 33%),
    radial-gradient(circle at 72% 74%, rgba(125,211,252,.11), transparent 38%);
  opacity: .52;
  mix-blend-mode: screen;
  animation: neuron-orbit-spin 20s linear infinite;
}
.neuron-stage::after {
  content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 2;
  background-image:
    radial-gradient(circle at 60% 32%, rgba(110,231,196,.06) 0 0.7px, transparent 1.2px),
    radial-gradient(circle at 28% 78%, rgba(129,237,215,.05) 0 0.7px, transparent 1.2px);
  background-size: 24px 24px, 18px 18px;
  opacity: .25;
  animation: neuron-noise-shimmer 4.7s ease-in-out infinite;
  mix-blend-mode: soft-light;
}
.neuron-toolbar {
  position: absolute; z-index: 12; top: 16px; left: 18px; right: 18px;
  display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; pointer-events: none;
}
.neuron-canvas-haze {
  position: absolute; inset: -10%; pointer-events: none; z-index: 2;
  background:
    radial-gradient(circle at 30% 20%, rgba(110,231,196,.08), transparent 22%),
    radial-gradient(circle at 82% 72%, rgba(124,211,255,.06), transparent 26%),
    radial-gradient(circle at 58% 58%, rgba(255,255,255,.02), transparent 32%);
  filter: blur(4px);
  animation: neuron-shell-ripple 16s ease-in-out infinite;
}
.neuron-noise-layer {
  position: absolute; inset: 0; pointer-events: none; z-index: 8;
  background-image: repeating-radial-gradient(circle at 20% 80%, rgba(255,255,255,.05), rgba(255,255,255,.05) 1.2px, transparent 1.2px, transparent 2.4px);
  mix-blend-mode: multiply;
  opacity: .09;
  animation: neuron-noise-flicker 3.2s linear infinite;
}
.neuron-title, .canvas-actions, .neuron-legend, .neuron-stats, .merge-dock { pointer-events: auto; }
.neuron-title { max-width: 390px; }
.neuron-title .eyebrow { display: block; margin-bottom: 5px; }
.neuron-title h2 { font-size: 18px; font-weight: 560; }
.neuron-title p { margin-top: 4px; color: var(--muted); font-size: 11px; }
.canvas-actions { display: flex; gap: 8px; }
.neuron-canvas { position: absolute; inset: 0; z-index: 5; }
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
.legend-node.soma { width: 10px; height: 10px; background: rgba(110,231,196,.18); }
.legend-node.hub { width: 10px; height: 7px; border-radius: 3px; border-style: dashed; border-color: #7dd3fc; box-shadow: none; }
.legend-node.tentative { border-style: dashed; border-color: var(--orange); box-shadow: none; }
.legend-node.anchor { width: 4px; height: 4px; border: 0; background: rgba(110,231,196,.56); }
.legend-edge { width: 16px; height: 0; border-top: 1.5px solid rgba(110,231,196,.55); }
.legend-edge.related { border-top-style: dashed; border-top-color: rgba(110,231,196,.4); }
.legend-edge.shared { border-top-style: dashed; border-top-color: rgba(99,179,237,.7); }
.detail-section { margin: 10px 0 8px; }
.detail-section h4 { margin: 0 0 6px; color: var(--accent); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.detail-path { color: #c5ddd4; font-size: 11px; line-height: 1.55; overflow-wrap: anywhere; }
.claim-list { margin-top: 10px; }
.claim-list h4 { margin: 0 0 6px; color: var(--accent); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.raw-file-row { cursor: pointer; }
.raw-file-row:hover { border-color: rgba(110,231,196,.45); }
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
  background: radial-gradient(circle at 20% 16%, rgba(110,231,196,.08), transparent 45%), rgba(7, 22, 17, .94);
  backdrop-filter: blur(22px); box-shadow: 0 24px 74px rgba(0,0,0,.52), 0 0 38px rgba(110,231,196,.20);
  opacity: 0; visibility: hidden; transform: translate(-50%, calc(-100% - 42px)) scale(.96);
  transform-origin: bottom center; transition: opacity .22s ease, transform .22s ease, visibility .22s;
  animation: neuron-popover-glow 2.6s ease-in-out infinite;
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
@keyframes neuron-shell-breath {
  0%, 100% { box-shadow: var(--shadow), inset 0 0 80px rgba(0,0,0,.30); }
  50% { box-shadow: 0 24px 86px rgba(0,0,0,.34), inset 0 0 98px rgba(110,231,196,.14); }
}
@keyframes neuron-shell-ripple {
  0%, 100% { transform: scale(0.98); opacity: .26; }
  50% { transform: scale(1.03); opacity: .48; }
}
@keyframes neuron-orbit-spin {
  to { transform: rotate(360deg); }
}
@keyframes neuron-grid-drift {
  0% { background-position: 0 0; }
  50% { background-position: 12px 14px; }
  100% { background-position: 24px 0; }
}
@keyframes neuron-noise-shimmer {
  0%, 100% { opacity: .16; transform: translateY(0); }
  50% { opacity: .32; transform: translateY(4px); }
}
@keyframes neuron-noise-flicker {
  0%, 100% { opacity: .06; }
  20% { opacity: .09; }
  40% { opacity: .03; }
  60% { opacity: .08; }
}
@keyframes neuron-scanline {
  0% { transform: skewX(-28deg) translateX(-140%); opacity: 0; }
  12% { opacity: .28; }
  30%, 100% { transform: skewX(-28deg) translateX(140%); opacity: 0; }
}
@keyframes neuron-popover-glow {
  0%, 100% { border-color: rgba(110,231,196,.42); box-shadow: 0 24px 64px rgba(0,0,0,.46), 0 0 30px rgba(110,231,196,.09); }
  50% { border-color: rgba(188,255,235,.65); box-shadow: 0 30px 82px rgba(0,0,0,.52), 0 0 44px rgba(110,231,196,.24); }
}
@keyframes pulse-spin { to { transform: rotate(360deg); } }

.build-progress {
  min-height: 360px; display: flex; flex-direction: column; justify-content: center;
  gap: 14px; max-width: 520px; margin: 40px auto; padding: 28px 24px;
  border: 1px solid var(--line); border-radius: 14px; background: rgba(7,22,17,.55);
}
.build-progress .bp-kicker { color: var(--accent); font-size: 10px; letter-spacing: .14em; text-transform: uppercase; }
.build-progress h2 { font-size: 18px; font-weight: 600; margin: 0; }
.build-progress .bp-msg { color: var(--muted); font-size: 13px; line-height: 1.6; min-height: 40px; }
.build-progress .bp-bar {
  height: 8px; border-radius: 999px; background: rgba(110,231,196,.12); overflow: hidden;
  border: 1px solid rgba(110,231,196,.18);
}
.build-progress .bp-bar > i {
  display: block; height: 100%; width: 0%; background: linear-gradient(90deg, #2a8f6f, #6ee7c4);
  transition: width .35s ease;
}
.build-progress .bp-meta { display: flex; justify-content: space-between; color: var(--faint); font-size: 11px; }
.build-progress .bp-phases { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.build-progress .bp-phase {
  padding: 3px 8px; border-radius: 999px; border: 1px solid var(--line); font-size: 10px; color: var(--faint);
}
.build-progress .bp-phase.active { border-color: rgba(110,231,196,.5); color: var(--accent); }
.build-progress .bp-phase.done { border-color: rgba(110,231,196,.28); color: #9fd9c4; }
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
.rule-cockpit-panel { border-color: rgba(110,231,196,.28); }
.rule-create-panel textarea { width: 100%; min-height: 82px; resize: vertical; padding: 11px 12px; border: 1px solid var(--line); border-radius: 9px; background: rgba(4,13,10,.78); color: var(--fg); }
.rule-context-grid { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 10px; margin-top: 12px; }
.rule-create-result { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(110,231,196,.34); border-radius: 9px; background: rgba(110,231,196,.06); }
.rule-create-result.error { border-color: rgba(255,125,136,.48); background: rgba(255,125,136,.06); }
.rule-decision-row, .rule-exception-row, .rule-receipt { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; padding: 9px 0; border-top: 1px solid var(--line); }
.rule-decision-row:first-of-type { border-top: 0; }
.rule-decision-row > .muted { flex: 1 1 100%; }
.rule-decision-link, .rule-receipts, .rule-exceptions { margin-top: 9px; padding-top: 8px; border-top: 1px solid var(--line); }
.rule-feedback-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.rule-feedback-actions .btn { min-height: 26px; padding: 3px 7px; font-size: 10px; }
.rule-advanced { margin-top: 10px; border: 1px solid rgba(233,187,100,.24); border-radius: 9px; background: rgba(233,187,100,.035); }
.rule-advanced > summary { padding: 8px 10px; color: var(--orange); cursor: pointer; font-size: 11px; list-style-position: inside; }
.rule-advanced-body { padding: 0 10px 10px; }
.rule-advanced-body .muted { font-size: 11px; }
.feedback-error { margin-top: 8px; color: var(--red); font-size: 12px; min-height: 1.4em; }
@media (max-width: 720px) { .rule-context-grid { grid-template-columns: 1fr; } }
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
      <div class="nav-item" role="tab" tabindex="0" data-tab="rules" onclick="switchTab('rules')">规则与习惯</div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="history" onclick="switchTab('history')">对话历史</div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="findings" onclick="switchTab('findings')">风险信号<span class="count" id="findings-count"></span></div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="releases" onclick="switchTab('releases')">变更记录<span class="count" id="releases-count"></span></div>
      <div class="nav-section-label">操作</div>
      <div class="nav-item" role="tab" tabindex="0" data-tab="governance" onclick="switchTab('governance')">治理台</div>
    </nav>
    <div class="sidebar-footer">
      <div class="reader-toggle">
        <div class="reader-toggle-label">阅读语言</div>
        <div class="reader-toggle-buttons" title="英文模式优先显示英文内容；无英文版本时显示来源原文">
          <button type="button" id="reader-auto" onclick="setReaderLanguage('auto')">自动</button>
          <button type="button" id="reader-zh" class="active" onclick="setReaderLanguage('zh')">中文</button>
          <button type="button" id="reader-en" onclick="setReaderLanguage('en')">English</button>
        </div>
      </div>
      <span class="local-badge">Local only · 构建内 LLM 整理 · MCP 可补做</span>
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
let selectedNeuronIds = new Set();
let neuronDragState = null;
let neuronTapSelectionAdditive = false;
let historyFocusSessionId = '';
let historyBackfillContinuation = null;
let readerLanguage = localStorage.getItem('memoryguard.readerLanguage') || 'zh';
if (readerLanguage === 'original') readerLanguage = 'en';
let sourcesScope = 'all';      // 数据源 sub-tab: 'all' | 'user' | 'project'
let discoveryResult = null;    // 缓存 discover_agents 结果
let activeAgentInstanceId = '';  // v3.2：当前选中的 Agent 卡片
let agentCardsData = null;     // v3.2：缓存 list_agents 结果
let dataPageMode = 'single_agent';  // v3.2：single_agent | multi_agent_shared_mcp
let activeShareGroupId = '';
let governanceSubTab = 'recent_events';  // 治理台子视图

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function setReaderLanguage(language) {
  readerLanguage = language;
  localStorage.setItem('memoryguard.readerLanguage', language);
  document.getElementById('reader-auto')?.classList.toggle('active', language === 'auto');
  document.getElementById('reader-zh')?.classList.toggle('active', language === 'zh');
  document.getElementById('reader-en')?.classList.toggle('active', language === 'en');
  renderStatusRail();
  renderContent();
}

function displayTitle(item) {
  if (readerLanguage === 'zh') return item.title_zh || item.zh_title || item.title || item.memory_id || '';
  if (readerLanguage === 'en') return item.title_en || item.en_title || item.original_title || item.title || item.title_zh || item.memory_id || '';
  return item.display_language === 'zh'
    ? (item.title_zh || item.zh_title || item.title || item.original_title || item.memory_id || '')
    : (item.original_title || item.title || item.title_zh || item.memory_id || '');
}

function displayBody(item) {
  if (readerLanguage === 'zh') return item.body_zh || item.zh_summary || item.body || item.body_preview || '';
  if (readerLanguage === 'en') return item.body_en || item.en_body || item.original_body || item.body || item.body_zh || item.body_preview || '';
  return item.display_language === 'zh'
    ? (item.body_zh || item.zh_summary || item.body || item.original_body || item.body_preview || '')
    : (item.original_body || item.body || item.body_zh || item.body_preview || '');
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

// Optional lifecycle endpoints are intentionally feature-detected so an older
// GUI can still browse the existing rules page while the rule cockpit service
// is being rolled out.  Mutations still go through callApi (and therefore the
// normal pywebview/request-queue bridge) when the endpoint is present.
async function callApiOptional(method, fallback, ...args) {
  try { return await callApi(method, ...args); }
  catch (_) { return fallback; }
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

async function loadGovernanceScopePreference() {
  try {
    const pref = await callApi('get_governance_scope');
    if (pref && !pref.empty && pref.scope) {
      if (pref.scope.mode === 'share_group' && pref.scope.share_group_id) {
        activeShareGroupId = pref.scope.share_group_id;
        dataPageMode = 'multi_agent_shared_mcp';
      } else if (pref.scope.agent_instance_id) {
        activeAgentInstanceId = pref.scope.agent_instance_id;
      }
    }
  } catch (_) {}
}

function isShareGroupScope() {
  return dataPageMode === 'multi_agent_shared_mcp' && !!activeShareGroupId;
}

function memoryGroupKind(groupId) {
  return String(groupId || '').startsWith('personal-') ? 'personal' : 'shared';
}

function memoryGroupLabel(groupId) {
  return memoryGroupKind(groupId) === 'personal' ? '个人记忆层' : '共享记忆层';
}

function scopeApiArgs() {
  if (isShareGroupScope()) {
    return [{ mode: 'share_group', share_group_id: activeShareGroupId }, '', activeShareGroupId];
  }
  if (activeAgentInstanceId) {
    return [{ mode: 'agent', agent_instance_id: activeAgentInstanceId }, activeAgentInstanceId, ''];
  }
  return [null, '', ''];
}

async function setActiveShareGroup(groupId) {
  activeShareGroupId = groupId || '';
  if (groupId) {
    dataPageMode = 'multi_agent_shared_mcp';
    await callApi('set_governance_scope', { mode: 'share_group', share_group_id: groupId });
  }
}

async function init() {
  const ready = await waitForPywebview(5000);
  if (!ready) { showToast('GUI 桥接未就绪，请稍后重试', 'error'); return; }
  try {
    // scope 偏好失败不阻塞首屏
    await loadGovernanceScopePreference();
  } catch (_) {}
  try {
    state.report = await callApi('get_audit');
    renderAll();
  } catch (e) {
    showToast('扫描失败：' + e, 'error');
    // 即使审计失败也渲染空壳，避免永久转圈、无法切 tab
    if (!state.report) {
      state.report = { findings: [], summary: {}, workspace: '', health_score: 0 };
      renderAll();
    }
  }
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
    selectedNeuronIds = new Set();
    neuronDragState = null;
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

function setContent(html) {
  document.getElementById('content').innerHTML = html;
  bindRuleHistoryActionDelegation();
}

function bindRuleHistoryActionDelegation() {
  if (document.documentElement.dataset.ruleHistoryDelegated) return;
  document.documentElement.dataset.ruleHistoryDelegated = '1';
  document.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-mg-action]');
    if (!target) return;
    const action = target.dataset.mgAction;
    const memoryId = target.dataset.memoryId || '';
    const sessionId = target.dataset.sessionId || '';
    const turnId = target.dataset.turnId || '';
    const nodeId = target.dataset.nodeId || '';
    if (!action) return;
    event.preventDefault();
    if (action === 'rule-edit') return ensureRuleAudienceEditor(memoryId);
    if (action === 'neuron-select-node') return selectNeuron(nodeId, true);
    if (action === 'neuron-open-virtual') return routeVirtualNeuron(selectedNeuronNode);
    if (action === 'neuron-rule-edit-body') return openNeuronRuleBodyEditor(memoryId);
    if (action === 'neuron-rule-delete') return governNeuronRule(memoryId, 'delete_memory');
    if (action === 'neuron-rule-restore') return governNeuronRule(memoryId, 'restore_memory');
    if (action === 'neuron-rule-body-close') return removeNeuronRuleBodyModal();
    if (action === 'neuron-rule-body-save') return saveNeuronRuleBody(memoryId);
    if (action === 'neuron-history-read') return openNeuronHistorySession(sessionId);
    if (action === 'rule-modal-close') return removeRuleAudienceModal();
    if (action === 'rule-save') return saveRuleAudience(memoryId);
    if (action === 'history-read-session') return readHistorySession(sessionId);
    if (action === 'history-extract') return previewHistoryExtract(sessionId);
    if (action === 'history-export') return exportHistorySession(sessionId);
    if (action === 'history-delete') return deleteHistorySession(sessionId);
    if (action === 'history-timeline') return showHistoryTimeline(sessionId, turnId);
if (action === 'history-read-turn') return readHistoryTurn(turnId);
if (action === 'history-back') return renderHistory();
if (action === 'history-search') return searchHistory();
if (action === 'history-backfill') return runHistoryBackfill();
  });
}

function renderAll() {
  if (!state.report) return;
  const r = state.report;
  document.getElementById('ws-path').textContent = r.workspace;
  const badge = document.getElementById('health-badge');
  document.getElementById('reader-auto')?.classList.toggle('active', readerLanguage === 'auto');
  document.getElementById('reader-zh')?.classList.toggle('active', readerLanguage === 'zh');
  document.getElementById('reader-en')?.classList.toggle('active', readerLanguage === 'en');
  badge.textContent = '健康度 ' + Math.round(r.health_score) + '/100';
  badge.style.color = r.health_score >= 70 ? 'var(--accent)' : r.health_score >= 40 ? 'var(--orange)' : 'var(--red)';
  document.getElementById('findings-count').textContent = r.findings.length || '';
  document.getElementById('sources-count').textContent = '';
  document.getElementById('releases-count').textContent = state.releases ? state.releases.length : '';
  renderContent();
  loadGovernanceSnapshot();
}

async function loadGovernanceSnapshot() {
  if (!activeShareGroupId) {
    state.governanceSnapshot = null;
    renderStatusRail();
    return;
  }
  try {
    state.governanceSnapshot = await callApi('get_governance_snapshot', activeShareGroupId);
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
  if (!activeShareGroupId) {
    container.innerHTML = `<div class="status-item zero"><span class="status-label">记忆治理组</span><span class="status-num">—</span></div>
      <div class="rail-link" onclick="switchTab('governance')">选择治理组 →</div>`;
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
  const nodes = (neuronGraph && neuronGraph.nodes) ? neuronGraph.nodes : [];
  const childCount = nodes.filter(n => n.parent_id === node.id).length;
  const isAnchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
  const isHub = node.node_kind === 'source_hub';
  const kindText = node.node_kind === 'topic' ? topicNodeLabel(node) : memoryKindLabel(node.kind || node.label || '');
  const title = isAnchor
    ? (displayTitle(node) || node.label || '未命名记忆')
    : (isHub ? (node.title || node.label || '同源突触')
      : (node.node_kind === 'topic' ? kindText : (node.label || '记忆胞体')));

  const virtualChildren = (heading = '分支（点击聚焦）') => {
    const children = nodes.filter(item => item.parent_id === node.id);
    if (!children.length) return '';
    return `<div class="claim-list"><h4>${escapeHtml(heading)}</h4>${children.map(item =>
      `<button class="raw-file-row" type="button" data-mg-action="neuron-select-node" data-node-id="${escapeHtml(item.id || '')}">
        <span>${escapeHtml(item.label || item.title || '未命名节点')}</span><span class="chip chip-medium">${Number(item.count || 0) || ''}</span>
      </button>`).join('')}</div>`;
  };

  // Virtual nodes are graph-native indexes.  Selecting them must never change
  // the current tab: the rail is the governance surface, and cross-page reads
  // are deliberate secondary actions only.
  if (node.node_kind === 'virtual_rule_ref') {
    const policy = node.injection_policy === 'always' ? '强制注入' : '按需召回';
    const audience = node.audience || ruleAudience({assignments: node.assignments || []});
    const restorable = ['deleted', 'shadowed', 'superseded'].includes(node.status || '');
    return `<div class="popover-kicker">受治理规则 · 图内管理</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="detail-section"><h4>规则正文</h4><div class="neuron-detail-body">${escapeHtml(node.body || '暂无正文内容')}</div></div>
      <div class="row"><span class="key">类型</span><span>${escapeHtml(memoryKindLabel(node.kind || ''))}</span></div>
      <div class="row"><span class="key">状态</span><span>${escapeHtml(node.status || 'active')}</span></div>
      <div class="row"><span class="key">注入</span><span>${escapeHtml(policy)}${node.injection_policy === 'always' ? ` · 优先级 ${Number(node.priority || 0)}` : ''}</span></div>
      <div class="row"><span class="key">适用范围</span><span>${escapeHtml(audience || '按需规则，无固定范围')}</span></div>
      ${node.locked ? '<div class="muted">此规则已锁定；正文修改将由治理层校验。</div>' : ''}
      <div class="finding-actions" style="margin:12px 0 6px;display:flex;flex-wrap:wrap;gap:6px">
        <button class="btn" type="button" data-mg-action="neuron-rule-edit-body" data-memory-id="${escapeHtml(node.memory_id || '')}">编辑正文</button>
        <button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(node.memory_id || '')}">管理适用范围</button>
        ${restorable
          ? `<button class="btn btn-primary" type="button" data-mg-action="neuron-rule-restore" data-memory-id="${escapeHtml(node.memory_id || '')}">恢复</button>`
          : `<button class="btn btn-danger" type="button" data-mg-action="neuron-rule-delete" data-memory-id="${escapeHtml(node.memory_id || '')}">软删除</button>`}
      </div>`;
  }

  if (node.node_kind === 'history_session') {
    return `<div class="popover-kicker">会话历史索引 · 按需读取</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(node.title || node.label || '会话')}</h3>
      <div class="neuron-detail-body">${escapeHtml(node.summary || '尚无摘要；原文仍隔离在本地历史库。')}</div>
      <div class="row"><span class="key">来源</span><span>${escapeHtml(node.provider || 'local')}</span></div>
      ${node.project_ref ? `<div class="row"><span class="key">项目</span><span>${escapeHtml(node.project_ref)}</span></div>` : ''}
      <div class="row"><span class="key">时间</span><span>${escapeHtml(node.created_at || node.imported_at || '未知')}</span></div>
      <div class="row"><span class="key">记录</span><span>${Number(node.turn_count || 0)} 条对话 · ${Number(node.evidence_count || 0)} 条已萃取证据</span></div>
      <div class="muted">点击此节点只在神经图聚焦；原文不会进入长期记忆或 bootstrap。</div>
      <div class="finding-actions" style="margin-top:12px"><button class="btn" type="button" data-mg-action="neuron-history-read" data-session-id="${escapeHtml(node.session_id || '')}">在历史页读取原文</button></div>`;
  }

  if (node.node_kind === 'virtual_bucket') {
    return `<div class="popover-kicker">规则分支 · 图内管理</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="status-item zero"><span class="status-label">规则条目</span><span class="status-num">${node.count || childCount}</span></div>
      <div class="neuron-detail-body">这是图内规则索引：选择下方规则节点即可在左侧直接治理；原生文件不改动。</div>
      ${node.has_more ? '<div class="muted">当前仅显示前 50 条；更多规则请继续扫描补齐索引。</div>' : ''}
      ${virtualChildren()}
      <div class="finding-actions"><button class="btn" type="button" data-mg-action="neuron-open-virtual">聚焦该分支（图内）</button></div>`;
  }

  if (node.virtual_category) {
    const history = node.virtual_category === 'conversation_history';
  const detail = history
        ? (node.requires_agent_selection
        ? '共享组不聚合成员的原始对话。请先显式选择一个 Agent，再查看其独立历史索引。'
        : `仅显示会话元数据索引；已显示 ${childCount} 条${node.has_more ? '，还有更多' : ''}。原文不会进入神经图或长期记忆。`)
      : '规则与习惯只引用既有受治理记忆，不创建第二份持久记录。选择分支或规则节点可直接在图内治理。';
    const label = history && node.requires_agent_selection ? '待选 Agent 后聚焦' : (history ? '查看会话索引（图内）' : '聚焦该分支（图内）');
    return `<div class="popover-kicker">虚拟索引 · 图内查看</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="status-item zero"><span class="status-label">索引条目</span><span class="status-num">${node.count || childCount}</span></div>
      <div class="neuron-detail-body">${escapeHtml(detail)}</div>
      ${node.load_error ? `<div class="detail-section"><h4>暂时不可用</h4><div class="neuron-detail-body">${escapeHtml(node.load_error)}</div></div>` : ''}
      ${virtualChildren(history ? '会话索引（点击聚焦）' : '规则分支（点击聚焦）')}
      <div class="finding-actions"><button class="btn" type="button" data-mg-action="neuron-open-virtual">${escapeHtml(label)}</button></div>`;
  }

  const linkRows = (items, chip) => (items || []).map(item => {
    const mid = item.memory_id || '';
    const rel = item.relation_label || chip || '相关';
    return `<div class="raw-file-row" onclick="selectNeuronByMemory('${escapeHtml(mid)}')" title="跳转到关联节点">
      <div><code>${escapeHtml(displayTitle(item) || item.title || mid || '')}</code>
        <div class="surface-meta">${escapeHtml(displayBody(item) || item.body_preview || '')}</div>
        ${item.relation_reason ? `<div class="surface-meta">${escapeHtml(item.relation_reason)}</div>` : ''}
      </div>
      <span class="chip chip-medium">${escapeHtml(rel)}</span>
    </div>`;
  }).join('');

  if (!isAnchor && !isHub) {
    const role = node.node_kind === 'topic' ? '主题树突' : '记忆胞体';
    return `<div class="status-item zero"><span class="status-label">${escapeHtml(role)}</span><span class="status-num">${childCount}</span></div>
      ${node.derivation ? `<div class="detail-section"><h4>衍生路径</h4><div class="detail-path">${escapeHtml(node.derivation)}</div></div>` : ''}
      ${node.edge_hint ? `<div class="row"><span class="key">连接说明</span><span>${escapeHtml(node.edge_hint)}</span></div>` : ''}
      <div class="neuron-detail-body">${escapeHtml(node.body || (node.node_kind === 'topic'
        ? `该主题下有 ${childCount} 个下级节点。点击光点查看具体内容。`
        : `当前投影共有 ${nodes.length} 个节点。`))}</div>
      <div class="rail-link" onclick="switchTab('governance')">进入治理台 →</div>`;
  }

  let members = node.members || [];
  if ((!members || !members.length) && isHub) {
    members = nodes.filter(n => n.parent_id === node.id && n.node_kind === 'claim_anchor').map(n => ({
      memory_id: n.memory_id || '',
      title: displayTitle(n) || n.label || '',
      kind: n.kind || '',
      body_preview: (displayBody(n) || n.body || '').slice(0, 180),
    }));
  }
  const related = node.related || [];
  const actionTarget = node.memory_id || node.id || '';
  const roleKick = isHub ? '同源突触' : '记忆末梢';
  return `<div class="popover-kicker">${escapeHtml(kindText)} · ${roleKick}</div>
    <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
    ${node.derivation ? `<div class="detail-section"><h4>衍生路径</h4><div class="detail-path">${escapeHtml(node.derivation)}</div></div>` : ''}
    ${node.edge_hint ? `<div class="row"><span class="key">连接说明</span><span>${escapeHtml(node.edge_hint)}</span></div>` : ''}
    <div class="detail-section"><h4>内容</h4><div class="neuron-detail-body">${escapeHtml(displayBody(node) || node.body || '暂无正文内容')}</div></div>
    <div class="row"><span class="key">作用域</span><span>${escapeHtml(node.scope || 'project')}</span></div>
    <div class="row"><span class="key">置信度</span><span>${escapeHtml(String(node.confidence ?? '—'))}</span></div>
    <div class="row"><span class="key">完整性</span><span>${escapeHtml(node.completeness || '—')}</span></div>
    <div class="row"><span class="key">来源</span><span>${node.provenance_count || 0} 个来源证据</span></div>
    ${node.source_key ? `<div class="row"><span class="key">同源键</span><code style="overflow-wrap:anywhere">${escapeHtml(node.source_key)}</code></div>` : ''}
    ${node.cluster_count ? `<div class="row"><span class="key">合并片段</span><span>${node.cluster_count} 条</span></div>` : ''}
    <div class="row"><span class="key">记录 ID</span><code style="overflow-wrap:anywhere">${escapeHtml(actionTarget)}</code></div>
    ${isAnchor ? `<div class="finding-actions" style="margin:12px 0 10px;display:flex;flex-wrap:wrap;gap:6px">
      <span class="chip chip-confirmed">自动纳入重构</span>
      <button class="btn btn-danger" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','exclude')">删除/排除</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','quarantine')">隔离</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','merge')">合并</button>
    </div>` : ''}
    ${members && members.length ? `<div class="claim-list"><h4>${isHub ? '突触末梢（点击跳转）' : '合并片段'}</h4>${linkRows(members, '末梢')}</div>` : ''}
    ${related && related.length ? `<div class="claim-list"><h4>相关连线（点击跳转）</h4>${linkRows(related)}</div>` : ''}`;
}

function renderContent() {
  switch (state.activeTab) {
    case 'overview': renderOverview(); break;
    case 'sources': renderSources(); break;
    case 'neurons': renderNeurons(); break;
    case 'rules': renderRulesHabits(); break;
    case 'history': renderHistory(); break;
    case 'findings': renderFindings(); break;
    case 'releases': renderReleases(); break;
    case 'governance': renderGovernance(); break;
  }
}

async function ensureGovernanceScope() {
  if (isShareGroupScope()) return true;
  if (activeAgentInstanceId) {
    try {
      await callApi('set_governance_scope', {
        mode: 'agent',
        agent_instance_id: activeAgentInstanceId,
      });
    } catch (_) {}
    return true;
  }
  try {
    const agents = agentCardsData || await callApi('list_agents');
    agentCardsData = agents;
    const list = (agents && agents.agents) || [];
    if (list.length) {
      activeAgentInstanceId = list[0].instance_id;
      await callApi('set_governance_scope', {
        mode: 'agent',
        agent_instance_id: activeAgentInstanceId,
      });
      return true;
    }
  } catch (_) {}
  return false;
}

async function renderNeurons() {
  setContent('<div class="loading">正在读取神经图投影</div>');
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) {
      setContent(`<div class="card empty-state"><div><div class="empty-orb"></div>
        <p>请先在数据源页选择 Agent，或进入多 Agent 共享组并设为治理范围。</p>
        <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源</button></div>
      </div></div>`);
      return;
    }
    const [scope, agentId, groupId] = scopeApiArgs();
    neuronGraph = await callApi('get_neuron_graph', projectionMode, scope, agentId, groupId);
    renderNeuronGraph();
  }
  catch (e) {
    showToast('神经图构建失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>神经图构建失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function kindColor(kind) {
  const colors = {
    fact: '#6ee7c4', preference: '#f6ad55', project: '#63b3ed', episode: '#fc8181', procedure: '#b794f4', correction: '#f687b3', workflow: '#b794f4', constraint: '#fbd38d',
    user: '#c084fc', agent: '#38bdf8', session: '#94a3b8', share_group: '#2dd4bf', unknown: '#64748b',
    rules_habits: '#f6ad55', conversation_history: '#7dd3fc'
  };
  return colors[kind] || '#6ee7c4';
}

function memoryKindLabel(kind) {
  const labels = {
    fact: '事实', preference: '偏好', project: '项目', episode: '事件', procedure: '流程', correction: '纠错',
    constraint: '约束', workflow: '流程', decision: '决策', context: '上下文', instruction: '指令', unknown: '未知',
    user: '用户来源', agent: 'Agent 来源', session: '会话来源', share_group: '共享项目'
  };
  return labels[kind] || kind || '未知';
}

function topicNodeLabel(node) {
  if (!node) return '主题';
  if (node.label && (String(node.label).includes('来源') || /[\u4e00-\u9fff]/.test(String(node.label)))) {
    return String(node.label);
  }
  return memoryKindLabel(node.kind || node.label || '');
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
  const topics = (children.main || []).filter(n => n.node_kind === 'topic' || n.node_kind === 'virtual_category');
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
  // 轻量级排斥后处理，减少高密度叠点。仅用于可视化舒适度，不影响语义。
  const radii = {};
  const nodeKinds = new Set(nodes.map(n => n.node_kind));
  const estimateRadius = (item) => {
    const kind = item.node_kind || '';
    if (kind === 'root') return 30;
    if (kind === 'source_hub') return 28;
    if (kind === 'claim_anchor' || kind === 'duplicate_cluster') return 16;
    if (item.node_kind === 'virtual_bucket') return 35;
    if (item.node_kind === 'virtual_category') return 30;
    if (item.node_kind === 'history_session') return 9;
    return 18 + Math.min(16, (item.provenance_count || 0) * 2.5);
  };
  nodes.forEach(node => {
    radii[node.id] = estimateRadius(node);
  });
  if ((nodeKinds.has('virtual_category') || nodeKinds.has('virtual_bucket') || nodes.length > 45) && Object.keys(positions).length) {
    const ids = nodes.map(item => item.id).filter(id => positions[id]);
    for (let iter = 0; iter < 70; iter++) {
      let moved = false;
      for (let i = 0; i < ids.length; i++) {
        const idA = ids[i];
        const posA = positions[idA];
        if (!posA || idA === 'main') continue;
        for (let j = i + 1; j < ids.length; j++) {
          const idB = ids[j];
          if (idB === 'main') continue;
          const posB = positions[idB];
          if (!posB) continue;
          const dx = posB.x - posA.x;
          const dy = posB.y - posA.y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const req = (radii[idA] || 20) + (radii[idB] || 20) + 5;
          if (dist >= req) continue;
          const gap = req - dist;
          const nx = dx / dist;
          const ny = dy / dist;
          const force = gap / 2 * (1 - iter / 70);
          posA.x -= nx * force;
          posA.y -= ny * force;
          posB.x += nx * force;
          posB.y += ny * force;
          moved = true;
        }
      }
      if (!moved) break;
    }
  }
  return positions;
}

function graphElements(graph) {
  // v3.1 §6.3：统一 v3 图契约
  // node: id / parent_id / label / node_kind / memory_id / kind / provenance_count
  // edge: id / source / target / edge_type (+ strength 粗细)
  const elements = [];
  const positions = neuronNodePositions(graph.nodes || []);
  const EDGE_STRENGTH = { derived_from: 0.58, related: 0.28, shared_source: 0.4, duplicate: 0.34 };
  for (const node of graph.nodes || []) {
    const root = node.node_kind === 'root';
    const hub = node.node_kind === 'source_hub';
    const anchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
    const cluster = node.node_kind === 'duplicate_cluster';
    const virtualCategory = node.node_kind === 'virtual_category';
    const historySession = node.node_kind === 'history_session';
    // v3：用 provenance_count 替代旧 claim_count 决定大小
    const provCount = node.provenance_count || 0;
    const size = root ? 66
      : hub ? Math.max(22, Math.min(40, 18 + (node.cluster_count || provCount || 2) * 3.5))
      : virtualCategory ? Math.max(34, Math.min(50, 31 + (node.count || 0) * .22))
      : historySession ? 12
      : cluster ? Math.max(15, Math.min(30, 12 + (node.cluster_count || 2) * 4))
      : anchor ? 7
      : Math.max(27, Math.min(54, 25 + provCount * 3.2));
    elements.push({ data: {
      id: node.id,
      label: anchor ? '' : String(node.node_kind === 'topic' ? topicNodeLabel(node) : (node.label || '')).slice(0, 18),
      kind: node.node_kind,
      memory_id: node.memory_id || '',
      record_kind: node.kind || '',
      cluster_count: node.cluster_count || 0,
      provenance_count: provCount,
      virtual_category: node.virtual_category || '',
      session_id: node.session_id || '',
      size,
      bg: node.bg || kindColor(node.kind || node.label || ''),
      opacity: 0.85,
    }, position: positions[node.id] || { x: 0, y: 0 }});
  }
  for (const edge of graph.edges || []) {
    elements.push({ data: {
      id: edge.id, source: edge.source, target: edge.target,
      etype: edge.edge_type || 'derived_from',
      strength: EDGE_STRENGTH[edge.edge_type] || 0.4,
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
    shared_memory_projection: '共享记忆',
    evidence_only: '证据/萃取来源'
  }[mode] || mode || '未知';
}

function sourceCategoryLabel(category) {
  return {
    native_memory: '原生记忆', project_memory: '项目记忆', knowledge_source: '知识文档',
    shared_memory: 'MCP 实时记忆',
    conversation_history: '会话历史', runtime_evidence: '运行证据', ignored_runtime_data: '忽略运行数据',
    control_surface: '控制面', skill_surface: '技能面'
  }[category] || category || '未知';
}

function renderProjectionSourceMap(sourceMap) {
  const entries = sourceMap?.entries || [];
  const summary = sourceMap?.summary || {};
  const shared = sourceMap?.projection_kind === 'shared_memory_projection';
  const rows = entries.length ? entries.map(renderProjectionSourceEntry).join('') : '<tr><td colspan="7" class="empty-note">暂无映射条目</td></tr>';
  return `<section class="card projection-source-map">
    <div class="card-head"><div><h2>${shared ? '共享记忆入库来源' : '当前数据源映射'}</h2><p>${shared ? '共享图直接读取 SharedMemoryStore。这里展示 active 记忆最初由 MCP 写入或从哪个已授权来源导入；取消来源不会自动删除已经入库的记忆。' : '这里只读展示数据源页已勾选的 Agent / 项目 / 来源。勾选和取消请回到数据源页处理。'}</p></div>
      <div class="chips">${shared ? `<span class="chip chip-info">入库来源 ${summary.shared_memory || 0}</span><span class="chip chip-info">参与投影 ${summary.enabled || 0}/${summary.total || 0}</span>` : `<span class="chip chip-info">启用 ${summary.enabled || 0}/${summary.total || 0}</span><span class="chip chip-info">原生 ${summary.native_memory || 0}</span><span class="chip chip-info">逻辑 ${summary.logical_reconstruction || 0}</span><span class="chip chip-medium">证据 ${summary.evidence_only || 0}</span>`}</div></div>
    <div class="source-map-table-wrap"><table class="source-map-table"><thead><tr><th>状态</th><th>投影</th><th>来源</th><th>Agent</th><th>项目/范围</th><th>策略</th><th>路径</th></tr></thead><tbody>${rows}</tbody></table></div>
  </section>`;
}

function renderProjectionSourceEntry(entry) {
  const eligible = entry.logical_eligible || entry.native_eligible;
  const sharedOrigin = entry.is_shared_memory_origin === true;
  const mode = projectionModeLabel(entry.projection_mode);
  const path = entry.path || '';
  const project = entry.project_ref || (entry.scope === 'project' ? '当前项目' : entry.scope || '未知');
  return `<tr class="${entry.enabled ? '' : 'muted-row'}">
    <td><span class="chip ${(sharedOrigin && entry.participates) || entry.enabled ? 'chip-confirmed' : 'chip-medium'}">${sharedOrigin ? (entry.participates ? `已入库 · ${entry.record_count || 0} 条` : '历史来源') : (entry.enabled ? '已勾选' : '未勾选')}</span></td>
    <td><span class="chip ${eligible ? 'chip-confirmed' : 'chip-medium'}">${escapeHtml(mode)}</span></td>
    <td><strong>${escapeHtml(entry.display_name || entry.surface_id || entry.root_id)}</strong><div class="surface-meta">${escapeHtml(sourceCategoryLabel(entry.source_category))}</div></td>
    <td>${escapeHtml(entry.agent_instance_id || '未绑定')}<div class="surface-meta">${escapeHtml(entry.surface_id || '—')}</div></td>
    <td>${escapeHtml(project)}</td>
    <td>${escapeHtml(entry.ingestion_policy || '')}${sharedOrigin && entry.first_imported_at ? `<div class="surface-meta">${escapeHtml(entry.first_imported_at)}</div>` : ''}</td>
    <td class="path-cell">${escapeHtml(path)}</td>
  </tr>`;
}

function projectionModeControls() {
  const nativeActive = projectionMode === 'native' ? 'btn-primary' : '';
  const reconstructedActive = projectionMode === 'reconstructed' ? 'btn-primary' : '';
  return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>投影模式</h2><p>原生投影只读查看当前真实记忆；重构治理投影写入 MemoryGuard 管理层，不覆盖原生记忆文件。</p></div></div>
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

function renderNeuronMetaBar(graph) {
  const meta = (graph && graph.meta) || {};
  if (isShareGroupScope()) {
    const gid = (graph && graph.scope && graph.scope.share_group_id)
      || meta.share_group_id || activeShareGroupId || '—';
    const agents = meta.bound_agents || [];
    const memberChips = agents.length
      ? agents.map(a => {
          const label = a.display_name || a.product || '未知 Agent';
          return `<span class="chip chip-info" title="${escapeHtml(a.agent_instance_id || '')}">成员 · ${escapeHtml(label)}</span>`;
        }).join('')
      : '<span class="chip chip-medium">成员 · 无绑定</span>';
    const groupLabel = memoryGroupLabel(gid);
    return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>记忆核心状态</h2>
      <p>${groupLabel}状态（组名 · 成员 · 记忆 · 冲突）</p></div></div>
      <div class="chips">
        <span class="chip chip-confirmed" title="${escapeHtml(gid)}">${groupLabel} · ${escapeHtml(gid)}</span>
        ${memberChips}
        <span class="chip chip-info">成员数 · ${meta.agent_count || agents.length || 0}</span>
        <span class="chip chip-info">记忆 · ${meta.active_records || 0}</span>
        <span class="chip chip-${(meta.conflict_count || 0) ? 'high' : 'confirmed'}">冲突 · ${meta.conflict_count || 0}</span>
        <span class="chip chip-${meta.coverage_status === 'complete' ? 'confirmed' : 'medium'}">覆盖 · ${escapeHtml(meta.coverage_status || 'unknown')}</span>
      </div></section>`;
  }
  const instances = meta.agent_instances || [];
  const instanceChips = instances.length ? instances.map(inst => {
    return `<span class="chip chip-info" title="${escapeHtml(inst.instance_id)}">${escapeHtml(inst.product || 'agent')} · ${escapeHtml(inst.takeover_state || 'not_detected')}</span>
      <span class="chip chip-info">版本 · ${escapeHtml(inst.managed_version ? inst.managed_version.slice(0,8) : '—')}</span>
      <span class="chip chip-info">记录 · ${inst.record_count || 0}</span>`;
  }).join('') : '<span class="chip chip-info">Agent · 未发现</span>';
  return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>记忆核心状态</h2>
    <p>顶部 7 项状态信息（v3.1 §6.1）</p></div></div>
    <div class="chips">
      ${instanceChips}
      <span class="chip chip-info">实例数 · ${meta.instance_count || 0}</span>
      <span class="chip chip-info">Release · ${meta.release_count || 0}</span>
      <span class="chip chip-${meta.coverage_status === 'complete' ? 'confirmed' : 'medium'}">覆盖 · ${escapeHtml(meta.coverage_status || 'unknown')}</span>
      <span class="chip chip-${meta.drifted ? 'high' : 'confirmed'}">漂移 · ${meta.drifted ? '是' : '否'}</span>
    </div></section>`;
}

function renderNeuronGraph() {
  const graph = neuronGraph;
  // 顶部状态：单 Agent 用实例条；共享组用组名 + 成员
  const meta = (graph && graph.meta) || {};
  const sourceMapPanel = renderProjectionSourceMap(graph?.source_map || {});
  const modeControls = projectionModeControls();
  const metaBar = renderNeuronMetaBar(graph);
  // 未构建时显示门控
  if (!graph || !graph.nodes || !graph.nodes.length || (graph.empty && !graph.virtual_overlay_available)) {
    stopNeuronSignalPulses();
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
            <strong>${isShareGroupScope() ? '共享 MCP 投影读取 SharedMemoryStore。' : projectionMode === 'native' ? '原生投影读取当前真实记忆。' : '重构治理会自动萃取、合并和清理记忆。'}</strong><br>
            ${isShareGroupScope() ? '确认正式接管时打版本快照；Agent 通过 MCP 读写共享记忆，不再各自写原生文件。' : projectionMode === 'native' ? '此操作只生成当前原生记忆的可视化图。' : '重构结果保存在 MemoryGuard 管理层；原生记忆文件保持只读。'}
          </div>
          <div class="finding-actions">
            <button class="btn btn-primary" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '构建原生投影' : '构建重构投影（含 LLM 整理）'}</button>
          </div>
        </div>
      </section>`);
    return;
  }
  const stats = graph.stats || {};
  const publishActions = isShareGroupScope()
    ? `<button class="btn btn-primary" type="button" onclick="commitSharedMemoryGovernance()">确认正式接管</button>`
    : '';
  const enrichInfo = graph.enrichment || {};
  const enrichPending = enrichInfo.pending_count || 0;
  const enrichChip = (isShareGroupScope() || projectionMode === 'reconstructed')
    ? `<span class="chip chip-info">待整理残留 · ${enrichPending}</span>`
      + (enrichInfo.auto_applied ? `<span class="chip chip-confirmed">本次整理 · ${enrichInfo.auto_applied}</span>` : '')
    : '';
  const baseEmptyNotice = graph.base_empty
    ? `<section class="card projection-gate" style="margin-bottom:14px"><div class="gate-body"><h3>基础投影尚未构建</h3><p class="gate-reason">规则与习惯、对话历史索引仍可浏览；构建投影后会显示受治理记忆的完整关系。</p><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="buildProjection()">构建基础投影</button></div></div></section>`
    : '';
  const suggestions = [];
  selectedNeuronId = null;
  selectedNeuronNode = null;
  selectedNeuronIds = new Set();
  neuronDragState = null;
  renderStatusRail();
  document.getElementById('neuron-count').textContent = stats.node_count || '';
  setContent(`<div class="view-heading"><span class="eyebrow">Live cognition map</span><h2>记忆核心</h2>
    <p>点击任意光点，在右侧查看可读内容。规则与习惯可直接在图内治理；原始对话仅按需打开历史页。</p></div>
    ${metaBar}
    ${modeControls}
    ${sourceMapPanel}
    ${baseEmptyNotice}
    <section class="neuron-shell">
    <div class="neuron-toolbar">
      <div class="neuron-title"><span class="eyebrow">Cognition map</span><h2>可读神经图</h2>
        <p>点击节点查看记忆内容；接受、排除、隔离、合并等操作统一在治理台完成。</p></div>
      <div class="canvas-actions">
        ${enrichChip}
        <button class="btn" type="button" onclick="fitNeuronGraph()">重置视野</button>
        <button class="btn" type="button" onclick="deleteProjection()">删除当前投影</button>
        <button class="btn" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '重建原生投影' : '重建投影（含 LLM 整理）'}</button>
        ${publishActions}
      </div>
    </div>
    <div class="neuron-stage" id="neuron-stage">
      <div class="neuron-canvas-haze" aria-hidden="true"></div>
      <div class="neuron-noise-layer" aria-hidden="true"></div>
      <div class="neuron-canvas" id="cy" aria-label="本地记忆神经图画布"></div>
      <div class="neuron-legend">
        <div class="legend-item"><span class="legend-node soma"></span>记忆胞体</div>
        <div class="legend-item"><span class="legend-node"></span>来源/类型主题</div>
        <div class="legend-item"><span class="legend-node hub"></span>同源突触</div>
        <div class="legend-item"><span class="legend-node anchor"></span>记忆末梢</div>
        <div class="legend-item"><span class="legend-edge"></span>衍生轴突</div>
        <div class="legend-item"><span class="legend-edge related"></span>相似关联</div>
        <div class="legend-item"><span class="legend-edge shared"></span>同源跨类型</div>
      </div>
      <div class="neuron-stats">
        <div class="neuron-stat"><strong>${stats.claim_anchor_count || 0}</strong><span>记忆末梢</span></div>
        <div class="neuron-stat"><strong>${stats.source_hub_count || 0}</strong><span>同源突触</span></div>
        <div class="neuron-stat"><strong>${stats.related_edge_count || 0}</strong><span>相似关联</span></div>
        <div class="neuron-stat"><strong>${stats.shared_source_edge_count || 0}</strong><span>同源连线</span></div>
        <div class="neuron-stat"><strong>${stats.edge_count || 0}</strong><span>关系边</span></div>
      </div>
      ${renderMergeDock(suggestions)}
      <aside class="neuron-popover" id="neuron-popover" role="dialog" aria-live="polite" aria-label="光点治理"></aside>
    </div>
  </section>`);

  if (typeof cytoscape === 'undefined') {
    document.getElementById('cy').innerHTML = '<div class="empty-state" style="color:var(--red)">本地 Cytoscape 资源加载失败</div>';
    return;
  }

  stopNeuronSignalPulses();
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
        'transition-property': 'border-width, border-color, background-color, opacity, shadow-blur, background-opacity',
        'transition-duration': '180ms',
      }},
      { selector: 'node[kind = "root"]', style: {
        'background-color': '#6ee7c4', 'background-opacity': .24, 'border-width': 2.5,
        'border-color': '#bcffeb', 'font-size': 11,
        'shadow-blur': 18, 'shadow-color': '#9ff0d6', 'shadow-opacity': .5,
      }},
      { selector: 'node[kind = "claim_anchor"]', style: {
        'background-opacity': .68, 'border-width': 0, 'label': '',
      }},
      { selector: 'node[kind = "source_hub"]', style: {
        'background-opacity': .42, 'border-width': 2.0,
        'border-color': '#7dd3fc', 'border-style': 'dashed',
        'font-size': 9, 'shape': 'round-rectangle',
      }},
      { selector: 'node[kind = "duplicate_cluster"]', style: {
        'background-opacity': .78, 'border-width': 1.8,
        'border-color': '#d8ffe9', 'label': '',
      }},
      { selector: 'node[record_kind = "rules_habits"]', style: {
        'background-color': '#f6ad55', 'background-opacity': .3,
        'border-width': 2.4, 'border-color': '#ffe3a1', 'shape': 'round-rectangle',
        'font-size': 10.5,
      }},
      { selector: 'node[record_kind = "conversation_history"]', style: {
        'background-color': '#7dd3fc', 'background-opacity': .28,
        'border-width': 2.4, 'border-color': '#c5efff', 'shape': 'round-rectangle',
        'font-size': 10.5,
      }},
      { selector: 'node[kind = "history_session"]', style: {
        'background-color': '#7dd3fc', 'background-opacity': .7,
        'border-width': 1.2, 'border-color': '#bae6fd', 'font-size': 8,
      }},
      { selector: 'edge[etype = "virtual_index"]', style: {
        'line-style': 'dotted', 'line-color': '#8de8cf', 'line-opacity': .36, 'width': 1.25,
      }},
      { selector: 'node[status = "tentative"]', style: {
        'background-color': '#2b2a20', 'border-color': '#e9bb64', 'border-style': 'dashed',
      }},
      { selector: 'edge', style: {
        'width': 'mapData(strength, 0, 1, .55, 3.2)', 'line-color': '#6ee7c4', 'line-opacity': .22,
        'curve-style': 'unbundled-bezier', 'control-point-distances': 20, 'control-point-weights': .5,
        'target-arrow-shape': 'none', 'transition-property': 'line-opacity, width, line-color', 'transition-duration': '140ms',
      }},
      { selector: 'edge[etype = "related"]', style: { 'line-style': 'dashed', 'line-opacity': .2, 'line-color': '#9ae6b4' }},
      { selector: 'edge[etype = "shared_source"]', style: { 'line-style': 'dashed', 'line-color': '#63b3ed', 'line-opacity': .34 }},
      { selector: 'edge[etype = "duplicate"]', style: { 'line-style': 'dashed', 'line-color': '#f6ad55', 'line-opacity': .28 }},
      { selector: 'edge.signal', style: {
        'line-opacity': .95, 'width': 3.6, 'line-color': '#e6fff6',
        'shadow-blur': 18, 'shadow-color': '#6ee7c4', 'shadow-opacity': .7,
      }},
      { selector: 'edge.signal-trail', style: {
        'line-opacity': .55, 'width': 2.4, 'line-color': '#98f5d0',
      }},
      { selector: 'node.signal', style: {
        'border-width': 4, 'border-color': '#ffffff',
        'shadow-blur': 38, 'shadow-color': '#bcffeb', 'shadow-opacity': .8,
      }},
      { selector: 'node.hover', style: {
        'border-width': 3.4, 'border-color': '#fff3a3', 'shadow-blur': 32, 'shadow-color': '#fff3a3', 'shadow-opacity': .58,
      }},
      { selector: 'node.focusPulse', style: {
        'border-width': 4.5, 'border-color': '#ffffff', 'shadow-blur': 48, 'shadow-color': '#ffffff', 'shadow-opacity': .95,
      }},
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

  cyInstance.on('tap', 'node', event => {
    neuronTapSelectionAdditive = !!(event.originalEvent && (event.originalEvent.shiftKey || event.originalEvent.ctrlKey || event.originalEvent.metaKey));
    selectNeuron(event.target.id());
  });
  cyInstance.on('mouseover', 'node', event => {
    const node = event.target;
    node.addClass('hover');
    node.addClass('neighborhood');
    node.connectedEdges().addClass('neighborhood');
    node.neighborhood('node').addClass('neighborhood');
  });
  cyInstance.on('mouseout', 'node', () => cyInstance.elements().removeClass('neighborhood hover'));
  cyInstance.on('tap', event => { if (event.target === cyInstance) hideNeuronPopover(); });
  cyInstance.on('pan zoom resize', () => { if (selectedNeuronId) positionNeuronPopover(selectedNeuronId); });
  cyInstance.on('drag position', 'node', event => {
    const selId = event.target.id();
    if (selectedNeuronId === selId) positionNeuronPopover(selectedNeuronId);
  });
  cyInstance.on('grab', 'node', event => {
    if (!cyInstance) return;
    const dragRoot = event.target;
    if (!dragRoot || !dragRoot.selected()) return;
    const selectedNodes = cyInstance.$('node:selected');
    if (!selectedNodes.length) return;
    const basePositions = {};
    selectedNodes.forEach(n => {
      if (n.id() === dragRoot.id()) return;
      basePositions[n.id()] = { ...n.position() };
    });
    neuronDragState = {
      dragRootId: dragRoot.id(),
      basePositions,
      rootStart: { ...dragRoot.position() },
    };
  });
  cyInstance.on('drag', 'node', event => {
    if (!neuronDragState || !cyInstance) return;
    if (event.target.id() !== neuronDragState.dragRootId) return;
    const root = cyInstance.getElementById(neuronDragState.dragRootId);
    if (!root || !root.length) return;
    const current = root.position();
    const deltaX = current.x - neuronDragState.rootStart.x;
    const deltaY = current.y - neuronDragState.rootStart.y;
    Object.entries(neuronDragState.basePositions).forEach(([nodeId, pos]) => {
      const n = cyInstance.getElementById(nodeId);
      if (!n || !n.length) return;
      n.position({ x: pos.x + deltaX, y: pos.y + deltaY });
      if (selectedNeuronId === nodeId) positionNeuronPopover(nodeId);
    });
    if (selectedNeuronId === neuronDragState.dragRootId) positionNeuronPopover(neuronDragState.dragRootId);
  });
  cyInstance.on('free', 'node', () => {
    neuronDragState = null;
  });
  startNeuronSignalPulses(cyInstance);
}

function stopNeuronSignalPulses() {
  if (window.__neuronSignalTimer) {
    clearInterval(window.__neuronSignalTimer);
    window.__neuronSignalTimer = null;
  }
  if (window.__neuronSomaPulse) {
    clearInterval(window.__neuronSomaPulse);
    window.__neuronSomaPulse = null;
  }
  if (window.__neuronSparkPulse) {
    clearInterval(window.__neuronSparkPulse);
    window.__neuronSparkPulse = null;
  }
  const pending = window.__neuronSignalChains || [];
  pending.forEach(id => clearTimeout(id));
  window.__neuronSignalChains = [];
  window.__neuronSignalRefs = {};
  if (cyInstance) {
    try {
      cyInstance.edges().removeClass('signal signal-trail');
      cyInstance.nodes().removeClass('signal');
    } catch (e) { /* graph torn down */ }
  }
}

function _signalRefKey(eleId, cls) {
  return eleId + '::' + cls;
}

function _acquireSignal(cy, eleId, cls) {
  if (!window.__neuronSignalRefs) window.__neuronSignalRefs = {};
  const key = _signalRefKey(eleId, cls);
  window.__neuronSignalRefs[key] = (window.__neuronSignalRefs[key] || 0) + 1;
  const ele = cy.getElementById(eleId);
  if (ele && ele.length) ele.addClass(cls);
}

function _releaseSignal(cy, eleId, cls) {
  if (!window.__neuronSignalRefs) window.__neuronSignalRefs = {};
  const key = _signalRefKey(eleId, cls);
  const next = Math.max(0, (window.__neuronSignalRefs[key] || 1) - 1);
  window.__neuronSignalRefs[key] = next;
  if (next > 0) return;
  delete window.__neuronSignalRefs[key];
  const ele = cy.getElementById(eleId);
  if (ele && ele.length) ele.removeClass(cls);
}

function pickNeuronSignalPath(cy) {
  const leaves = cy.nodes().filter(n => n.data('kind') === 'claim_anchor');
  if (!leaves.length) return null;
  const leaf = leaves[Math.floor(Math.random() * leaves.length)];
  const pathNodes = [];
  const pathEdges = [];
  let cur = leaf;
  const seen = new Set();
  while (cur && cur.length && !seen.has(cur.id())) {
    seen.add(cur.id());
    pathNodes.unshift(cur);
    const incomers = cur.incomers('edge').filter(e => {
      const t = e.data('etype');
      return !t || t === 'derived_from';
    });
    if (!incomers.length) break;
    const edge = incomers[Math.floor(Math.random() * Math.min(incomers.length, 2))];
    pathEdges.unshift(edge);
    cur = edge.source();
    if (cur.data('kind') === 'root') {
      pathNodes.unshift(cur);
      break;
    }
  }
  if (pathEdges.length < 1) return null;
  return { nodes: pathNodes, edges: pathEdges };
}

function runNeuronSignalPulse(cy, path) {
  if (!cy || !path || !path.edges.length) return;
  const stepMs = 120;
  const holdMs = 220;
  path.edges.forEach((edge, index) => {
    const tid = setTimeout(() => {
      if (!cyInstance || cyInstance !== cy) return;
      _acquireSignal(cy, edge.id(), 'signal');
      const src = edge.source();
      const tgt = edge.target();
      if (src && src.length) _acquireSignal(cy, src.id(), 'signal');
      if (tgt && tgt.length) _acquireSignal(cy, tgt.id(), 'signal');
      const releaseId = setTimeout(() => {
        if (!cyInstance || cyInstance !== cy) return;
        _releaseSignal(cy, edge.id(), 'signal');
        _acquireSignal(cy, edge.id(), 'signal-trail');
        if (src && src.length) _releaseSignal(cy, src.id(), 'signal');
        if (tgt && tgt.length) _releaseSignal(cy, tgt.id(), 'signal');
        const trailId = setTimeout(() => {
          if (!cyInstance || cyInstance !== cy) return;
          _releaseSignal(cy, edge.id(), 'signal-trail');
        }, holdMs + 180);
        (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(trailId);
      }, holdMs);
      (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(releaseId);
    }, index * stepMs);
    (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(tid);
  });
}

function startNeuronSignalPulses(cy) {
  stopNeuronSignalPulses();
  if (!cy) return;
  const rootPulse = () => {
    if (!cyInstance || cyInstance !== cy) return;
    try {
      const root = cy.$('node[kind = "root"]');
      if (root && root.length) {
        root.flashClass('pulse', 900);
        root.flashClass('focusPulse', 480);
      }
    } catch (e) { /* ignore */ }
  };
  const spark = () => {
    if (!cyInstance || cyInstance !== cy) return;
    const candidates = cy.nodes('node[kind = "claim_anchor"], node[kind = "history_session"], node[kind = "virtual_bucket"]');
    if (!candidates || !candidates.length) return;
    const count = Math.min(2, candidates.length);
    for (let i = 0; i < count; i++) {
      const n = candidates[Math.floor(Math.random() * candidates.length)];
      if (n && n.flashClass) n.flashClass('pulse', 650);
    }
    try {
      const root = cy.$('node[kind = "root"]');
      if (root && root.length) root.flashClass('focusPulse', 560);
    } catch (_) { /* ignore */ }
  };
  rootPulse();
  spark();
  window.__neuronSomaPulse = setInterval(rootPulse, 3600);
  window.__neuronSparkPulse = setInterval(spark, 5200);
  const fireWave = () => {
    if (!cyInstance || cyInstance !== cy) return;
    const count = 3 + Math.floor(Math.random() * 4); // 3-6
    for (let i = 0; i < count; i++) {
      const delay = Math.floor(Math.random() * 280);
      const tid = setTimeout(() => {
        if (!cyInstance || cyInstance !== cy) return;
        const path = pickNeuronSignalPath(cy);
        if (path) runNeuronSignalPulse(cy, path);
      }, delay);
      (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(tid);
    }
  };
  fireWave();
  window.__neuronSignalTimer = setInterval(fireWave, 1600);
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
  cyNode.flashClass('focusPulse', 980);
}

function routeVirtualNeuron(node) {
  if (!node || !node.virtual_category) return;
  if (node.virtual_category === 'rules_habits') {
    showToast('规则与习惯为图内索引，选择下方规则即可直接治理。', 'success');
    selectNeuron(node.id);
    return;
  }
  if (node.virtual_category === 'conversation_history') {
    if (node.requires_agent_selection || !activeAgentInstanceId) {
      showToast('对话历史按 Agent 隔离。请先在数据源页选择一个 Agent。', 'info');
      return;
    }
    showToast('会话历史索引已在图内展示。点击会话节点后再按“读取原文”打开。', 'success');
    selectNeuron(node.id);
    return;
  }
}

function openNeuronHistorySession(sessionId) {
  if (!sessionId) return showToast('未找到会话索引', 'error');
  if (!activeAgentInstanceId) {
    showToast('对话历史按 Agent 隔离。请先在数据源页选择一个 Agent。', 'info');
    switchTab('sources');
    return;
  }
  historyFocusSessionId = sessionId;
  switchTab('history');
}

function removeNeuronRuleBodyModal() {
  document.getElementById('neuron-rule-body-modal')?.remove();
}

function openNeuronRuleBodyEditor(memoryId) {
  const node = (neuronGraph?.nodes || []).find(item => item.memory_id === memoryId && item.node_kind === 'virtual_rule_ref');
  if (!memoryId || !node) return showToast('未找到可编辑的规则节点', 'error');
  removeNeuronRuleBodyModal();
  const modal = document.createElement('div');
  modal.id = 'neuron-rule-body-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="编辑规则正文">
    <div class="modal-head"><h3>编辑规则正文</h3><p>保存后写回同一条受治理记忆，不会创建副本。</p></div>
    <div class="modal-body"><label class="field"><span>正文</span><textarea id="neuron-rule-body-input" rows="8" maxlength="12000"></textarea></label></div>
    <div class="modal-actions"><button class="btn" type="button" data-mg-action="neuron-rule-body-close">取消</button><button class="btn btn-primary" type="button" data-mg-action="neuron-rule-body-save" data-memory-id="${escapeHtml(memoryId)}">保存</button></div>
  </div>`;
  document.body.appendChild(modal);
  const input = document.getElementById('neuron-rule-body-input');
  if (input) input.value = String(node.body || '');
}

async function refreshNeuronRuleGovernance(memoryId, message = '') {
  await refreshNeuronGraph(message);
  const next = (neuronGraph?.nodes || []).find(item => item.memory_id === memoryId && item.node_kind === 'virtual_rule_ref');
  if (next) selectNeuron(next.id, false);
}

async function saveNeuronRuleBody(memoryId) {
  const body = String(document.getElementById('neuron-rule-body-input')?.value || '').trim();
  if (!body) return showToast('规则正文不能为空', 'error');
  try {
    const result = await callApi('edit_memory', memoryId, body, activeShareGroupId || 'default');
    if (result.error || result.ok === false) throw new Error(result.error || '更新失败');
    removeNeuronRuleBodyModal();
    await refreshNeuronRuleGovernance(memoryId, result.message || '规则正文已更新');
  } catch (error) { showToast(`规则正文更新失败：${error.message || error}`, 'error'); }
}

async function governNeuronRule(memoryId, method) {
  if (!memoryId || !['delete_memory', 'restore_memory'].includes(method)) return;
  const deleting = method === 'delete_memory';
  if (deleting && !confirm('确认软删除这条规则？可在图内恢复，原始历史不会受影响。')) return;
  try {
    const result = await callApi(method, memoryId, activeShareGroupId || 'default');
    if (result.error || result.ok === false) throw new Error(result.error || '操作失败');
    await refreshNeuronRuleGovernance(memoryId, result.message || (deleting ? '规则已软删除' : '规则已恢复'));
  } catch (error) { showToast(`规则${deleting ? '删除' : '恢复'}失败：${error.message || error}`, 'error'); }
}

function selectNeuron(nodeId, focus = true) {
  const node = (neuronGraph.nodes || []).find(item => item.id === nodeId);
  const popover = document.getElementById('neuron-popover');
  if (!node) return;
  selectedNeuronId = nodeId;
  selectedNeuronNode = node;
  const additive = !!neuronTapSelectionAdditive;
  neuronTapSelectionAdditive = false;

  if (cyInstance) {
    const target = cyInstance.getElementById(nodeId);
    const wasSelected = target && target.length && target.selected();
    if (!additive) {
      cyInstance.elements().unselect();
      selectedNeuronIds.clear();
    }
    if (target && target.length) {
      if (additive && wasSelected) {
        target.unselect();
        selectedNeuronIds.delete(nodeId);
      } else {
        target.select();
        selectedNeuronIds.add(nodeId);
      }
    }
    if (additive && selectedNeuronIds.size && !selectedNeuronIds.has(nodeId)) {
      selectedNeuronIds.add(nodeId);
    }
    if (!additive) {
      selectedNeuronId = target && target.length && target.selected() ? nodeId : null;
      selectedNeuronNode = selectedNeuronId ? node : null;
    } else if (!selectedNeuronIds.size) {
      selectedNeuronId = null;
      selectedNeuronNode = null;
    } else if (selectedNeuronIds.has(nodeId) && wasSelected === false && additive) {
      selectedNeuronId = nodeId;
    } else if (!selectedNeuronIds.has(selectedNeuronId) && selectedNeuronIds.size) {
      const nextId = selectedNeuronIds.values().next().value;
      selectedNeuronId = nextId;
      selectedNeuronNode = (neuronGraph.nodes || []).find(item => item.id === nextId) || node;
    }
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

const DECISION_REASON_OPTIONS = {
  exclude: [
    { id: 'wrong', label: '内容错误或过时' },
    { id: 'irrelevant', label: '与当前项目无关' },
    { id: 'duplicate', label: '重复记忆' },
    { id: 'privacy', label: '隐私 / 不应保留' },
    { id: 'low_quality', label: '质量过低，无治理价值' },
  ],
  quarantine: [
    { id: 'secret', label: '含敏感信息（密钥/令牌等）' },
    { id: 'untrusted', label: '内容可疑 / 不可信' },
    { id: 'review', label: '待人工复核' },
    { id: 'policy', label: '与安全策略冲突' },
  ],
  supersede: [
    { id: 'replaced', label: '已被更新内容替代' },
    { id: 'stale', label: '过时版本' },
    { id: 'merged', label: '已合并到其他记忆' },
  ],
  takeover: [
    { id: 'mcp_takeover', label: 'MCP 正式接管（推荐）' },
    { id: 'migration_complete', label: '原生记忆迁移完成' },
    { id: 'governance_verified', label: '治理结果已人工确认' },
  ],
};

async function neuronAction(nodeId, action) {
  // v3.1 §6.2：图上操作 → DecisionEvent → 轻量刷新投影（不跑 LLM）
  let reason = '';
  if (action === 'exclude' || action === 'quarantine' || action === 'supersede') {
    reason = await pickDecisionReason(action);
    if (!reason) return;
  }
  // 先从本地图拿掉，避免「写入中」干等
  const mid = (selectedNeuronNode && (selectedNeuronNode.memory_id || selectedNeuronNode.id)) || nodeId;
  optimisticRemoveNeuron(nodeId, mid);
  showToast('正在写入决策并刷新投影…');
  try {
    const [scope, agentId, groupId] = scopeApiArgs();
    const result = await callApi('neuron_decide', nodeId, action, reason, true, scope, agentId, groupId);
    if (result.error) {
      showToast(result.error, 'error');
      await refreshNeuronGraph();
      return;
    }
    const ver = result.memory_version || result.version_id || '';
    showToast(`已${action === 'exclude' ? '排除' : action === 'quarantine' ? '隔离' : '更新'}${ver ? ' · ' + ver.slice(0, 8) : ''}`, 'success');
    await refreshNeuronGraph();
  } catch (e) {
    showToast('操作失败：' + e, 'error');
    await refreshNeuronGraph();
  }
}

function optimisticRemoveNeuron(nodeId, memoryId) {
  try {
    if (cyInstance && !cyInstance.destroyed()) {
      const el = cyInstance.getElementById(nodeId);
      if (el && el.length) el.remove();
      if (memoryId) {
        cyInstance.nodes().filter(n => n.data('memory_id') === memoryId).remove();
      }
    }
    if (neuronGraph && Array.isArray(neuronGraph.nodes)) {
      neuronGraph.nodes = neuronGraph.nodes.filter(n =>
        n.id !== nodeId && n.memory_id !== memoryId && !(n.member_ids || []).includes(memoryId)
      );
    }
    selectedNeuronIds.delete(nodeId);
    if (memoryId) {
      [...selectedNeuronIds].forEach(id => {
        const targetNode = (neuronGraph?.nodes || []).find(item => item.id === id);
        if (targetNode && targetNode.memory_id === memoryId) selectedNeuronIds.delete(id);
      });
    }
    if (!selectedNeuronIds.size) {
      selectedNeuronId = null;
      selectedNeuronNode = null;
    } else if (!selectedNeuronNode || !selectedNeuronIds.has(selectedNeuronId)) {
      const nextId = selectedNeuronIds.values().next().value;
      selectedNeuronId = nextId;
      selectedNeuronNode = (neuronGraph.nodes || []).find(item => item.id === nextId) || null;
    }
    renderStatusRail();
  } catch (_) {}
}

function pickDecisionReason(action) {
  const options = DECISION_REASON_OPTIONS[action] || [];
  if (!options.length) return Promise.resolve(action);
  return new Promise((resolve) => {
    closeDecisionReasonModal();
    const titles = {
      exclude: '选择排除原因',
      quarantine: '选择隔离原因',
      supersede: '选择覆盖原因',
      takeover: '选择正式接管说明',
    };
    const rows = options.map((o, i) => `<label class="release-option">
      <input type="radio" name="decision-reason" value="${i}" ${i === 0 ? 'checked' : ''}>
      <span><div class="release-title">${escapeHtml(o.label)}</div></span>
    </label>`).join('');
    const modal = document.createElement('div');
    modal.id = 'decision-reason-modal';
    modal.className = 'modal-backdrop';
    modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="${escapeHtml(titles[action] || '选择原因')}">
      <div class="modal-head"><h3>${escapeHtml(titles[action] || '选择原因')}</h3>
        <p>请点选预设原因，无需手输文字。</p></div>
      <div class="modal-body">${rows}</div>
      <div class="modal-actions">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn btn-primary" type="button" data-act="ok">确认</button>
      </div>
    </div>`;
    const finish = (value) => {
      closeDecisionReasonModal();
      resolve(value);
    };
    modal.addEventListener('click', (event) => {
      if (event.target === modal) finish('');
    });
    modal.querySelector('[data-act="cancel"]').onclick = () => finish('');
    modal.querySelector('[data-act="ok"]').onclick = () => {
      const selected = modal.querySelector('input[name="decision-reason"]:checked');
      if (!selected) return showToast('请选择一个原因', 'error');
      const opt = options[Number(selected.value)];
      finish(opt ? opt.label : '');
    };
    document.body.appendChild(modal);
  });
}

function closeDecisionReasonModal() {
  const modal = document.getElementById('decision-reason-modal');
  if (modal) modal.remove();
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
  selectedNeuronIds = new Set();
  neuronDragState = null;
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
  const [scope, agentId, groupId] = scopeApiArgs();
  neuronGraph = await callApi('get_neuron_graph', projectionMode, scope, agentId, groupId);
  renderNeuronGraph();
  if (message) showToast(message, 'success');
}

async function pollBuildProgress(jobId) {
  const maxWaitMs = 10 * 60 * 1000;
  const started = Date.now();
  const phases = [
    { id: 'scan', label: '扫描' },
    { id: 'normalize', label: '规范化' },
    { id: 'scope', label: '范围' },
    { id: 'enrich_queue', label: '入队' },
    { id: 'enrich', label: 'LLM 整理' },
    { id: 'graph', label: '出图' },
    { id: 'save', label: '保存' },
    { id: 'done', label: '完成' },
  ];
  while (Date.now() - started < maxWaitMs) {
    const prog = await callApi('get_build_progress', jobId);
    renderBuildProgressPage(prog, phases);
    renderBuildStatusRail(prog);
    if (prog.status === 'done') {
      if (prog.error) return showToast(prog.error, 'error');
      const enr = (prog.result && prog.result.enrichment) || {};
      const applied = enr.auto_applied || 0;
      if (enr.host_action_required || enr.enrich_mode === 'host' || enr.engine === 'host_deferred') {
        const n = enr.pending_count || (enr.pending_tasks || []).length || 0;
        await refreshNeuronGraph(n ? `投影已出图，${n} 条待对话 Skill 整理` : '投影已出图（宿主 Skill 未调用模型）');
        showToast(
          n
            ? `宿主 Skill 未在本对话执行。请在 Cursor 聊天说「整理 MemoryGuard pending」或选 Cursor Agent CLI 重建。`
            : '选了宿主 Skill，但 GUI 不能唤起聊天；图上内容多半是旧启发式，不是本次模型整理。',
          'info',
        );
        return;
      }
      await refreshNeuronGraph(applied ? `投影构建完成（已整理 ${applied} 条）` : '投影构建完成');
      return;
    }
    if (prog.status === 'error') return showToast(prog.error || prog.message || '构建失败', 'error');
    if (prog.status === 'cancelled') return showToast('构建已取消', 'info');
    await new Promise(r => setTimeout(r, 400));
  }
  showToast('构建超时，请稍后刷新神经图', 'error');
}

function renderBuildProgressPage(prog, phases) {
  const pct = Math.max(0, Math.min(100, prog.percent != null ? prog.percent : 0));
  const msg = prog.message || '构建中…';
  const phase = prog.phase || '';
  const phaseOrder = phases.map(p => p.id);
  const idx = Math.max(0, phaseOrder.indexOf(phase));
  const chips = phases.map((p, i) => {
    const cls = i < idx ? 'done' : (i === idx || phase === p.id ? 'active' : '');
    return `<span class="bp-phase ${cls}">${escapeHtml(p.label)}</span>`;
  }).join('');
  setContent(`<div class="build-progress" role="status" aria-live="polite">
    <div class="bp-kicker">Build progress</div>
    <h2>正在构建投影</h2>
    <div class="bp-msg">${escapeHtml(msg)}</div>
    <div class="bp-bar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
    <div class="bp-meta"><span>${escapeHtml(phase || 'starting')}</span><span>${pct}%</span></div>
    <div class="bp-phases">${chips}</div>
    <div class="finding-actions" style="margin-top:8px">
      <button class="btn" type="button" onclick="cancelActiveBuild('${escapeHtml(prog.job_id || '')}')">取消构建</button>
    </div>
  </div>`);
}

function renderBuildStatusRail(prog) {
  const container = document.getElementById('status-rail-content');
  const title = document.querySelector('#status-rail h3');
  if (!container) return;
  if (title) title.textContent = '构建状态';
  const pct = prog.percent != null ? prog.percent : 0;
  container.innerHTML = `
    <div class="status-item"><span class="status-label">阶段</span><span class="status-num" style="font-size:12px">${escapeHtml(prog.phase || '—')}</span></div>
    <div class="status-item"><span class="status-label">进度</span><span class="status-num">${pct}%</span></div>
    <div class="neuron-detail-body" style="margin-top:10px">${escapeHtml(prog.message || '构建中…')}</div>
    <div class="rail-link" onclick="cancelActiveBuild('${escapeHtml(prog.job_id || '')}')">取消构建</div>`;
}

async function cancelActiveBuild(jobId) {
  try {
    await callApi('cancel_build_projection', jobId || '', true);
    showToast('已请求取消构建', 'info');
  } catch (e) { showToast('取消失败：' + e, 'error'); }
}

async function buildProjection(llmAgent = '', llmCli = '', skipConfirm = false) {
  const native = projectionMode === 'native';
  const shared = isShareGroupScope();

  // 多 Agent / 共享组：必须弹窗选整理引擎；多个 CLI 时也弹窗
  if (!native && !llmAgent) {
    try {
      const agentsResp = await callApi('list_host_llm_agents');
      const agents = (agentsResp && agentsResp.agents) || [];
      const needPick = shared || agents.length > 1;
      if (needPick && agents.length >= 1) {
        showLlmPickModal({
          agents,
          suggested_agent: agentsResp.primary || agents[0].agent || 'host',
          for_build: true,
          title: shared ? '多 Agent 共享组：选择整理用 LLM' : '选择构建用 LLM',
        });
        return;
      }
      if (agents.length === 1) {
        llmAgent = agents[0].agent || '';
        llmCli = agents[0].cli || '';
      } else {
        showToast('未检测到可用整理引擎，将用本地启发式', 'info');
      }
    } catch (e) {
      showToast('检测 LLM 失败，将用启发式：' + e, 'info');
    }
  }

  const llmHint = llmAgent === 'host'
    ? '\n· LLM：宿主 Skill（GUI 只入队；须在 Cursor 对话里继续整理）'
    : (llmAgent ? `\n· LLM：${llmAgent}` : '\n· LLM：启发式兜底');
  const message = shared
    ? `构建共享 MCP 记忆投影？\n\n· 共享组：${activeShareGroupId}${llmHint}\n· 入队后整理，再生成投影\n\n继续？`
    : native
    ? '构建原生记忆投影？\n\n· 读取已勾选原生/项目记忆\n· 只生成当前真实记忆图\n· 不调用 LLM\n\n继续？'
    : `构建重构治理投影？\n\n· 萃取、合并、清理已勾选来源${llmHint}\n· 分类/翻译后出图\n\n继续？`;
  if (!skipConfirm && !confirm(message)) return;

  setContent(`<div class="build-progress"><div class="bp-kicker">Build progress</div><h2>正在启动构建</h2><div class="bp-msg">准备中…</div><div class="bp-bar"><i style="width:2%"></i></div><div class="bp-meta"><span>starting</span><span>0%</span></div></div>`);
  renderBuildStatusRail({ phase: 'starting', message: '正在启动…', percent: 0, job_id: '' });
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) return showToast('缺少治理范围，请先选择 Agent 或共享组', 'error');
    const [scope, agentId, groupId] = scopeApiArgs();
    const enrichMode = llmAgent === 'host' ? 'host' : (llmAgent && llmCli ? 'cli' : 'auto');
    const result = await callApi(
      'start_build_projection', true, projectionMode, scope, agentId, groupId, llmAgent, llmCli, enrichMode,
    );
    if (result.error && !result.job_id) return showToast(result.error, 'error');
    if (result.job_id) {
      await pollBuildProgress(result.job_id);
      return;
    }
    if (result.error) return showToast(result.error, 'error');
    await refreshNeuronGraph(shared ? '共享组投影构建完成' : native ? '原生投影构建完成' : '重构投影构建完成');
  } catch (e) { showToast('构建失败：' + e, 'error'); }
}

async function deleteProjection() {
  if (!confirm(`删除当前${projectionMode === 'native' ? '原生' : '重构'}投影？\n\n只删除投影文件，不删除原生记忆。`)) return;
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) return showToast('缺少治理范围，无法删除投影', 'error');
    const [scope, agentId, groupId] = scopeApiArgs();
    const result = await callApi('delete_projection', true, projectionMode, scope, agentId, groupId);
    if (result.error) return showToast(result.error, 'error');
    await refreshNeuronGraph('当前投影已删除，可随时重建');
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

function showLlmPickModal(payload) {
  closeLlmPickModal();
  const agents = payload.agents || [];
  if (!agents.length) {
    showToast(payload.message || '未检测到可用 Agent CLI', 'error');
    return;
  }
  const suggested = payload.suggested_agent || agents[0].agent;
  const rows = agents.map((a, i) => `<label class="release-option">
    <input type="radio" name="llm-pick" value="${i}" ${a.agent === suggested || (!suggested && i === 0) ? 'checked' : ''}>
    <span><div class="release-title">${escapeHtml(a.label || a.agent)}</div>
      <div class="release-meta">${escapeHtml(a.agent)}${a.cli ? ' · ' + escapeHtml(a.cli) : ' · Skill/MCP 自动整理'}</div></span>
  </label>`).join('');
  const head = payload.title || '选择构建用 LLM';
  const modal = document.createElement('div');
  modal.id = 'llm-pick-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="${escapeHtml(head)}">
    <div class="modal-head"><h3>${escapeHtml(head)}</h3>
      <p>多 Agent 必须选择整理引擎。「宿主 Skill」只入队，须在 Cursor 对话里继续；要 GUI 内同步整理请选 Cursor Agent / Codex 等 CLI。</p></div>
    <div class="modal-body">${rows}</div>
    <div class="modal-actions">
      <button class="btn" type="button" onclick="closeLlmPickModal()">取消</button>
      <button class="btn btn-primary" type="button" onclick="confirmLlmPickModal()">确认并构建</button>
    </div>
  </div>`;
  modal.__agents = agents;
  modal.__forBuild = true;
  modal.addEventListener('click', (event) => { if (event.target === modal) closeLlmPickModal(); });
  document.body.appendChild(modal);
}

function closeLlmPickModal() {
  const modal = document.getElementById('llm-pick-modal');
  if (modal) modal.remove();
}

async function confirmLlmPickModal() {
  const modal = document.getElementById('llm-pick-modal');
  const selected = document.querySelector('input[name="llm-pick"]:checked');
  if (!modal || !selected) return showToast('请选择一个 Agent LLM', 'error');
  const agent = modal.__agents[Number(selected.value)];
  closeLlmPickModal();
  if (!agent) return;
  await buildProjection(agent.agent || '', agent.cli || '', false);
}

async function commitSharedMemoryGovernance() {
  const gid = activeShareGroupId;
  if (!gid) return showToast('请先选择或创建共享组', 'error');
  const reason = await pickDecisionReason('takeover');
  if (!reason) return;
  if (!confirm(`确认对共享组正式接管？\n\n· 共享组：${gid}\n· 对 SharedMemoryStore 打版本快照\n· Agent 已通过 MCP 重定向读写共享记忆\n\n继续？`)) return;
  showToast('正在提交共享记忆治理…');
  try {
    const result = await callApi('commit_shared_memory_governance', gid, reason, true);
    if (result.error) return showToast(result.error, 'error');
    showToast(`正式接管已确认：${result.version_id || ''}`, 'success');
    try {
      await refreshNeuronGraph();
      if (result.projection_warning) {
        showToast('正式接管已完成；后台投影重建提示：' + result.projection_warning, 'info');
      }
    } catch (refreshError) {
      showToast('正式接管已完成，但神经图刷新失败：' + refreshError, 'info');
    }
  } catch (e) { showToast('提交失败：' + e, 'error'); }
}

async function importNativeMemoriesToGroup(groupId) {
  const gid = groupId || activeShareGroupId;
  if (!gid) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`从各 Agent 已勾选的原生/项目记忆导入共享组？\n\n· 共享组：${gid}\n· 一次性迁移，写入 SharedMemoryStore\n\n继续？`)) return;
  showToast('正在导入原生记忆…');
  try {
    const result = await callApi('import_native_memories_to_group', gid, null, true);
    if (result.error) return showToast(result.error, 'error');
    await setActiveShareGroup(gid);
    await refreshNeuronGraph(`导入完成：${result.records_written || 0} 条记录`);
  } catch (e) { showToast('导入失败：' + e, 'error'); }
}

async function installSharedGroupMcpRedirects(groupId) {
  const gid = groupId || activeShareGroupId;
  if (!gid) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`为共享组内 Agent 安装全局 MCP + Hook 接管？\n\n· 写入各 Agent 的用户级配置\n· 自动安装宿主支持的生命周期 Hook\n· 固定连接当前 MemoryGuard 控制目录\n· 以后从任意项目启动都读写同一共享记忆\n\n继续？`)) return;
  showToast('正在安装 MCP 与宿主 Hook…');
  try {
    const result = await callApi('install_shared_group_mcp_redirects', gid, true);
    if (result.error) return showToast(result.error, 'error');
    const configured = result.configured_count || 0;
    const skipped = result.skipped_count || 0;
    const failed = result.error_count || 0;
    const warnings = result.warning_count || 0;
    const hookConfigured = result.hook_configured_count || 0;
    const hookUnsupported = result.hook_unsupported_count || 0;
    const hookFailed = result.hook_error_count || 0;
    const warningText = (result.installed || []).flatMap(x => x.warnings || []).join('；');
    if (result.status === 'failure') {
      return showToast(`MCP 配置失败：${failed} 个错误，${skipped} 个暂缺自动安装适配器`, 'error');
    }
    const suffix = `${skipped ? `，${skipped} 个暂缺自动安装适配器` : ''}${failed ? `，${failed} 个 MCP 失败` : ''}${hookUnsupported ? `，${hookUnsupported} 个宿主无已验证 Hook` : ''}${hookFailed ? `，${hookFailed} 个 Hook 失败` : ''}${warnings ? `，${warnings} 条配置提示` : ''}`;
    showToast(`全局 MCP 已配置 ${configured} 个 Agent，Hook 已配置 ${hookConfigured} 个${suffix}。请按提示重启/信任 Hook；之后任意项目都会连接当前共享记忆。${warningText ? ` ${warningText}` : ''}`,
      result.status === 'partial' ? 'info' : 'success');
  } catch (e) { showToast('安装失败：' + e, 'error'); }
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
  const items = report.findings.map((finding, index) => `<article class="finding-item sev-${escapeHtml(finding.severity)}" role="button" tabindex="0"
    aria-expanded="${index === 0 ? 'true' : 'false'}"
    onclick="toggleFinding('${escapeHtml(finding.id)}')"
    onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleFinding('${escapeHtml(finding.id)}')}">
    <div class="finding-header"><span class="finding-rule"><span class="chip chip-${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span> ${escapeHtml(finding.rule_id)}</span>
      <span class="finding-toggle" id="toggle-${escapeHtml(finding.id)}">${index === 0 ? '收起详情' : '展开详情'}</span></div>
    <div class="finding-evidence">${escapeHtml(finding.evidence)}</div>
    <div class="finding-detail" id="detail-${escapeHtml(finding.id)}" style="display:${index === 0 ? 'block' : 'none'}">
      <div class="row"><span class="key">维度</span><span>${escapeHtml(finding.dimension)}</span></div>
      <div class="row"><span class="key">表面</span><span>${escapeHtml(finding.surface)}</span></div>
      <div class="row"><span class="key">位置</span><code>${escapeHtml(finding.location.path)}:${finding.location.span[0]}</code></div>
      <div class="row"><span class="key">影响</span><span>${escapeHtml(finding.impact)}</span></div>
      <div class="row"><span class="key">建议</span><span>${escapeHtml(finding.suggestion)}</span></div>
      <div class="row"><span class="key">置信度</span><span>${(finding.confidence * 100).toFixed(0)}%</span></div>
      <div class="finding-actions">
        <button class="btn" type="button" onclick="event.stopPropagation();copyFindingForAgent('${escapeHtml(finding.id)}')">复制给 Agent 处理</button>
        ${finding.fixable ? `<button class="btn btn-primary" type="button" onclick="event.stopPropagation();generatePlan('${escapeHtml(finding.id)}')">生成修复计划</button>` : ''}
      </div>
    </div></article>`).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Risk signals</span><h2>风险信号</h2>
    <p>带“可生成变更”的风险可由 MemoryGuard 自动修复；其余是诊断证据，请交给 Agent 分析处理，完成后重新扫描验证。</p>
    <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="copyAllFindingsForAgent()">复制全部风险给 Agent</button></div>
  </div>${items}`);
}

function toggleFinding(id) {
  const element = document.getElementById('detail-' + id);
  if (!element) return;
  const open = element.style.display === 'none';
  element.style.display = open ? 'block' : 'none';
  const card = element.closest('.finding-item');
  if (card) card.setAttribute('aria-expanded', open ? 'true' : 'false');
  const label = document.getElementById('toggle-' + id);
  if (label) label.textContent = open ? '收起详情' : '展开详情';
}

function findingAgentPrompt(finding) {
  const line = finding.location && finding.location.span ? finding.location.span[0] : 1;
  const path = finding.location && finding.location.path ? finding.location.path : '未知位置';
  return [
    '请处理下面的 MemoryGuard 风险信号：',
    `规则：${finding.rule_id || ''}`,
    `严重度：${finding.severity || ''}`,
    `维度/表面：${finding.dimension || ''} / ${finding.surface || ''}`,
    `位置：${path}:${line}`,
    `证据：${finding.evidence || ''}`,
    `影响：${finding.impact || ''}`,
    `建议：${finding.suggestion || ''}`,
    '',
    '要求：先核验证据和根因，再修改被引用的项目内容；不要修改 MemoryGuard 的来源文件，也不要盲目删除。完成后重新运行扫描，确认该风险消失且没有引入回归。',
  ].join('\n');
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    return true;
  } catch (_) {
    return false;
  }
}

async function copyFindingForAgent(findingId) {
  const finding = (state.report.findings || []).find(item => item.id === findingId);
  if (!finding) return showToast('未找到风险信号', 'error');
  const ok = await copyText(findingAgentPrompt(finding));
  showToast(ok ? '已复制，可直接粘贴给 Agent 处理' : '复制失败，请展开后手动复制证据', ok ? 'success' : 'error');
}

async function copyAllFindingsForAgent() {
  const findings = state.report.findings || [];
  if (!findings.length) return showToast('当前没有风险信号', 'info');
  const text = findings.map((finding, index) =>
    `# 风险 ${index + 1}\n${findingAgentPrompt(finding)}`
  ).join('\n\n---\n\n');
  const ok = await copyText(text);
  showToast(ok ? `已复制 ${findings.length} 条风险，可直接粘贴给 Agent 处理` : '复制失败，请逐条复制', ok ? 'success' : 'error');
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
    const [agentData, sourcesResult, rawResult, bindingsResult] = await Promise.all([
      activeAgentInstanceId ? callApi('get_agent_data', activeAgentInstanceId) : Promise.resolve(null),
      callApi('list_sources'),
      callApi('get_raw_memory'),
      callApi('list_bindings'),
    ]);
    renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult);
  } catch (e) {
    showToast('数据源加载失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>数据源加载失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function selectAgentCard(instanceId) {
  activeAgentInstanceId = instanceId;
  dataPageMode = 'single_agent';
  activeShareGroupId = '';
  callApi('set_governance_scope', {
    mode: 'agent',
    agent_instance_id: instanceId,
  }).catch(() => {});
  if (state.activeTab === 'history') renderHistory();
  else renderSources();
}

function renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult) {
  const sources = sourcesResult.sources || [];
  const cov = rawResult.coverage || {};
  document.getElementById('sources-count').textContent = sources.length || '';

  // v3.2 Agent 卡片
  const agents = (agentCardsData && agentCardsData.agents) || [];
  const activeBindings = ((bindingsResult && bindingsResult.bindings) || []).filter(b => b.status === 'active');
  const residuals = (agentCardsData && agentCardsData.residuals) || [];
  const lifecycleLabels = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
  const lifecycleChips = { installed: 'confirmed', installed_no_data: 'info', data_only: 'medium', uncertain: 'info', ignored: 'low', not_detected: 'low' };
  const agentCardsHtml = agents.length ? agents.map(a => {
    const isActive = a.instance_id === activeAgentInstanceId;
    const lifecycle = a.lifecycle_state || 'uncertain';
    const binding = activeBindings.find(b => b.agent_instance_id === a.instance_id);
    const kindLabel = binding ? (binding.group_kind === 'personal' ? '个人记忆层' : '共享记忆层') : '未绑定';
    const bindingAction = binding
      ? `<button class="btn" type="button" onclick="event.stopPropagation(); viewMemoryLayer('${escapeHtml(binding.share_group_id)}')">进入记忆层</button>
         <button class="btn" type="button" onclick="event.stopPropagation(); installMemoryGroupMcp('${escapeHtml(binding.share_group_id)}')">重新安装 MCP</button>
         ${binding.group_kind === 'shared' ? `<button class="btn btn-danger" type="button" onclick="event.stopPropagation(); leaveSharedToPersonal('${escapeHtml(a.instance_id)}')">退出共享组并回个人层</button>` : ''}`
      : `<button class="btn btn-primary" type="button" onclick="event.stopPropagation(); ensurePersonalLayer('${escapeHtml(a.instance_id)}')">启用个人记忆层</button>`;
    return `<div class="agent-card ${isActive ? 'active' : ''}" onclick="selectAgentCard('${escapeHtml(a.instance_id)}')">
      <div class="agent-name">${escapeHtml(a.product)}</div>
      <div class="agent-meta">${a.found_surface_count}/${a.surface_count} 表面 · 私有 ${a.private_data_surface_count || 0} · 共享 ${a.shared_surface_count || 0} · ${a.bound_source_count} 来源</div>
      <div class="agent-badge">${escapeHtml(a.target_capability || 'export_only')}</div>
      <span class="chip chip-${lifecycleChips[lifecycle] || 'info'}">${escapeHtml(lifecycleLabels[lifecycle] || lifecycle)}</span>
      <div class="surface-meta">${kindLabel}${binding ? ` · ${escapeHtml(binding.share_group_id)} · ${escapeHtml(binding.canonical_store_path || '')}` : ''}</div>
      ${binding && binding.migration_required ? '<div class="chip chip-medium">待迁移（仅提示）</div>' : ''}
      <div class="finding-actions">${bindingAction}</div>
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
      // JSON string keeps arbitrary local filenames out of executable HTML.
      const viewArgs = escapeHtml(JSON.stringify([String(f.root_id || ''), String(f.relative_path || '')]));
      const clickAttr = canOpen ? ` onclick="viewSourceFile(...${viewArgs})"` : '';
      const statusText = canOpen ? (f.read_status || '') : '仅发现，需先授权';
      return `<div class="raw-file-row"${clickAttr} style="${canOpen ? '' : 'cursor:default;opacity:.72'}">
        <span class="raw-file-path"><code>${escapeHtml(f.relative_path || '').replaceAll('\\', '/')}</code></span>
        <span class="chip chip-${canOpen && f.read_status === 'read' ? 'confirmed' : 'medium'}">${escapeHtml(statusText)}</span>
        <span style="color:var(--faint);font-size:10px">${escapeHtml(f.media_type || '')}</span>
      </div>`;
    }).join('')}
  </div>`;
  const knowledgeTypes = new Set(['selected_directory', 'selected_file', 'obsidian_vault']);
  const nonKnowledgeCategories = new Set([
    'native_memory', 'project_memory', 'control_surface', 'skill_surface',
    'conversation_history', 'runtime_evidence', 'ignored_runtime_data',
  ]);
  const knowledgeSources = sources.filter(s =>
    knowledgeTypes.has(s.type) && !nonKnowledgeCategories.has(s.source_category || 'unknown')
  );
  const rawGroupsByRoot = new Map(((rawResult && rawResult.groups) || []).map(g => [g.root_id, g]));
  const sourceTypeLabels = {
    selected_directory: '文件夹知识库',
    selected_file: '单文件知识库',
    obsidian_vault: 'Obsidian 知识库',
  };
  const knowledgeCardsHtml = knowledgeSources.length ? knowledgeSources.map(source => {
    const group = rawGroupsByRoot.get(source.root_id) || {};
    const files = (group.files || []).map(file => ({
      ...file,
      root_id: source.root_id,
      authorized: true,
    }));
    const visibleFiles = files.slice(0, 24);
    const remaining = Math.max(0, files.length - visibleFiles.length);
    const connected = source.path_exists !== false;
    return `<article class="knowledge-card ${connected ? '' : 'missing'}">
      <div class="finding-header">
        <div>
          <div class="knowledge-title">${escapeHtml(source.display_name || '未命名知识库')}</div>
          <div class="surface-meta">${escapeHtml(sourceTypeLabels[source.type] || '本地知识库')} · ${files.length} 个文件 · ${escapeHtml(scopeLabels[source.scope] || source.scope || '未归属')}</div>
        </div>
        <span class="chip chip-${connected ? 'confirmed' : 'high'}">${connected ? '已连接' : '路径失效'}</span>
      </div>
      <div class="knowledge-path" title="${escapeHtml(source.path || '')}"><code>${escapeHtml(source.path || '')}</code></div>
      <details class="knowledge-files">
        <summary>${files.length ? `查看已扫描文件（${files.length}）` : '暂无可读取文件'}</summary>
        ${files.length ? renderFiles(visibleFiles) : '<div class="surface-meta" style="margin-top:8px">目录为空，或没有符合扫描策略的文件。</div>'}
        ${remaining ? `<div class="surface-meta" style="margin-top:8px">另有 ${remaining} 个文件未在卡片中展开。</div>` : ''}
      </details>
      <div class="knowledge-actions">
        <button class="btn btn-danger" type="button"
          data-source-id="${escapeHtml(source.root_id)}"
          data-source-name="${escapeHtml(source.display_name || source.root_id)}"
          onclick="removeSourceCard(this)">删除映射</button>
      </div>
    </article>`;
  }).join('') : `<div class="empty-state"><div><div class="empty-orb"></div>
    <p>尚未添加本地知识库。添加文件夹后会在这里显示；原文件保持只读。</p></div></div>`;
  const knowledgeSection = `<section class="card">
    <div class="card-head"><div><h2>本地知识库</h2>
      <p>${knowledgeSources.length} 个来源 · 只读扫描；文档需萃取后才进入长期记忆</p></div>
      <button class="btn btn-primary" type="button" onclick="addSourceDialog()">+ 添加文件夹或文件</button></div>
    <div class="knowledge-grid">${knowledgeCardsHtml}</div>
  </section>`;
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
    ${knowledgeSection}
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
    const [agentsResult, bindingsResult, hooksResult] = await Promise.all([
      callApi('list_agents'),
      callApi('list_bindings'),
      callApi('get_host_hook_status'),
    ]);
    showMultiAgentBinding(agentsResult, bindingsResult, hooksResult);
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function showMultiAgentBinding(agentsResult, bindingsResult, hooksResult) {
  const agents = (agentsResult && agentsResult.agents) || [];
  const existingBindings = (bindingsResult && bindingsResult.bindings) || [];
  const hookAgents = (hooksResult && hooksResult.agents) || [];
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

  const activeBindings = existingBindings.filter(b => b.status === 'active');
  const personalLayerHtml = agents.map(a => {
    const b = activeBindings.find(x => x.agent_instance_id === a.instance_id);
    const hook = hookAgents.find(x => x.agent_instance_id === a.instance_id);
    const label = b ? (b.group_kind === 'personal' ? '个人记忆层' : '共享记忆层') : '未启用个人记忆层';
    const action = b
      ? (b.group_kind === 'shared' ? '' : `<button class="btn" type="button" onclick="ensurePersonalLayer('${escapeHtml(a.instance_id)}')">保持个人层</button>`)
      : `<button class="btn btn-primary" type="button" onclick="ensurePersonalLayer('${escapeHtml(a.instance_id)}')">启用个人记忆层</button>`;
    const viewAction = b ? `<button class="btn" type="button" onclick="viewMemoryLayer('${escapeHtml(b.share_group_id)}')">进入记忆层</button>` : '';
    const hookStatus = hook ? (hook.runtime_verified ? '运行已验证' : (hook.configured ? '已配置待运行' : (hook.supported === false ? '宿主无 Hook' : '未配置'))) : '未配置';
    const hookChip = hook && hook.runtime_verified ? 'confirmed' : (hook && hook.configured ? 'medium' : 'info');
    const hookActions = hook && hook.supported !== false
      ? `<button class="btn" type="button" onclick="setHostHookMode('${escapeHtml(hook.provider)}','${escapeHtml(a.instance_id)}','enforce')">强制</button>
         <button class="btn" type="button" onclick="setHostHookMode('${escapeHtml(hook.provider)}','${escapeHtml(a.instance_id)}','paused')">暂停 Hook</button>
         <button class="btn btn-danger" type="button" onclick="uninstallHostHook('${escapeHtml(hook.provider)}')">卸载 Hook</button>`
      : '';
    return `<article class="plan-item"><div class="finding-header"><span class="finding-rule">${escapeHtml(a.product || a.instance_id)}</span><span class="chip chip-info">${label}</span><span class="chip chip-${hookChip}">${hookStatus}</span></div>
      <div class="row"><span class="key">group</span><code>${escapeHtml(b ? b.share_group_id : '未绑定')}</code></div>
      <div class="row"><span class="key">canonical DB</span><span>${escapeHtml(b ? (b.canonical_store_path || '') : '—')}</span></div>
      <div class="row"><span class="key">Hook</span><span>${escapeHtml(hookStatus)}${hook && hook.mode ? ` · ${escapeHtml(hook.mode)}` : ''}</span></div>
      <div class="row"><span class="key">last receipt</span><span>${escapeHtml((hook && hook.last_seen_at) || '—')}</span></div>
      <div class="finding-actions">${viewAction}${action}${hookActions}${b && b.group_kind === 'shared' ? `<button class="btn btn-danger" type="button" onclick="leaveSharedToPersonal('${escapeHtml(a.instance_id)}')">退出共享组并回个人层</button>` : ''}</div></article>`;
  }).join('');

  // 已有共享组分组展示
  const agentNameById = new Map(agents.map(a => [a.instance_id, a.product || a.instance_id]));
  const groupMap = new Map();
  existingBindings.forEach(b => {
    if (b.status !== 'active' || b.group_kind !== 'shared') return;
    if (!groupMap.has(b.share_group_id)) groupMap.set(b.share_group_id, []);
    groupMap.get(b.share_group_id).push(b);
  });
  const groupsHtml = groupMap.size ? Array.from(groupMap.entries()).map(([gid, binds]) => `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">共享组 ${escapeHtml(gid.slice(0, 16))}</span>
      <span class="chip chip-confirmed">${binds.length} 个 Agent</span>
    </div>
    <div class="finding-evidence">${binds.map(b => escapeHtml(agentNameById.get(b.agent_instance_id) || b.agent_instance_id)).join(' · ')}</div>
    <div class="finding-actions">
      <button class="btn" type="button" onclick="activateShareGroup('${escapeHtml(gid)}')">设为治理范围</button>
      <button class="btn" type="button" onclick="previewSharedGroup('${escapeHtml(gid)}')">查看共享组预览</button>
      <button class="btn btn-danger" type="button" onclick="dissolveSharedGroup('${escapeHtml(gid)}')">解散共享组</button>
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
    <section class="card"><div class="card-head"><div><h2>个人记忆层</h2><p>个人层与共享层都使用 MemoryGuard SharedMemoryStore；原生文件仅只读扫描源。</p></div></div>${personalLayerHtml}</section>
    <section class="card"><div class="card-head"><div><h2>已有共享组</h2></div></div>
      ${groupsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
      </div>
    </section>`);
}

function historyScope() {
  return {agent_instance_id: activeAgentInstanceId || '', share_group_id: activeShareGroupId || ''};
}

let ruleScopeOptions = null;
let rulePreviewAgentId = '';
let rulePreviewProjectRef = '';
let rulePreviewProvider = '';
let rulePreviewRuntimeRole = '';
let ruleRangeFilter = 'all';
let ruleVisibilityFilter = 'effective';
let ruleRecordsById = new Map();
let rulePreviewById = new Map();
let ruleDecisionRows = [];
let ruleScopeMetrics = {};
let ruleReceiptRows = [];
let ruleExceptionRows = [];
let ruleCreateResult = null;

function ruleAudience(record) {
  const items = record.assignments || [];
  if (!items.length) return record.audience_label || '未配置适用范围';
  const names = {
    agent: 'Agent', group: '共享组', project: '项目', agent_project: 'Agent + 项目',
    provider: '宿主', runtime_role: '运行角色', system: '系统',
  };
  return items.map(a => {
    const target = a.target_type === 'agent_project'
      ? `${a.target_id} / ${a.project_ref}` : (a.target_id || a.project_ref || '全部');
    const override = Number.isInteger(a.priority_override) ? `，覆盖优先级 ${a.priority_override}` : '';
    return `${a.effect === 'exclude' ? '排除 ' : ''}${names[a.target_type] || a.target_type}: ${target}${override}`;
  }).join('；');
}

function ruleSelectOptions(items, selected = '') {
  return (items || []).map(item => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? 'selected' : ''}>${escapeHtml(item.label || item.id)}</option>`).join('');
}

function rulePreviewState(record) {
  return rulePreviewById.get(record.memory_id) || 'unavailable';
}

function ruleDecisionFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleDecisionRows.find(item =>
    String(item.rule_id || item.memory_id || item.target_rule_id || '') === String(id)
  ) || null;
}

function ruleStatsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return (ruleScopeMetrics.stats || []).find(item =>
    String(item.rule_id || item.memory_id || '') === String(id)
  ) || null;
}

function ruleReceiptsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleReceiptRows.filter(item => String(item.memory_id || '') === String(id));
}

function ruleExceptionsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleExceptionRows.filter(item =>
    String(item.parent_rule || item.parent_rule_id || '') === String(id)
  );
}

function ruleConfidenceLabel(value) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  const pct = Math.round(Math.max(0, Math.min(1, n)) * 100);
  const cls = pct >= 80 ? 'chip-confirmed' : pct >= 50 ? 'chip-medium' : 'chip-high';
  return `<span class="chip ${cls}" title="自动范围置信度">范围置信度 ${pct}%</span>`;
}

function renderRuleReceiptActions(receipt) {
  const receiptId = receipt?.receipt_id || '';
  if (!receiptId) return '';
  const feedback = receipt.feedback || {};
  const outcome = feedback.outcome || '';
  const outcomes = [
    ['followed', '已遵循'], ['violated', '已违反'], ['not_applicable', '不适用'],
    ['corrected', '纠正'], ['exception', '例外'],
  ];
  return `<div class="rule-receipt"><span class="chip chip-info">命中回执 ${escapeHtml(receiptId.slice(0, 12))}</span>
    ${outcome ? `<span class="chip chip-confirmed">反馈：${escapeHtml(outcome)}</span>` : '<span class="muted">尚无反馈</span>'}
    <div class="rule-feedback-actions">${outcomes.map(([value, label]) =>
      `<button class="btn btn-icon" type="button" title="${label}" onclick="submitRuleFeedback('${escapeHtml(receiptId)}','${value}')">${label}</button>`
    ).join('')}</div></div>`;
}

function renderRuleAutoScopePanel() {
  const metrics = ruleScopeMetrics.auto_scope || ruleScopeMetrics.metrics || {};
  const decisions = ruleDecisionRows || [];
  const total = Number(metrics.total ?? metrics.decisions ?? metrics.assignment_count ?? decisions.length ?? 0);
  const low = Number(metrics.low_confidence ?? metrics.low_confidence_count ?? decisions.filter(d => Number(d.scope_confidence ?? d.confidence) < .5).length ?? 0);
  const narrowed = Number(metrics.narrowed ?? metrics.narrowed_count ?? 0);
  const undone = Number(metrics.undone ?? metrics.undone_count ?? decisions.filter(d => d.status === 'undone' || d.undone).length ?? 0);
  const rows = decisions.slice(-20).reverse().map(item => {
    const id = item.decision_id || item.event_id || '';
    const confidence = item.scope_confidence ?? item.confidence;
    const canUndo = !!(item.undo_id || id) && item.status !== 'undone' && !item.undone;
    return `<div class="rule-decision-row"><div><strong>${escapeHtml(item.action || 'auto_scope')}</strong>
      <span class="chip chip-info">${escapeHtml((item.rule_id || item.memory_id || '').slice(0, 14))}</span>
      ${ruleConfidenceLabel(confidence)}</div>
      <div class="muted">${escapeHtml(item.scope_reason || item.reason || '未提供原因')} · ${escapeHtml(item.created_at || '')}</div>
      <div class="finding-actions"><code>${escapeHtml(id)}</code>${canUndo ? `<button class="btn btn-danger btn-icon" type="button" onclick="undoRuleDecision('${escapeHtml(id)}')">撤销自动决定</button>` : '<span class="chip chip-medium">已撤销</span>'}</div></div>`;
  }).join('');
  return `<section class="card rule-cockpit-panel"><div class="card-head"><div><h2>自动范围决策</h2><p>范围只从当前 Agent / 项目上下文推导；低置信度不会扩大到共享组或系统。</p></div>
    <div class="chips"><span class="chip chip-info">自动决定 ${total}</span><span class="chip ${low ? 'chip-high' : 'chip-confirmed'}">低置信度 ${low}</span><span class="chip chip-info">已收窄 ${narrowed}</span><span class="chip chip-info">已撤销 ${undone}</span></div></div>
    ${rows || '<p class="muted">暂无自动范围决定。</p>'}</section>`;
}

function renderRuleCreatePanel(options) {
  const agents = options.agents || [];
  const groups = options.groups || [];
  // Preview controls are diagnostic-only.  Never copy preview Agent/project
  // into the creation context or its read-only display.
  const selectedAgent = activeAgentInstanceId || '';
  const selectedAgentLabel = agents.find(item => item.id === selectedAgent)?.label || agents.find(item => item.id === selectedAgent)?.name || selectedAgent || '当前 Agent';
  const selectedGroupId = activeShareGroupId || 'default';
  const selectedGroupLabel = groups.find(item => item.id === selectedGroupId)?.label || groups.find(item => item.id === selectedGroupId)?.name || selectedGroupId;
  const result = ruleCreateResult;
  const resultHtml = result ? `<div class="rule-create-result ${result.error ? 'error' : ''}">
    ${result.error ? `<strong>未创建：${escapeHtml(result.error)}</strong>` : `<strong>已创建规则 ${escapeHtml(result.rule_id || result.memory_id || '')}</strong>
      <div class="chips">${result.kind ? `<span class="chip chip-info">分类 ${escapeHtml(result.kind)}</span>` : ''}${ruleConfidenceLabel(result.scope_confidence)}${result.decision_id ? `<span class="chip chip-info">决定 ${escapeHtml(result.decision_id.slice(0, 14))}</span>` : ''}</div>
      <p>${escapeHtml(result.scope_reason || result.blocked_reason || '范围由当前上下文确定')}</p>`}
  </div>` : '';
  // v2: 日常新增规则零额外选择。  Agent、共享组与项目是只读上下文标签，
  // 不作为可选项暴露给用户；服务端只读取当前可信上下文自动定范围。
  return `<section class="card rule-create-panel"><div class="card-head"><div><h2>一句话新增规则</h2><p>规则范围由当前可信上下文自动判断，无需手动选择。系统范围与跨 Agent 范围不会由自动流程创建。</p></div></div>
    <textarea id="rule-create-text" rows="3" maxlength="12000" placeholder="例如：所有 Unity UI 修复先补 EditMode 回归测试"></textarea>
    <div class="rule-context-readonly"><span class="chip chip-info">当前上下文：${escapeHtml(selectedAgentLabel)} · ${escapeHtml(selectedGroupLabel)}</span></div>
    <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="createRuleFromText()">分析并创建</button><span class="muted">系统只自动写入当前 Agent / 当前 Agent + 项目范围，绝不扩大。</span></div>${resultHtml}</section>`;
}

async function createRuleFromText() {
  const text = String(document.getElementById('rule-create-text')?.value || '').trim();
  if (!text) return showToast('请输入规则正文。', 'error');
  ruleCreateResult = null;
  try {
    // Creation intentionally sends text only.  The backend derives Agent and
    // project from its trusted runtime context; diagnostic preview state never
    // crosses this boundary.
    const result = await callApi('create_rule_from_text', text);
    ruleCreateResult = result || {error: 'empty_service_response'};
    if (result?.error || result?.ok === false) showToast(result.error || '规则未创建', 'error');
    else showToast('规则已创建，正在刷新范围与回执。', 'success');
    await renderRulesHabits();
  } catch (error) {
    ruleCreateResult = {error: error.message || String(error)};
    showToast(`规则创建失败：${error.message || error}`, 'error');
    await renderRulesHabits();
  }
}

async function undoRuleDecision(decisionId) {
  if (!decisionId || !confirm('确认撤销这条自动范围决定？')) return;
  try {
    const result = await callApi('undo_rule_decision', decisionId, activeShareGroupId || 'default', true, {
      // Preview selectors are diagnostic-only and cannot authorize undo.
      agent_instance_id: activeAgentInstanceId || '',
      share_group_id: activeShareGroupId || 'default',
      project_ref: '',
    });
    if (result?.error || result?.ok === false) throw new Error(result.error || '撤销失败');
    showToast('自动范围决定已撤销。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`撤销失败：${error.message || error}`, 'error'); }
}

async function submitRuleFeedback(receiptId, outcome) {
  if (!receiptId || !outcome) return;
  if (outcome === 'exception') return openRuleExceptionFeedbackModal(receiptId);
  // Evidence is optional; keep the one-click feedback action free of native
  // prompt dialogs so the desktop and localhost surfaces behave identically.
  const evidence = '';
  try {
    // Backend fixes producer/source=user and actor=user; no Agent identity
    // comes from diagnostic UI state.
    const result = await callApi('submit_rule_feedback', receiptId, outcome, '', evidence, activeShareGroupId || 'default', 1.0);
    if (result?.error || result?.ok === false) throw new Error(result.error || '反馈提交失败');
    showToast('反馈已记录。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`反馈提交失败：${error.message || error}`, 'error'); }
}

function openRuleExceptionFeedbackModal(receiptId) {
  if (!receiptId) return;
  document.getElementById('rule-exception-feedback-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'rule-exception-feedback-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="提交规则例外">
    <div class="modal-head"><h3>记录规则例外</h3><p>当前项目应遵循什么替代规则？只填写替代规则正文，提交后由治理层创建可撤销例外。</p></div>
    <div class="modal-body"><label class="field"><span>替代规则正文</span><textarea id="rule-exception-override" rows="5" maxlength="6000" placeholder="例如：仅在本项目的测试目录中允许……"></textarea></label><div id="rule-exception-feedback-error" class="feedback-error" role="alert" aria-live="polite"></div></div>
    <div class="modal-actions"><button class="btn" type="button" onclick="document.getElementById('rule-exception-feedback-modal')?.remove()">取消</button><button class="btn btn-primary" type="button" onclick="submitRuleExceptionFeedback('${escapeHtml(receiptId)}')">提交例外</button></div>
  </div>`;
  modal.addEventListener('click', event => { if (event.target === modal) modal.remove(); });
  document.body.appendChild(modal);
  document.getElementById('rule-exception-override')?.focus?.();
}

async function submitRuleExceptionFeedback(receiptId) {
  const input = document.getElementById('rule-exception-override');
  const errorNode = document.getElementById('rule-exception-feedback-error');
  const override = String(input?.value || '').trim();
  if (!override) {
    if (errorNode) errorNode.textContent = '请填写替代规则正文。';
    showToast('替代规则正文不能为空。', 'error');
    return;
  }
  try {
    // The GUI bridge fixes producer=user and validates receipt ownership.  The
    // override is passed as evidence because the service treats it as the
    // atomic child-rule body for an exception outcome.
    const result = await callApi('submit_rule_feedback', receiptId, 'exception', '', override, activeShareGroupId || 'default', 1.0);
    if (result?.error || result?.ok === false) throw new Error(result.error || '例外提交被治理层阻断');
    document.getElementById('rule-exception-feedback-modal')?.remove();
    showToast('例外已记录并创建替代规则。', 'success');
    await renderRulesHabits();
  } catch (error) {
    const message = error.message || String(error);
    if (errorNode) errorNode.textContent = `治理层阻断：${message}`;
    showToast(`例外提交失败：${message}`, 'error');
  }
}

async function createChildException(parentRule) {
  if (!parentRule) return;
  document.getElementById('rule-exception-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'rule-exception-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="新增子例外">
    <div class="modal-head"><h3>新增子例外</h3><p>父规则：<code>${escapeHtml(parentRule)}</code>。子规则必须不同，且原因必填。</p></div>
    <div class="modal-body"><label class="field"><span>子例外规则 ID</span><input id="rule-exception-child" maxlength="160" /></label>
      <label class="field"><span>优先级（-100 到 100）</span><input id="rule-exception-priority" type="number" min="-100" max="100" value="0" /></label>
      <label class="field"><span>例外原因</span><textarea id="rule-exception-reason" rows="3" maxlength="2000"></textarea></label></div>
    <div class="modal-actions"><button class="btn" type="button" onclick="document.getElementById('rule-exception-modal')?.remove()">取消</button><button class="btn btn-primary" type="button" onclick="submitChildException('${escapeHtml(parentRule)}')">创建</button></div>
  </div>`;
  modal.addEventListener('click', event => { if (event.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function submitChildException(parentRule) {
  const child = String(document.getElementById('rule-exception-child')?.value || '').trim();
  const priorityRaw = String(document.getElementById('rule-exception-priority')?.value || '0');
  const priority = Number.isFinite(Number(priorityRaw)) ? Number(priorityRaw) : 0;
  const reason = String(document.getElementById('rule-exception-reason')?.value || '').trim();
  if (!child) return showToast('请填写子例外规则 ID。', 'error');
  if (!reason) return showToast('请填写例外原因。', 'error');
  document.getElementById('rule-exception-modal')?.remove();
  try {
    const result = await callApi('create_child_exception', parentRule, child.trim(), priority, reason.trim(), activeShareGroupId || 'default', true);
    if (result?.error || result?.ok === false) throw new Error(result.error || '创建例外失败');
    showToast('子例外已创建。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`创建子例外失败：${error.message || error}`, 'error'); }
}

async function revokeRuleException(exceptionId) {
  if (!exceptionId || !confirm('确认撤销这条子例外？')) return;
  try {
    const result = await callApi('revoke_rule_exception', exceptionId, activeShareGroupId || 'default', true);
    if (result?.error || result?.ok === false) throw new Error(result.error || '撤销例外失败');
    showToast('子例外已撤销。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`撤销例外失败：${error.message || error}`, 'error'); }
}

function ruleCard(record) {
  const state = rulePreviewState(record);
  const range = (record.assignments || []).map(a => a.target_type);
  if (ruleRangeFilter !== 'all' && !range.includes(ruleRangeFilter)) return '';
  if (rulePreviewAgentId && ruleVisibilityFilter !== 'all' && record.injection_policy === 'always' && state !== ruleVisibilityFilter) return '';
  const preview = record._preview || {};
  const stateLabel = state === 'effective' ? '当前 Agent 生效'
    : state === 'excluded' ? '当前 Agent 被排除' : state === 'unavailable' ? '当前 Agent 不适用' : '';
  const stateChip = state === 'effective' ? 'chip-confirmed' : (state === 'excluded' ? 'chip-high' : 'chip-info');
  const sources = state === 'effective' ? `来源：${(preview.matched_sources || []).join('；')}`
    : state === 'excluded' ? `排除原因：${(preview.excluded_sources || []).join('；')}` : '';
  const editable = record.injection_policy === 'always' || record.status === 'active';
  const decision = ruleDecisionFor(record);
  const stats = ruleStatsFor(record);
  const receipts = ruleReceiptsFor(record);
  const exceptions = ruleExceptionsFor(record);
  const confidence = decision?.scope_confidence ?? record.scope_confidence ?? record.auto_scope_confidence ?? record.confidence;
  const decisionId = decision?.decision_id || record.decision_id || '';
  const exceptionHtml = exceptions.length ? `<div class="rule-exceptions"><div class="muted">子例外（父规则：${escapeHtml(record.memory_id)}）</div>${exceptions.map(item => `<div class="rule-exception-row"><code>${escapeHtml(item.child_exception || item.child_rule_id || '')}</code><span class="chip chip-info">priority ${Number(item.priority || 0)}</span><span class="muted">${escapeHtml(item.reason || '')}</span>${item.active === false ? '<span class="chip chip-medium">已撤销</span>' : `<button class="btn btn-danger btn-icon" type="button" onclick="revokeRuleException('${escapeHtml(item.exception_id || '')}')">撤销</button>`}</div>`).join('')}</div>` : '';
  // Pure parent/child relation editing is an administrator diagnostic path.
  // Daily governance uses the receipt-level “例外” action above, which carries
  // the actual project override and enters the atomic service path.
  const advancedHtml = `<details class="rule-advanced"><summary>诊断与高级治理（管理员）</summary><div class="rule-advanced-body"><p class="muted">仅用于排查已有关系；日常例外请从命中回执提交替代规则。</p><div class="finding-actions"><button class="btn" type="button" onclick="createChildException('${escapeHtml(record.memory_id)}')">新增子例外关系</button></div>${exceptionHtml}</div></details>`;
  const receiptHtml = receipts.slice(-3).map(renderRuleReceiptActions).join('');
  return `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(displayTitle(record))}</strong>
    <span class="chip ${record.injection_policy === 'always' ? 'chip-confirmed' : ''}">${record.injection_policy === 'always' ? '强制' : '按需'}</span>
    ${stateLabel ? `<span class="chip ${stateChip}">${escapeHtml(stateLabel)}</span>` : ''}</div>
    <p>${escapeHtml(displayBody(record)).slice(0, 300)}</p>
    <div class="muted">适用范围：${escapeHtml(ruleAudience(record))}</div>
    ${record.injection_policy === 'always' ? `<div class="muted">基础优先级：${Number(record.priority || 0)}${sources ? ` · ${escapeHtml(sources)}` : ''}</div>` : ''}
    ${confidence !== undefined ? `<div class="chips">${ruleConfidenceLabel(confidence)}${decision?.scope_reason || record.scope_reason ? `<span class="muted">${escapeHtml(decision?.scope_reason || record.scope_reason)}</span>` : ''}</div>` : ''}
    ${stats ? `<div class="muted">范围命中：${Number(stats.total || 0)} · 遵循 ${Number(stats.accepted || 0)} · 纠正 ${Number(stats.corrected || 0)} · 作用域错误 ${Number(stats.wrong_scope || 0)}</div>` : ''}
    ${decisionId ? `<div class="rule-decision-link"><code>decision ${escapeHtml(decisionId)}</code>${decision?.undo_id || decision?.status !== 'undone' ? `<button class="btn btn-danger btn-icon" type="button" onclick="undoRuleDecision('${escapeHtml(decisionId)}')">撤销自动决定</button>` : '<span class="chip chip-medium">已撤销</span>'}</div>` : ''}
    ${receiptHtml ? `<div class="rule-receipts"><div class="muted">命中回执与反馈</div>${receiptHtml}</div>` : ''}
    <div class="finding-actions">${editable ? `<button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(record.memory_id)}">管理适用范围</button>` : ''}</div>
    ${advancedHtml}
  </article>`;
}

async function setRulePreviewAgent(agentId) {
  rulePreviewAgentId = agentId || '';
  await renderRulesHabits();
}

async function setRulePreviewProject(projectRef) {
  rulePreviewProjectRef = projectRef || '';
  await renderRulesHabits();
}

async function setRulePreviewProvider(provider) {
  rulePreviewProvider = provider || '';
  await renderRulesHabits();
}

async function setRulePreviewRuntimeRole(runtimeRole) {
  rulePreviewRuntimeRole = runtimeRole || '';
  await renderRulesHabits();
}

async function setRuleVisibilityFilter(value) {
  ruleVisibilityFilter = value || 'all';
  await renderRulesHabits();
}

async function setRuleRangeFilter(value) {
  ruleRangeFilter = value || 'all';
  await renderRulesHabits();
}

function removeRuleAudienceModal() {
  document.getElementById('rule-audience-modal')?.remove();
}

function ruleTargetControls(type, assignment = {}) {
  const options = ruleScopeOptions || {};
  const select = (name, list, selected, label) => `<label class="field"><span>${label}</span><select id="rule-audience-${name}"><option value="">请选择</option>${ruleSelectOptions(list, selected || '')}</select></label>`;
  if (type === 'agent') return select('target', options.agents, assignment.target_id, 'Agent');
  if (type === 'group') return select('target', options.groups, assignment.target_id, '共享组');
  if (type === 'project') return select('project', options.projects, assignment.project_ref || assignment.target_id, '项目');
  if (type === 'agent_project') return `${select('target', options.agents, assignment.target_id, 'Agent')}${select('project', options.projects, assignment.project_ref, '项目')}`;
  if (type === 'provider') return select('target', options.providers, assignment.target_id, '宿主');
  if (type === 'runtime_role') return select('target', options.runtime_roles, assignment.target_id, '运行角色');
  return '<p class="muted">系统范围不需要额外目标；它会对所有可信宿主生效。</p>';
}

function refreshRuleAudienceTarget() {
  const type = document.getElementById('rule-audience-type')?.value || 'agent';
  const box = document.getElementById('rule-audience-targets');
  if (box) box.innerHTML = ruleTargetControls(type);
}

function openRuleAudienceEditor(memoryId) {
  const record = ruleRecordsById.get(memoryId);
  if (!record || !ruleScopeOptions) return;
  removeRuleAudienceModal();
  const legacyIds = new Set(record.legacy_unknown_assignment_ids || []);
  const assignmentRows = (record.assignments || []).map((item, index) => {
    const legacy = legacyIds.has(item.assignment_id);
    return `<label class="raw-file-row" style="grid-template-columns:auto 1fr;align-items:start"><input type="checkbox" data-rule-assignment="${index}" checked><span>${escapeHtml(ruleAudience({assignments:[item]}))}${legacy ? ' · legacy_unknown（仅可保留或删除，不能新增此目标）' : ''}</span></label>`;
  }).join('') || '<p class="muted">尚未配置适用范围。若设为强制，至少新增一个“包含”范围。</p>';
  const priority = Number.isInteger(record.priority) ? record.priority : 0;
  const modal = document.createElement('div');
  modal.id = 'rule-audience-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="管理规则适用范围">
    <div class="modal-head"><h3>管理规则适用范围</h3><p>删除范围不会删除记忆。只有“强制”规则才会按范围注入。</p></div>
    <div class="modal-body">
      <label class="field"><span>注入策略</span><select id="rule-audience-policy"><option value="relevant" ${record.injection_policy === 'relevant' ? 'selected' : ''}>按需召回</option><option value="always" ${record.injection_policy === 'always' ? 'selected' : ''}>强制规则</option></select></label>
      <label class="field"><span>基础优先级</span><select id="rule-audience-priority">${[-100, -50, 0, 50, 100].map(value => `<option value="${value}" ${value === priority ? 'selected' : ''}>${value}</option>`).join('')}</select></label>
      <h4>已有适用范围</h4><div id="rule-existing-assignments">${assignmentRows}</div>
      <h4>新增适用范围</h4>
      <label class="field"><span>作用</span><select id="rule-audience-effect"><option value="include">包含</option><option value="exclude">排除</option></select></label>
      <label class="field"><span>范围类型</span><select id="rule-audience-type" onchange="refreshRuleAudienceTarget()">${['agent','group','project','agent_project','provider','runtime_role','system'].map(type => `<option value="${type}">${escapeHtml({agent:'Agent',group:'共享组',project:'项目',agent_project:'Agent + 项目',provider:'宿主',runtime_role:'运行角色',system:'系统'}[type])}</option>`).join('')}</select></label>
      <div id="rule-audience-targets">${ruleTargetControls('agent')}</div>
      <label class="field"><span>覆盖优先级</span><select id="rule-audience-override"><option value="">不覆盖</option>${[-100, -50, 0, 50, 100].map(value => `<option value="${value}">${value}</option>`).join('')}</select></label>
      <label class="raw-file-row" style="grid-template-columns:auto 1fr"><input type="checkbox" id="rule-audience-add"><span>保存时加入这条新范围</span></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-mg-action="rule-modal-close">取消</button><button class="btn btn-primary" type="button" data-mg-action="rule-save" data-memory-id="${escapeHtml(memoryId)}">确认更新</button></div>
  </div>`;
  document.body.appendChild(modal);
}

async function ensureRuleAudienceEditor(memoryId) {
  const groupId = activeShareGroupId || 'default';
  try {
    if (!ruleScopeOptions) ruleScopeOptions = await callApi('get_rule_scope_options', groupId);
    if (!ruleScopeOptions || ruleScopeOptions.error) throw new Error(ruleScopeOptions?.error || '无法读取规则范围');
    if (!ruleRecordsById.has(memoryId)) {
      const data = await callApi('list_rules_habits', groupId);
      if (data.error) throw new Error(data.error);
      ruleRecordsById = new Map(Object.values(data.buckets || {}).flat().map(record => [record.memory_id, record]));
    }
    openRuleAudienceEditor(memoryId);
  } catch (error) { showToast(`无法打开规则范围：${error.message || error}`, 'error'); }
}

async function saveRuleAudience(memoryId) {
  const record = ruleRecordsById.get(memoryId);
  if (!record) return;
  const policy = document.getElementById('rule-audience-policy')?.value || 'relevant';
  let assignments = (record.assignments || []).filter((_, index) => document.querySelector(`[data-rule-assignment="${index}"]`)?.checked).map(item => ({...item}));
  // relevant has no audience relation by definition.  The server performs the
  // policy flip and assignment cleanup in one transaction after confirmation.
  if (policy === 'relevant') assignments = [];
  if (policy !== 'relevant' && document.getElementById('rule-audience-add')?.checked) {
    const type = document.getElementById('rule-audience-type')?.value || '';
    const targetId = document.getElementById('rule-audience-target')?.value || '';
    const projectRef = document.getElementById('rule-audience-project')?.value || '';
    const effect = document.getElementById('rule-audience-effect')?.value || 'include';
    const priorityValue = document.getElementById('rule-audience-override')?.value || '';
    const targetRequired = ['agent', 'group', 'provider', 'runtime_role'].includes(type);
    if ((targetRequired && !targetId) || (type === 'project' && !projectRef) || (type === 'agent_project' && (!targetId || !projectRef))) {
      showToast('请选择已发现的适用对象；没有可选对象时不能保存。', 'error');
      return;
    }
    assignments.push({target_type: type, target_id: targetId, project_ref: projectRef, effect, priority_override: priorityValue === '' ? null : Number(priorityValue)});
  }
  if (policy === 'always' && !assignments.some(item => item.effect === 'include')) {
    showToast('强制规则至少需要一个“包含”适用范围。', 'error');
    return;
  }
  if (!confirm(policy === 'relevant' ? '切回按需召回并清理适用范围？记忆正文不会删除。' : '确认更新这条规则的适用范围？')) return;
  try {
    const priority = Number(document.getElementById('rule-audience-priority')?.value || 0);
    const result = await callApi('update_rule_audience', memoryId, assignments, activeShareGroupId || 'default', policy, priority, true);
    if (result.error || result.ok === false) throw new Error(result.error || '更新失败');
    removeRuleAudienceModal();
    showToast(result.message || '适用范围已更新。', 'success');
    if (state.activeTab === 'neurons') await refreshNeuronRuleGovernance(memoryId);
    else await renderRulesHabits();
  } catch (error) { showToast(`规则范围更新失败：${error.message || error}`, 'error'); }
}

async function renderRulesHabits() {
  setContent('<div class="loading">正在读取规则与习惯…</div>');
  try {
    const groupId = activeShareGroupId || 'default';
    const [data, options, decisions, metrics, receipts, exceptions] = await Promise.all([
      callApi('list_rules_habits', groupId), callApi('get_rule_scope_options', groupId),
      callApiOptional('list_rule_decisions', {decisions: [], total: 0}, groupId, 50),
      callApiOptional('get_rule_auto_scope_metrics', {stats: [], auto_scope: {}}, groupId),
      callApiOptional('list_rule_match_receipts', {receipts: [], total: 0}, groupId, '', activeAgentInstanceId, 50),
      callApiOptional('list_rule_exceptions', {exceptions: [], total: 0}, groupId, ''),
    ]);
    if (data.error) throw new Error(data.error);
    if (options.error) throw new Error(options.error);
    ruleDecisionRows = Array.isArray(decisions) ? decisions : (decisions.decisions || decisions.items || []);
    ruleScopeMetrics = metrics || {};
    ruleReceiptRows = Array.isArray(receipts) ? receipts : (receipts.receipts || receipts.items || []);
    ruleExceptionRows = Array.isArray(exceptions) ? exceptions : (exceptions.exceptions || exceptions.items || []);
    ruleScopeOptions = options;
    const agents = options.agents || [];
    const projects = options.projects || [];
    const providers = options.providers || [];
    const runtimeRoles = options.runtime_roles || [];
    if (!agents.some(item => item.id === rulePreviewAgentId)) {
      rulePreviewAgentId = agents.some(item => item.id === activeAgentInstanceId) ? activeAgentInstanceId : '';
    }
    if (!projects.some(item => item.id === rulePreviewProjectRef)) rulePreviewProjectRef = '';
    if (!providers.some(item => item.id === rulePreviewProvider)) rulePreviewProvider = '';
    if (!runtimeRoles.some(item => item.id === rulePreviewRuntimeRole)) rulePreviewRuntimeRole = '';
    rulePreviewById = new Map();
    const labels = {mandatory: '强制规则', preferences: '长期习惯与偏好', procedures: '工作流程', corrections: '纠错与禁忌', projects: '项目决策'};
    const allRecords = Object.values(data.buckets || {}).flat();
    ruleRecordsById = new Map(allRecords.map(record => [record.memory_id, record]));
    if (rulePreviewAgentId) {
      const preview = await callApi(
        'preview_effective_rules', rulePreviewAgentId, groupId,
        rulePreviewProjectRef, rulePreviewProvider, rulePreviewRuntimeRole,
      );
      if (preview.error) throw new Error(preview.error);
      (preview.effective || []).forEach(item => { rulePreviewById.set(item.memory_id, 'effective'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
      (preview.excluded || []).forEach(item => { rulePreviewById.set(item.memory_id, 'excluded'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
      (preview.unavailable || []).forEach(item => { rulePreviewById.set(item.memory_id, 'unavailable'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
    }
    const blocks = Object.entries(labels).map(([key, label]) => {
      const cards = ((data.buckets || {})[key] || []).map(ruleCard).filter(Boolean).join('');
      if (!cards) return '';
      return `<section class="card" style="margin-bottom:12px"><div class="section-head"><h3>${label}</h3></div>${cards}</section>`;
    }).join('');
    const agentOptions = `<option value="">全部范围（不做有效性预览）</option>${ruleSelectOptions(agents, rulePreviewAgentId)}`;
    const projectOptions = `<option value="">未确认项目（不猜测）</option>${ruleSelectOptions(projects, rulePreviewProjectRef)}`;
    const providerOptions = `<option value="">未确认宿主</option>${ruleSelectOptions(providers, rulePreviewProvider)}`;
    const roleOptions = `<option value="">未确认运行角色</option>${ruleSelectOptions(runtimeRoles, rulePreviewRuntimeRole)}`;
    const scopeOptions = `<option value="all">全部范围类型</option>${['agent','group','project','agent_project','provider','runtime_role','system'].map(type => `<option value="${type}" ${ruleRangeFilter === type ? 'selected' : ''}>${escapeHtml({agent:'Agent',group:'共享组',project:'项目',agent_project:'Agent + 项目',provider:'宿主',runtime_role:'运行角色',system:'系统'}[type])}</option>`).join('')}`;
    const visibilityOptions = `<option value="effective" ${ruleVisibilityFilter === 'effective' ? 'selected' : ''}>仅当前 Agent 生效</option><option value="excluded" ${ruleVisibilityFilter === 'excluded' ? 'selected' : ''}>仅当前 Agent 被排除</option><option value="all" ${ruleVisibilityFilter === 'all' ? 'selected' : ''}>不按当前 Agent 过滤</option>`;
    // v2: the six diagnostic filters are hidden behind a closed <details> so
    // they never enter the first-viewport tab order and never participate in
    // a routine "add rule" request.
    const diagnosticFilters = `<section class="card"><details id="rule-diagnostics"><summary class="card-head" style="cursor:pointer"><div><h2>诊断与高级筛选</h2><p>管理员或排查时才需要调整；日常使用保持默认值。</p></div></summary>
      <div class="page-actions"><label class="field"><span>预览 Agent</span><select onchange="setRulePreviewAgent(this.value)">${agentOptions}</select></label><label class="field"><span>项目</span><select onchange="setRulePreviewProject(this.value)">${projectOptions}</select></label><label class="field"><span>宿主</span><select onchange="setRulePreviewProvider(this.value)">${providerOptions}</select></label><label class="field"><span>运行角色</span><select onchange="setRulePreviewRuntimeRole(this.value)">${roleOptions}</select></label><label class="field"><span>显示</span><select onchange="setRuleVisibilityFilter(this.value)">${visibilityOptions}</select></label><label class="field"><span>范围</span><select onchange="setRuleRangeFilter(this.value)">${scopeOptions}</select></label></div><p class="muted">预览只使用已发现/可信上下文；未知项目、宿主或角色保持空值，不会猜测命中规则。</p></details></section>`;
    setContent(`<div class="page-head"><div><h1>规则与习惯</h1><p>规则受众独立于记忆来源。范围删除不会删除记忆；只有强制规则会按范围注入。</p></div></div>
      ${renderRuleCreatePanel(options)}${renderRuleAutoScopePanel()}
      ${diagnosticFilters}${blocks || '<div class="card empty-state"><p>当前筛选范围没有规则或习惯。</p></div>'}`);
  } catch (e) { setContent(`<div class="card empty-state"><p>规则加载失败：${escapeHtml(e)}</p></div>`); }
}

function historyBytes(value) {
  const n = Number(value || 0);
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function renderHistoryBackfillPanel(inventory) {
  const sources = (inventory && inventory.sources) || [];
  const groups = new Map();
  sources.forEach(source => {
    const status = source.status || (source.supported ? 'importable' : 'unsupported');
    const key = [source.provider || 'unknown', status, source.matched_agent_id || '', source.support_reason || ''].join('|');
    const current = groups.get(key) || { ...source, status, file_count: 0, byte_count: 0, path_count: 0 };
    current.file_count += Number(source.file_count || 0);
    current.byte_count += Number(source.byte_count || 0);
    current.path_count += 1;
    groups.set(key, current);
  });
  const grouped = Array.from(groups.values());
  const rows = grouped.map(source => {
    const status = source.status || (source.supported ? 'importable' : 'unsupported');
    const label = { importable: '可导入', complete: '已完成', partial: '部分导入（会话已索引）', pending_binding: '待绑定 Agent', unsupported: '暂不支持', error: '扫描失败' }[status] || status;
    const tone = status === 'importable' ? 'chip-confirmed' : (status === 'unsupported' || status === 'pending_binding' || status === 'partial' ? 'chip-medium' : 'chip-high');
    const bound = source.matched_agent_id ? ` → ${source.matched_agent_id}` : '';
    const location = source.path_count > 1 ? `${source.path_count} 个本地路径` : (source.path || '');
    return `<tr><td><strong>${escapeHtml(source.provider || 'unknown')}</strong></td><td class="path-cell">${escapeHtml(location)}</td><td>${Number(source.file_count || 0)}</td><td>${historyBytes(source.byte_count)}</td><td><span class="chip ${tone}">${escapeHtml(label)}</span><div class="surface-meta">${escapeHtml(source.support_reason || '')}${escapeHtml(bound)}</div></td></tr>`;
  }).join('') || '<tr><td colspan="5" class="empty-note">未发现可识别的本地会话来源。</td></tr>';
  const canImport = sources.some(source => source.status === 'importable' && source.matched_agent_id);
  return `<section class="card" id="history-backfill"><div class="card-head"><div><h2>扫描旧会话</h2><p>只扫描本机可读日志。导入会写入独立历史库，不会写入长期记忆或注入当前对话。</p></div><div class="finding-actions"><button class="btn" type="button" onclick="renderHistory()">重新扫描</button>${canImport ? `<button class="btn btn-primary" data-mg-action="history-backfill">导入已绑定来源</button>` : ''}</div></div><div class="source-map-table-wrap"><table class="source-map-table"><thead><tr><th>宿主</th><th>来源</th><th>文件</th><th>大小</th><th>状态 / 绑定</th></tr></thead><tbody>${rows}</tbody></table></div><p class="muted">超大日志只会导入有界可见前缀，并标为“部分导入”；专有数据库会明确标为暂不支持，未绑定宿主不会被错误导入到当前 Agent。</p></section>`;
}

async function runHistoryBackfill() {
  if (!confirm('确认导入这一批本机旧会话？原文只进入独立对话历史库，不会写入长期记忆。')) return;
  try {
    const data = await callApi('backfill_local_history', historyBackfillContinuation);
    if (data.error) throw new Error(data.error);
    historyBackfillContinuation = data.continuation || null;
    const errors = Array.isArray(data.errors) ? data.errors.length : Number(data.errors || 0);
    showToast(`旧会话批次完成：导入 ${Number(data.imported || 0)}，跳过 ${Number(data.skipped || 0)}${errors ? `，错误 ${errors}` : ''}`, errors ? 'info' : 'success');
    await renderHistory();
  } catch (e) { showToast(`旧会话导入失败：${e.message || e}`, 'error'); }
}

function historySessionTitle(session) {
  const title = String(session?.title || '').trim();
  if (title && title !== '未命名会话') return title;
  const provider = String(session?.provider || '本地').trim() || '本地';
  const stamp = String(session?.created_at || session?.imported_at || '').trim();
  return stamp ? `${provider} 对话 · ${stamp}` : `${provider} 对话`;
}

function renderHistoryGrouped(data) {
  const groups = new Map();
  for (const session of (data.sessions || [])) {
    const key = session.project_key || 'history-project-unknown';
    if (!groups.has(key)) groups.set(key, {meta: session, agents: new Map()});
    const group = groups.get(key);
    const owner = session.owner_agent_instance_id || session.agent_instance_id || 'unknown-agent';
    if (!group.agents.has(owner)) group.agents.set(owner, []);
    group.agents.get(owner).push(session);
  }
  return [...groups.values()].map(group => {
    const meta = group.meta;
    const status = meta.project_status === 'removed' ? ' · 路径已移除' : '';
    const parent = meta.project_parent ? ` · ${meta.project_parent}` : '';
    const agents = [...group.agents.entries()].map(([owner, sessions]) => {
      const canDelete = !!activeAgentInstanceId && owner === activeAgentInstanceId;
      return `<section class="history-agent-group"><h3>${escapeHtml(owner)}</h3>${sessions.map(s => `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(historySessionTitle(s))}</strong><span class="chip">${escapeHtml(s.provider || 'local')}</span></div><p>${escapeHtml(s.summary || '尚无摘要')}</p><div class="muted">${escapeHtml(s.created_at || s.imported_at || '')} · ${s.turn_count || 0} 条 · ${s.evidence_count || 0} 条已萃取证据</div><div class="finding-actions"><button class="btn" data-mg-action="history-read-session" data-session-id="${escapeHtml(s.session_id)}">阅读原文</button><button class="btn" data-mg-action="history-extract" data-session-id="${escapeHtml(s.session_id)}">萃取预览</button><button class="btn" data-mg-action="history-export" data-session-id="${escapeHtml(s.session_id)}">导出</button>${canDelete ? `<button class="btn" data-mg-action="history-delete" data-session-id="${escapeHtml(s.session_id)}">删除历史</button>` : '<span class="muted">仅会话 owner 可删除</span>'}</div></article>`).join('')}</section>`;
    }).join('');
    return `<section class="card history-project-group"><h2>${escapeHtml(meta.project_label || '未识别项目')}${escapeHtml(status + parent)}</h2>${agents}</section>`;
  }).join('');
}

async function renderHistory() {
  setContent('<div class="loading">正在读取本地对话历史…</div>');
  let inventory = { sources: [] };
  try {
    inventory = await callApi('discover_local_history_sources');
    if (inventory.error) throw new Error(inventory.error);
  } catch (e) {
    setContent(`<div class="card empty-state"><p>旧会话扫描失败：${escapeHtml(e.message || e)}</p></div>`);
    return;
  }
  if (!activeAgentInstanceId && !isShareGroupScope()) {
    setContent(`<div class="page-head"><div><h1>对话历史</h1><p>会话索引可显示在神经图；原文不会进入长期记忆或 bootstrap。</p></div></div>${renderHistoryBackfillPanel(inventory)}<div class="card empty-state"><p>请先在数据源页选择一个 Agent，再查看它的独立对话历史。</p></div>`);
    return;
  }
  try {
    const data = await callApi('list_history_sessions', historyScope(), 50, 0, null, '', '');
    if (data.error) throw new Error(data.error);
    const cards = renderHistoryGrouped(data);
    setContent(`<div class="page-head"><div><h1>对话历史</h1><p>本地证据库，不会自动进入长期记忆或神经图。</p></div><div class="page-actions"><input id="history-search" placeholder="搜索历史" /><button class="btn" data-mg-action="history-search">搜索</button></div></div><section class="card"><p class="muted">检索顺序：搜索摘要 → 附近时间线 → 单条原文。原始对话归属 owner；当前共享组成员可查，仅 owner 可删。</p></section><div id="history-results">${cards || '<div class="card empty-state"><p>当前 Agent 没有对话历史；可从数据源页导入导出包。</p></div>'}</div>`);
    const historyHead = document.querySelector('#content .page-head');
    if (historyHead) {
      const intro = historyHead.querySelector('p');
      if (intro) intro.textContent = '会话索引会显示在神经图；原文不会进入长期记忆或 bootstrap，需在此页按需读取。';
      historyHead.insertAdjacentHTML('afterend', renderHistoryBackfillPanel(inventory));
    }
    if (historyFocusSessionId) {
      const sessionId = historyFocusSessionId;
      historyFocusSessionId = '';
      await readHistorySession(sessionId);
    }
  } catch (e) { setContent(`<div class="card empty-state"><p>对话历史加载失败：${escapeHtml(e)}</p></div>`); }
}

async function searchHistory() {
  const query = document.getElementById('history-search')?.value?.trim();
  if (!query) return;
  const box = document.getElementById('history-results');
  try {
    const data = await callApi('search_history', query, historyScope(), 20, 0);
    if (data.error) throw new Error(data.error);
    box.innerHTML = (data.results || []).map(r => {
      const anchor = r.anchor_turn_id || r.turn_id || '';
      const actions = r.can_timeline && anchor
        ? `<button class="btn" data-mg-action="history-timeline" data-session-id="${escapeHtml(r.session_id)}" data-turn-id="${escapeHtml(anchor)}">查看附近记录</button><button class="btn" data-mg-action="history-read-turn" data-turn-id="${escapeHtml(anchor)}">读取此条原文</button>`
        : `<button class="btn" data-mg-action="history-read-session" data-session-id="${escapeHtml(r.session_id)}">读取所属会话</button>`;
      return `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(r.title || '未命名会话')}</strong><span class="chip">${escapeHtml(r.result_type || 'turn')}</span></div><p>${escapeHtml(r.matched_summary || r.summary || '匹配到历史记录')}</p><div class="muted">${escapeHtml(r.provider || '')} · ${escapeHtml(r.turn_created_at || r.created_at || '')}</div><div class="finding-actions">${actions}</div></article>`;
    }).join('') || '<div class="card empty-state"><p>没有匹配历史。</p></div>';
  } catch (e) { showToast('历史搜索失败：' + e, 'error'); }
}

async function showHistoryTimeline(sessionId, turnId) {
  try {
    const data = await callApi('history_timeline', sessionId, turnId, historyScope(), 4);
    if (data.error) throw new Error(data.error);
    const rows = (data.turns || []).map(t => `<div class="memory-card"><strong>${escapeHtml(t.role)}</strong><p>${escapeHtml(t.content_preview)}</p></div>`).join('');
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><h3>附近时间线</h3>${rows}`;
  } catch (e) { showToast('时间线读取失败：' + e, 'error'); }
}

async function readHistoryTurn(turnId) {
  try {
    const data = await callApi('history_read', '', turnId, historyScope(), 1, 0);
    if (data.error) throw new Error(data.error);
    const t = data.turn || {};
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><article class="memory-card"><strong>${escapeHtml(t.role || '')}</strong><pre class="raw-content">${escapeHtml(t.content || '')}</pre></article>`;
  } catch (e) { showToast('原文读取失败：' + e, 'error'); }
}

async function readHistorySession(sessionId) {
  try {
    const data = await callApi('history_read', sessionId, '', historyScope(), 100, 0);
    if (data.error) throw new Error(data.error);
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button>${(data.turns || []).map(t => `<article class="memory-card"><strong>${escapeHtml(t.role || '')}</strong><pre class="raw-content">${escapeHtml(t.content || '')}</pre></article>`).join('')}`;
  } catch (e) { showToast('会话读取失败：' + e, 'error'); }
}

async function previewHistoryExtract(sessionId) {
  try {
    const data = await callApi('history_extract_preview', sessionId, null, historyScope(), 8);
    if (data.error) throw new Error(data.error);
    const rows = (data.candidates || []).map(c => `<article class="memory-card"><strong>${escapeHtml(c.title)}</strong><p>${escapeHtml(c.body)}</p><div class="muted">证据：${escapeHtml(c.evidence?.turn_id || '')}</div></article>`).join('');
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><p class="muted">以下仅为候选预览，尚未写入长期记忆。</p>${rows || '<div class="card empty-state"><p>没有可萃取内容。</p></div>'}`;
  } catch (e) { showToast('萃取预览失败：' + e, 'error'); }
}

async function exportHistorySession(sessionId) {
  try {
    const data = await callApi('export_history', [sessionId], historyScope());
    if (data.error) throw new Error(data.error);
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `memoryguard-history-${sessionId}.json`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (e) { showToast('历史导出失败：' + e, 'error'); }
}

async function deleteHistorySession(sessionId) {
  if (!confirm('只删除这份历史？关联长期记忆不会自动删除。')) return;
  try {
    // Delete is one atomic provenance action: the backend always tombstones
    // valid evidence before removing raw history, so no cancel branch can
    // leave a valid link whose source no longer exists.
    const data = await callApi('delete_history', [sessionId], historyScope(), true, true);
    if (data.error) throw new Error(data.error);
    showToast('历史已删除；长期记忆未删除。', 'success'); renderHistory();
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function setHostHookMode(provider, agentId, mode) {
  const verb = mode === 'paused' ? '暂停' : (mode === 'observe' ? '观察' : '强制');
  if (!confirm(`确认将 ${provider} 的 MemoryGuard Hook 切换为“${verb}”模式？`)) return;
  const result = await callApi('set_host_hook_mode', provider, agentId, mode, true);
  if (!result || result.error || result.ok === false) {
    return showToast((result && result.error) || 'Hook 模式更新失败', 'error');
  }
  showToast(`Hook 已切换为 ${mode}`, 'success');
  await renderMultiAgentBinding();
}

async function uninstallHostHook(provider) {
  if (!confirm(`确认卸载 ${provider} 的 MemoryGuard 用户级 Hook？\n\n只删除 MemoryGuard 自己的 handler；MCP、规则和其他 Hook 保留。`)) return;
  const result = await callApi('uninstall_host_hook', provider, true);
  if (!result || result.error || result.ok === false) {
    return showToast((result && result.error) || 'Hook 卸载失败', 'error');
  }
  showToast('MemoryGuard Hook 已卸载', 'success');
  await renderMultiAgentBinding();
}

async function createSharedBinding() {
  const checks = document.querySelectorAll('input[type=checkbox][data-agent-id]:checked');
  const agentIds = Array.from(checks).map(c => c.dataset.agentId);
  if (agentIds.length < 2) return showToast('多 Agent 共享组至少需要选择 2 个 Agent', 'error');
  if (!confirm(`确认创建共享组绑定？\n\n· ${agentIds.length} 个 Agent 通过 MemoryGuard MCP 共享同一组记忆\n· 原生记忆模式默认为 redirected（MCP 接管）\n· 创建后建议：导入原生记忆 → 安装 MCP 重定向 → 构建投影 → 确认正式接管\n\n继续？`)) return;
  showToast('正在创建绑定…');
  try {
    const result = await callApi('bind_agents_to_shared_group', agentIds);
    if (result.error) return showToast(result.error, 'error');
    await setActiveShareGroup(result.share_group_id);
    showToast(`已创建共享组，绑定 ${agentIds.length} 个 Agent`, 'success');
    showSharedGroupPreview(result.share_group_id, result.preview);
  } catch (e) { showToast('创建失败：' + e, 'error'); }
}

async function activateShareGroup(groupId) {
  await setActiveShareGroup(groupId);
  showToast(`已切换治理范围到共享组 ${groupId.slice(0, 12)}…`, 'success');
  switchTab('neurons');
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
        <button class="btn btn-primary" type="button" onclick="importNativeMemoriesToGroup('${escapeHtml(groupId)}')">导入原生记忆</button>
        <button class="btn" type="button" onclick="installSharedGroupMcpRedirects('${escapeHtml(groupId)}')">安装 MCP 重定向</button>
        <button class="btn" type="button" onclick="activateShareGroup('${escapeHtml(groupId)}')">设为治理范围并打开神经图</button>
        <button class="btn btn-danger" type="button" onclick="dissolveSharedGroup('${escapeHtml(groupId)}')">解散共享组</button>
        <button class="btn" type="button" onclick="renderMultiAgentBinding()">← 返回多 Agent 模式</button>
      </div>
    </section>`);
}

async function dissolveSharedGroup(groupId) {
  if (!groupId) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`确认解散共享组？\n\n· 解绑组内全部 Agent\n· 删除共享组投影\n· 归档 SharedMemoryStore 目录（可从 shared-memory-archived 找回）\n· 不清空已写入的 MCP 重定向文件（需手动恢复）\n\n继续？`)) return;
  showToast('正在解散共享组…');
  try {
    const result = await callApi('dissolve_shared_group', groupId, true, true);
    if (result.error) return showToast(result.error, 'error');
    if (activeShareGroupId === groupId) {
      activeShareGroupId = '';
      dataPageMode = 'multi_agent_shared_mcp';
    }
    const n = result.unbound_count || 0;
    const arch = result.archived_to ? '，数据已归档' : '';
    showToast(`已解散共享组，解绑 ${n} 个 Agent${arch}`, 'success');
    renderMultiAgentBinding();
  } catch (e) { showToast('解散失败：' + e, 'error'); }
}

async function exitMultiAgentMode() {
  dataPageMode = 'single_agent';
  activeShareGroupId = '';
  try {
    if (activeAgentInstanceId) {
      await callApi('set_governance_scope', {
        mode: 'agent',
        agent_instance_id: activeAgentInstanceId,
      });
    }
  } catch (_) {}
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
  const MEMORY_SELECT_CATS = new Set(['native_memory', 'project_memory']);
  const EXTRACT_DISPLAY_CATS = new Set(['conversation_history', 'runtime_evidence', 'knowledge_source']);
  const extractFiles = [];
  const scopeTabs = `
    <div class="scope-tabs">
      <div class="scope-tab active" data-scope="all" onclick="filterSelectionScope('all')">全部</div>
      <div class="scope-tab" data-scope="user" onclick="filterSelectionScope('user')">全局/用户</div>
      <div class="scope-tab" data-scope="project" onclick="filterSelectionScope('project')">项目</div>
      <div class="scope-tab" data-scope="unknown" onclick="filterSelectionScope('unknown')">未归属</div>
    </div>`;
  const scopeLabels = { user: '全局/用户', project: '项目', unknown: '未归属' };
  const scopeSourceLabels = { profile_declared: 'Profile声明', project_resolver: '项目解析器', fallback: '默认' };

  function collectExtractFiles(categories) {
    for (const cat of categories || []) {
      if (!EXTRACT_DISPLAY_CATS.has(cat.category)) continue;
      for (const f of cat.files || []) extractFiles.push({ ...f, category: cat.category });
    }
  }
  function renderScopeCategories(categories, scope, projectRef) {
    let html = '';
    for (const cat of categories || []) {
      if (EXTRACT_DISPLAY_CATS.has(cat.category)) continue;
      if (!MEMORY_SELECT_CATS.has(cat.category)) continue;
      html += renderSelectionCategory(cat, scope, projectRef);
    }
    return html;
  }

  let treeHtml = '';
  for (const scopeObj of (tree.scopes || [])) {
    const scope = scopeObj.scope;
    const scopeLabel = scopeLabels[scope] || scope;
    const scopeSourceLabel = scopeSourceLabels[scopeObj.scope_source] || scopeObj.scope_source || '';
    const projects = scope === 'project' ? (scopeObj.projects || []) : [
      ...((scopeObj.projects || [])),
    ];
    if (scope !== 'project' && (scopeObj.categories || []).length) {
      collectExtractFiles(scopeObj.categories);
      const catHtml = renderScopeCategories(scopeObj.categories, scope);
      if (catHtml) {
        treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}">
          <div class="finding-header" style="margin:14px 0 8px">
            <span class="finding-rule">${scopeLabel}</span>
            <span class="chip chip-info">${escapeHtml(scopeSourceLabel)}</span>
          </div>${catHtml}</div>`;
      }
    }
    for (const proj of projects) {
      collectExtractFiles(proj.categories);
      const catHtml = renderScopeCategories(proj.categories, scope, proj.project_ref);
      if (!catHtml) continue;
      treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}" data-project="${escapeHtml(proj.project_ref || '')}">
        <div class="finding-header" style="margin:14px 0 8px">
          <span class="finding-rule">${scopeLabel}${proj.project_ref ? ' · ' + escapeHtml(proj.project_ref) : ''}</span>
          <span class="chip chip-info">${escapeHtml(proj.scope_source || scopeObj.scope_source || '')}</span>
        </div>${catHtml}</div>`;
    }
  }

  const extractSection = renderExtractFileSection(extractFiles);
  const notes = (tree.discovery_notes || []).map(n => {
    const chip = n.level === 'warn' ? 'high' : 'info';
    return `<article class="plan-item"><div class="finding-header">
      <span class="finding-rule">${escapeHtml(n.code || 'note')}</span>
      <span class="chip chip-${chip}">${escapeHtml(n.level || 'info')}</span></div>
      <p>${escapeHtml(n.message || '')}</p>
      ${n.hint ? `<div class="surface-meta">${escapeHtml(n.hint)}</div>` : ''}
    </article>`;
  }).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Selection</span><h2>分类勾选授权</h2>
    <p>上方只勾选 Agent 长期记忆。下方会话/知识文档可点开萃取。Skill 与控制面不展示。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>授权摘要</h2><p>Agent：${escapeHtml(tree.product || instanceId)}</p></div></div>
      <div class="row"><span class="key">记忆勾选</span><span>原生记忆 · 项目记忆</span></div>
      <div class="row"><span class="key">可萃取</span><span>会话 · 知识文档（不勾选）</span></div>
      <div class="row"><span class="key">不展示</span><span>Skill · 控制面（规则层，非记忆）</span></div>
    </section>
    ${notes ? `<section class="card"><div class="card-head"><div><h2>发现提示</h2></div></div>${notes}</section>` : ''}
    <section class="card"><div class="card-head"><div><h2>记忆来源勾选</h2></div></div>
      ${scopeTabs}
      ${treeHtml || '<div class="empty-state"><p>未发现可勾选的记忆来源。</p></div>'}
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="confirmSelection('${escapeHtml(instanceId)}')">确认授权</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>
    ${extractSection}`);
}

function renderExtractFileSection(files) {
  if (!files.length) return '';
  const catTitles = { conversation_history: '会话历史', knowledge_source: '知识文档', runtime_evidence: '运行证据' };
  const byCat = {};
  files.forEach(f => { const c = f.category || 'unknown'; (byCat[c] ||= []).push(f); });
  const html = Object.keys(byCat).map(cat => {
    const list = byCat[cat];
    const rows = list.slice(0, 12).map(f => {
      const p = escapeHtml(f.path || '').replaceAll("'", "\\'");
      return `<div class="raw-file-row" style="cursor:pointer;grid-template-columns:1fr auto" onclick="extractSourceFileByPath('${p}')">
        <span><code>${escapeHtml((f.path || '').split(/[/\\\\]/).slice(-2).join('/'))}</code>
          <div class="surface-meta">${escapeHtml(f.session_title || catTitles[cat] || cat)}</div></span>
        <span class="chip chip-info">萃取</span></div>`;
    }).join('');
    return `<div style="margin-bottom:12px"><div class="finding-header"><span class="finding-rule">${escapeHtml(catTitles[cat] || cat)}</span>
      <span class="chip chip-info">${list.length} 个</span></div><div class="raw-file-list">${rows}</div></div>`;
  }).join('');
  return `<section class="card"><div class="card-head"><div><h2>可萃取来源</h2>
    <p>点开进入萃取预览，确认后才写入长期记忆。</p></div></div>${html}</section>`;
}

async function extractSourceFileByPath(absPath) {
  if (!absPath) return;
  try {
    // 优先走已授权 SourceRoot；否则对已发现的会话/证据路径直接预览萃取
    const data = await callApi('list_sources');
    const sources = data.sources || [];
    const full = String(absPath).replace(/\\/g, '/');
    const hit = sources.find(s => {
      const p = String(s.path || '').replace(/\\/g, '/');
      return full.startsWith(p + '/') || full === p;
    });
    if (hit && hit.root_id) {
      const root = String(hit.path).replace(/\\/g, '/');
      const rel = full.startsWith(root + '/') ? full.slice(root.length + 1) : '';
      await extractSourceFile(hit.root_id, rel);
      return;
    }
    const result = await callApi(
      'extract_preview_by_path',
      absPath,
      activeAgentInstanceId || '',
      20,
    );
    if (result.error) return showToast(result.error, 'error');
    showExtractPreviewByPath(absPath, result);
  } catch (e) { showToast('萃取失败：' + e, 'error'); }
}

function showExtractPreviewByPath(absPath, result) {
  const candidates = result.candidates || [];
  if (!candidates.length) {
    showToast('未萃取到可用候选', 'info');
    return;
  }
  const rows = candidates.map(c => `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto">
    <input type="checkbox" data-candidate-id="${escapeHtml(c.candidate_id)}" checked>
    <div><code>${escapeHtml(c.kind || '')}</code><div class="surface-meta">${escapeHtml(c.preview || c.body || '')}</div></div>
    <span class="chip chip-${c.risk_level === 'high' ? 'high' : c.risk_level === 'medium' ? 'medium' : 'info'}">${escapeHtml(c.risk_level || 'low')}</span>
  </label>`).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Extract preview</span><h2>会话/证据萃取预览</h2>
    <p>来自发现路径（无需先勾选授权）。确认后写入共享记忆库。</p></div>
    <section class="card"><div class="card-head"><div><h2>文件</h2></div></div>
      <code style="overflow-wrap:anywhere">${escapeHtml(absPath)}</code></section>
    <section class="card"><div class="card-head"><div><h2>候选 ${candidates.length}</h2></div></div>
      <div class="raw-file-list">${rows}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="acceptExtractByPath('${escapeHtml(result.extract_id)}')">接受所选候选</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>`);
}

async function acceptExtractByPath(extractId) {
  const checks = document.querySelectorAll('input[type=checkbox][data-candidate-id]:checked');
  const ids = Array.from(checks).map(c => c.dataset.candidateId);
  if (!ids.length) return showToast('请至少选择一个候选', 'error');
  const gid = activeShareGroupId || 'default';
  showToast('正在写入候选…');
  try {
    const result = await callApi('accept_candidates', extractId, ids, gid, activeAgentInstanceId || 'document-extractor');
    if (result.error) return showToast(result.error, 'error');
    showToast(`已写入 ${result.accepted || ids.length} 条候选`, 'success');
    renderSources();
  } catch (e) { showToast('写入失败：' + e, 'error'); }
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
      <input type="checkbox" data-selectable="true" data-cat="${escapeHtml(cat.category)}" data-path="${escapeHtml(f.path)}" data-scope="${escapeHtml(fScope)}" data-scope-source="${escapeHtml(scopeSource)}" data-project-ref="${escapeHtml(fProjectRef)}" data-discovery-object-id="${escapeHtml(discoveryId)}" ${checked}>
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
  const checks = document.querySelectorAll('input[type=checkbox][data-selectable="true"]:checked');
  const selected = Array.from(checks).map(c => ({
    category: c.dataset.cat,
    path: c.dataset.path,
    scope: c.dataset.scope || 'project',
    scope_source: c.dataset.scopeSource || 'fallback',
    project_ref: c.dataset.projectRef || '',
    discovery_object_id: c.dataset.discoveryObjectId || ''
  }));
  if (!selected.length) return showToast('请至少勾选一个记忆来源（原生/项目记忆）', 'error');
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
  // 显示名称由后端从已选择路径自动推导，避免重复手工录入。
  const name = '';
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
    const result = await callApi('create_import', path, true, activeAgentInstanceId || 'local-default', '', activeShareGroupId || '');
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

async function removeSourceCard(button) {
  await removeSource(button.dataset.sourceId || '', button.dataset.sourceName || '');
}

async function removeSource(rootId, displayName = '') {
  if (!rootId) return showToast('来源标识缺失，无法删除', 'error');
  const label = displayName || rootId;
  if (!confirm('删除知识库映射“' + label + '”？\n仅移除 MemoryGuard 映射，磁盘中的原文件不会被删除。')) return;
  try {
    const result = await callApi('remove_source', rootId, true);
    if (result.error) return showToast(result.error, 'error');
    if (!result.ok) return showToast('来源不存在或不可删除', 'error');
    showToast('知识库映射已删除，原文件未改动', 'success');
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
    const extractArgs = escapeHtml(JSON.stringify([String(rootId || ''), String(relativePath || '')]));
    setContent(`<div class="view-heading"><span class="eyebrow">Raw memory</span><h2>${escapeHtml(result.display_name)} · ${escapeHtml(relativePath).replaceAll('\\', '/')}</h2>
      <p>原始记忆文件，只读查看。size=${result.size} bytes · lines=${lines}</p></div>
      <section class="card"><div class="card-head"><div>
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
        <button class="btn btn-primary" type="button" onclick="extractSourceFile(...${extractArgs})">萃取为实用记忆</button>
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
  const viewArgs = escapeHtml(JSON.stringify([String(rootId || ''), String(relativePath || '')]));
  setContent(`<div class="view-heading"><span class="eyebrow">Accept</span><h2>写入结果</h2>
    <p>已接受 ${result.total || 0} 条记忆，写入共享组 <code>${escapeHtml(result.share_group_id || 'default')}</code>。</p></div>
    <section class="card"><div class="card-head"><div><h2>写入的记忆</h2></div></div>
      ${itemsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="viewSourceFile(...${viewArgs})">← 返回源文件</button>
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

  setContent(`<div class="view-heading"><span class="eyebrow">Changes</span><h2>变更记录</h2>
    <p>这里只记录 MemoryGuard 治理层的规则修复。原生记忆仅用于扫描与投影，文件保持只读。</p></div>
    ${warningsHtml}
    <section class="card"><div class="card-head"><div><h2>规则级修复</h2><p>对单个 Finding 生成最小补丁，备份后应用，重扫验证，可撤销</p></div></div>
      ${plansHtml}</section>
    <section class="card"><div class="gate-warning" style="margin-top:0">
      <strong>只读边界：</strong>治理结果写入 MemoryGuard 管理层与共享记忆库，不覆盖任何 Agent 原生记忆文件。
    </div></section>`);
}

async function ensurePersonalLayer(agentId) {
  if (!confirm('确认启用该 Agent 的个人记忆层并安装全局 MCP + Hook？\n\n· 创建 MemoryGuard 管理的个人数据库\n· 写入该 Agent 的用户级 MCP、Hook 与记忆规则\n· 原生记忆文件保持只读\n· 已有共享绑定不会被自动切换\n· 安装后按提示重启/信任 Hook')) return;
  const result = await callApi('ensure_personal_memory_group', agentId, true);
  if (result && result.error) return showToast(result.error, 'error');
  const gid = result.group_id || result.share_group_id || (result.binding && result.binding.share_group_id);
  if (gid) {
    const install = await callApi('install_shared_group_mcp_redirects', gid, true);
    if (!install || install.error || install.ok === false) {
      const detail = install && (install.error || `${install.error_count || 0} 失败，${install.skipped_count || 0} 跳过`);
      showToast('个人记忆层已创建，但 MCP 未完整安装：' + (detail || '未知错误'), 'error');
    } else {
      showToast(`个人记忆层与 MCP 已配置；Hook ${install.hook_configured_count || 0} 个。请按提示重启/信任`, 'success');
    }
  } else {
    showToast('个人记忆层返回结果缺少组 ID', 'error');
  }
  if (dataPageMode === 'multi_agent_shared_mcp') await renderMultiAgentBinding(); else await renderSources();
}

async function installMemoryGroupMcp(groupId) {
  const label = memoryGroupLabel(groupId);
  if (!confirm(`确认重新安装${label}的全局 MCP + Hook？\n\n· 会更新用户级 MCP、Hook 与记忆规则\n· 不改写原生记忆文件\n· 完成后按提示重启/信任 Hook`)) return;
  const result = await callApi('install_shared_group_mcp_redirects', groupId, true);
  if (!result || result.error || result.ok === false) {
    const detail = result && (result.error || `${result.error_count || 0} 失败，${result.skipped_count || 0} 跳过`);
    return showToast('MCP 未完整安装：' + (detail || '未知错误'), 'error');
  }
  showToast(`MCP 已配置，Hook ${result.hook_configured_count || 0} 个；请按提示重启/信任`, 'success');
}

async function viewMemoryLayer(groupId) {
  await setActiveShareGroup(groupId);
  switchTab('neurons');
}

async function leaveSharedToPersonal(agentId) {
  if (!confirm('确认退出共享组并回到个人记忆层？\n\n· 两边记忆不会合并或删除\n· 会更新该 Agent 的全局 MCP 规则指向个人层\n· 完成后需要重启对应 Agent')) return;
  const result = await callApi('leave_shared_group_to_personal', agentId, true);
  if (result && result.error) return showToast(result.error, 'error');
  const gid = result.group_id || result.share_group_id || (result.binding && result.binding.share_group_id);
  const install = gid ? await callApi('install_shared_group_mcp_redirects', gid, true) : null;
  if (!install || install.error || install.ok === false) {
    const detail = install && (install.error || `${install.error_count || 0} 失败，${install.skipped_count || 0} 跳过`);
    showToast('已切换到个人记忆层，但 MCP 未完整更新：' + (detail || '缺少个人组 ID'), 'error');
  } else {
    showToast(`已回到个人记忆层并更新 MCP + Hook（${install.hook_configured_count || 0} 个）；请按提示重启/信任`, 'success');
  }
  if (dataPageMode === 'multi_agent_shared_mcp') await renderMultiAgentBinding(); else await renderSources();
}

async function exportMemoryGroup(groupId) {
  if (!confirm(`确认导出${memoryGroupLabel(groupId)}？\n\n导出包包含记忆、事件、治理历史、完整版本快照、binding 清单和来源文件映射；不包含原生文件正文。`)) return;
  const result = await callApi('export_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '导出失败', 'error');
  setContent(`<div class="view-heading"><span class="eyebrow">Memory export</span><h2>记忆层已导出</h2>
    <p>${memoryGroupLabel(groupId)} <code>${escapeHtml(groupId)}</code></p></div>
    <section class="card"><div class="card-head"><div><h2>导出包</h2><p>原生文件未包含，也未被修改。</p></div></div>
      <div class="row"><span class="key">路径</span><code>${escapeHtml(result.export_path || '')}</code></div>
      <div class="row"><span class="key">记录</span><span>${escapeHtml((result.counts && result.counts.records) || 0)}</span></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="copyText(${JSON.stringify(result.export_path || '')})">复制路径</button>
        <button class="btn" type="button" onclick="renderGovernance()">返回治理台</button>
      </div>
    </section>`);
}

async function showMemorySourceMap(groupId) {
  setContent('<div class="loading">正在解析记忆到文件的来源链</div>');
  const result = await callApi('get_memory_source_map', groupId);
  if (!result || result.error) return showToast((result && result.error) || '来源映射失败', 'error');
  const mappings = result.mappings || [];
  const rows = mappings.length ? mappings.map(item => {
    const sources = (item.sources || []).map(source => {
      const location = source.absolute_path || source.relative_path || '无本地文件';
      const state = source.origin_kind === 'local_file'
        ? (source.exists ? '文件存在' : source.path_valid ? '文件已移动/删除' : '路径无效或未授权')
        : 'MCP/衍生来源';
      return `<div class="source-item">
        <div><strong>${escapeHtml(source.display_name || source.origin_kind)}</strong></div>
        <div class="surface-meta">${escapeHtml(state)} · ${escapeHtml(source.scope || '')} · ${escapeHtml(source.locator || '')}</div>
        <code>${escapeHtml(location)}</code>
      </div>`;
    }).join('');
    return `<article class="plan-item">
      <div class="finding-header"><span class="finding-rule">${escapeHtml(item.kind)} · ${escapeHtml(item.memory_id)}</span><span class="chip chip-info">${escapeHtml(item.status)}</span></div>
      <div class="finding-evidence">${escapeHtml(item.body_preview || '')}</div>
      <div class="source-list">${sources}</div>
    </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>当前记忆层为空。</p></div>';
  setContent(`<div class="view-heading"><span class="eyebrow">Source map</span><h2>记忆文件映射</h2>
    <p>${memoryGroupLabel(groupId)} · ${result.total_records || 0} 条记忆 · ${result.file_source_count || 0} 条本地文件来源</p></div>
    <section class="card"><div class="gate-warning" style="margin-top:0"><strong>映射规则：</strong>本地文件只是只读来源；MCP 对话写入没有对应文件；衍生记忆会继续追溯其上游记忆。</div></section>
    <section class="card">${rows}<div class="finding-actions"><button class="btn" type="button" onclick="renderGovernance()">返回治理台</button></div></section>`);
}

async function clearMemoryGroup(groupId) {
  if (!confirm(`确认清空${memoryGroupLabel(groupId)}？\n\n· 系统会先自动导出可恢复 ZIP\n· 清空全部记忆、事件、治理历史与版本\n· 保留 binding、MCP 配置和空数据库\n· 不修改任何原生文件\n\n此操作完成后只能从导出包恢复。`)) return;
  const result = await callApi('clear_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '清空失败', 'error');
  showToast(`记忆层已清空；备份：${result.export_path || ''}`, 'success');
  await loadGovernanceSnapshot();
  renderGovernance();
}

async function archiveMemoryGroup(groupId) {
  if (!confirm(`确认归档删除${memoryGroupLabel(groupId)}？\n\n· 系统会先自动导出 ZIP\n· 解绑组内 Agent 并归档整个数据库目录\n· 原生文件不变\n· 现有 MCP 将因无活动 binding 而拒绝读写，直到重新启用记忆层\n\n继续？`)) return;
  const result = await callApi('archive_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '归档失败', 'error');
  activeShareGroupId = '';
  state.governanceSnapshot = null;
  showToast(`记忆层已归档；导出：${result.export_path || ''}`, 'success');
  renderGovernance();
  renderStatusRail();
}

// ===========================================================================
// 治理台 tab：最近写入 / 覆盖记录 / 冲突队列 / 隔离队列 / 版本回滚
// ===========================================================================

async function renderGovernance() {
  setContent('<div class="loading">正在读取记忆治理组</div>');
  let groups = [];
  try {
    const result = await callApi('list_share_groups');
    groups = result.groups || [];
  } catch (e) {
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>记忆治理组加载失败：${escapeHtml(String(e))}</p></div></div>`);
    return;
  }
  if (activeShareGroupId && !groups.some(g => g.share_group_id === activeShareGroupId)) {
    activeShareGroupId = '';
  }
  const groupOptions = groups.map(g => `<option value="${escapeHtml(g.share_group_id)}" ${g.share_group_id === activeShareGroupId ? 'selected' : ''}>
    ${g.group_kind === 'personal' ? '个人' : '共享'} · ${escapeHtml(g.share_group_id)} · ${g.agent_count || 0} Agent · ${g.active_records || 0} active
  </option>`).join('');
  const groupSelector = groups.length
    ? `<section class="card"><div class="card-head"><div><h2>治理范围</h2><p>选择个人或共享记忆层；所有读取和处置都严格绑定此组。</p></div></div>
        <select class="scope-select" aria-label="选择记忆治理组" onchange="selectGovernanceGroup(this.value)">
          <option value="">请选择记忆层</option>${groupOptions}
        </select></section>`
    : `<section class="card empty-state"><div><div class="empty-orb"></div><p>尚无个人或共享记忆层。</p>
        <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源启用记忆层</button></div></div></section>`;
  if (!activeShareGroupId) {
    setContent(`<div class="view-heading"><span class="eyebrow">Governance</span><h2>治理台</h2>
      <p>请先选择个人或共享记忆层，系统不会再隐式使用 default。</p></div>${groupSelector}`);
    return;
  }
  const tabs = [
    { id: 'records', label: '记忆记录' },
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
    ${groupSelector}
    <section class="card"><div class="card-head"><div><h2>记忆层生命周期</h2>
      <p>导出与映射不会改原生文件；清空保留绑定；归档删除会解绑并使 MCP 安全拒绝。</p></div></div>
      <div class="finding-actions">
        <button class="btn" type="button" onclick="showMemorySourceMap('${escapeHtml(activeShareGroupId)}')">文件映射</button>
        <button class="btn" type="button" onclick="exportMemoryGroup('${escapeHtml(activeShareGroupId)}')">导出记忆层</button>
        <button class="btn btn-danger" type="button" onclick="clearMemoryGroup('${escapeHtml(activeShareGroupId)}')">清空记忆</button>
        <button class="btn btn-danger" type="button" onclick="archiveMemoryGroup('${escapeHtml(activeShareGroupId)}')">归档删除记忆层</button>
      </div>
    </section>
    <div class="scope-tabs">${tabsHtml}</div>
    <div id="governance-content"><div class="loading">正在加载</div></div>`);
  renderGovernanceSub();
}

async function selectGovernanceGroup(groupId) {
  if (!groupId) {
    activeShareGroupId = '';
    state.governanceSnapshot = null;
    renderGovernance();
    renderStatusRail();
    return;
  }
  await setActiveShareGroup(groupId);
  await loadGovernanceSnapshot();
  renderGovernance();
}

function switchGovernanceSub(subTab) {
  governanceSubTab = subTab;
  renderGovernance();
}

function renderGovernanceSub() {
  switch (governanceSubTab) {
    case 'records': renderMemoryRecords(); break;
    case 'recent_events': renderRecentEvents(); break;
    case 'supersede': renderSupersedeChain(); break;
    case 'conflicts': renderConflictQueue(); break;
    case 'quarantine': renderQuarantine(); break;
    case 'rollback': renderRollback(); break;
  }
}

async function renderMemoryRecords() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取记忆记录</div>';
  try {
    const result = await callApi('list_memory', '', '', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const records = result.records || [];
    if (!records.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>当前记忆层没有记录。</p></div></div>';
      return;
    }
    const items = records.map(rec => {
      const isAlways = rec.injection_policy === 'always';
      const active = rec.status === 'active';
      const targetPolicy = isAlways ? 'relevant' : 'always';
      const badge = isAlways ? '强制' : '按需';
      const explanation = isAlways
        ? '强制规则每任务注入'
        : '按需记忆按相关性召回';
      const action = active ? `<button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(rec.memory_id)}">管理注入范围</button>` : '';
      return `<article class="plan-item">
        <div class="finding-header"><span class="finding-rule">${escapeHtml(rec.kind || 'fact')}</span>
          <span class="chip chip-${isAlways ? 'confirmed' : 'medium'}">${badge}</span>
          <span class="chip chip-info">优先级 ${escapeHtml(String(rec.priority ?? 0))}</span>
          <span class="chip chip-medium">${escapeHtml(rec.status || '')}</span></div>
        <div class="finding-evidence">${escapeHtml((rec.body || '').slice(0, 260))}</div>
        <div class="surface-meta">${explanation}${rec.locked ? ' · 已锁定（仍可切换注入策略）' : ''}</div>
        <div class="finding-actions" style="margin-top:10px">${action}</div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>记忆记录</h2>
      <p>强制规则每任务注入；按需记忆按相关性召回。仅 active 记录可切换。</p></div></div>${items}</section>`;
  } catch (e) {
    showToast('加载记忆记录失败：' + e, 'error');
  }
}

async function toggleMemoryInjectionPolicy(memoryId, targetPolicy, priority) {
  const label = targetPolicy === 'always' ? '强制' : '按需';
  if (!confirm(`确认切换为${label}？\n\n强制规则每任务注入；按需记忆按相关性召回。`)) return;
  try {
    const result = await callApi('set_memory_injection_policy', memoryId, targetPolicy, priority, activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    if (result.ok === false) return showToast(result.blocked_reason || '切换被拒绝', 'error');
    showToast(`已切换为${label}`, 'success');
    renderMemoryRecords();
    renderStatusRail();
  } catch (e) {
    showToast('切换失败：' + e, 'error');
  }
}

async function renderRecentEvents() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取最近写入</div>';
  try {
    const result = await callApi('get_recent_events', activeShareGroupId);
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
    const result = await callApi('get_supersede_decisions', activeShareGroupId);
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
      callApi('get_conflicts', activeShareGroupId),
      callApi('list_memory', '', '', activeShareGroupId),
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
    const result = await callApi('resolve_conflict', groupId, keepId, activeShareGroupId);
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
    const result = await callApi('get_quarantine', activeShareGroupId);
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
    const result = await callApi('release_quarantine', quarantineId, activeShareGroupId);
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
    const result = await callApi('delete_quarantine', quarantineId, activeShareGroupId);
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
    const result = await callApi('list_memory_versions', activeShareGroupId);
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
    const result = await callApi('rollback_memory', versionId, activeShareGroupId);
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
