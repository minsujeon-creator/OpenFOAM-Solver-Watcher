# OpenFOAM Solver Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure, dependency-free, single-case OpenFOAM dashboard that automatically detects solver telemetry and function-object output, evaluates numerical convergence, and classifies selected physical quantities as evolving, plateaued, statistically stationary, or periodic.

**Architecture:** A standard-library Python pipeline inspects one case, incrementally parses a selected solver log, discovers normalized post-processing series, evaluates numerical and physical state independently, and exposes a loopback-only HTTP API to a bundled HTML/CSS/JavaScript dashboard. The only case mutation is an atomically validated `.foam-watcher.json` preferences file.

**Tech Stack:** Python 3.10+ standard library, `unittest`, `http.server`, HTML5, CSS, browser JavaScript, Canvas/SVG; no runtime packages, CDN assets, Node build, database, or framework.

## Global Constraints

- Monitor exactly one OpenFOAM case per watcher process.
- Bind the server to IPv4 loopback `127.0.0.1` only; version one must not offer a network-bind override.
- Remote access is through SSH port forwarding.
- Remain advisory-only: never start, stop, signal, or modify the solver.
- Never edit OpenFOAM dictionaries or simulation outputs.
- The only watcher-owned case file is `<case>/.foam-watcher.json`.
- Require Python 3.10 or newer and only the Python standard library at runtime.
- Make no external browser requests and load no CDN assets.
- Treat unknown solvers and unknown numeric function-object tables as partially supported rather than fatal.
- Never report positive convergence or stationarity without sufficient explicit evidence.
- Keep numerical convergence and physical stationarity as separate verdicts.
- Persist configuration through a versioned, closed schema and atomic same-directory replacement.
- Reject configuration symlinks, unsafe paths, non-finite JSON numbers, unknown schema keys, and unsupported versions.
- Release readiness requires all automated tests plus manual steady, transient, and SSH-tunnel smoke tests.

---

## File Map

| Path | Responsibility |
|---|---|
| `foam-watch` | Direct executable entry point and argument parsing |
| `pyproject.toml` | Python version and project metadata without dependencies |
| `watcher/__init__.py` | Package version |
| `watcher/models.py` | Shared immutable data models and JSON conversion |
| `watcher/case_config.py` | Safe OpenFOAM tokenization, dictionary inspection, case/mode detection |
| `watcher/log_reader.py` | Log discovery, ranking, incremental reads, truncation/replacement detection |
| `watcher/log_parser.py` | Stateful solver-log event parsing and telemetry histories |
| `watcher/postprocessing.py` | Known/generic function-object discovery, table parsing, normalization, candidate scoring |
| `watcher/convergence.py` | Steady convergence and transient numerical-health assessments |
| `watcher/stationarity.py` | Plateau, statistical-stationarity, and periodicity analysis |
| `watcher/persistence.py` | Closed configuration schema, safe loading, validation, and atomic writes |
| `watcher/snapshot.py` | Collector orchestration, process/host inspection, JSON snapshot/series API models |
| `watcher/server.py` | Loopback HTTP service, secure headers, endpoints, request validation |
| `static/index.html` | Accessible dashboard structure |
| `static/styles.css` | Responsive dark control-room presentation |
| `static/app.js` | Polling, state, rendering, checkboxes, settings, and charts |
| `tests/helpers.py` | Temporary-case and fixture helpers |
| `tests/test_case_config.py` | Case/dictionary/mode tests |
| `tests/test_log_reader.py` | Log selection and incremental-read tests |
| `tests/test_log_parser.py` | Steady/transient/restart/failure parser tests |
| `tests/test_postprocessing.py` | Known/generic table and candidate tests |
| `tests/test_convergence.py` | Numerical assessment tests |
| `tests/test_stationarity.py` | Synthetic physical-state tests |
| `tests/test_persistence.py` | Schema and filesystem-safety tests |
| `tests/test_snapshot.py` | Collector integration and JSON-safety tests |
| `tests/test_server.py` | HTTP and security tests |
| `tests/test_static_contract.py` | Dependency-free frontend/API contract checks |
| `tests/demo_case.py` | Deterministic steady/transient-style demo case generator |
| `README.md` | Installation, use, SSH tunnel, interpretation, and troubleshooting |
| `SECURITY.md` | Threat model, supported exposure, and reporting guidance |

---

### Task 1: Package Skeleton, Shared Models, and Case Inspection

**Files:**
- Create: `pyproject.toml`
- Create: `watcher/__init__.py`
- Create: `watcher/models.py`
- Create: `watcher/case_config.py`
- Create: `tests/__init__.py`
- Create: `tests/helpers.py`
- Create: `tests/test_case_config.py`

**Interfaces:**
- Produces: `inspect_case(case_dir: Path) -> CaseInspection`
- Produces: `parse_foam_file(path: Path, case_dir: Path) -> tuple[dict[str, object], tuple[str, ...]]`
- Produces: immutable `CaseInspection`, `FunctionObjectSpec`, and `ResidualTarget` dataclasses
- Consumes: no earlier application interfaces

- [ ] **Step 1: Write failing case-inspection tests**

Create `tests/helpers.py` with a `TemporaryCase` context manager that creates
`system`, `constant`, and requested text files using `pathlib.Path`.

Create tests covering valid-case recognition, comments, quoted regex keys,
`#include`, refusal to follow a dynamic code directive, residual targets,
SIMPLE/PIMPLE mode detection, parallel rank counting, and malformed input:

