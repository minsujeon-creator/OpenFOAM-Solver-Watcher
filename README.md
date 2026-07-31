# OpenFOAM Solver Watcher

A dependency-free, single-case dashboard for watching an OpenFOAM solver or
`snappyHexMesh` run from your browser. It automatically chooses the active
workflow. Solver mode reports residuals, numerical convergence or transient
health, `postProcessing` histories, and advisory quasi-steady/stationarity
evidence. Meshing mode reports stages, phase-local progress, mesh counts,
surface layer coverage, and warnings. For either workflow, a stable mesh is
checked automatically with the installed OpenFOAM `checkMesh` utility.

The server listens on `127.0.0.1` only. For a solver running on another
machine, access the dashboard through an SSH tunnel rather than exposing a web
port.

> **Status:** version 0.1 is advisory. It never starts, stops, signals, or
> modifies the OpenFOAM solver.

## Highlights

- Python 3.10+ standard library only; no `pip install` is required.
- Automatic `controlDict` application and steady/transient mode detection.
- Automatic solver/`snappyHexMesh` workflow detection and log selection, with
  an explicit `--log` override.
- Incremental residual, time, Courant number, continuity, corrector, execution
  time, and fatal-state parsing.
- Separate numerical and physical assessments:
  - steady residual convergence;
  - transient-step health;
  - plateau detection;
  - statistical stationarity;
  - periodic-state detection.
- Automatic discovery of scalar, vector, force, moment, coefficient, probe,
  and other table-like `postProcessing` output.
- Suggested physical quantities are opt-in checkboxes.
- Per-case preferences stored in one restricted file:
  `.foam-watcher.json`.
- Bounded API responses and charts so long-running cases remain responsive.
- Responsive, accessible dashboard with no CDN or third-party web assets.
- Live `snappyHexMesh` stage, morph/smoothing, layer, mesh-size,
  `maxGlobalCells`, warning, completion, failure, and stale evidence.
- Automatic background `checkMesh -latestTime -allTopology -allGeometry`
  after mesh files remain stable for 15 seconds, with `-meshQuality` added when
  its required dictionary exists.
- Thorough mesh geometry/topology evidence and per-patch realised-versus-
  requested layer coverage, presented as advisories rather than guessed
  acceptance criteria.

## Scope and non-goals

One watcher process monitors one OpenFOAM case. Version 0.1 supports log and
function-object formats that can be interpreted safely as text tables.
Unrecognized data is reported as a notice instead of guessed.

The watcher does **not**:

- decide that a CFD result is physically valid;
- replace mesh, discretization, conservation, or uncertainty checks;
- control or restart a solver;
- edit OpenFOAM dictionaries;
- support direct public-network exposure, reverse proxies, or multi-user
  hosting.

## Install

Clone it into your home directory on the machine where OpenFOAM runs:

```bash
cd ~
git clone https://github.com/minsujeon-creator/OpenFOAM-Solver-Watcher.git
cd OpenFOAM-Solver-Watcher
python3 --version
./foam-watch --help
```

Python 3.10 or newer is required.

Launch the watcher from a shell where your OpenFOAM environment is sourced so
that `checkMesh` is on `PATH`. The dashboard remains usable if the utility is
unavailable and explains the missing environment in Mesh quality.

Optionally make `foam-watch` available everywhere:

```bash
mkdir -p ~/.local/bin
ln -sfn "$HOME/OpenFOAM-Solver-Watcher/foam-watch" \
  "$HOME/.local/bin/foam-watch"
```

Ensure `~/.local/bin` is in `PATH`, for example:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Run beside a case

From the case directory:

```bash
cd /path/to/openfoam-case
~/OpenFOAM-Solver-Watcher/foam-watch --port 8765
```

Or supply the case explicitly:

```bash
~/OpenFOAM-Solver-Watcher/foam-watch \
  --case /path/to/openfoam-case \
  --port 8765
```

Open <http://127.0.0.1:8765> on the same machine.

### CLI

```text
foam-watch [--case PATH] [--log PATH] [--port PORT]
```

- `--case PATH` — case directory; defaults to the current directory.
- `--log PATH` — select a particular OpenFOAM log, such as
  `log.pimpleFoam` or `log.snappyHexMesh`; otherwise the workflow and log are
  selected automatically.
- `--port PORT` — loopback port; defaults to `8765`.

