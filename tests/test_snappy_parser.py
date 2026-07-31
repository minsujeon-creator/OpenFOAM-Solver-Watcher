from __future__ import annotations

from unittest import TestCase

from tests.helpers import TemporaryCase
from watcher.models import LogChunk
from watcher.snappy_parser import SnappyHexMeshParser, read_snappy_settings


def _chunk(case: TemporaryCase, text: str, *, segment: int = 0, reset: bool = False) -> LogChunk:
    path = case.write("log.snappyHexMesh", text)
    stat = path.stat()
    return LogChunk(
        path=path,
        segment=segment,
        reset=reset,
        lines=tuple(text.splitlines()),
        file_size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
    )


class SnappySettingsTests(TestCase):
    def test_reads_progress_settings_from_nested_dictionary(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/snappyHexMeshDict",
                """
                addLayers true;
                castellatedMeshControls
                {
                    maxGlobalCells 10000000;
                }
                snapControls
                {
                    nSolveIter 300;
                    nRelaxIter 15; // zero-based Morph iteration loop
                }
                """,
            )

            settings = read_snappy_settings(case.path)

        self.assertTrue(settings.add_layers)
        self.assertEqual(settings.n_solve_iter, 300)
        self.assertEqual(settings.n_relax_iter, 15)
        self.assertEqual(settings.max_global_cells, 10_000_000)
        self.assertEqual(settings.stage_count, 3)

    def test_missing_values_remain_unknown_and_disabled_layers_use_two_stages(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers false;\n")

            settings = read_snappy_settings(case.path)

        self.assertFalse(settings.add_layers)
        self.assertIsNone(settings.n_solve_iter)
        self.assertIsNone(settings.n_relax_iter)
        self.assertIsNone(settings.max_global_cells)
        self.assertEqual(settings.stage_count, 2)


class SnappyParserTests(TestCase):
    def test_parses_zero_based_morph_and_smoothing_progress_with_mesh_counts(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/snappyHexMeshDict",
                """
                addLayers true;
                snapControls { nSolveIter 300; nRelaxIter 15; }
                castellatedMeshControls { maxGlobalCells 10000000; }
                """,
            )
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    """
Castellated mesh generation
Snapping phase
Morph iteration 13
Smoothing displacement iteration 180
cells: 8903300
faces: 27984450
points: 10191464
--> FOAM Warning : maxGlobalCells 10000000 reached; stopping refinement
ExecutionTime = 42 s  ClockTime = 45 s
""".lstrip(),
                )
            )

            telemetry = parser.snapshot()

        self.assertEqual(telemetry.stage, "snapping")
        self.assertEqual(telemetry.stage_index, 2)
        self.assertEqual(telemetry.stage_count, 3)
        self.assertEqual(telemetry.active_morph_iteration, 14)
        self.assertEqual(telemetry.completed_morph_iterations, 13)
        self.assertEqual(telemetry.morph_total, 15)
        self.assertAlmostEqual(telemetry.phase_progress_percent or 0.0, 13 / 15 * 100)
        self.assertEqual(telemetry.smoothing_iteration, 180)
        self.assertEqual(telemetry.smoothing_total, 300)
        self.assertEqual(telemetry.mesh_cells, 8_903_300)
        self.assertEqual(telemetry.mesh_faces, 27_984_450)
        self.assertEqual(telemetry.mesh_points, 10_191_464)
        self.assertTrue(telemetry.max_global_cells_reached)
        self.assertEqual(len(telemetry.warnings), 1)
        self.assertEqual(telemetry.execution_seconds, 42.0)
        self.assertEqual(telemetry.clock_seconds, 45.0)

    def test_detects_layer_stage_completion_and_fatal_failure(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers true;\n")
            completed_parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            completed_parser.feed(_chunk(case, "Layer addition phase\nEnd\n"))
            completed = completed_parser.snapshot()

            failed_parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            failed_parser.feed(_chunk(case, "Snapping phase\nFOAM FATAL ERROR:\n"))
            failed = failed_parser.snapshot()

        self.assertEqual(completed.stage, "completed")
        self.assertTrue(completed.completed)
        self.assertIsNone(completed.failure)
        self.assertEqual(failed.stage, "snapping")
        self.assertEqual(failed.failure.label, "FOAM fatal error")

    def test_reset_clears_previous_run_progress(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/snappyHexMeshDict",
                "addLayers false; snapControls { nRelaxIter 10; }\n",
            )
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(_chunk(case, "Morph iteration 8\n", segment=0))
            parser.feed(_chunk(case, "Starting mesh refinement\n", segment=1, reset=True))

            telemetry = parser.snapshot()

        self.assertEqual(telemetry.stage, "castellation")
        self.assertIsNone(telemetry.active_morph_iteration)
        self.assertEqual(telemetry.current_segment, 1)

    def test_stage_does_not_regress_on_late_refinement_message(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers true;\n")
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    "Layer addition phase\nMesh refinement stopped at configured limit\n",
                )
            )

            telemetry = parser.snapshot()

        self.assertEqual(telemetry.stage, "layers")

    def test_reads_point_displacement_linear_solver_iterations(self) -> None:
        with TemporaryCase() as case:
            case.write(
                "system/snappyHexMeshDict",
                "addLayers false; snapControls { nSolveIter 300; nRelaxIter 15; }\n",
            )
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    """
Morph iteration 4
smoothDisplacement: Solving for pointDisplacementx, Initial residual = 1, Final residual = 1e-6, No Iterations 180
""".lstrip(),
                )
            )

            telemetry = parser.snapshot()

        self.assertEqual(telemetry.smoothing_iteration, 180)
        self.assertIn("pointDisplacementx", telemetry.current_work or "")

    def test_max_global_cells_limit_is_visible_without_foam_warning_prefix(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    "maxGlobalCells 10000000 reached; further shell refinement was stopped.\n",
                )
            )

            telemetry = parser.snapshot()

        self.assertTrue(telemetry.max_global_cells_reached)
        self.assertEqual(len(telemetry.warnings), 1)

    def test_openfoam_trap_banner_is_not_fatal_but_real_signal_is(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    "trapFpe : Floating point exception trapping enabled (FOAM_SIGFPE).\n",
                )
            )
            banner = parser.snapshot()
            parser.feed(_chunk(case, "Floating point exception (core dumped)\n"))
            failure = parser.snapshot()

        self.assertIsNone(banner.failure)
        self.assertEqual(failure.failure.label, "floating point exception")

    def test_final_mesh_messages_advance_layer_stage_to_finalization(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers true;\n")
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(_chunk(case, "Layer addition phase\nFinalising layer addition\n"))
            first = parser.snapshot()
            parser.feed(_chunk(case, "Checking final mesh ...\n"))
            second = parser.snapshot()

        self.assertEqual(first.stage, "finalizing")
        self.assertEqual(second.stage, "finalizing")

    def test_preserves_total_warning_count_when_warning_list_is_bounded(self) -> None:
        with TemporaryCase() as case:
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            parser = SnappyHexMeshParser(read_snappy_settings(case.path))
            parser.feed(
                _chunk(
                    case,
                    "\n".join(f"FOAM Warning warning-{index}" for index in range(250)) + "\n",
                )
            )

            telemetry = parser.snapshot()

        self.assertEqual(telemetry.warning_count, 250)
        self.assertEqual(telemetry.notice_count, 250)
        self.assertEqual(len(telemetry.warnings), 200)


if __name__ == "__main__":
    import unittest

    unittest.main()
