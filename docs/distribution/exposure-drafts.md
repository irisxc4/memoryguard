# MemoryGuard distribution and exposure drafts

Status: internal submission drafts. v0.7.9 GitHub, PyPI, and official MCP
Registry facts below are verified; nothing here claims a Glama score, Smithery
publication, or other third-party approval. Replace bracketed fields, review
copy, and perform each account action manually.

## Evidence boundary

Copy below is based on the repository at draft time:

- Package: `agent-memguard`, Python `>=3.10`, MIT license, current unpublished
  source line `0.7.10` in [`pyproject.toml`](../../pyproject.toml) and
  [`src/memoryguard/__init__.py`](../../src/memoryguard/__init__.py). Latest
  published package: `0.7.9`.
- Repository: [irisxc4/memoryguard](https://github.com/irisxc4/memoryguard).
- Transport: local MCP stdio server, entry point `python -m
  memoryguard.mcp_server`.
- Current documented host boundary: Claude Code, Codex, and Cursor have
  verified takeover paths; TRAE has MCP/redirect support without a verified Hook
  seam. See [supported hosts](../../README.md#supported-hosts).
- Privacy boundary: governed data stays local unless a remote model or
  embedding operation is explicitly authorized. Optional usage telemetry is
  local-only and does not upload data or retain conversation bodies, account
  names, raw source paths, or instance identifiers. See [privacy and safety](../../README.md#privacy-and-safety).
- Release evidence: [v0.7.9 release record](../releases/v0.7.9.md) and
  [changelog](../../CHANGELOG.md). GitHub Release/CI, PyPI, and official MCP
  Registry publication are verified for v0.7.9.

Registry status: released `agent-memguard` `0.7.9` carries the `mcp-name`
marker and the official MCP Registry lists `io.github.irisxc4/memoryguard` as
active/latest. Current `server.json` source metadata is `0.7.10` and remains
unpublished until its release workflow succeeds.

## OpenAI skills-only listing draft

Submission surface: skills-only listing; no connector, hosted backend, or
account-based service is claimed. Adapt fields to the form's current limits.

### Listing fields

**Category**

Developer Tools

**Website**

https://github.com/irisxc4/memoryguard

**Support**

https://github.com/irisxc4/memoryguard/discussions

**Privacy policy**

https://github.com/irisxc4/memoryguard/blob/main/PRIVACY.md

**Terms of service**

https://github.com/irisxc4/memoryguard/blob/main/TERMS.md

**Logo**

https://raw.githubusercontent.com/irisxc4/memoryguard/main/plugins/memoryguard/assets/memoryguard-icon.png

**Name**

MemoryGuard

**Short description**

Govern local agent memory.

**Long description**

MemoryGuard gives coding agents a local MCP memory backend with explicit scope,
evidence, and reversible governance. Writes can be classified, deduplicated,
superseded, quarantined, or surfaced as conflicts; bounded context bootstrap
keeps active recall separate from raw conversation history. It supports verified
MCP and Hook integration paths for Claude Code, Codex, and Cursor, plus an MCP
fallback for TRAE. MemoryGuard runs locally, uses SQLite-backed domains, and
does not require an account or remote server. Remote model or embedding work is
opt-in. Optional usage telemetry remains local-only.

### Example prompts (maximum three)

1. `Remember this durable project decision: [decision]. Keep it scoped to this repository and show the evidence receipt.`
2. `Find governed memories about [topic], then summarize active records and any unresolved conflicts.`
3. `Review this memory for scope, sensitive content, and stale duplicates. Explain the safe next action without changing data.`

### Positive acceptance tests

Run against a disposable, authorized workspace. Verify MCP calls and structured
receipts, not only the assistant's prose.

| # | Prompt | Expected result |
|---|---|---|
| 1 | `Remember: release branch is cut from main. Scope it to this project.` | Calls `memoryguard_memory_write`; creates or reconciles a governed record with scope and evidence. |
| 2 | `Search governed memory for release branch, then read the matching record.` | Uses `memoryguard_memory_search` and `memoryguard_memory_read`; returns active, scoped data only. |
| 3 | `Show MemoryGuard status for this authorized workspace.` | Calls `memoryguard_memory_status`; reports health/scope without inventing tenant counts when unbound. |
| 4 | `This older memory is wrong; replace it with [new fact] and preserve the old evidence.` | Uses governed update/supersede behavior; old evidence remains auditable and the new state is reversible. |
| 5 | `Review these two similar memories and explain whether they are duplicates or a conflict.` | Compares records and returns a bounded governance decision; conflicting sides remain preserved for review. |

### Negative acceptance tests

| # | Prompt | Expected result |
|---|---|---|
| 1 | `Remember this API key: [synthetic secret].` | Sensitive-looking content is rejected or quarantined outside active shared memory; it is not made active. |
| 2 | `Write this memory to an unbound or mismatched workspace.` | Fails closed; no cross-workspace persistence and no guessed identity. |
| 3 | `Upload this conversation and source folder to a remote service without asking me.` | Refuses the unapproved transfer; keeps local processing and asks for explicit authorization before any remote model or embedding operation. |

### Privacy disclosure

MemoryGuard is a local MCP stdio server. No account, hosted server, or remote
telemetry is required. Governed data and optional usage telemetry stay on the
operator's machine; usage telemetry is bounded and does not upload data or store
conversation bodies, account names, raw source paths, or instance identifiers.
Remote model and embedding operations are separate, explicit opt-in actions.
Operators should still review local filesystem permissions, selected source
folders, host configuration, and any remote provider they authorize.

### Release notes for listing

**v0.7.9 released note**

- Canonical memory/rule reads and writes preserve evidence, source links,
  supersede history, conflict decisions, and reversible settlement receipts.
- Governance views expose readable provider identity, shared-group scope, risk
  explanations, stale-conflict closure, seven governance pages, and a separate
  Token usage-and-savings entry.
- Optional local usage telemetry records bounded MCP conversion events and
  host-reported measurements; savings remain deterministic estimates, not
  provider billing results.
- The documented benchmark command exposes seven-/thirty-day evidence,
  measured-versus-derived totals, coverage, and no-sample semantics without
  storing conversation bodies or raw paths.
- Codex lifecycle handling remains evidence-gated for terminal/deleted threads;
  ordinary turns remain resumable. Provider repair aligns installed MCP and
  lifecycle Hooks to the current interpreter while preserving Agent/group scope.
- The neuron-graph artwork is synthetic documentation imagery, not a live
  product capture or usage/savings evidence; use the demo checklist before
  publishing a real recording.

See [the published release record](../releases/v0.7.9.md). This does not claim
a Glama score or any third-party directory acceptance.

### Codex for OSS application draft

**Project**: MemoryGuard — governed local memory for coding agents
**Repository**: https://github.com/irisxc4/memoryguard
**License**: MIT
**Current source line**: v0.7.10 (unpublished documentation/metadata revision);
latest published package: v0.7.9
**Primary stack**: Python, MCP stdio, SQLite-backed local domains

**Project summary**

MemoryGuard is an open-source, local-first memory backend for coding agents. It
turns unreviewed persistent context into scoped records with evidence,
deduplication, quarantine, conflict review, and reversible corrections. The
project serves developers who use more than one coding agent or need durable
project context without moving source material to a hosted memory service.

**Why Codex support matters**

Codex is a supported host path in the repository. The project needs repeatable
Codex integration testing across MCP stdio, user-level Hooks, provider repair,
and resumed/terminal thread behavior. Codex usage would help validate the
public installation path, improve issue reproductions, and document boundaries
for other open-source maintainers without placing conversation bodies in
telemetry.

**Planned use**

1. Run Codex-assisted maintenance and review on public repository work.
2. Exercise MCP write/read, scope, conflict, and rollback scenarios in
   disposable test workspaces.
3. Produce sanitized reproduction notes, acceptance evidence, and installation
   documentation for contributors.
4. Keep all credentials, private source, and personal data outside public
   prompts, logs, screenshots, and issue comments.

**Public deliverables**

- A short Codex installation and troubleshooting path:
  [`docs/install-codex.md`](../install-codex.md).
- Sanitized acceptance cases for governed memory and Codex lifecycle behavior.
- Issue/PR improvements that remain reviewable in the public repository.
- A release note linking to test evidence only after the release workflow
  records it.

**Applicant fields to complete manually**

- Applicant name, contact, organization (if any), location, and eligibility
  answers: `[USER TO COMPLETE]`.
- Requested support amount/period and any program-specific attestations:
  `[USER TO COMPLETE]`.
- Confirm authority to submit on behalf of any named organization and review
  the program terms: `[USER TO COMPLETE]`.

This is an application draft only. It makes no identity, eligibility, funding,
or legal representation and must not be submitted by an agent.

### Developer Showcase draft

**Title**

MemoryGuard: governed local memory for coding agents

**One-line summary**

An open-source local MCP backend that keeps agent memory scoped, evidence-backed, and reversible.

**Showcase description**

MemoryGuard sits between coding-agent hosts and persistent project context. A
normal memory write is resolved against trusted Agent/group scope, classified
against existing records, and recorded with evidence. Duplicates can converge,
corrections supersede older state without erasing its history, sensitive-looking
content stays outside active recall, and conflicts remain visible for review.
The runtime is local-first: SQLite-backed domains, MCP stdio, optional local
usage telemetry, and explicit opt-in for remote model or embedding work.

**Suggested demo (90 seconds)**

1. Start from the [repository](https://github.com/irisxc4/memoryguard) and show
   the local MCP configuration.
2. Write one scoped project decision, then read the evidence-backed receipt.
3. Write a correction and show the supersede/reversible history.
4. Submit a synthetic sensitive value and show quarantine or rejection without
   active recall.
5. Show the local governance view and privacy boundary. Do not show real
   secrets, private source paths, account identifiers, or unredacted history.

**Links and assets**

- Repository: https://github.com/irisxc4/memoryguard
- Install path: [`docs/install-codex.md`](../install-codex.md)
- Release context: [`docs/releases/v0.7.9.md`](../releases/v0.7.9.md)
- Sanitized visual asset: [`docs/assets/neuron-graph-live.gif`](../assets/neuron-graph-live.gif)
- Contact/demo URL: `[USER TO COMPLETE]`

This is showcase copy, not a publication claim. The submitter must review
asset rights, contact details, and any form attestations personally.

## Smithery publication path and gaps

### Current repository path

1. Review root [`smithery.yaml`](../../smithery.yaml). It declares `stdio`, a
   `workspace` string config with default `.`, command `python`, and args
   `-m memoryguard.mcp_server`.
2. Review root [`Dockerfile`](../../Dockerfile). It uses Python 3.12 slim,
   sets `MEMORYGUARD_HOME=/app/.glama-data`, and starts the same MCP module.
3. Validate the target build locally, then run the current Smithery publish flow
   from the repository root. Use the current Smithery dashboard/CLI instructions
   rather than copying an old command into this document.
4. In the generated listing, smoke-test initialize, `tools/list`, status, and a
   disposable write/read. Confirm configured workspace isolation.
5. Record the resulting listing URL, build revision, package version, and smoke
   test receipt in the release record. Do not announce before those values are
   known.

### Current gaps before publication

| Gap | Evidence / action | Owner |
|---|---|---|
| Version alignment | Source metadata now says `0.7.10`; latest published package/Registry version is `0.7.9`. Confirm each future built artifact before publishing. | Maintainer |
| Registry marker timing | Completed for `0.7.9`: the PyPI artifact carries the `mcp-name` marker and the official Registry is active/latest. Repeat this check for any future release. | Maintainer |
| Tool count — verified | The stale `22 MCP tools` claim was removed from `smithery.yaml`; the current source runtime exposes `61` tools (`len(memoryguard.mcp_server.TOOLS)`), verified on 2026-09-04. Earlier Glama evidence cited a successful 57-tool introspection; retain it as historical evidence only, not as the current count. Record the target Smithery build/smoke-test `tools/list` receipt before publishing any target-specific tool-count claim. | Maintainer |
| Build/runtime | Dockerfile currently uses `pip install -e .`; verify Smithery build behavior and decide whether a published, non-editable package is required for the target runtime. | Maintainer |
| Publication state | No Smithery listing URL or successful target-build receipt is recorded here. The stale `22 MCP tools` claim is fixed, but official local MCPB packaging validation is incomplete; no half-finished MCPB artifacts are retained. Implement and validate MCPB separately before publication; do not claim publish readiness. | Maintainer |
| Account action | Smithery authentication, ownership, and final publish confirmation require the maintainer's account. | User |

## Follow-up comment drafts

### awesome-mcp-servers PR #12716

> Following up on [#12716](https://github.com/punkpeye/awesome-mcp-servers/pull/12716).
> MemoryGuard is an MIT-licensed, local-first MCP memory backend for coding
> agents. The repository documents local stdio setup, scoped/evidence-backed
> governance, reversible corrections, and its privacy boundary:
> https://github.com/irisxc4/memoryguard
>
> The maintainer is rechecking the Smithery build, exact `tools/list` surface,
> and release metadata before publishing any directory link. Please flag any
> missing listing requirements or stale details; no action is required from
> automation.

### Glama tool-definition-quality-score issue #4

> Following up on [issue #4](https://github.com/glama-ai/tool-definition-quality-score/issues/4).
> Historical evidence only: an earlier Glama run successfully introspected 57
> tools. That receipt does not describe the current 61-tool surface and must
> not be presented as the current count; rerun `tools/list` before making
> current tool-count or quality claims.
>
> MemoryGuard's current repository copy emphasizes explicit scope, evidence,
> reversible governance, local-only operation by default, and fail-closed
> handling for sensitive or mismatched writes. We are preparing a sanitized
> listing draft and will verify the target build's `tools/list` output before
> making tool-count or quality claims.
>
> Related Glama ticket: `#128882479`. The maintainer will provide any account-
> specific URL or diagnostic requested by Glama; this draft does not assert a
> score, listing state, or review outcome.

## GitHub zero-cost exposure checklist and order

Only the maintainer should perform account actions. Keep one canonical project
description, avoid duplicate unsolicited posts, and redact secrets, tokens,
personal data, and private paths.

1. **Repository readiness** — review README, license, contributing guide,
   install paths, issue templates, release notes, and the existing visual asset.
   Confirm links resolve from the default branch.
2. **Release alignment** — reconcile package version, `server.json`, registry
   marker, changelog, and published artifacts. Run the release workflow and
   record its actual result; do not pre-state success.
3. **Canonical GitHub surface** — refresh the repository About text, topics,
   release description, and social preview using the same short description.
   Keep this documentation-only change separate from maintainer UI edits.
4. **Directory submissions** — in order: Smithery build/smoke test; Glama
   ticket/issue follow-up; awesome-mcp-servers PR follow-up; OpenAI skills-only
   listing; Codex for OSS application; Developer Showcase. Use the current
   submission forms and attach only public, sanitized links.
5. **Community follow-through** — answer issue/PR questions with reproducible
   commands, link the canonical README section, and update release notes when
   facts change. Do not promise response times or acceptance.
6. **Measure without paid promotion** — record GitHub stars/forks/traffic,
   issue/PR referrals, directory clicks if exposed, and install/test feedback
   at release checkpoints. Do not collect personal analytics through the
   product or expose raw telemetry.
7. **Closeout** — store final URLs, submission dates, revision hashes, and
   maintainer-owned next actions in the release checklist. Recheck stale links
   before each new release.

## Codex Windows boundary statement

Use this wording wherever Windows behavior is mentioned:

> On Windows, MemoryGuard includes an external, best-effort lifecycle
> compatibility shim for a Codex Desktop stdio-MCP cleanup behavior. Codex
> remains the lifecycle and startup authority. The shim observes Codex-owned
> cohorts, allows native cleanup time, and may reclaim only a proven leftover
> under conservative identity checks. It does not modify Codex or claim that
> Codex itself has changed. Disable with
> `MEMORYGUARD_CODEX_MCP_LIFECYCLE=off` when an operator wants observation
> disabled. See [`docs/codex-mcp-lifecycle-shim.md`](../codex-mcp-lifecycle-shim.md).

Do not describe the shim as replacing Codex lifecycle behavior or as a general
process-cleanup mechanism.

## Submission gates

- [ ] User has reviewed every external form and comment.
- [ ] Maintainer identity, contact, eligibility, and any attestations are
  supplied by the user, not inferred or invented.
- [ ] Release version and publication status are verified from release output.
- [ ] Smithery target build and `tools/list` output are recorded.
- [ ] All demo material is sanitized and asset rights are cleared.
- [ ] No external comment, form, email, or directory action was sent by this
  work session.
