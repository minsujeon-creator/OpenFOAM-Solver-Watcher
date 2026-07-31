# Automatic Mesh Quality and Layer Coverage Design

**Date:** 2026-08-01

## Goal

Extend the single-case, dependency-free watcher with an advisory mesh-health view that:

- runs a thorough `checkMesh` automatically when an undecomposed mesh is stable;
- explains the important topology and geometry evidence reported by `checkMesh`;
- reads the final `snappyHexMesh` layer-coverage table and compares each patch with the requested surface layers;
- remains read-only, bounded, and safe to expose only through the existing localhost/SSH-tunnel workflow.

## User Experience

The dashboard gains a **Mesh quality** view. It reports whether the automatic check is waiting, running, passing, failing, or unavailable; the command and check time; principal dimensions; important geometry metrics; problem counts; and readable advisories. The meshing view gains a **Layer coverage** section with one row per reported patch: face count, requested layers, average realised layers, realised/requested fraction, average thickness, realised thickness percentage, and a factual status.

Layer status is deliberately evidence-based:

- `met`: reported average layer count reaches the requested count;
- `partial`: some layers exist but the average is below the request;
- `missing`: the reported average is zero for a patch requesting layers;
- `not requested` or `unknown`: no valid comparison is possible.

No arbitrary engineering acceptance threshold is invented. The watcher explains what OpenFOAM reported and leaves suitability for the simulation to the user.

## Automatic `checkMesh` Policy

The watcher resolves `checkMesh` from the environment in which the server was launched. For a complete undecomposed `polyMesh`, it observes the core files (`points`, `faces`, `owner`, `neighbour`, and `boundary`) and waits until their size and modification signature has remained unchanged for 15 seconds. It then starts exactly one background process:

```text
checkMesh -latestTime -allTopology -allGeometry -meshQuality
```

The command is executed as an argument list with `shell=False`, the case as its working directory, stdout and stderr captured together, and no write options. The watcher does not use `-writeSets`, `-writeSurfaces`, or launch MPI. If only decomposed processor meshes exist, it advises reconstruction rather than guessing a parallel invocation. A changed stable mesh schedules one new check; an unchanged mesh is never checked repeatedly.

The background worker keeps page requests responsive. Output is bounded in memory, and server shutdown terminates a still-running child cleanly. While active `snappyHexMesh` output indicates that the mesh is being generated, the check remains deferred; the stability test is an additional guard against reading partially written files.

## Parsing and Assessment

The parser is tolerant of the common OpenFOAM Foundation and OpenCFD wording variants. It extracts:

- `Mesh OK` or `Failed N mesh checks` as the authoritative overall result;
- point, face, internal-face, cell, and region counts;
- aspect ratio, face-area range, cell-volume range, non-orthogonality, skewness, openness, determinant, face-weight, volume-ratio, concavity, and tet-quality evidence when present;
- explicitly reported counts of severe, illegal, negative, concave, disconnected, or highly skew faces/cells;
- fatal/error lines and the process exit code.

An individual metric is marked passing or failing only when the output supplies that evidence, such as an `OK` marker, an explicit problem count, or a printed limit. Otherwise it is informational. The overall result is `passing` only when `checkMesh` reports `Mesh OK` and exits successfully, `failing` on reported failed checks, fatal errors, or a non-zero exit, and `indeterminate` when the output is incomplete.

## Layer Coverage

`snappyHexMesh` prints a final table headed `patch faces layers overall thickness`. The watcher retains the latest complete table and parses each patch row from the right so normal OpenFOAM patch names remain intact. It also reads `addLayersControls.layers` (and the older `addLayerControls` spelling) from `system/snappyHexMeshDict`, including regex-like patch selectors and each `nSurfaceLayers` request.

For each printed patch, the UI shows:

- surface face count;
- average realised layer count;
- requested layer count, when a selector matches;
- realised/requested layer fraction;
- average realised thickness and the printed realised/wanted thickness percentage.

The advisory warns that average coverage cannot expose the spatial distribution of collapsed layers. Optional OpenFOAM layer fields or set files are not created automatically because version one is read-only and dependency-free.

## Data Flow

The existing collector owns a `CheckMeshMonitor`. Each snapshot updates the mesh signature and receives an immutable status/report model. The existing `SnappyParser` owns layer-table and requested-layer parsing. The JSON snapshot exposes top-level `meshQuality` and `meshing.layerCoverage` objects. The static dependency-free dashboard renders both, without changing the localhost-only bind or authentication model.

## Failure Modes

- **`checkMesh` not on PATH:** show the exact environment remedy; do not fail the server.
- **No complete mesh:** remain unavailable until one appears.
- **Decomposed-only mesh:** explain that an undecomposed mesh is required.
- **Command failure or unsupported option:** retain exit code and bounded diagnostic output.
- **Log rotation/truncation:** use the latest layer table currently available; do not combine incompatible partial tables.
- **Server stop:** terminate the child, wait briefly, then kill only that owned process if required.

## Verification

Tests use only standard-library fakes and fixtures. They cover both common output dialects, passing and failing metrics, layer-table parsing, requested-layer matching, stability/defer/rerun scheduling, command construction without a shell, snapshot JSON, shutdown, and dashboard contracts. The complete existing suite must remain green.