```python
from pathlib import Path
from tests.helpers import TemporaryCase
from watcher.case_config import inspect_case


def test_detects_simple_case_and_residual_control() -> None:
    with TemporaryCase() as case:
        case.write(
            "system/controlDict",
            "application simpleFoam;\nstartTime 0;\nendTime 500;\ndeltaT 1;\n",
        )
        case.write(
            "system/fvSolution",
            """
            SIMPLE
            {
                nNonOrthogonalCorrectors 1;
                residualControl
                {
                    U 1e-5;
                    "(k|omega)" 1e-4;
                }
            }
            """,
        )
        result = inspect_case(case.path)

    assert result.application == "simpleFoam"
    assert result.mode == "steady_simple"
    assert result.end_time == 500.0
    assert [(item.pattern, item.threshold) for item in result.residual_targets] == [
        ("U", 1e-5),
        ("(k|omega)", 1e-4),
    ]


def test_detects_transient_pimple_and_included_functions() -> None:
    with TemporaryCase() as case:
        case.write(
            "system/controlDict",
            """
            application pimpleFoam;
            endTime 20;
            deltaT 0.01;
            adjustTimeStep yes;
            maxCo 0.8;
            #include "functions.inc"
            """,
        )
        case.write(
            "system/functions.inc",
            "functions { lift { type forceCoeffs; patches (body); } }\n",
        )
        case.write("system/fvSolution", "PIMPLE { nOuterCorrectors 3; }\n")
        result = inspect_case(case.path)

    assert result.mode == "transient_pimple"
    assert result.adjust_time_step is True
    assert result.max_co == 0.8
    assert result.function_objects["lift"].type_name == "forceCoeffs"
```

- [ ] **Step 2: Run the case-inspection tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_case_config -v
```

Expected: import failure because `watcher.case_config` does not exist.

- [ ] **Step 3: Add the package metadata, models, parser, and case inspector**

Set `requires-python = ">=3.10"` and an empty dependency list in
`pyproject.toml`.

Define these exact models in `watcher/models.py`:

```python
@dataclass(frozen=True)
class ResidualTarget:
    pattern: str
    threshold: float


@dataclass(frozen=True)
class FunctionObjectSpec:
    name: str
    type_name: str
    fields: tuple[str, ...]
    region: str | None
    operation: str | None


@dataclass(frozen=True)
class CaseInspection:
    case_dir: Path
    application: str | None
    openfoam_version: str | None
    mode: str
    mode_confidence: str
    mode_evidence: tuple[str, ...]
    start_time: float | None
    end_time: float | None
    delta_t: float | None
    adjust_time_step: bool | None
    max_co: float | None
    max_delta_t: float | None
    parallel_ranks: int
    multi_region: bool
    residual_targets: tuple[ResidualTarget, ...]
    function_objects: Mapping[str, FunctionObjectSpec]
    notices: tuple[str, ...]
```

Implement a non-evaluating tokenizer for words, quoted strings, braces,
parentheses, semicolons, numbers, and `#include`. Strip `//` and `/* ... */`
comments while preserving quoted text. Parse nested dictionaries and lists
without evaluating `$` variables or `#codeStream`.

Resolve `#include` and `#includeIfPresent` by:

1. resolving relative paths against the including file;
2. allowing a result inside the resolved case directory;
3. allowing a result inside `WM_PROJECT_DIR` only when that environment
   variable is defined and the result remains under its resolved directory;
4. recording a notice and skipping every other target;
5. tracking visited paths to prevent cycles.

Implement mode evidence in this precedence order:

1. PIMPLE dictionary -> `transient_pimple`;
2. PISO dictionary -> `transient_piso`;
3. SIMPLE dictionary plus transient time scheme or local-time-stepping marker
   -> `pseudo_transient`;
4. SIMPLE dictionary -> `steady_simple`;
5. otherwise -> `unknown`.

Read OpenFOAM version from case file banners when present, count top-level
`processor[0-9]+` directories, and treat more than one `regionProperties`
region as multi-region.

- [ ] **Step 4: Run case-inspection tests**

Run:

```bash
python3 -m unittest tests.test_case_config -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the package and case-inspection foundation**

```bash
git add pyproject.toml watcher tests
git commit -m "feat: inspect OpenFOAM case configuration"
```

---

### Task 2: Log Discovery and Incremental Reading

**Files:**
- Create: `watcher/log_reader.py`
- Create: `tests/test_log_reader.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: `CaseInspection`
- Produces: `discover_logs(inspection: CaseInspection, explicit: Path | None, saved_relative: str | None) -> tuple[LogCandidate, ...]`
- Produces: `IncrementalLogReader.read() -> LogChunk`
- Produces: immutable `LogCandidate` and `LogChunk` dataclasses

- [ ] **Step 1: Write failing discovery and incremental-reader tests**

```python
def test_application_log_outranks_newer_mesh_log() -> None:
    with simple_case(application="simpleFoam") as case:
        case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 1\nSolving for U")
        case.write("log.checkMesh", "OpenFOAM v2412\nMesh OK.\n")
        case.touch("log.checkMesh", seconds_after=5)
        inspection = inspect_case(case.path)
        ranked = discover_logs(inspection, explicit=None, saved_relative=None)

    assert ranked[0].relative_path == "log.simpleFoam"
    assert "application-name match" in ranked[0].reasons


def test_incremental_reader_handles_partial_line_and_truncation() -> None:
    with TemporaryCase() as case:
        log = case.write("log.pimpleFoam", "Time = 0.1\nCourant Number")
        reader = IncrementalLogReader(case.path, log)
        first = reader.read()
        assert first.lines == ("Time = 0.1",)

        case.append("log.pimpleFoam", " mean: 0.1 max: 0.8\n")
        second = reader.read()
        assert second.lines == ("Courant Number mean: 0.1 max: 0.8",)

        case.write("log.pimpleFoam", "Time = 0.0\n")
        third = reader.read()
        assert third.reset is True
        assert third.segment == 1
```

- [ ] **Step 2: Run log-reader tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_log_reader -v
```

Expected: import failure because `watcher.log_reader` does not exist.

- [ ] **Step 3: Implement candidate scoring and stateful reading**

Add:

```python
@dataclass(frozen=True)
class LogCandidate:
    path: Path
    relative_path: str
    score: int
    reasons: tuple[str, ...]
    modified_ns: int
    size: int


@dataclass(frozen=True)
class LogChunk:
    path: Path
    segment: int
    reset: bool
    lines: tuple[str, ...]
    file_size: int
    modified_ns: int
```

Use the following deterministic scores:

- explicit valid path: `+1000`;
- saved valid path: `+900`;
- filename contains configured application: `+200`;
- recognizable OpenFOAM banner: `+50`;
- residual or `Time =` record in the final 64 KiB: `+50`;
- file modified within 120 seconds: `+25`;
- case-root file: `+10`;
- preprocessing utility signature: `-300`;
- zero-length file: `-25`.

Sort by descending score, then descending modification time, then relative
path. Resolve explicit and saved paths and reject them unless contained by the
resolved case.

`IncrementalLogReader` must keep byte offset, pending incomplete bytes, file
identity `(st_dev, st_ino)` when available, previous size, and segment number.
On replacement or size decrease, increment the segment, clear pending bytes,
and read from byte zero. Decode UTF-8 with replacement and emit only complete
lines.

- [ ] **Step 4: Run log-reader tests**

Run:

```bash
python3 -m unittest tests.test_log_reader -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit log discovery and incremental reading**

