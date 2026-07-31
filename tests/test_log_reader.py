from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import TemporaryCase
from watcher import log_reader
from watcher.case_config import inspect_case
from watcher.log_reader import IncrementalLogReader, discover_logs


class LogDiscoveryTests(TestCase):
    def test_discovery_skips_candidate_that_disappears_before_scoring(self) -> None:
        # Removing the OSError boundary in discovery should let the vanished log abort it.
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            vanished = case.write("log.gone", "OpenFOAM v2412\nTime = 1\n")
            case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 2\n")
            original_candidate = log_reader._candidate

            def remove_before_scoring(*args: object) -> object:
                path = args[0]
                if path == vanished:
                    vanished.unlink()
                return original_candidate(*args)  # type: ignore[arg-type]

            with patch("watcher.log_reader._candidate", side_effect=remove_before_scoring):
                candidates = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)

        self.assertEqual(tuple(candidate.relative_path for candidate in candidates), ("log.simpleFoam",))

    def test_candidate_score_combines_documented_solver_signals(self) -> None:
        # Dropping the tail-record signal should reduce this hand-derived score by 50.
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 1\n")

            candidate = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)[0]

        self.assertEqual(candidate.score, 335)
        self.assertIn("solver record", candidate.reasons)

    def test_application_log_outranks_newer_mesh_log(self) -> None:
        # Removing the application-name score should make the mesh log win.
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 1\nSolving for U")
            case.write("log.checkMesh", "OpenFOAM v2412\nMesh OK.\n")
            case.touch("log.checkMesh", seconds_after=5)

            ranked = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)

        self.assertEqual(ranked[0].relative_path, "log.simpleFoam")
        self.assertIn("application-name match", ranked[0].reasons)

    def test_discovery_classifies_solver_snappy_and_other_utility_logs(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 1\n")
            case.write("log.snappyHexMesh", "OpenFOAM v2412\nStarting mesh refinement\n")
            case.write("log.checkMesh", "OpenFOAM v2412\nMesh OK.\n")

            candidates = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)

        workflows = {candidate.relative_path: candidate.workflow for candidate in candidates}
        self.assertEqual(workflows["log.simpleFoam"], "solver")
        self.assertEqual(workflows["log.snappyHexMesh"], "snappy_hex_mesh")
        self.assertEqual(workflows["log.checkMesh"], "utility")

    def test_fresh_snappy_log_is_ranked_above_old_solver_log(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("log.simpleFoam", "OpenFOAM v2412\nTime = 1\n")
            case.write("log.snappyHexMesh", "OpenFOAM v2412\nMorph iteration 0\n")
            case.touch("log.snappyHexMesh", seconds_after=5)

            candidates = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)

        recognized = [item for item in candidates if item.workflow in {"solver", "snappy_hex_mesh"}]
        self.assertEqual(recognized[0].relative_path, "log.snappyHexMesh")

    def test_generic_configured_solver_log_outranks_newer_utility_at_startup(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("log", "OpenFOAM v2412\nExec : simpleFoam\nCreate time\n")
            case.write("log.checkMesh", "OpenFOAM v2412\nMesh OK.\n")
            case.touch("log.checkMesh", seconds_after=5)

            candidates = discover_logs(inspect_case(case.path), explicit=None, saved_relative=None)

        self.assertEqual(candidates[0].relative_path, "log")
        self.assertEqual(candidates[0].workflow, "solver")
        self.assertIn("application-content match", candidates[0].reasons)

    def test_explicit_and_saved_selection_are_ranked_and_contained(self) -> None:
        # Omitting containment checks would include the external selection.
        with TemporaryCase() as case, TemporaryDirectory() as outside_directory:
            case.write("system/controlDict", "application simpleFoam;\n")
            explicit = case.write("logs/simpleFoam.log", "OpenFOAM v2412\nTime = 1\n")
            case.write("log.simpleFoam.saved", "OpenFOAM v2412\nTime = 2\n")
            outside = Path(outside_directory) / "log.out"
            outside.write_text("OpenFOAM v2412\nTime = 3\n", encoding="utf-8")

            ranked = discover_logs(
                inspect_case(case.path),
                explicit=explicit,
                saved_relative="log.simpleFoam.saved",
            )
            combined = discover_logs(
                inspect_case(case.path),
                explicit=explicit,
                saved_relative="logs/simpleFoam.log",
            )
            rejected = discover_logs(
                inspect_case(case.path),
                explicit=outside,
                saved_relative="../log.saved",
            )

        self.assertEqual(ranked[0].relative_path, "logs/simpleFoam.log")
        self.assertIn("explicit selection", ranked[0].reasons)
        self.assertEqual(
            combined[0].reasons[:2],
            ("explicit selection", "saved selection"),
        )
        self.assertFalse(any(candidate.path == outside for candidate in rejected))


class IncrementalLogReaderTests(TestCase):
    def test_incremental_reader_handles_partial_line_and_truncation(self) -> None:
        # Reading a partial line early would make the first chunk contain it.
        with TemporaryCase() as case:
            log = case.write("log.pimpleFoam", "Time = 0.1\nCourant Number")
            reader = IncrementalLogReader(case.path, log)

            first = reader.read()
            self.assertEqual(first.lines, ("Time = 0.1",))

            case.append("log.pimpleFoam", " mean: 0.1 max: 0.8\n")
            second = reader.read()
            self.assertEqual(second.lines, ("Courant Number mean: 0.1 max: 0.8",))

            case.write("log.pimpleFoam", "Time = 0.0\n")
            third = reader.read()

        self.assertTrue(third.reset)
        self.assertEqual(third.segment, 1)
        self.assertEqual(third.lines, ("Time = 0.0",))

    def test_incremental_reader_restarts_when_file_is_replaced(self) -> None:
        # Ignoring file identity would skip the replacement's first line.
        with TemporaryCase() as case:
            log = case.write("log.pimpleFoam", "Time = 0.1\n")
            reader = IncrementalLogReader(case.path, log)
            reader.read()
            replacement = case.path / "replacement.log"
            replacement.write_text("Time = 0.2\n", encoding="utf-8")
            os.replace(replacement, log)

            chunk = reader.read()

        self.assertTrue(chunk.reset)
        self.assertEqual(chunk.segment, 1)
        self.assertEqual(chunk.lines, ("Time = 0.2",))
