from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Sequence

from watcher.case_config import inspect_case
from watcher.models import WatcherConfig
from watcher.persistence import save_config
from watcher.postprocessing import discover_series


def _write(case_dir: Path, relative_path: str, content: str) -> None:
    target = case_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def generate_demo_case(output: Path) -> Path:
    """Create a deterministic transient case that exercises every dashboard view."""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Demo output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "constant").mkdir(exist_ok=True)

    _write(
        output,
        "system/controlDict",
        """\
FoamFile
{
    format ascii;
    class dictionary;
    object controlDict;
}
application pimpleFoam;
startTime 0;
endTime 4;
deltaT 0.05;
adjustTimeStep yes;
maxCo 0.8;
maxDeltaT 0.05;
functions
{
    aero
    {
        type forceCoeffs;
        fields (Cd Cl CmPitch);
    }
}
""",
    )
    _write(
        output,
        "system/fvSolution",
        """\
PIMPLE
{
    nOuterCorrectors 2;
}
""",
    )

    log_lines = [
        "OpenFOAM demo watcher fixture",
        "Create time",
    ]
    for index in range(1, 81):
        time_value = index * 0.05
        courant_max = 0.36 + 0.18 * abs(math.sin(index / 8))
        u_residual = max(1e-7, 0.08 * math.exp(-index / 9))
        p_residual = max(2e-7, 0.12 * math.exp(-index / 8))
        log_lines.extend(
            [
                f"Time = {time_value:.2f}",
                "deltaT = 0.05",
                f"Courant Number mean: {courant_max / 3:.6f} max: {courant_max:.6f}",
                (
                    "smoothSolver:  Solving for Ux, Initial residual = "
                    f"{u_residual:.8g}, Final residual = {u_residual / 80:.8g}, "
                    "No Iterations 2"
                ),
                (
                    "GAMG:  Solving for p, Initial residual = "
                    f"{p_residual:.8g}, Final residual = {p_residual / 100:.8g}, "
                    "No Iterations 3"
                ),
                (
                    "time step continuity errors : sum local = 1e-08, "
                    f"global = {1e-09 * math.sin(index):.8g}, cumulative = 2e-08"
                ),
            ]
        )
    log_lines.append("ExecutionTime = 38 s  ClockTime = 40 s")
    _write(output, "log.pimpleFoam", "\n".join(log_lines) + "\n")

    coefficient_rows = []
    for index in range(240):
        time_value = index / 60
        cd = 0.31 + 0.06 * math.exp(-index / 35)
        cl = 0.82 + 0.045 * math.sin(2 * math.pi * index / 30)
        moment = -0.022 + 0.0003 * math.sin(2 * math.pi * index / 20)
        coefficient_rows.append(
            f"{time_value:.8g} {cd:.8g} {cl:.8g} {moment:.8g}"
        )
    _write(
        output,
        "postProcessing/aero/0/coefficient.dat",
        "# Time Cd Cl CmPitch\n" + "\n".join(coefficient_rows) + "\n",
    )
    _write(
        output,
        "postProcessing/broken/0/data.dat",
        "# Time incomplete\n0\n",
    )

    series = discover_series(inspect_case(output))
    selected = tuple(list(series)[:3])
    save_config(
        output,
        WatcherConfig(
            version=1,
            selected_log=None,
            selected_series=selected,
            overrides={},
            accepted_states=frozenset(
                {"plateau", "periodic", "statistically_stationary"}
            ),
        ),
    )
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic OpenFOAM Solver Watcher demo case.",
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        generated = generate_demo_case(arguments.output)
    except (FileExistsError, OSError, ValueError) as error:
        parser.error(str(error))
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
