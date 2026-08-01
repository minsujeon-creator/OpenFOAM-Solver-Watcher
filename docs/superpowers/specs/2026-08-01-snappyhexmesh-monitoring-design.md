# snappyHexMesh Monitoring Design

## Goal

Extend the dependency-free single-case watcher so `foam-watch` automatically
chooses between solver monitoring and `snappyHexMesh` monitoring. Solver
residual, convergence, transient-health, post-processing, and stationarity
behavior must remain unchanged when a solver log is selected.

## Workflow detection

Every log candidate is classified as `solver`, `snappy_hex_mesh`, `utility`, or
`unknown` from its filename and bounded prefix/tail evidence. Explicit `--log`
and saved selections remain overrides. Automatic selection prefers a detected
running case-contained process, then the freshest recognized active log, then
the highest-ranked recognized historical log. This prevents an old
`log.snappyHexMesh` from displacing a newer solver log.

The snapshot exposes `workflow.kind` and the classification of every log
candidate. A selected snappy log is parsed only by the snappy parser; solver
convergence is reported as not applicable rather than inferred from meshing
messages.

## snappyHexMesh telemetry

The parser incrementally records:

- state evidence: running, finished, failed, or stale;
- stage: initialization, castellation, snapping, layer addition, finalization,
  or completed;
- configured stage count using `addLayers` from `system/snappyHexMeshDict`;
- active and completed morph iterations, using the log's zero-based morph
  iteration and the configured/log-reported total;
- displacement-smoothing iteration and `nSolveIter` when available;
- latest cells, faces, and points;
- `maxGlobalCells` limit evidence;
- OpenFOAM warnings, fatal errors, execution time, and latest activity.

Progress percentages are always labelled as phase-local. The watcher never
claims an overall wall-clock completion percentage or ETA for snappyHexMesh.
Unknown or version-specific output remains visible as indeterminate evidence.

## Dashboard

Solver mode keeps the current views. Meshing mode hides solver-only views and
shows a dedicated Meshing view containing stage, phase-local progress, current
operation, mesh size, configuration facts, and warnings. Summary cards adapt
their labels and values to the selected workflow. Diagnostics show workflow
classification and ranking reasons for all candidate logs.

## Safety and compatibility

The server remains bound to loopback by default and remote access continues to
use an SSH tunnel. No third-party Python or browser dependencies are added.
Only bounded file regions are used for discovery, and live log contents are
read incrementally. The per-case `.foam-watcher.json` format remains backward
compatible.

## Verification

Unit tests cover candidate classification/selection, dictionary settings,
snappy parsing across stages and failures, stale-state derivation, snapshot
mode separation, and accessible dashboard structure. The complete existing
test suite must pass before the feature is committed.
