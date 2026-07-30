# OpenFOAM Solver Watcher Design

**Date:** 2026-07-28  
**Status:** Approved for implementation planning

## Purpose

Build a dependency-free, advisory-only web dashboard for monitoring one
OpenFOAM case on the machine where the solver runs. The watcher detects the
solver and available telemetry automatically, reports numerical convergence,
discovers function-object output, and distinguishes an evolving transient from
a plateau, statistically stationary state, or periodic state.

The server listens only on `127.0.0.1`. A remote user reaches it through an SSH
port-forwarding tunnel. The watcher never starts, stops, signals, or modifies an
OpenFOAM simulation.

## Scope

Version one monitors exactly one case per watcher process. It supports:

- Steady SIMPLE cases.
- Transient PISO and PIMPLE cases.
- Pseudo-transient and local-time-stepping cases when their log exposes enough
  evidence for detection.
- Parallel and multi-region cases.
- Standard OpenFOAM solvers, specialized solver families, and custom solvers
  that retain recognizable OpenFOAM log or function-object formats.
- Automatic function-object discovery with known adapters and a generic
  numeric-table fallback.
- Numerical convergence and transient numerical-health reporting.
- User-selected physical quantities for plateau, statistical-stationarity, and
  periodic-state decisions.

Version one is advisory only. It does not control the solver, edit OpenFOAM
dictionaries, expose the server to a network interface, or monitor several
independent cases from one process.

## Repository and Installation Layout

The project is a GitHub-ready source repository that can be cloned directly
under the user's home directory:

```text
~/OpenFOAM-Solver-Watcher/
├── foam-watch
├── watcher/
│   ├── case_config.py
│   ├── log_reader.py
│   ├── log_parser.py
│   ├── postprocessing.py
│   ├── convergence.py
│   ├── stationarity.py
│   ├── snapshot.py
│   ├── persistence.py
│   └── server.py
├── static/
├── tests/
├── docs/
├── README.md
└── pyproject.toml
```

The application runs directly from the checkout:

```bash
cd /path/to/OpenFOAM/case
~/OpenFOAM-Solver-Watcher/foam-watch
```

An optional setup step may create a symlink at
`~/.local/bin/foam-watch`. No package installation or virtual environment is
required. Python 3.10 or newer and its standard library are the only runtime
requirements.

The only watcher-owned file inside a case is:

```text
<case>/.foam-watcher.json
```

It stores the selected log, selected physical quantities, labels, units, and
threshold overrides. It contains no runtime cache or parsed solver data.

## Architecture

The application uses a capability-based pipeline:

```text
case inspection
  -> log discovery and incremental reading
  -> normalized solver events
  -> function-object discovery and parsing
  -> numerical analysis
  -> physical-state analysis
  -> normalized snapshot
  -> localhost HTTP API and dashboard
```

Each component has one responsibility:

- `case_config.py` recognizes a case and reads the limited OpenFOAM dictionary
  values needed for monitoring.
- `log_reader.py` discovers and incrementally reads solver logs while handling
  truncation, rotation, restarts, and partial final lines.
- `log_parser.py` converts solver text into normalized time, residual, corrector,
  continuity, Courant, timing, warning, failure, and completion events.
- `postprocessing.py` discovers function objects and converts known or generic
  tabular outputs into normalized series.
- `convergence.py` evaluates steady convergence and transient numerical health.
- `stationarity.py` evaluates selected physical series for evolution, plateau,
  statistical stationarity, and periodicity.
- `snapshot.py` assembles a stable, JSON-safe API model without non-finite
  numbers.
- `persistence.py` validates and atomically updates the single per-case
  configuration file.
- `server.py` serves the static dashboard and the restricted API on loopback.

Unknown solvers and unknown function objects do not disable the watcher. The
dashboard exposes every capability that can be supported by the data actually
found.

## Case and Execution-Mode Detection

The watcher accepts the case path from the current working directory by
default. A `--case PATH` option may select another case. A valid case contains
`system/controlDict` and at least one of `constant`, an initial time directory,
or a numeric result time directory.