```bash
git add watcher/models.py watcher/log_reader.py tests/test_log_reader.py
git commit -m "feat: discover and tail solver logs"
```

---

### Task 3: Solver Log Parsing and Normalized Telemetry

**Files:**
- Create: `watcher/log_parser.py`
- Create: `tests/test_log_parser.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: `LogChunk`
- Produces: `OpenFOAMLogParser.feed(chunk: LogChunk) -> None`
- Produces: `OpenFOAMLogParser.snapshot() -> SolverTelemetry`
- Produces: immutable `ResidualSample`, `TimeStepSample`, `FailureRecord`, and `SolverTelemetry`

- [ ] **Step 1: Write failing steady, transient, restart, and failure tests**

Use representative OpenFOAM-format lines:

```python
STEADY_LOG = """\
OpenFOAM-v2412
Time = 12
smoothSolver:  Solving for Ux, Initial residual = 1e-03, Final residual = 2e-07, No Iterations 3
GAMG:  Solving for p_rgh, Initial residual = 2e-02, Final residual = 8e-05, No Iterations 2
ExecutionTime = 24 s  ClockTime = 25 s
"""

TRANSIENT_LOG = """\
Time = 0.25
deltaT = 0.005
Courant Number mean: 0.12 max: 0.73
PIMPLE: iteration 2
smoothSolver:  Solving for T, Initial residual = 0.01, Final residual = 1e-06, No Iterations 2
time step continuity errors : sum local = 1e-08, global = -2e-09, cumulative = 4e-08
ExecutionTime = 12 s  ClockTime = 13 s
"""


def test_parses_transient_health_records() -> None:
    parser = OpenFOAMLogParser()
    parser.feed(chunk(TRANSIENT_LOG, segment=0))
    data = parser.snapshot()

    assert data.current_time == 0.25
    assert data.current_delta_t == 0.005
    assert data.time_steps[-1].courant_max == 0.73
    assert data.time_steps[-1].outer_correctors == 2
    assert data.time_steps[-1].continuity_cumulative == 4e-08


def test_banner_trap_fpe_is_not_a_failure_but_real_signal_is() -> None:
    parser = OpenFOAMLogParser()
    parser.feed(chunk("trapFpe: Floating point exception trapping enabled\n"))
    assert parser.snapshot().failure is None
    parser.feed(chunk("Floating point exception (core dumped)\n"))
    assert parser.snapshot().failure.label == "Floating point exception"
```

Also test vector components, solver convergence flags, `End`, convergence
criteria messages, MPI abort, segmentation fault, multi-region prefixes,
repeated PIMPLE solves, and a new segment after restart.

- [ ] **Step 2: Run parser tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_log_parser -v
```

Expected: import failure because `watcher.log_parser` does not exist.

- [ ] **Step 3: Implement the stateful line parser**

Define:

```python
@dataclass(frozen=True)
class ResidualSample:
    simulation_time: float
    segment: int
    field: str
    initial: float
    final: float
    iterations: int
    converged: bool | None
    region: str | None
    outer_corrector: int | None


@dataclass(frozen=True)
class TimeStepSample:
    simulation_time: float
    segment: int
    delta_t: float | None
    courant_mean: float | None
    courant_max: float | None
    mesh_courant_mean: float | None
    mesh_courant_max: float | None
    continuity_local: float | None
    continuity_global: float | None
    continuity_cumulative: float | None
    outer_correctors: int


@dataclass(frozen=True)
class SolverTelemetry:
    current_time: float | None
    current_segment: int
    current_delta_t: float | None
    execution_seconds: float | None
    clock_seconds: float | None
    completed: bool
    solver_declared_converged: bool
    residuals: tuple[ResidualSample, ...]
    time_steps: tuple[TimeStepSample, ...]
    warnings: tuple[str, ...]
    failure: FailureRecord | None
    notices: tuple[str, ...]
```

Compile anchored patterns for `Time`, `deltaT`, solver performance,
`Courant Number`, `Mesh Courant Number`, continuity errors, corrector
iterations, execution time, convergence messages, `End`, and known fatal
markers. Accept optional region prefixes and both `No Iterations` and
`No Iterations =` spellings.

Finalize the previous time-step accumulator when a new `Time =` record arrives,
and include the current accumulator in snapshots without duplicating it.
Downsample histories only when serializing later; retain exact parser data.
Limit stored warnings to the newest 200 and residual samples to the newest
100,000 to bound memory.

- [ ] **Step 4: Run log-parser tests**

Run:

```bash
python3 -m unittest tests.test_log_parser -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit normalized solver telemetry**

```bash
git add watcher/models.py watcher/log_parser.py tests/test_log_parser.py
git commit -m "feat: parse OpenFOAM solver telemetry"
```

---

### Task 4: Function-Object and Generic Table Discovery

**Files:**
- Create: `watcher/postprocessing.py`
- Create: `tests/test_postprocessing.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: `CaseInspection.function_objects`
- Produces: `discover_series(inspection: CaseInspection, now: float | None = None) -> Mapping[str, SeriesData]`
- Produces: `parse_numeric_table(path: Path) -> ParsedTable`
- Produces: immutable `SeriesData`, `ParsedTable`, and `CandidateInfo`

- [ ] **Step 1: Write failing table, restart, vector, and scoring tests**

