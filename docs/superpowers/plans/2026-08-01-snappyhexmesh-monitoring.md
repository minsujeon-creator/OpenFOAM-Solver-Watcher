# snappyHexMesh Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatically selected, dependency-free `snappyHexMesh` progress monitoring to the existing single-case watcher.

**Architecture:** Classify log candidates before selection, route the selected incremental log stream to either the existing solver parser or a focused snappy parser, and expose a workflow-specific snapshot. Adapt the existing static dashboard to switch views without conflating phase progress with solver convergence or total runtime completion.

**Tech Stack:** Python 3.10+ standard library, HTML5, CSS, dependency-free JavaScript, `unittest`.

## Global Constraints

- Preserve the existing solver watcher behavior and per-case configuration format.
- Add no runtime dependencies.
- Keep the HTTP server loopback-only by default and retain SSH-tunnel guidance.
- Report only phase-local snappy progress; never invent total wall-clock progress or ETA.
- Keep all reads contained within the selected OpenFOAM case.

---

### Task 1: Workflow-aware log discovery and selection

**Files:**
- Modify: `watcher/models.py`
- Modify: `watcher/log_reader.py`
- Modify: `watcher/snapshot.py`
- Test: `tests/test_log_reader.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Produces: `LogCandidate.workflow: str` and ranking reasons that distinguish solver, snappy, utility, and unknown logs.
- Produces: automatic selection that respects explicit/saved overrides and otherwise favors active/fresh recognized workflows.

- [ ] Add failing tests showing that `log.snappyHexMesh` is classified as `snappy_hex_mesh`, an old snappy log cannot displace a fresher solver log, and a fresh snappy log is automatically selected.
- [ ] Run the focused discovery/snapshot tests and confirm failures are caused by missing workflow classification.
- [ ] Add the candidate field, bounded evidence classifiers, and deterministic selection ranking.
- [ ] Run the focused tests and the existing log-selection tests until green.

### Task 2: Parse snappyHexMesh configuration and live telemetry

**Files:**
- Create: `watcher/snappy_parser.py`
- Modify: `watcher/models.py`
- Test: `tests/test_snappy_parser.py`

**Interfaces:**
- Produces: `SnappySettings` loaded from `system/snappyHexMeshDict` with `add_layers`, `n_solve_iter`, and `max_global_cells`.
- Produces: `SnappyHexMeshParser.feed(LogChunk)` and `.snapshot()` returning immutable stage, iteration, mesh-count, warning, failure, completion, and timing evidence.

- [ ] Add failing configuration tests for comments, nested `snapControls`, disabled layers, and missing dictionary values.
- [ ] Run the configuration tests and confirm the parser API is missing.
- [ ] Implement contained dictionary loading through the existing OpenFOAM parser and typed defaults.
- [ ] Run the configuration tests until green.
- [ ] Add failing parser tests for castellation, zero-based morph progress, smoothing progress, layer addition, mesh counts, `maxGlobalCells`, warnings, `End`, truncation resets, and fatal errors.
- [ ] Run the parser tests and confirm failures identify missing telemetry behavior.
- [ ] Implement line-oriented incremental parsing with bounded warnings/notices and phase-local progress fields.
- [ ] Run all snappy parser tests until green.

### Task 3: Build workflow-specific snapshots and state

**Files:**
- Modify: `watcher/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Produces: top-level `workflow` with `kind`, `label`, and `selectedLog`.
- Produces: top-level `meshing` for snappy telemetry and a not-applicable numerical assessment in meshing mode.
- Preserves: existing `solver`, `numerics`, `physical`, and post-processing data contracts for solver mode.

- [ ] Add failing integration tests for automatic snappy snapshots, solver-mode regression behavior, switching parsers when the active log changes, completed/failed/stale process states, and JSON safety.
- [ ] Run the focused snapshot tests and confirm expected failures.
- [ ] Route incremental chunks to the workflow parser, derive snappy process state from process/log evidence with a five-minute stale threshold, and assemble the workflow/meshing models.
- [ ] Run snapshot and server tests until green.

### Task 4: Add the adaptive Meshing dashboard

**Files:**
- Modify: `static/index.html`
- Modify: `static/styles.css`
- Modify: `static/app.js`
- Modify: `tests/test_static_contract.py`

**Interfaces:**
- Consumes: `snapshot.workflow.kind` and `snapshot.meshing`.
- Produces: accessible `meshing-view`, adaptive summary cards, solver-only tab visibility, and workflow diagnostics.

- [ ] Add failing static-contract tests requiring the Meshing tab/panel, progress element, stage/current-work facts, mesh-count table, and warning list.
- [ ] Run the static-contract tests and confirm the new DOM contract is absent.
- [ ] Add semantic HTML and minimal styles for the Meshing view.
- [ ] Add JavaScript workflow visibility and render functions, keeping unknown data explicit and percentages phase-labelled.
- [ ] Run static-contract and server tests until green.

### Task 5: Documentation, end-to-end fixtures, and final verification

**Files:**
- Modify: `README.md`
- Modify: `tests/demo_case.py`
- Modify: `tests/test_static_contract.py`

**Interfaces:**
- Produces: documented automatic workflow behavior, snappy status meanings, limitations, and secure launch/tunnel instructions.

- [ ] Add a failing demo assertion that a generated snappy case exercises the Meshing dashboard model.
- [ ] Run the demo test and confirm the fixture is missing.
- [ ] Extend the demo generator or add a focused snappy fixture and document usage with `foam-watch --case CASE`.
- [ ] Run `python -m unittest discover -s tests -v` and require zero failures.
- [ ] Run `python -m compileall watcher tests` and require exit code 0.
- [ ] Review `git diff --check`, the design requirements, and repository status; then create one intentional feature commit containing the complete implementation.
