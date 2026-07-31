# Automatic Mesh Quality and Layer Coverage Implementation Plan

> **For Codex:** implement each task test-first, keeping the watcher dependency-free and read-only.

**Goal:** Add automatic, safe, thorough `checkMesh` assessment and per-surface `snappyHexMesh` layer-coverage evidence to the single-case dashboard.

**Architecture:** A new `watcher.checkmesh` module parses command output and owns a single background subprocess governed by a stable-mesh signature. `SnappyParser` is extended with requested-layer and coverage-table parsing. `WatcherCollector` composes both results into snapshot JSON, while the existing static client renders a mesh-quality view and layer-coverage table.

**Tech Stack:** Python 3.10+ standard library, `unittest`, HTML, CSS, vanilla JavaScript.

---

### Task 1: Model and parse thorough `checkMesh` output

**Files:**

- Create: `watcher/checkmesh.py`
- Modify: `watcher/models.py`
- Create: `tests/test_checkmesh.py`

1. Add failing tests for a passing Foundation-style report, a failing OpenCFD-style report, incomplete/fatal output, metric/problem extraction, and bounded diagnostics.
2. Run `python -m unittest tests.test_checkmesh -v` and confirm parser tests fail because the module/API does not exist.
3. Add immutable report/metric models and implement a line-oriented, version-tolerant parser. Only derive pass/fail from explicit output evidence.
4. Re-run the focused tests until green.

### Task 2: Parse requested layers and realised layer coverage

**Files:**

- Modify: `watcher/models.py`
- Modify: `watcher/snappy_parser.py`
- Modify: `tests/test_snappy_parser.py`

1. Add failing tests for `addLayersControls.layers`, exact and regex selectors, the latest `patch faces layers overall thickness` table, `met`/`partial`/`missing` comparisons, and absent/incomplete tables.
2. Confirm the tests fail for missing coverage fields.
3. Extend `SnappySettings` and `SnappyTelemetry`, resetting a partially replaced table at each new header and calculating only factual comparisons.
4. Re-run `python -m unittest tests.test_snappy_parser -v`.

### Task 3: Run one automatic read-only check after mesh stability

**Files:**

- Modify: `watcher/checkmesh.py`
- Modify: `tests/test_checkmesh.py`

1. Add fake-clock, fake-signature, and fake-process tests for startup stability, active-mesher deferral, exact argument-list construction (including conditional `-meshQuality`), no-shell execution, one-run-at-a-time, mesh-change rerun, missing command, decomposed-only mesh, and clean termination.
2. Confirm focused failures before implementation.
3. Implement `mesh_signature()` and `CheckMeshMonitor` with a lock, one daemon worker, bounded output, and `close()`.
4. Re-run focused tests and inspect all subprocess arguments in assertions.

### Task 4: Integrate monitor lifecycle and snapshot JSON

**Files:**

- Modify: `watcher/snapshot.py`
- Modify: `watcher/server.py`
- Modify: `tests/test_snapshot.py`
- Modify: `tests/test_server.py`

1. Add failing snapshot tests for top-level `meshQuality`, meshing `layerCoverage`, running/deferred states, and preserved solver snapshots.
2. Add a server test that collector shutdown calls the monitor's `close()` without breaking lightweight test collectors.
3. Instantiate/update the monitor in `WatcherCollector`, serialize its report every snapshot, and close it from `_ServerState.server_close()` via a guarded callable lookup.
4. Run snapshot/server focused suites.

### Task 5: Render important evidence and document operation

**Files:**

- Modify: `static/index.html`
- Modify: `static/styles.css`
- Modify: `static/app.js`
- Modify: `tests/test_static_contract.py`
- Modify: `README.md`
- Modify: `SECURITY.md`

1. Add failing DOM/static contract tests for the Mesh quality view, summary, metric/problem tables, and layer coverage table.
2. Implement the view, status badges, empty states, accessible table labels, and compact responsive styling.
3. Document automatic timing, required sourced OpenFOAM environment, the exact read-only command, decomposed-case limitation, layer evidence interpretation, and advisory-only scope.
4. Run static tests and manually validate JavaScript syntax where a runtime is available.

### Task 6: Verify, review, and commit

**Files:** all modified files.

1. Run `python -m unittest discover -s tests -v` from the repository root.
2. Run syntax compilation for all Python modules and an available JavaScript syntax checker.
3. Inspect `git diff --check`, `git diff --stat`, and the final diff for unbounded output, unsafe subprocess use, or invented engineering thresholds.
4. Request a focused code review and resolve substantive findings test-first.
5. Re-run the complete verification suite, confirm a clean expected worktree, and commit the completed feature.