```python
def test_partial_row_is_ignored_and_later_restart_wins() -> None:
    with simple_case() as case:
        case.write(
            "postProcessing/roomAverage/0/volFieldValue.dat",
            "# Time volAverage(T)\n0 300\n1 302\n2\n",
        )
        case.write(
            "postProcessing/roomAverage/1/volFieldValue.dat",
            "# Time volAverage(T)\n1 305\n2 306\n",
        )
        inspection = inspect_case(case.path)
        found = discover_series(inspection, now=case.now)

    temperature = next(item for item in found.values() if item.field == "T")
    assert temperature.times == (0.0, 1.0, 2.0)
    assert temperature.values == (300.0, 305.0, 306.0)


def test_unknown_vector_table_produces_components_and_magnitude() -> None:
    with simple_case() as case:
        case.write(
            "postProcessing/customProbe/0/data.dat",
            "# Time customVector\n0 (1 2 2)\n1 (2 3 6)\n",
        )
        found = discover_series(inspect_case(case.path), now=case.now)

    components = {item.component: item for item in found.values()}
    assert components["x"].values == (1.0, 2.0)
    assert components["magnitude"].values == (3.0, 7.0)
```

Also test force and force-coefficient layouts, probes with several locations,
surface/volume field values, quoted headers, `nan`/`inf` removal, tensor
components, multi-region paths, staleness, stable identifiers, and candidate
ranking.

- [ ] **Step 2: Run post-processing tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_postprocessing -v
```

Expected: import failure because `watcher.postprocessing` does not exist.

- [ ] **Step 3: Implement adapters, generic parsing, normalization, and scoring**

Define:

```python
@dataclass(frozen=True)
class CandidateInfo:
    score: int
    confidence: str
    recommended: bool
    explanation: str


@dataclass(frozen=True)
class SeriesData:
    series_id: str
    label: str
    function_name: str
    function_type: str
    source_relative: str
    region: str | None
    field: str
    operation: str | None
    component: str | None
    units: str | None
    times: tuple[float, ...]
    values: tuple[float, ...]
    modified_ns: int
    stale: bool
    candidate: CandidateInfo
    notices: tuple[str, ...]
```

Build stable IDs by hashing this UTF-8 identity with SHA-256 and keeping the
first 20 hex characters:

```text
function_name|function_type|region|field|operation|component|source_relative
```

Use adapter selection by declared type and file-header shape. Parse OpenFOAM
parenthesized vectors/tensors before generic whitespace splitting. A row is
valid only when every required value parses to a finite float. Merge restart
directories by numeric start time, with later directories winning duplicate
times.

Candidate scores:

- force/force coefficient, flow/flux, pressure difference, power: `+80`;
- region average/integral, heat transfer, species, phase, interface: `+70`;
- named probes: `+60`;
- at least 50 finite samples: `+20`;
- at least 20 finite samples: `+10`;
- numerical-only diagnostic: `-60`;
- constant series after 20 samples: `-20`;
- stale series: `-20`.

Set `recommended=True` at score 70 or greater. Explain every applied positive
or negative factor in one concise sentence.

- [ ] **Step 4: Run post-processing tests**

Run:

```bash
python3 -m unittest tests.test_postprocessing -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit function-object discovery**

```bash
git add watcher/models.py watcher/postprocessing.py tests/test_postprocessing.py
git commit -m "feat: discover post-processing series"
```

---

### Task 5: Steady Convergence and Transient Numerical Health

**Files:**
- Create: `watcher/convergence.py`
- Create: `tests/test_convergence.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: `CaseInspection`, `SolverTelemetry`
- Produces: `evaluate_numerics(inspection: CaseInspection, telemetry: SolverTelemetry) -> NumericalAssessment`
- Produces: immutable `AssessmentCheck` and `NumericalAssessment`

- [ ] **Step 1: Write failing numerical-assessment tests**

```python
def test_steady_requires_every_configured_target() -> None:
    inspection = steady_inspection(targets={"U": 1e-5, "p_rgh": 1e-4})
    telemetry = steady_telemetry(
        iterations=[
            {"U": 8e-6, "p_rgh": 9e-5},
            {"U": 7e-6, "p_rgh": 8e-5},
            {"U": 6e-6, "p_rgh": 7e-5},
        ]
    )
    result = evaluate_numerics(inspection, telemetry)

    assert result.kind == "steady_convergence"
    assert result.status == "passing"
    assert all(check.passed for check in result.checks)


def test_missing_steady_targets_is_not_configured() -> None:
    result = evaluate_numerics(steady_inspection(targets={}), steady_telemetry())
    assert result.status == "not_configured"


def test_transient_health_fails_recent_courant_limit() -> None:
    inspection = transient_inspection(max_co=1.0)
    telemetry = transient_telemetry(courant_max=[0.4, 0.8, 1.4])
    result = evaluate_numerics(inspection, telemetry)
    assert result.kind == "transient_health"
    assert result.status == "failing"
    assert any(check.code == "courant_limit" and not check.passed for check in result.checks)
```

Also test regex target expansion, vector component grouping, missing required
field, solver-declared convergence, continuity degradation, incomplete current
step exclusion, failure override, and insufficient transient history.

- [ ] **Step 2: Run convergence tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_convergence -v
```

Expected: import failure because `watcher.convergence` does not exist.

- [ ] **Step 3: Implement deterministic numerical assessments**

Define:

```python
@dataclass(frozen=True)
class AssessmentCheck:
    code: str
    label: str
    passed: bool | None
    observed: float | str | None
    target: float | str | None
    explanation: str


@dataclass(frozen=True)
class NumericalAssessment:
    kind: str
    status: str
    summary: str
    checks: tuple[AssessmentCheck, ...]
    healthy_step_percent: float | None
```

For steady cases, group residuals by `(segment, simulation_time)`, use the last
complete three iterations for the sustained result, expand targets with
`re.fullmatch`, and require every target to match at least one observed field.
For a literal vector target such as `U`, treat observed `Ux`, `Uy`, and `Uz` as
its components and require every component observed in the iteration to pass.
Use initial residuals. A fatal record forces `failing`; a
solver-declared-converged message forces `passing` while retaining individual
checks.

For transient cases, inspect the newest 20 finalized time steps. A step is
healthy when its Courant maximum is within configured `maxCo`, all finite
continuity values exist, no failure occurred, and residual records do not carry
an explicit non-convergence flag. Report:

- `passing` at 95–100 percent healthy;
- `warning` at 80–94.999 percent;
- `failing` below 80 percent or after a fatal record;
- `insufficient_data` with fewer than three finalized steps.

Add a continuity trend check by comparing the median absolute global error in
the first and second halves; warn when the second exceeds five times the first
and the increase exceeds `1e-12`.

- [ ] **Step 4: Run convergence tests**