Inspection reads:

- `system/controlDict` for application, start and end controls, time-step
  controls, write controls, and function definitions.
- `system/fvSolution` for SIMPLE, PISO, and PIMPLE settings, linear-solver
  tolerances, and `residualControl`.
- `system/fvSchemes` for time-discretization evidence.
- Initial and latest time directories for fields and regions.
- `processor*` directories for decomposition evidence.
- Solver log banners and emitted runtime records.

The dictionary reader handles comments, nested dictionaries, lists, regex field
keys, variable references needed for matching, and `#include` directives. It
may follow includes only when the resolved file is readable and lies within the
case or a recognized OpenFOAM installation directory. It never executes code
directives, shell commands, or dynamic content.

Detection produces one of these modes:

- steady SIMPLE;
- transient PISO/PIMPLE;
- pseudo-transient/local-time-stepping;
- unknown/custom.

Parallel and multi-region are independent capability flags. Detection records
its evidence and confidence so the dashboard can explain ambiguous results.

## Log Discovery and Reading

An explicit `--log PATH` has highest priority. Otherwise, the watcher searches
case-local `log.*`, `.log`, and common redirected-output names. Candidates are
scored using:

- match with the configured application;
- an OpenFOAM banner;
- recognizable solver events;
- modification time and recent growth;
- location at the case root;
- absence of signatures identifying a preprocessing utility.

The selected log and ranked alternatives are visible in the dashboard. A user
may choose another candidate, and that relative path is saved in
`.foam-watcher.json`. Explicit and saved log paths must resolve inside the case.

The reader tracks file identity, size, and offset. It detects truncation or
replacement, keeps restart segments distinct, ignores incomplete final lines
until completed, and avoids reparsing unchanged content during normal polling.
Later restart data supersedes earlier data at duplicate simulation times for
physical time series. Residual histories preserve segment boundaries so a
restart is not presented as a continuous numerical trend.

## Solver Telemetry

Core telemetry includes:

- current simulation time or steady iteration;
- time-step index when available;
- initial and final residuals;
- linear-solver iteration counts and convergence flags;
- SIMPLE/PISO/PIMPLE corrector progress;
- local, global, and cumulative continuity errors;
- mean and maximum Courant number;
- mesh Courant number;
- `deltaT` and adjustable-time-step behavior;
- execution and clock time;
- simulated-time rate, wall-clock rate, real-time factor, progress, and ETA when
  mathematically meaningful;
- solver completion, convergence messages, warnings, fatal errors, MPI aborts,
  floating-point exceptions, and segmentation failures.

Additional named scalar or vector fields are retained dynamically. Solver names
are hints for labeling and detection, not a hard-coded compatibility boundary.

## Numerical Convergence and Health

Numerical convergence and physical stationarity are separate verdicts.

For steady cases, the watcher:

- parses `residualControl`, including regex groups and vector components;
- compares the corresponding initial residual from each complete iteration;
- reports final residuals and linear iteration counts separately;
- treats a solver-emitted convergence-criteria message as authoritative;
- reports both the current threshold result and whether it has held for recent
  complete iterations;
- reports `not_configured` when no convergence criteria can be derived.

The watcher never claims numerical convergence solely from an arbitrary
hard-coded residual threshold.

For transient cases, numerical health is evaluated per completed time step. The
dashboard reports target attainment, corrector behavior, continuity, Courant
limits, `deltaT`, repeated or rejected steps, solver failures, recent
healthy-step percentage, and degrading trends. This result does not claim that
the physical solution is globally converged.

## Function-Object Discovery

Discovery combines `controlDict` definitions with outputs found under
`postProcessing/`. Function-object names need not follow a convention.

Known adapters cover:

- `solverInfo` and residual tables;
- `volFieldValue` and `surfaceFieldValue`;
- `probes` and `patchProbes`;
- `forces` and `forceCoeffs`;
- `fieldAverage`;
- flow, flux, pressure, power, and pressure-drop outputs;
- `yPlus`, wall shear stress, wall heat flux, and heat-transfer coefficient;
- interface height, phase measures, and phase forces;
- species, reaction-rate, heat-release, and combustion outputs;
- cloud and particle statistics;
- six-degree-of-freedom and moving-mesh outputs;
- common scalar, vector, tensor, and multi-region tables.

The generic adapter handles whitespace- or delimiter-separated numeric tables
with comment headers. It identifies time columns, scalar columns, vector and
tensor groupings, restart directories, duplicate times, non-finite values, and
an actively written incomplete row. Unknown series remain chartable and
selectable.

Every normalized series records a stable identifier, display label, source
function and file, region, field, operation, component, units when known,
sample times, values, freshness, and parser notices.

## Candidate Selection

Candidate scoring uses function-object type, operation, field name, history
length, sampling regularity, data variability, and whether the quantity
represents a physical result or a numerical diagnostic.

High-confidence candidates include force and moment coefficients, flow rates,
pressure differences, region averages and integrals, heat transfer, species
measures, phase measures, and user probes. Residuals, Courant number, and
wall-quality diagnostics stay visible but are not preselected as physical
quasi-steady gates.

The physical-quantity view shows every discovered series with:

- a selection checkbox;
- source and human-readable label;
- known, configured, or user-assigned units;
- candidate confidence and explanation;
- current value and history;
- threshold controls;
- per-series state and evidence.

The aggregate physical verdict uses only checked series. Zero checked series,
missing checked data, stale checked data, or insufficient evidence cannot
produce a positive verdict.

## Physical-State Analysis

Analysis operates on scalar series. Vector and tensor outputs are offered as
individual components and, where physically valid, derived magnitudes.

Before analysis, samples are ordered, restart duplicates are resolved, invalid
values are removed with a visible notice, and gross sampling gaps split the
history into segments. Only the latest sufficiently continuous segment gates
the current verdict.

Rolling adjacent windows evaluate:

- normalized slope;
- adjacent-window mean shift;
- standard deviation and robust spread;
- mean shift relative to estimated standard error;
- integrated autocorrelation time;
- effective independent sample count;
- data freshness and coverage.

The conservative default plateau or statistical-stationarity evidence requires:

- at least 20 effective independent samples;
- adjacent-window mean shift below 2 percent;
- mean shift below two estimated standard errors;
- no significant normalized trend.

Thresholds are configurable per selected quantity. Relative checks use a
stable scale based on the larger of the recent absolute mean, robust spread,
and a configurable absolute floor so quantities near zero remain well-defined.

Periodicity analysis detrends the latest continuous series, identifies
non-zero-lag autocorrelation peaks, and validates the candidate period against
cycle-to-cycle behavior. A periodic result requires at least three complete
recent cycles and stable cycle mean, period, and amplitude. A detected
oscillation with drifting mean or amplitude remains `evolving`.

Each series is classified as:

- `evolving`;
- `plateau`;
- `statistically_stationary`;
- `periodic`;
- `indeterminate`.

The aggregate verdict passes only when every selected series is in one of the
user-accepted passing states. Version one accepts plateau, statistically
stationary, and periodic as passing states by default, while displaying their
different meanings explicitly.

## Per-Case Configuration

`.foam-watcher.json` uses a versioned, closed schema. It stores:

- schema version;
- selected relative log path;
- selected stable series identifiers;
- optional display labels and units for discovered series;
- per-series analysis-threshold overrides;
- accepted physical states.

Unknown top-level keys, invalid types, non-finite values, unsafe paths, and
unsupported schema versions are rejected with an actionable dashboard error.
Default behavior remains available if the file is absent or invalid; an invalid
file is never silently overwritten.

The server writes the configuration through a same-origin, token-protected JSON
endpoint. It writes a complete validated temporary file in the case directory,
sets user-only permissions where supported, flushes it, and atomically replaces
the target. It refuses to write through a symlink.

## Dashboard

