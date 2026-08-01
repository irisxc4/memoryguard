"""Runtime smoke tests for the rule cockpit HTML/JavaScript surface."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


def _rendered_html() -> str:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from memoryguard.interactive import render_interactive_html

    return render_interactive_html()


def test_rule_diagnostics_has_no_undefined_state():
    html = _rendered_html()
    assert "diagnosticsExpanded" not in html


def _run_rule_page_smoke() -> dict:
    """Execute real page functions and return smoke assertions from Node."""

    html = _rendered_html()
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const matches = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).filter(Boolean);
if (!matches.length) throw new Error('inline script missing');
const source = matches[matches.length - 1];

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, value: '', innerHTML: '', textContent: '', style: {},
      className: '',
      classList: { toggle() {}, add() {}, remove() {} },
      appendChild() {}, remove() {}, addEventListener() {},
      querySelector() { return null; },
      querySelectorAll() { return []; },
    });
  }
  return elements.get(id);
}
const sandbox = {
  console,
  URLSearchParams,
  Promise,
  Map,
  Set,
  Math,
  Number,
  String,
  JSON,
  Date,
  Intl,
  document: {
    getElementById: element,
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement(tag) { return element(`created-${tag}-${elements.size}`); },
    addEventListener() {},
    documentElement: { dataset: {} },
    body: { appendChild() {}, removeChild() {} },
  },
  window: { addEventListener() {}, removeEventListener() {}, pywebview: null },
  localStorage: { getItem() { return null; }, setItem() {} },
  navigator: { language: 'zh-CN' },
  confirm() { return true; },
  alert() {},
  fetch() { throw new Error('fetch should not be used in this smoke test'); },
  setTimeout() { return 1; },
  clearTimeout() {},
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'memoryguard-interactive.js' });

const calls = [];
sandbox.callApi = async (method, ...args) => {
  calls.push({ method, args });
  if (method === 'list_rules_habits') return { buckets: {}, total: 0 };
  if (method === 'get_rule_scope_options') return {
    agents: [
      { id: 'current-agent', label: 'Current Agent' },
      { id: 'preview-agent', label: 'Preview Agent' },
    ],
    groups: [{ id: 'default', label: 'default' }],
    projects: [
      { id: 'current-project', label: 'Current Project' },
      { id: 'preview-project', label: 'Preview Project' },
    ],
    providers: [], runtime_roles: [],
  };
  if (method === 'list_rule_decisions') return { decisions: [], total: 0 };
  if (method === 'get_rule_auto_scope_metrics') return { stats: [], auto_scope: {} };
  if (method === 'list_rule_match_receipts') return { receipts: [], total: 0 };
  if (method === 'list_rule_exceptions') return { exceptions: [], total: 0 };
  if (method === 'preview_effective_rules') return {
    effective: [], excluded: [], unavailable: [],
  };
  if (method === 'create_rule_from_text') return {
    ok: true, rule_id: 'rule-1', memory_id: 'rule-1', kind: 'procedure',
    assignments: [], scope_confidence: 0.9,
  };
  throw new Error(`unexpected API method: ${method}`);
};

(async () => {
  await sandbox.renderRulesHabits();
  await sandbox.setRulePreviewAgent('preview-agent');
  await sandbox.setRulePreviewProject('preview-project');
  element('rule-create-text').value = 'trusted rule';
  await sandbox.createRuleFromText();
  const creates = calls.filter(item => item.method === 'create_rule_from_text');
  if (creates.length !== 1) throw new Error(`expected one create call, got ${creates.length}`);
  if (JSON.stringify(creates[0].args) !== JSON.stringify(['trusted rule'])) {
    throw new Error(`preview context leaked into create args: ${JSON.stringify(creates[0].args)}`);
  }
  process.stdout.write(JSON.stringify({ ok: true, createArgs: creates[0].args }));
})().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""

    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "rule-page.html"
        html_path.write_text(html, encoding="utf-8")
        result = subprocess.run(
            ["node", "-e", runner, str(html_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(result.stderr or result.stdout)
    return json.loads(result.stdout)


def _run_rule_exception_smoke() -> dict:
    """Run the receipt exception modal through real JavaScript.

    This deliberately exercises both branches: an empty override must not
    call the API, while a non-empty override must submit the receipt/outcome
    pair and refresh the cockpit after success.
    """

    html = _rendered_html()
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const matches = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).filter(Boolean);
const source = matches[matches.length - 1];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, value: '', innerHTML: '', textContent: '', style: {}, className: '',
      classList: { toggle() {}, add() {}, remove() {} },
      appendChild() {}, remove() {}, addEventListener() {}, focus() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
    });
  }
  return elements.get(id);
}
const sandbox = {
  console, URLSearchParams, Promise, Map, Set, Math, Number, String, JSON, Date, Intl,
  document: {
    getElementById: element,
    querySelector() { return null; }, querySelectorAll() { return []; },
    createElement(tag) { return element(`created-${tag}-${elements.size}`); },
    addEventListener() {}, body: { appendChild() {}, removeChild() {} },
  },
  window: { addEventListener() {}, removeEventListener() {}, pywebview: null },
  localStorage: { getItem() { return null; }, setItem() {} },
  navigator: { language: 'zh-CN' }, confirm() { return true; }, alert() {},
  fetch() { throw new Error('fetch should not be used in this smoke test'); },
  setTimeout() { return 1; }, clearTimeout() {},
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'memoryguard-interactive.js' });
const calls = [];
let refreshes = 0;
let blocked = true;
sandbox.callApi = async (method, ...args) => {
  calls.push({ method, args });
  return blocked ? { ok: false, error: 'feedback_agent_does_not_own_receipt' } : { ok: true };
};
sandbox.renderRulesHabits = async () => { refreshes += 1; };
(async () => {
  sandbox.openRuleExceptionFeedbackModal('receipt-ex-1');
  await sandbox.submitRuleExceptionFeedback('receipt-ex-1');
  const emptyCalls = calls.length;
  const emptyError = element('rule-exception-feedback-error').textContent;
  element('rule-exception-override').value = '仅当前项目测试目录允许替代规则';
  await sandbox.submitRuleExceptionFeedback('receipt-ex-1');
  const blockedError = element('rule-exception-feedback-error').textContent;
  blocked = false;
  if (emptyCalls !== 0) throw new Error(`empty override called API: ${emptyCalls}`);
  if (!emptyError) throw new Error('empty override did not expose visible error');
  if (!blockedError.includes('feedback_agent_does_not_own_receipt')) throw new Error(`backend block not visible: ${blockedError}`);
  await sandbox.submitRuleExceptionFeedback('receipt-ex-1');
  if (calls.length !== 2) throw new Error(`expected two API calls, got ${calls.length}`);
  const call = calls[1];
  if (call.method !== 'submit_rule_feedback') throw new Error(`wrong method: ${call.method}`);
  if (call.args[0] !== 'receipt-ex-1' || call.args[1] !== 'exception' || call.args[3] !== '仅当前项目测试目录允许替代规则') {
    throw new Error(`wrong exception payload: ${JSON.stringify(call.args)}`);
  }
  if (refreshes !== 1) throw new Error(`success did not refresh: ${refreshes}`);
  process.stdout.write(JSON.stringify({ ok: true, call: call.args, emptyError, blockedError, refreshes }));
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
"""
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "rule-page.html"
        html_path.write_text(html, encoding="utf-8")
        result = subprocess.run(
            ["node", "-e", runner, str(html_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    if result.returncode != 0:
        pytest.fail(result.stderr or result.stdout)
    if not result.stdout:
        pytest.fail(f"exception smoke produced no JSON; stderr={result.stderr!r}")
    return json.loads(result.stdout)


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="Node.js unavailable")
def test_rule_page_javascript_executes_without_reference_error():
    payload = _run_rule_page_smoke()
    assert payload["ok"] is True


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="Node.js unavailable")
def test_diagnostic_preview_never_changes_create_context():
    payload = _run_rule_page_smoke()
    assert payload["createArgs"] == ["trusted rule"]


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="Node.js unavailable")
def test_rule_exception_modal_requires_body_and_submits_receipt():
    payload = _run_rule_exception_smoke()
    assert payload["ok"] is True
    assert payload["call"][0:2] == ["receipt-ex-1", "exception"]
    assert payload["call"][3]
    assert payload["refreshes"] == 1


def test_relation_only_exception_editor_is_advanced_admin_surface():
    html = _rendered_html()
    marker = "新增子例外关系"
    assert marker in html
    advanced_start = html.index('<details class="rule-advanced">')
    assert advanced_start < html.index(marker)
    # The daily card keeps receipt feedback, but no direct relation editor.
    daily_fragment = html[html.index("function ruleCard"):advanced_start]
    assert marker not in daily_fragment