Run:

```bash
python3 -m unittest tests.test_convergence -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit numerical convergence analysis**

```bash
git add watcher/models.py watcher/convergence.py tests/test_convergence.py
git commit -m "feat: assess numerical convergence"
```

---

### Task 6: Plateau, Statistical-Stationarity, and Periodicity Analysis

**Files:**
- Create: `watcher/stationarity.py`
- Create: `tests/test_stationarity.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: `SeriesData`
- Produces: `analyze_series(series: SeriesData, settings: StationaritySettings, now: float) -> StationarityResult`
- Produces: `aggregate_stationarity(selected_ids: Sequence[str], results: Mapping[str, StationarityResult], accepted_states: AbstractSet[str]) -> AggregateStationarity`
- Produces: immutable `StationaritySettings`, `StationarityEvidence`, `StationarityResult`, and `AggregateStationarity`

- [ ] **Step 1: Write deterministic synthetic-signal tests**

Use a fixed random seed and explicit sample construction:

```python
def test_flat_noisy_signal_is_statistically_stationary() -> None:
    rng = random.Random(31)
    values = [10.0 + rng.gauss(0.0, 0.05) for _ in range(600)]
    result = analyze_series(
        series(values),
        StationaritySettings(),
        now=599.0,
    )
    assert result.state in {"plateau", "statistically_stationary"}
    assert result.evidence.effective_samples >= 20


def test_drifting_signal_is_evolving() -> None:
    values = [2.0 + 0.002 * index for index in range(600)]
    result = analyze_series(series(values), StationaritySettings(), now=599.0)
    assert result.state == "evolving"
    assert result.evidence.normalized_slope > result.thresholds["max_normalized_slope"]


def test_stable_sinusoid_is_periodic() -> None:
    values = [4.0 + 0.8 * math.sin(2 * math.pi * index / 40) for index in range(800)]
    result = analyze_series(series(values), StationaritySettings(), now=799.0)
    assert result.state == "periodic"
    assert abs(result.evidence.period - 40.0) < 1.0
```

Also test amplitude drift, mean drift, near-zero scales, autocorrelated noise,
irregular sample times, large gaps, stale series, non-finite removal, fewer
than 20 effective samples, zero selected series, missing selected series, and
aggregate accepted-state behavior.

- [ ] **Step 2: Run stationarity tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_stationarity -v
```

Expected: import failure because `watcher.stationarity` does not exist.

- [ ] **Step 3: Implement the statistical decision pipeline**

Define defaults:

```python
@dataclass(frozen=True)
class StationaritySettings:
    minimum_effective_samples: float = 20.0
    max_mean_shift_fraction: float = 0.02
    max_mean_shift_standard_errors: float = 2.0
    max_normalized_slope: float = 0.02
    minimum_cycles: int = 3
    max_period_variation_fraction: float = 0.05
    max_amplitude_variation_fraction: float = 0.10
    absolute_floor: float = 1e-12
    stale_after_seconds: float = 120.0
```

Use the newest continuous segment after splitting where a time gap exceeds
five times the median positive spacing. Divide it into two equal adjacent
windows containing at least 30 raw samples each.

Calculate:

- mean and population standard deviation for each window;
- least-squares slope in the newest window;
- stable scale `max(abs(latest_mean), latest_std, absolute_floor)`;
- normalized mean shift and normalized slope over one window duration;
- positive-sequence integrated autocorrelation time capped at 200 lags;
- effective samples `n / tau`;
- standard error `std * sqrt(tau / n)`;
- mean-shift standard-error ratio.

Test periodicity by detrending the newest segment, calculating normalized
autocorrelation for lags from 2 to half the segment, selecting the strongest
local peak above 0.5, then dividing the newest data into complete cycles.
Require the configured minimum cycle count, coefficient of variation of cycle
period at or below 5 percent, and peak-to-peak amplitude variation at or below
10 percent.

Decision order:

1. stale, discontinuous without enough latest data, or insufficient effective
   samples -> `indeterminate`;
2. valid periodic evidence with stable cycle mean -> `periodic`;
3. all mean-shift, slope, and standard-error checks pass with very low
   coefficient of variation (`<= 0.01`) -> `plateau`;
4. those checks pass with higher finite variation -> `statistically_stationary`;
5. otherwise -> `evolving`.

- [ ] **Step 4: Run stationarity tests**

Run:

```bash
python3 -m unittest tests.test_stationarity -v
```

Expected: all tests pass repeatedly with the fixed seed.

- [ ] **Step 5: Commit physical-state analysis**

```bash
git add watcher/models.py watcher/stationarity.py tests/test_stationarity.py
git commit -m "feat: classify physical stationarity"
```

---

### Task 7: Restricted Per-Case Configuration Persistence

**Files:**
- Create: `watcher/persistence.py`
- Create: `tests/test_persistence.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Produces: `default_config() -> WatcherConfig`
- Produces: `load_config(case_dir: Path) -> ConfigLoadResult`
- Produces: `validate_config_payload(payload: object, known_series: AbstractSet[str]) -> WatcherConfig`
- Produces: `save_config(case_dir: Path, config: WatcherConfig) -> None`
- Produces: immutable `WatcherConfig`, `SeriesOverride`, and `ConfigLoadResult`

- [ ] **Step 1: Write failing schema and filesystem-safety tests**

```python
def test_round_trip_configuration() -> None:
    with TemporaryCase() as case:
        config = WatcherConfig(
            version=1,
            selected_log="log.pimpleFoam",
            selected_series=("abc123",),
            overrides={"abc123": SeriesOverride(label="Lift", units="N")},
            accepted_states=frozenset({"plateau", "statistically_stationary", "periodic"}),
        )
        save_config(case.path, config)
        loaded = load_config(case.path)

    assert loaded.config == config
    assert loaded.error is None


def test_rejects_unknown_key_and_non_finite_number() -> None:
    with self.assertRaises(ConfigValidationError):
        validate_config_payload({"version": 1, "extra": True}, set())
    with self.assertRaises(ConfigValidationError):
        validate_config_payload(
            {"version": 1, "selectedSeries": [], "overrides": {"x": {"absoluteFloor": math.inf}}},
            {"x"},
        )


@unittest.skipIf(os.name == "nt", "POSIX symlink and mode semantics")
def test_refuses_configuration_symlink() -> None:
    with TemporaryCase() as case:
        outside = case.path.parent / "outside.json"
        outside.write_text("{}")
        (case.path / ".foam-watcher.json").symlink_to(outside)
        with self.assertRaises(UnsafeConfigPath):
            save_config(case.path, default_config())
```