The frontend uses bundled HTML, CSS, and JavaScript with Canvas or SVG charts.
It makes no external requests and loads no CDN assets.

The header shows case identity, absolute path, application, OpenFOAM version,
execution mode, region count, process state, last activity, and loopback/SSH
security posture.

Four primary status cards show:

- solver/process state;
- numerical convergence or transient-step health;
- physical state;
- progress, rate, and ETA.

Views include:

1. **Overview:** current state, warnings, case facts, timing, and verdicts.
2. **Residuals:** log-scale histories, targets, values, linear iterations, and
   pass/fail evidence.
3. **Transient health:** time, step, `deltaT`, Courant data, continuity,
   correctors, real-time factor, and healthy-step percentage. It is hidden when
   irrelevant.
4. **Physical quantities:** discovered series, candidate selection, charts,
   units, confidence, and thresholds.
5. **Stationarity:** rolling windows, trend, variability, effective samples,
   periods, cycle stability, and aggregate reasoning.
6. **Diagnostics:** warnings, failures, selected and alternative logs, parser
   notices, stale data, and available host metrics.

The browser polls lightweight snapshots and requests detailed series only when
needed. The interface is responsive, keyboard accessible, readable without
animation, and consistent with the dark control-room style of the reference
dashboard.

## HTTP Interface and Security

The server binds to IPv4 loopback `127.0.0.1` only. Version one has no option to
bind to `0.0.0.0` or another network interface.

Remote use follows this pattern:

```bash
ssh -L 8765:127.0.0.1:8765 user@solver-host
```

The HTTP surface consists of static assets, read-only snapshot and series
endpoints, a health endpoint, and one configuration-update endpoint.

Controls include:

- no CORS;
- strict Content Security Policy and browser security headers;
- same-origin validation and an unguessable per-process session token for
  writes;
- JSON-only configuration writes with a small request-size limit;
- fixed configuration destination;
- static and log path containment checks;
- no shell execution or solver/process-control endpoints;
- concise client errors without raw Python tracebacks.

Host process inspection uses read-only `/proc` data when available. Missing
`/proc`, unavailable load metrics, or restricted process visibility does not
prevent file-based monitoring.

## Resilience

An actively running solver may leave files in incomplete states. Parsers skip
incomplete records, retain the last valid snapshot, mark stale sources, and
surface concise notices instead of failing the entire refresh.

The watcher degrades gracefully for missing files, unknown formats, log
replacement, restart data, duplicate times, absent host metrics, and temporary
read errors. A failure in one function-object adapter does not suppress other
series or core solver telemetry.

## Verification

The standard-library `unittest` suite covers:

- OpenFOAM dictionaries, includes, comments, regex keys, and rejected dynamic
  directives;
- steady, transient, pseudo-transient, parallel, multi-region, failure,
  restart, and custom-solver logs;
- known and generic function-object tables;
- incomplete rows, vectors, tensors, non-finite values, restart segments, and
  sampling gaps;
- synthetic evolving, plateau, statistically stationary, and periodic signals;
- false-positive resistance for insufficient, discontinuous, or stale data;
- configuration schema validation, symlink refusal, permissions, and atomic
  persistence;
- HTTP traversal, origin, token, content-type, and payload-size protections;
- end-to-end snapshots from temporary case directories.

Release readiness requires the complete test suite to pass and a manual
SSH-tunnel smoke test with representative steady and transient OpenFOAM cases.

## Success Criteria

Version one is successful when a user can clone the repository into their home
directory, enter a single OpenFOAM case, start `foam-watch` without installing
packages, connect through an SSH tunnel, and:

- see the solver and execution mode detected with supporting evidence;
- inspect live steady or transient numerical telemetry;
- understand whether configured numerical convergence has been met;
- inspect all discoverable function-object histories;
- select physical quantities with checkboxes and retain those selections in the
  case;
- receive evidence-backed evolving, plateau, statistically stationary, or
  periodic classifications;
- recover cleanly from restarts and actively written files;
- do all of this without exposing a network listener or controlling the solver.
