from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from watcher.log_parser import OpenFOAMLogParser
from watcher.models import LogChunk


def chunk(text: str, segment: int = 0, reset: bool = False) -> LogChunk:
    return LogChunk(
        path=Path("log.solver"),
        segment=segment,
        reset=reset,
        lines=tuple(text.splitlines()),
        file_size=len(text.encode("utf-8")),
        modified_ns=0,
    )


STEADY_LOG = """\
OpenFOAM-v2412
Time = 12
smoothSolver:  Solving for Ux, Initial residual = 1e-03, Final residual = 2e-07, No Iterations 3
GAMG:  Solving for p_rgh, Initial residual = 2e-02, Final residual = 8e-05, No Iterations = 2
ExecutionTime = 24 s  ClockTime = 25 s
"""


TRANSIENT_LOG = """\
Time = 0.25
deltaT = 0.005
Courant Number mean: 0.12 max: 0.73
Mesh Courant Number mean: 0.02 max: 0.15
PIMPLE: iteration 2
smoothSolver:  Solving for T, Initial residual = 0.01, Final residual = 1e-06, No Iterations 2
time step continuity errors : sum local = 1e-08, global = -2e-09, cumulative = 4e-08
ExecutionTime = 12 s  ClockTime = 13 s
"""


class OpenFOAMLogParserTests(TestCase):
    def test_parses_steady_residuals_and_timing(self) -> None:
        # Removing the solver-performance match would leave this normalized history empty.
        parser = OpenFOAMLogParser()
        parser.feed(chunk(STEADY_LOG))

        data = parser.snapshot()

        self.assertEqual(data.current_time, 12.0)
        self.assertEqual(data.execution_seconds, 24.0)
        self.assertEqual(data.clock_seconds, 25.0)
        self.assertEqual(
            [(sample.field, sample.initial, sample.final, sample.iterations) for sample in data.residuals],
            [("Ux", 1e-03, 2e-07, 3), ("p_rgh", 2e-02, 8e-05, 2)],
        )

    def test_parses_transient_health_records(self) -> None:
        # Dropping any time-step record match would make a literal health value disappear.
        parser = OpenFOAMLogParser()
        parser.feed(chunk(TRANSIENT_LOG))

        data = parser.snapshot()

        self.assertEqual(data.current_time, 0.25)
        self.assertEqual(data.current_delta_t, 0.005)
        self.assertEqual(data.time_steps[-1].courant_mean, 0.12)
        self.assertEqual(data.time_steps[-1].courant_max, 0.73)
        self.assertEqual(data.time_steps[-1].mesh_courant_max, 0.15)
        self.assertEqual(data.time_steps[-1].outer_correctors, 2)
        self.assertEqual(data.time_steps[-1].continuity_cumulative, 4e-08)

    def test_finalizes_each_step_once_when_a_new_time_arrives(self) -> None:
        # Finalizing the active step into stored history too early would duplicate time 0.1.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("Time = 0.1\ndeltaT = 0.01\nCourant Number mean: 0.2 max: 0.5\n"))
        parser.feed(chunk("Time = 0.2\ndeltaT = 0.02\n"))

        data = parser.snapshot()

        self.assertEqual([(step.simulation_time, step.delta_t) for step in data.time_steps], [(0.1, 0.01), (0.2, 0.02)])

    def test_records_vector_components_and_explicit_linear_solver_result(self) -> None:
        # Ignoring the result suffix would lose the solver's explicit failure signal.
        parser = OpenFOAMLogParser()
        parser.feed(
            chunk(
                "Time = 3\n"
                "smoothSolver: Solving for Uy, Initial residual = 0.2, Final residual = 0.001, No Iterations 4, converged\n"
                "smoothSolver: Solving for Uz, Initial residual = 0.2, Final residual = 0.3, No Iterations 9, not converged\n"
            )
        )

        data = parser.snapshot()

        self.assertEqual([(sample.field, sample.converged) for sample in data.residuals], [("Uy", True), ("Uz", False)])

    def test_uses_region_prefix_and_current_region_for_residuals(self) -> None:
        # Removing either region form would mislabel a valid multi-region residual.
        parser = OpenFOAMLogParser()
        parser.feed(
            chunk(
                "Region fluid\n"
                "Time = 1\n"
                "smoothSolver: Solving for p, Initial residual = 0.1, Final residual = 0.01, No Iterations 1\n"
                "solid: GAMG: Solving for T, Initial residual = 0.3, Final residual = 0.02, No Iterations 2\n"
            )
        )

        data = parser.snapshot()

        self.assertEqual([(sample.field, sample.region) for sample in data.residuals], [("p", "fluid"), ("T", "solid")])

    def test_uses_latest_pimple_iteration_for_repeated_solves(self) -> None:
        # Resetting the counter on each residual would report the wrong outer corrector.
        parser = OpenFOAMLogParser()
        parser.feed(
            chunk(
                "Time = 4\n"
                "PIMPLE: iteration 1\n"
                "smoothSolver: Solving for p, Initial residual = 0.1, Final residual = 0.01, No Iterations 1\n"
                "PIMPLE: iteration 2\n"
                "smoothSolver: Solving for p, Initial residual = 0.02, Final residual = 0.002, No Iterations 1\n"
            )
        )

        data = parser.snapshot()

        self.assertEqual([sample.outer_corrector for sample in data.residuals], [1, 2])
        self.assertEqual(data.time_steps[-1].outer_correctors, 2)

    def test_marks_completion_and_solver_declared_convergence(self) -> None:
        # Ignoring terminal messages would leave an ended converged solver indistinguishable from active.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("solution converged due to convergence criteria\nEnd\n"))

        data = parser.snapshot()

        self.assertTrue(data.solver_declared_converged)
        self.assertTrue(data.completed)
        self.assertIn("solution converged due to convergence criteria", data.notices)

    def test_banner_trap_fpe_is_not_a_failure_but_real_signal_is(self) -> None:
        # Treating every FPE mention as fatal would falsely fail normal startup banners.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("trapFpe: Floating point exception trapping enabled\n"))
        self.assertIsNone(parser.snapshot().failure)

        parser.feed(chunk("Floating point exception (core dumped)\n"))

        self.assertEqual(parser.snapshot().failure.label, "Floating point exception")

    def test_trapping_enabled_fpe_banner_without_trapfpe_is_not_a_failure(self) -> None:
        # A substring FPE check would falsely classify this normal capability banner as terminal.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("Floating point exception trapping enabled\n"))

        self.assertIsNone(parser.snapshot().failure)

    def test_explanatory_mpi_and_fatal_text_is_not_a_terminal_failure(self) -> None:
        # Unanchored MPI/fatal substring checks would turn diagnostic prose into a failed run.
        parser = OpenFOAMLogParser()
        parser.feed(
            chunk(
                "This guide explains how to recover after an MPI abort.\n"
                "The previous run reported a fatal error, but this solver is starting.\n"
            )
        )

        self.assertIsNone(parser.snapshot().failure)

    def test_records_mpi_abort_and_segmentation_failure(self) -> None:
        # Missing either fatal marker would hide a solver that has stopped abnormally.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("MPI_ABORT was invoked on rank 0 in communicator MPI_COMM_WORLD\n"))
        self.assertEqual(parser.snapshot().failure.label, "MPI abort")

        parser.feed(chunk("Segmentation fault (core dumped)\n"))

        self.assertEqual(parser.snapshot().failure.label, "Segmentation fault")

    def test_starts_a_new_active_segment_after_reader_reset(self) -> None:
        # Retaining active state across reset would attach the restart's records to segment zero.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("Time = 5\ndeltaT = 0.1\n", segment=0))
        parser.feed(chunk("Time = 1\ndeltaT = 0.01\n", segment=1, reset=True))

        data = parser.snapshot()

        self.assertEqual(data.current_segment, 1)
        self.assertEqual(data.current_time, 1.0)
        self.assertEqual([(step.segment, step.simulation_time) for step in data.time_steps], [(0, 5.0), (1, 1.0)])

    def test_negated_convergence_criteria_messages_do_not_declare_convergence(self) -> None:
        # Accepting a positive keyword without its negation would invert the solver's declaration.
        parser = OpenFOAMLogParser()
        parser.feed(
            chunk(
                "solution not converged due to convergence criteria\n"
                "residuals not satisfied by convergence criteria\n"
            )
        )

        self.assertFalse(parser.snapshot().solver_declared_converged)

    def test_keeps_only_the_latest_bounded_warning_and_residual_histories(self) -> None:
        # Removing either retention bound would let an indefinitely-running solver grow memory unboundedly.
        parser = OpenFOAMLogParser()
        parser.feed(chunk("\n".join(f"WARNING: diagnostic {index}" for index in range(201))))
        parser.feed(
            chunk(
                "Time = 1\n"
                + "\n".join(
                    "smoothSolver: Solving for p, Initial residual = 1, Final residual = 0.1, No Iterations 1"
                    for _ in range(100_001)
                )
            )
        )

        data = parser.snapshot()

        self.assertEqual((len(data.warnings), data.warnings[0]), (200, "WARNING: diagnostic 1"))
        self.assertEqual(len(data.residuals), 100_000)