Also test unsafe absolute/parent log paths, unsupported versions, unknown
series IDs, duplicate IDs, invalid accepted states, malformed existing JSON,
preservation of an invalid existing file, atomic replacement, and POSIX mode
`0600`.

- [ ] **Step 2: Run persistence tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_persistence -v
```

Expected: import failure because `watcher.persistence` does not exist.

- [ ] **Step 3: Implement the closed schema and atomic writer**

The JSON shape is:

```json
{
  "version": 1,
  "selectedLog": "log.pimpleFoam",
  "selectedSeries": ["abc123"],
  "overrides": {
    "abc123": {
      "label": "Lift coefficient",
      "units": null,
      "maxMeanShiftFraction": 0.02,
      "maxNormalizedSlope": 0.02,
      "absoluteFloor": 1e-12
    }
  },
  "acceptedStates": ["periodic", "plateau", "statistically_stationary"]
}
```

Permit only the shown keys; override keys are `label`, `units`,
`maxMeanShiftFraction`, `maxMeanShiftStandardErrors`, `maxNormalizedSlope`,
`minimumEffectiveSamples`, `minimumCycles`, `maxPeriodVariationFraction`,
`maxAmplitudeVariationFraction`, `absoluteFloor`, and `staleAfterSeconds`.

Reject booleans where numbers are required, reject all non-finite floats,
limit labels and units to 100 characters, limit selected series to 1,000, and
require every series ID to be known at update time. Validate `selectedLog` as a
relative non-parent path; final containment is rechecked when the log opens.

Write with `tempfile.mkstemp(prefix=".foam-watcher.", dir=case_dir)`, mode
`0o600`, UTF-8 JSON with `allow_nan=False`, `flush`, `os.fsync`, and
`os.replace`. Resolve the case once at startup, reject a symlink at the target,
and remove only the exact temporary file on failure.

- [ ] **Step 4: Run persistence tests**

Run:

```bash
python3 -m unittest tests.test_persistence -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit safe per-case preferences**

```bash
git add watcher/models.py watcher/persistence.py tests/test_persistence.py
git commit -m "feat: persist watcher preferences safely"
```

---

### Task 8: Snapshot Collector and Bounded API Models

**Files:**
- Create: `watcher/snapshot.py`
- Create: `tests/test_snapshot.py`
- Modify: `watcher/models.py`

**Interfaces:**
- Consumes: all case, log, parser, post-processing, convergence, stationarity, and persistence interfaces
- Produces: `WatcherCollector(case_dir: Path, explicit_log: Path | None = None)`
- Produces: `WatcherCollector.snapshot() -> dict[str, object]`
- Produces: `WatcherCollector.series(series_id: str, limit: int = 2000) -> dict[str, object]`
- Produces: `WatcherCollector.update_config(payload: object) -> dict[str, object]`

- [ ] **Step 1: Write failing collector integration tests**

```python
def test_snapshot_combines_case_numerics_and_selected_series() -> None:
    with populated_transient_case() as case:
        collector = WatcherCollector(case.path)
        snapshot = collector.snapshot()

    assert snapshot["case"]["application"] == "pimpleFoam"
    assert snapshot["case"]["mode"] == "transient_pimple"
    assert snapshot["solver"]["currentTime"] == 0.25
    assert snapshot["numerics"]["kind"] == "transient_health"
    assert snapshot["physical"]["aggregate"]["state"] != "passing"
    assert snapshot["seriesCatalog"]
    json.dumps(snapshot, allow_nan=False)


def test_snapshot_survives_one_broken_postprocessing_file() -> None:
    with populated_transient_case() as case:
        case.write_bytes("postProcessing/broken/0/data.dat", b"\xff\xfe\x00")
        snapshot = WatcherCollector(case.path).snapshot()
    assert snapshot["solver"]["currentTime"] == 0.25
    assert any("broken" in notice["source"] for notice in snapshot["notices"])
```

Also test running/stopped/completed/failed process state, end-time progress,
ETA, missing `/proc`, no log, changed config, downsampling endpoint, unknown
series ID, stale files, and exact JSON safety.

- [ ] **Step 2: Run snapshot tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_snapshot -v
```

Expected: import failure because `watcher.snapshot` does not exist.

- [ ] **Step 3: Implement collector orchestration and serialization**

Construct one `CaseInspection`, discover/rank logs, select explicit then saved
then highest-ranked, and keep one `IncrementalLogReader` and
`OpenFOAMLogParser` per selected log. Recreate reader/parser when selection
changes.

Refresh post-processing when the combined `(maximum mtime_ns, total size,
file count)` signature changes. Re-run stationarity on refresh because the
configuration thresholds may change.

Return these top-level keys:

```text
generatedAt, refreshSeconds, case, process, host, solver, numerics,
physical, seriesCatalog, logSelection, notices, configuration
```

Downsample chart data with min/max envelope buckets so spikes survive. The
catalog contains at most 300 preview points per series; `/api/series` returns
at most 2,000 requested points. Recursively convert dataclasses, `Path`,
tuples, mappings, and sets to JSON-safe primitives; convert every non-finite
float to `None`.

Determine process state from read-only `/proc/<pid>/cwd` and command lines when
available. Fall back to log age:

- active process or modification within 90 seconds -> `running`;
- fatal record -> `failed`;
- `End` or configured end reached -> `completed`;
- parsed partial log without recent activity -> `stopped`;
- no solver log -> `not_started`.

- [ ] **Step 4: Run snapshot tests**

Run:

```bash
python3 -m unittest tests.test_snapshot -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit collector integration**

```bash
git add watcher/models.py watcher/snapshot.py tests/test_snapshot.py
git commit -m "feat: assemble watcher snapshots"
```

---

### Task 9: Secure Loopback HTTP Server and CLI