The case must contain `system/controlDict` and either `constant/` or a numeric
time directory.

## Remote access through SSH

Start the watcher on the solver host:

```bash
cd /path/to/openfoam-case
~/OpenFOAM-Solver-Watcher/foam-watch --port 8765
```

On your local computer, create the tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@solver-host
```

Keep that SSH session open and browse to
<http://127.0.0.1:8765>.

If local port `8765` is occupied, use a different local port while keeping the
remote port unchanged:

```bash
ssh -N -L 9876:127.0.0.1:8765 user@solver-host
```

Then open <http://127.0.0.1:9876>.

The watcher intentionally has no option to bind to `0.0.0.0`.

## Reading the dashboard

### snappyHexMesh progress

When `log.snappyHexMesh` is the best active match, the watcher changes to a
dedicated Meshing view. It reads `system/snappyHexMeshDict` when available and
shows:

- castellation, snapping, layer-addition, finalization, and completion stages;
- active and completed zero-based morph loop positions;
- displacement-smoothing progress from `nSolveIter`;
- latest cell, face, and point counts;
- `maxGlobalCells`, OpenFOAM warnings, fatal failures, and log age;
- running, completed, failed, stale, or stopped evidence.

A percentage is shown only when its denominator is known, and it is always
labelled as progress within the current phase. For example, `13/15 = 86.7%`
means 13 morph iterations have completed; it does **not** mean 86.7% of the
total wall-clock meshing run. The watcher does not estimate a snappyHexMesh
ETA.

When layer addition prints its final `patch faces layers overall thickness`
table, the watcher compares each patch's average realised layer count with the
matching `nSurfaceLayers` entry in `addLayersControls.layers`. Exact names are
preferred and OpenFOAM-style regex selectors are supported. The table also
shows average thickness and the printed realised/wanted thickness percentage.
`met`, `partial`, and `missing` refer only to that reported patch average; they
do not prove that every face has the requested layers or that the near-wall
resolution is suitable.

### Mesh quality

The Mesh quality view is available for solver and meshing workflows. The
watcher observes the core undecomposed `polyMesh` files and automatically runs
one background assessment after their size and modification signature remains
unchanged for 15 seconds:

```bash
checkMesh -latestTime -allTopology -allGeometry
```

When `system/meshQualityDict` exists, the watcher automatically appends
`-meshQuality` to apply those user-defined criteria. OpenFOAM requires that
dictionary for the option, so omitting the flag when the file is absent avoids
turning an otherwise valid thorough check into a fatal missing-file error.

It never starts the check while active `snappyHexMesh` evidence says the mesh
is being generated. A later mesh or `meshQualityDict` change schedules one new
check after another stable interval; unchanged inputs are not checked
repeatedly. Only one `checkMesh` child can run at a time.

The view retains the authoritative `Mesh OK` or failed-check count, command
exit status, mesh dimensions and regions, explicitly printed geometry metrics,
problem-face/cell counts, and a bounded tail of the utility output. Individual
metrics are marked passing or failing only when `checkMesh` provides that
evidence. The watcher does not invent a universal quality threshold.

The invocation is read-only: it does not pass `-writeSets` or
`-writeSurfaces`. It also does not guess an MPI command. If a case contains
only `processor*` meshes, reconstruct an undecomposed mesh before using the
automatic check. Version 0.1 checks the default-region undecomposed mesh.

### Numerical convergence

For steady solvers, residual targets are read from OpenFOAM convergence
controls when possible. The watcher reports the target coverage and recent
residual behavior. A passing indication means the configured numerical
criteria appear satisfied; it is not proof of mesh independence or physical
accuracy.

### Transient health

For transient solvers, residual convergence at the final simulated time is not
the appropriate global criterion. The watcher instead summarizes recent time
steps, Courant values, continuity errors, correctors, fatal records, and the
fraction of healthy completed steps.

### Physical state

Physical assessment uses only the quantities selected in the dashboard:

- **Evolving** — recent windows still show material drift or change.
- **Plateau** — adjacent recent windows have a small mean shift and slope.
- **Statistically stationary** — window evidence is stable after accounting
  for autocorrelation and effective sample count.
- **Periodic** — stable repeated cycles are detected with consistent period,
  amplitude, and cycle mean.
- **Indeterminate** — evidence is missing, stale, discontinuous, too short, or
  otherwise insufficient.

Physical stationarity and numerical convergence are deliberately independent.
A case may satisfy one and not the other.

## Physical quantities and saved preferences

The watcher scans `postProcessing/` and presents detected series as candidates.
Select only quantities that make physical sense for your case—for example:

- force or moment coefficients;
- pressure drop or mass flow;
- probe temperature or concentration;
- area/volume averages;
- heat-transfer or flux quantities.

Selections, labels, units, accepted states, threshold overrides, and an
optional selected log are saved in:

```text
<case>/.foam-watcher.json
```

This is the only case file the watcher is allowed to create or replace.
Configuration writes are schema-validated, size-limited, symlink-checked, and
atomic. Deleting the file restores defaults.

## Logs and restarts

When `--log` and a saved log selection are absent, candidate logs are
classified and ranked from their path, bounded content evidence, configured
solver, running case-contained processes, and modification time. A running
workflow wins; otherwise a newer recognized `snappyHexMesh` run can supersede
an old solver log, while unrelated utility logs do not. The chosen log and all
alternatives are shown in Diagnostics.

The reader tails logs incrementally and detects truncation or replacement.
Repeated time values and restart segments in `postProcessing` tables are
resolved conservatively. Use `--log` when several active logs are ambiguous.

## Test

From the repository:

```bash
python3 -m compileall -q watcher tests
python3 -m unittest discover -s tests -v
python3 -c "import pathlib, tomllib; data = tomllib.loads(pathlib.Path('pyproject.toml').read_text()); assert data['project'].get('dependencies', []) == []"
```

Create a deterministic demonstration case:

```bash
python3 -m tests.demo_case --output /tmp/foam-watch-demo
./foam-watch --case /tmp/foam-watch-demo --port 8765
```

Create a `snappyHexMesh` demonstration instead:

```bash
python3 -m tests.demo_case --workflow snappy --output /tmp/foam-watch-snappy-demo
./foam-watch --case /tmp/foam-watch-snappy-demo --port 8765
```

## Troubleshooting

### No solver log

Start the solver with a captured log, for example:

```bash
pimpleFoam 2>&1 | tee log.pimpleFoam
```

Or pass the correct existing file with `--log`.

For meshing, capture the log in the same way:

```bash
snappyHexMesh -overwrite 2>&1 | tee log.snappyHexMesh
```

### Unknown solver or mode

Check `application` in `system/controlDict`. Included dictionaries are
supported within the case and trusted OpenFOAM configuration roots, but highly
dynamic preprocessing may remain indeterminate. Telemetry can still be shown
without claiming convergence.

### Output is stale

Confirm that the solver or function objects are still writing, that simulation
time is advancing, and that the selected log is current. Stale physical series
cannot produce a passing aggregate state.

### Mesh quality says unavailable

If the dashboard says `checkMesh` is not on `PATH`, stop the watcher, source
the OpenFOAM environment used for the case, and start it again from that
shell. If it reports a decomposed-only mesh, reconstruct the case first using
the reconstruction workflow appropriate to your OpenFOAM version. The watcher
intentionally does not run MPI or reconstruct case data.

### Port is occupied

Choose another port:

```bash
./foam-watch --case /path/to/case --port 8766
```

### `/proc` is unavailable

On non-Linux systems or restricted containers, process detection falls back to
solver-log age. This affects the running/stopped label but not parsed log data.

### Invalid `.foam-watcher.json`

The watcher preserves an invalid file and reports the validation error. Rename
it for inspection or delete it to restore defaults:

```bash
mv .foam-watcher.json .foam-watcher.json.invalid
```

### Dashboard is not reachable remotely

Do not change the bind address. Verify the watcher on the solver host:

```bash
curl http://127.0.0.1:8765/api/health
ss -ltnp | grep ':8765'
```

Then verify the SSH tunnel and browse to the client-side loopback port.

## Contributing

1. Fork the repository and create a focused branch.
2. Add or update standard-library `unittest` coverage.
3. Run the complete test commands above.
4. Open a pull request describing the OpenFOAM version, solver, representative
   log/function-object format, and expected watcher behavior.

Avoid committing real case data, credentials, private hostnames, or
`.foam-watcher.json`.

## Security

See [SECURITY.md](SECURITY.md). The supported remote-access design is:

```text
browser -> local 127.0.0.1 -> SSH tunnel -> solver-host 127.0.0.1 -> watcher
```
