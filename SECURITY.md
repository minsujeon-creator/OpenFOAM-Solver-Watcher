# Security Policy

## Supported deployment

OpenFOAM Solver Watcher version 0.1 supports a single deployment model:

1. run the watcher as the same trusted user who owns or reads the OpenFOAM
   case;
2. listen only on `127.0.0.1`;
3. use SSH local-port forwarding for remote access.

Example:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@solver-host
```

Direct network exposure, binding to non-loopback addresses, reverse-proxy
deployment, shared hosting, and multi-user access are unsupported.

## Data access and writes

The watcher reads:

- `system/controlDict` and referenced case configuration needed for
  inspection;
- solver log files selected inside the case;
- text files below `postProcessing/`;
- core files in the latest complete undecomposed `polyMesh` to detect a stable
  mesh signature;
- limited read-only process information from `/proc` when available.

The only case file it may create or replace is:

```text
.foam-watcher.json
```

Configuration is schema-validated and written atomically. The watcher does not
edit OpenFOAM dictionaries or result data.

After mesh files remain unchanged for 15 seconds, the watcher may launch the
OpenFOAM `checkMesh` executable found on `PATH` with a fixed argument list:

```text
checkMesh -latestTime -allTopology -allGeometry
```

The fixed `-meshQuality` flag is appended only when
`system/meshQualityDict` exists, because OpenFOAM treats that dictionary as a
required input for the option.

It uses no shell, user-supplied command fragments, MPI launcher, `-writeSets`,
or `-writeSurfaces`. Output is captured with a fixed in-memory retention bound,
only one owned child is allowed at a time, and shutdown terminates that child
if necessary. The OpenFOAM installation and case remain inside the same-user
trust boundary.

## No solver control

The watcher is advisory. It does not launch, stop, restart, signal, or send
commands to an OpenFOAM process. Dashboard convergence and stationarity states
must not be used as an automatic solver-control signal in version 0.1.

## Trust assumptions

- The local operating-system account and case directory are trusted.
- Another process running as the same user can race filesystem operations;
  portable Python does not provide compare-and-swap file replacement on every
  supported platform.
- On POSIX, the watcher pins the resolved case directory for configuration
  operations and uses no-follow checks where available.
- On Windows, ancestor-directory identity cannot be pinned with the Python
  standard library, so path safety is best effort within the same-user trust
  boundary.
- The per-process dashboard token protects configuration writes against
  unrelated web origins; it is not a multi-user authentication system.

## Reporting a vulnerability

Do not publish exploit details in a public issue.

Report the problem privately to the repository owner through GitHub's private
vulnerability-reporting feature if it is enabled. Otherwise contact
`minsujeon@snu.ac.kr` with:

- the affected commit or version;
- deployment environment and Python/OpenFOAM versions;
- reproduction steps;
- expected and observed impact;
- suggested mitigation, if known.

Please omit confidential case data, solver logs, credentials, and hostnames
unless they are strictly required and safe to share.