**Files:**
- Create: `foam-watch`
- Create: `watcher/server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `WatcherCollector`
- Produces: `create_server(case_dir: Path, port: int, explicit_log: Path | None = None) -> ThreadingHTTPServer`
- Produces CLI: `foam-watch [--case PATH] [--log PATH] [--port 8765]`
- HTTP: `GET /api/health`, `GET /api/snapshot`, `GET /api/series?id=...&limit=...`, `GET /api/session`, `POST /api/config`

- [ ] **Step 1: Write failing endpoint and security tests**

Start the server on port zero in a background test thread and use
`urllib.request`:

```python
def test_health_and_security_headers(self) -> None:
    response = self.get("/api/health")
    assert json.loads(response.read()) == {"ok": True}
    assert response.headers["Access-Control-Allow-Origin"] is None
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")


def test_config_post_requires_origin_json_and_token(self) -> None:
    session = json.loads(self.get("/api/session").read())
    payload = json.dumps(valid_config_payload()).encode()

    assert self.post("/api/config", payload, headers={}).status == 403
    assert self.post(
        "/api/config",
        payload,
        headers={
            "Origin": self.base_url,
            "Content-Type": "application/json",
            "X-Watcher-Token": session["token"],
        },
    ).status == 200


def test_path_traversal_is_forbidden(self) -> None:
    assert self.get("/..%2f..%2fetc%2fpasswd").status == 403
```

Also test binding address, unsupported methods, Host/Origin mismatch, wrong
token, missing/incorrect content type, body over 64 KiB, malformed JSON,
unknown series, invalid configuration, static cache policy, API no-store, and
client errors without tracebacks.

- [ ] **Step 2: Run server tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_server -v
```

Expected: import failure because `watcher.server` does not exist.

- [ ] **Step 3: Implement the server and CLI**

Create `ThreadingHTTPServer(("127.0.0.1", port), handler)` directly; expose no
host argument. Reject requests whose `Host` is not `127.0.0.1:<actual-port>` or
`localhost:<actual-port>`. Use an exact allowed-origin set derived from those
two loopback URLs.

Generate `secrets.token_urlsafe(32)` once per server process. `/api/session`
returns the token with `Cache-Control: no-store`; configuration posts require
the exact token header, exact allowed origin, `application/json` media type,
and declared/actual body no larger than 65,536 bytes.

Apply:

```text
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy: camera=(), microphone=(), geolocation=()
Cross-Origin-Opener-Policy: same-origin
```

Serve static assets only from the resolved repository `static` directory after
`relative_to` containment. Use `no-store` for API and `index.html`, and a
five-minute cache for other static assets.

The CLI validates Python version and case before constructing the server,
prints the SSH command with detected hostname, and handles `Ctrl+C` cleanly.
If the requested port is occupied by another watcher, query `/api/health` and
report it; otherwise return a non-zero exit with a suggested next port.

- [ ] **Step 4: Run server and complete-suite tests**

Run:

```bash
python3 -m unittest tests.test_server -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass and the server test confirms
`server.server_address[0] == "127.0.0.1"`.

- [ ] **Step 5: Commit the secure local service**

```bash
git add foam-watch watcher/server.py tests/test_server.py
git commit -m "feat: serve watcher securely on loopback"
```

---

### Task 10: Dependency-Free Dashboard

**Files:**
- Create: `static/index.html`
- Create: `static/styles.css`
- Create: `static/app.js`
- Create: `tests/demo_case.py`
- Create: `tests/test_static_contract.py`

**Interfaces:**
- Consumes: HTTP contracts from Task 9
- Produces: single-case dashboard with overview, residual, transient-health, physical-quantity, stationarity, and diagnostics views

- [ ] **Step 1: Write failing static contract tests**

```python
from html.parser import HTMLParser


class DashboardHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.external_assets: list[str] = []
        self.h1_count = 0
        self.live_regions = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "h1":
            self.h1_count += 1
        if values.get("aria-live") == "polite":
            self.live_regions += 1
        source = values.get("src") or values.get("href")
        if source and source.startswith(("http://", "https://", "//")):
            self.external_assets.append(source)


class StaticContractTests(unittest.TestCase):
    def test_served_dashboard_has_accessible_views_and_local_assets(self) -> None:
        response = self.get("/")
        parser = DashboardHTMLParser()
        parser.feed(response.read().decode("utf-8"))

        for view_id in (
            "overview-view",
            "residuals-view",
            "transient-view",
            "physical-view",
            "stationarity-view",
            "diagnostics-view",
        ):
            self.assertIn(view_id, parser.ids)
        self.assertEqual(parser.h1_count, 1)
        self.assertGreaterEqual(parser.live_regions, 1)
        self.assertEqual(parser.external_assets, [])
        self.assertEqual(response.headers["Content-Security-Policy"].split(";")[0], "default-src 'self'")

    def test_frontend_assets_are_served_with_expected_media_types(self) -> None:
        self.assertIn("text/css", self.get("/styles.css").headers["Content-Type"])
        self.assertIn("javascript", self.get("/app.js").headers["Content-Type"])
```

Parse the served HTML to assert labeled navigation buttons, table headers,
checkbox label association, and required DOM IDs. Verify reduced-motion and
responsive behavior during the manual browser smoke test instead of grepping
stylesheet source.

- [ ] **Step 2: Run static contract tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_static_contract -v
```

Expected: failure because static files do not exist.

- [ ] **Step 3: Build the semantic HTML and responsive visual system**

Create:

- a header with case/application/version/mode, connection state, refresh, and
  loopback/SSH indicator;
- four status cards for process, numerics, physical state, and progress/rate;
- keyboard-operable tab buttons using `aria-controls` and `aria-selected`;
- six views with semantic headings, tables, form labels, and chart canvases;
- a polite live region for connection and save results;
- a diagnostics notice list that displays text via `textContent`.

Use CSS custom properties and a dark green/cyan/amber/red control-room palette.
At widths below 1100 px, stack the main columns; below 700 px, use one-column
status cards and horizontally scroll data tables. Honor
`prefers-reduced-motion: reduce`.

- [ ] **Step 4: Implement polling, rendering, settings, and charts**

In `static/app.js`:

1. Fetch `/api/session` once and retain the token in memory.
2. Fetch `/api/snapshot` immediately and every returned `refreshSeconds`.
3. Preserve the selected tab and expanded series across refreshes.
4. Render all untrusted values with `textContent` and DOM construction.
5. Hide the transient tab for non-transient modes.
6. Draw residuals on a log-y Canvas chart with target lines.
7. Draw selected physical histories and stationarity windows on linear charts.
8. Submit complete configuration JSON to `/api/config`; let the browser supply
   the same-origin `Origin` header and set `Content-Type` plus
   `X-Watcher-Token` explicitly.
9. Disable a changed checkbox while saving, restore it on failure, and announce
   the result through the live region.
10. Fetch `/api/series?id=<encoded>&limit=2000` when a series detail opens.

Implement a reusable Canvas renderer that uses device-pixel ratio, finite data
only, at most six tick labels per axis, visible empty states, min/max envelopes,
and `requestAnimationFrame` resize debouncing.

- [ ] **Step 5: Run frontend contract and server tests**

Run:

```bash
python3 -m unittest tests.test_static_contract tests.test_server -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 6: Manually smoke-test the dashboard with fixture data**

Run:

```bash
python3 -m tests.demo_case --output /tmp/foam-watch-demo
./foam-watch --case /tmp/foam-watch-demo --port 8765
```

Open `http://127.0.0.1:8765`, verify every view, resize to phone width, toggle a
physical quantity, reload, and confirm
`/tmp/foam-watch-demo/.foam-watcher.json` retains the selection. Stop with
`Ctrl+C`.

Expected: no browser console errors, no external requests, and no writes other
than `.foam-watcher.json`.

- [ ] **Step 7: Commit the dashboard**

```bash
git add static tests/test_static_contract.py tests/demo_case.py
git commit -m "feat: add single-case monitoring dashboard"
```

---

### Task 11: Documentation, Security Guidance, and Release Verification

**Files:**
- Create: `README.md`
- Create: `SECURITY.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: completed CLI, dashboard, and tests
- Produces: reproducible installation, SSH, interpretation, troubleshooting, and security instructions

- [ ] **Step 1: Write operator and contributor documentation**

`README.md` must include:

- supported scope and non-goals;
- cloning under `~/OpenFOAM-Solver-Watcher`;
- direct launch and optional `~/.local/bin` symlink;
- CLI reference for `--case`, `--log`, and `--port`;
- exact local and SSH-tunnel workflows;
- explanations of numerical convergence, transient health, plateau,
  statistical stationarity, periodicity, and indeterminate results;
- candidate selection and `.foam-watcher.json`;
- restart/log-selection behavior;
- test command;
- troubleshooting for no log, unknown solver, stale output, occupied port,
  missing `/proc`, and invalid configuration;
- GitHub contribution workflow without machine-specific paths.

`SECURITY.md` must state:

- loopback plus SSH is the only supported remote-access mode;
- direct network exposure and reverse-proxy deployment are unsupported in
  version one;
- the single allowed case write;
- no solver control;
- local same-user trust assumptions;
- how to report a vulnerability without posting exploit details publicly.

Ignore Python bytecode, cache directories, coverage output, editor files, and
`.foam-watcher.json` in `.gitignore`.

- [ ] **Step 2: Review documentation against the operator workflow**

Follow the README from a fresh shell: run `foam-watch --help`, generate the
demo case, start the documented local server command, and construct the
documented SSH forwarding command. Verify every command uses repository- and
case-relative paths rather than this development machine's absolute paths.
Check that `SECURITY.md` clearly rejects direct network exposure and names
`.foam-watcher.json` as the only case write.

- [ ] **Step 3: Run all automated verification**

Run:

```bash
python3 -m compileall -q watcher tests
python3 -m unittest discover -s tests -v
python3 -c "import tomllib, pathlib; data = tomllib.loads(pathlib.Path('pyproject.toml').read_text()); assert data['project'].get('dependencies', []) == []"
```

Expected: compilation succeeds, every test passes, and project metadata
declares no runtime dependencies.

- [ ] **Step 4: Perform representative steady and transient smoke tests**

For one steady case:

```bash
cd /path/to/steady-case
~/OpenFOAM-Solver-Watcher/foam-watch --port 8765
```

Verify application/mode detection, residual target matching, convergence
evidence, post-processing discovery, checkbox persistence, and advisory-only
behavior.

For one transient case, repeat and verify time, `deltaT`, Courant values,
continuity, correctors, healthy-step percentage, selected physical histories,
and a non-false-positive stationarity result.

- [ ] **Step 5: Perform the SSH-tunnel security smoke test**

On the client:

```bash
ssh -L 8765:127.0.0.1:8765 user@solver-host
```

Open `http://127.0.0.1:8765`. On the solver host, verify the listener:

```bash
ss -ltnp | grep ':8765'
```

Expected: listener address is `127.0.0.1:8765`, the remote dashboard works
through SSH, direct access to `solver-host:8765` fails, browser network tools
show no third-party requests, and the server creates or changes no case file
other than `.foam-watcher.json`.

- [ ] **Step 6: Review the final diff and repository status**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -12
```

Expected: no whitespace errors, only intended tracked changes, and one focused
commit per task.

- [ ] **Step 7: Commit documentation and release verification**

```bash
git add README.md SECURITY.md .gitignore tests
git commit -m "docs: document secure watcher operation"
```

---

## Final Acceptance Checklist

- [ ] A fresh clone under `~/OpenFOAM-Solver-Watcher` runs on Python 3.10+
      without package installation.
- [ ] The process binds only to `127.0.0.1`.
- [ ] SSH forwarding provides remote access.
- [ ] One watcher process monitors one valid case.
- [ ] The selected solver log is automatic, explainable, and overrideable.
- [ ] Steady and transient telemetry are parsed incrementally across restarts.
- [ ] Unknown standard-format solvers degrade gracefully.
- [ ] Known and generic `postProcessing` tables appear as normalized series.
- [ ] Suggested physical candidates are explained and user-selectable.
- [ ] Selections survive reload through `.foam-watcher.json`.
- [ ] Numerical and physical verdicts remain separate.
- [ ] Evolving, plateau, statistically stationary, periodic, and indeterminate
      states have evidence and false-positive tests.
- [ ] No process-control or OpenFOAM-editing endpoint exists.
- [ ] Configuration writes pass schema, containment, symlink, token, origin,
      size, and atomicity tests.
- [ ] All automated tests and three manual smoke-test classes pass.
