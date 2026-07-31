from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import time
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import TemporaryCase
from watcher.persistence import ConfigValidationError, save_config
from watcher.models import MeshQualityStatus, SeriesOverride, WatcherConfig, to_json_safe
from watcher.snapshot import WatcherCollector


EXPECTED_TOP_LEVEL = {
    "generatedAt",
    "refreshSeconds",
    "case",
    "process",
    "host",
    "solver",
    "numerics",
    "physical",
    "seriesCatalog",
    "logSelection",
    "notices",
    "configuration",
    "workflow",
    "meshing",
    "meshQuality",
}


class _FakeCheckMeshMonitor:
    def __init__(self, status: MeshQualityStatus) -> None:
        self.status = status
        self.busy_values: list[bool] = []
        self.closed = False

    def update(self, *, mesh_busy: bool) -> MeshQualityStatus:
        self.busy_values.append(mesh_busy)
        return self.status

    def close(self) -> None:
        self.closed = True


def _populate_case(case: TemporaryCase, *, end_time: float = 1.0) -> None:
    case.write(
        "system/controlDict",
        f"""
        application pimpleFoam;
        startTime 0;
        endTime {end_time};
        deltaT 0.05;
        maxCo 1;
        functions
        {{
            lift
            {{
                type forceCoeffs;
                fields (Cl);
            }}
        }}
        """,
    )
    case.write("system/fvSolution", "PIMPLE { nOuterCorrectors 2; }\n")
    case.write(
        "log.pimpleFoam",
        """\
Time = 0.10
Courant Number mean: 0.1 max: 0.4
time step continuity errors : sum local = 1e-8, global = 1e-9, cumulative = 2e-8
Time = 0.20
Courant Number mean: 0.1 max: 0.5
time step continuity errors : sum local = 1e-8, global = 1e-9, cumulative = 3e-8
Time = 0.25
deltaT = 0.05
Courant Number mean: 0.1 max: 0.6
time step continuity errors : sum local = 1e-8, global = 1e-9, cumulative = 4e-8
ExecutionTime = 9 s  ClockTime = 10 s
""",
    )
    rows = "\n".join(f"{index / 10:.1f} {1.0 + index / 1000:.6f}" for index in range(80))
    case.write("postProcessing/lift/0/coefficient.dat", f"# Time Cl\n{rows}\n")


def _old(path: Path) -> None:
    modified = time.time() - 180.0
    os.utime(path, (modified, modified))


class SnapshotIntegrationTests(TestCase):
    def test_snapshot_exposes_mesh_quality_and_layer_coverage(self) -> None:
        status = MeshQualityStatus(
            state="stabilizing",
            summary="Mesh changed; waiting for stable files.",
            mesh_source="constant/polyMesh",
            stable_for_seconds=4.0,
            next_check_seconds=11.0,
            report=None,
            advisory="Advisory only.",
        )
        monitor = _FakeCheckMeshMonitor(status)
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("system/snappyHexMeshDict", "addLayers true;\n")
            case.write(
                "log.snappyHexMesh",
                """\
patch faces layers overall thickness
[m] [%]
wall 100 2 0.001 80
End
""",
            )
            snapshot = WatcherCollector(
                case.path,
                checkmesh_monitor=monitor,
            ).snapshot()

        self.assertEqual(snapshot["meshQuality"]["state"], "stabilizing")
        self.assertEqual(snapshot["meshQuality"]["meshSource"], "constant/polyMesh")
        self.assertEqual(snapshot["meshing"]["layerCoverage"]["reportedPatchCount"], 1)
        self.assertEqual(snapshot["meshing"]["layerCoverage"]["rows"][0]["patch"], "wall")
        self.assertEqual(monitor.busy_values, [False])

    def test_active_snappy_workflow_defers_automatic_mesh_check(self) -> None:
        status = MeshQualityStatus("deferred", "Mesher active.", None, 0.0, None, None, "Advisory")
        monitor = _FakeCheckMeshMonitor(status)
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("system/snappyHexMeshDict", "addLayers true;\n")
            case.write("log.snappyHexMesh", "Morph iteration 2\n")

            WatcherCollector(case.path, checkmesh_monitor=monitor).snapshot()

        self.assertEqual(monitor.busy_values, [True])

    def test_active_snappy_process_defers_check_even_when_solver_log_is_explicit(self) -> None:
        status = MeshQualityStatus("deferred", "Mesher active.", None, 0.0, None, None, "Advisory")
        monitor = _FakeCheckMeshMonitor(status)
        with TemporaryCase() as case:
            _populate_case(case)

            def process_match(_case_dir: Path, application: str | None):
                if application == "snappyHexMesh":
                    return 4321, "snappyHexMesh -overwrite"
                return None

            with patch("watcher.snapshot._matching_process", side_effect=process_match):
                WatcherCollector(
                    case.path,
                    explicit_log=case.path / "log.pimpleFoam",
                    checkmesh_monitor=monitor,
                ).snapshot()

        self.assertEqual(monitor.busy_values, [True])

    def test_collector_closes_mesh_monitor(self) -> None:
        status = MeshQualityStatus("waiting", "Waiting.", None, None, None, None, "Advisory")
        monitor = _FakeCheckMeshMonitor(status)
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            collector = WatcherCollector(case.path, checkmesh_monitor=monitor)
            collector.close()

        self.assertTrue(monitor.closed)

    def test_snapshot_bounds_unique_solver_notices_and_keeps_critical_sources(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            messages = "\n".join(
                f"convergence criteria satisfied at checkpoint {index}"
                for index in range(1_000)
            )
            case.write("log.pimpleFoam", f"Time = 0.25\n{messages}\n")
            broken = case.path / "postProcessing/broken/0/data.dat"
            broken.parent.mkdir(parents=True)
            broken.write_bytes(b"\xff\xfe\x00")
            case.write(".foam-watcher.json", "{broken")

            snapshot = WatcherCollector(case.path).snapshot()

        self.assertLessEqual(len(snapshot["notices"]), 300)
        self.assertEqual(snapshot["solver"]["noticeCount"], 1_000)
        self.assertTrue(snapshot["solver"]["noticesTruncated"])
        self.assertGreaterEqual(snapshot["solver"]["snapshotNoticeCount"], 1_002)
        self.assertTrue(snapshot["solver"]["snapshotNoticesTruncated"])
        self.assertTrue(
            any(
                notice["source"] == ".foam-watcher.json"
                for notice in snapshot["notices"]
            )
        )
        self.assertTrue(
            any("broken" in notice["source"] for notice in snapshot["notices"])
        )
        self.assertTrue(
            any(
                "checkpoint 999" in notice["message"]
                for notice in snapshot["notices"]
            )
        )
        self.assertLess(len(json.dumps(snapshot, allow_nan=False)), 250_000)

    def test_snapshot_counts_snappy_notices_omitted_by_parser_bound(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            warnings = "\n".join(
                f"maxGlobalCells {index} reached; refinement stopped"
                for index in range(250)
            )
            case.write("log.snappyHexMesh", warnings + "\n")

            snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["meshing"]["noticeCount"], 250)
        self.assertGreaterEqual(snapshot["solver"]["snapshotNoticeCount"], 250)
        self.assertTrue(snapshot["solver"]["snapshotNoticesTruncated"])

    def test_snapshot_bounds_long_solver_histories_and_payload_growth(self) -> None:
        def records(start: int, stop: int, spike_at: int) -> str:
            lines: list[str] = []
            for index in range(start, stop):
                initial = 1e9 if index == spike_at else 1e-3
                lines.extend(
                    (
                        f"Time = {index}",
                        (
                            "smoothSolver: Solving for Ux, "
                            f"Initial residual = {initial}, Final residual = 1e-8, "
                            "No Iterations 2"
                        ),
                    )
                )
            return "\n".join(lines) + "\n"

        with TemporaryCase() as case:
            _populate_case(case, end_time=20_000.0)
            log = case.path / "log.pimpleFoam"
            log.write_text(records(0, 6_000, 3_517), encoding="utf-8")
            collector = WatcherCollector(case.path)
            first = collector.snapshot()
            first_size = len(json.dumps(first, allow_nan=False))

            with log.open("a", encoding="utf-8") as stream:
                stream.write(records(6_000, 12_000, 10_123))
            second = collector.snapshot()
            second_size = len(json.dumps(second, allow_nan=False))

        for snapshot in (first, second):
            self.assertLessEqual(len(snapshot["solver"]["residuals"]), 1_000)
            self.assertLessEqual(len(snapshot["solver"]["timeSteps"]), 1_000)
            self.assertEqual(snapshot["solver"]["residualCount"], snapshot["solver"]["timeStepCount"])
            self.assertEqual(
                snapshot["solver"]["residuals"][-1]["simulationTime"],
                snapshot["solver"]["currentTime"],
            )
        self.assertIn(1e9, [item["initial"] for item in first["solver"]["residuals"]])
        self.assertIn(1e9, [item["initial"] for item in second["solver"]["residuals"]])
        self.assertLess(first_size, 1_000_000)
        self.assertLessEqual(second_size, first_size + 50_000)

    def test_snapshot_combines_case_numerics_and_selected_series(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(set(snapshot), EXPECTED_TOP_LEVEL)
        self.assertEqual(snapshot["case"]["application"], "pimpleFoam")
        self.assertEqual(snapshot["case"]["mode"], "transient_pimple")
        self.assertEqual(snapshot["solver"]["currentTime"], 0.25)
        self.assertEqual(snapshot["numerics"]["kind"], "transient_health")
        self.assertNotEqual(snapshot["physical"]["aggregate"]["state"], "passing")
        self.assertTrue(snapshot["seriesCatalog"])
        self.assertEqual(snapshot["workflow"]["kind"], "solver")
        self.assertIsNone(snapshot["meshing"])
        json.dumps(snapshot, allow_nan=False)

    def test_snapshot_automatically_selects_fresh_snappy_workflow(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write(
                "system/snappyHexMeshDict",
                "addLayers true; snapControls { nSolveIter 300; nRelaxIter 15; }\n",
            )
            case.write(
                "log.snappyHexMesh",
                "Snapping phase\nMorph iteration 13\nSmoothing displacement iteration 180\n",
            )
            case.touch("log.snappyHexMesh", seconds_after=5)

            snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["workflow"]["kind"], "snappy_hex_mesh")
        self.assertEqual(snapshot["logSelection"]["selected"], "log.snappyHexMesh")
        self.assertEqual(snapshot["meshing"]["stage"], "snapping")
        self.assertAlmostEqual(snapshot["meshing"]["phaseProgressPercent"], 13 / 15 * 100)
        self.assertEqual(snapshot["numerics"]["kind"], "not_applicable")
        self.assertEqual(snapshot["numerics"]["status"], "not_applicable")
        self.assertIsNone(snapshot["solver"]["currentTime"])
        json.dumps(snapshot, allow_nan=False)

    def test_collector_switches_between_snappy_and_solver_parsers(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            case.write("log.snappyHexMesh", "Morph iteration 2\n")
            case.touch("log.snappyHexMesh", seconds_after=5)
            collector = WatcherCollector(case.path)
            meshing = collector.snapshot()

            case.touch("log.pimpleFoam", seconds_after=10)
            solving = collector.snapshot()

        self.assertEqual(meshing["workflow"]["kind"], "snappy_hex_mesh")
        self.assertEqual(solving["workflow"]["kind"], "solver")
        self.assertIsNone(solving["meshing"])
        self.assertEqual(solving["solver"]["currentTime"], 0.25)

    def test_snappy_process_state_distinguishes_completed_failed_and_stale(self) -> None:
        snapshots = []
        for content, make_old in (
            ("Snapping phase\nEnd\n", False),
            ("Snapping phase\nFOAM FATAL ERROR:\n", False),
            ("Snapping phase\nMorph iteration 1\n", True),
        ):
            with TemporaryCase() as case:
                case.write("system/controlDict", "application simpleFoam;\n")
                case.write("system/snappyHexMeshDict", "addLayers false;\n")
                log = case.write("log.snappyHexMesh", content)
                if make_old:
                    _old(log)
                    older = time.time() - 400.0
                    os.utime(log, (older, older))
                snapshots.append(WatcherCollector(case.path).snapshot())

        self.assertEqual(snapshots[0]["process"]["state"], "completed")
        self.assertEqual(snapshots[1]["process"]["state"], "failed")
        self.assertEqual(snapshots[2]["process"]["state"], "stale")

    def test_snappy_log_remains_running_until_five_minute_stale_threshold(self) -> None:
        with TemporaryCase() as case:
            case.write("system/controlDict", "application simpleFoam;\n")
            case.write("system/snappyHexMeshDict", "addLayers false;\n")
            log = case.write("log.snappyHexMesh", "Snapping phase\nMorph iteration 1\n")
            _old(log)

            snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["process"]["state"], "running")

    def test_constructs_case_inspection_only_once(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            from watcher.case_config import inspect_case as real_inspect

            with patch("watcher.snapshot.inspect_case", wraps=real_inspect) as inspect:
                collector = WatcherCollector(case.path)
                collector.snapshot()
                collector.snapshot()

        self.assertEqual(inspect.call_count, 1)

    def test_explicit_then_saved_then_ranked_log_selection(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write("explicit.log", "Time = 3\n")
            case.write("saved.log", "Time = 2\n")
            save_config(
                case.path,
                WatcherConfig(
                    version=1,
                    selected_log="saved.log",
                    selected_series=(),
                    overrides={},
                    accepted_states=frozenset({"plateau"}),
                ),
            )

            explicit = WatcherCollector(case.path, case.path / "explicit.log").snapshot()
            saved = WatcherCollector(case.path).snapshot()
            (case.path / ".foam-watcher.json").unlink()
            ranked = WatcherCollector(case.path).snapshot()

        self.assertEqual(explicit["logSelection"]["selected"], "explicit.log")
        self.assertEqual(explicit["solver"]["currentTime"], 3.0)
        self.assertEqual(saved["logSelection"]["selected"], "saved.log")
        self.assertEqual(saved["solver"]["currentTime"], 2.0)
        self.assertEqual(ranked["logSelection"]["selected"], "log.pimpleFoam")

    def test_reader_and_parser_reset_when_saved_selection_changes(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write("other.log", "Time = 9\n")
            collector = WatcherCollector(case.path)
            self.assertEqual(collector.snapshot()["solver"]["currentTime"], 0.25)

            collector.update_config(
                {
                    "version": 1,
                    "selectedLog": "other.log",
                    "selectedSeries": [],
                    "overrides": {},
                    "acceptedStates": ["plateau"],
                }
            )
            changed = collector.snapshot()

        self.assertEqual(changed["logSelection"]["selected"], "other.log")
        self.assertEqual(changed["solver"]["currentTime"], 9.0)
        self.assertEqual(changed["solver"]["residuals"], [])

    def test_postprocessing_refresh_is_signature_cached(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            from watcher.postprocessing import discover_series as real_discover

            with patch("watcher.snapshot.discover_series", wraps=real_discover) as discover:
                collector = WatcherCollector(case.path)
                collector.snapshot()
                collector.snapshot()
                case.append("postProcessing/lift/0/coefficient.dat", "8.1 1.2\n")
                collector.snapshot()

        self.assertEqual(discover.call_count, 2)

    def test_failed_postprocessing_refresh_retries_unchanged_signature(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            from watcher.postprocessing import discover_series as real_discover

            attempts = 0

            def flaky_discovery(inspection: object) -> object:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("temporarily unavailable")
                return real_discover(inspection)  # type: ignore[arg-type]

            with patch(
                "watcher.snapshot.discover_series",
                side_effect=flaky_discovery,
            ) as discover:
                collector = WatcherCollector(case.path)
                failed = collector.snapshot()
                recovered = collector.snapshot()

        self.assertEqual(failed["seriesCatalog"], [])
        self.assertTrue(recovered["seriesCatalog"])
        self.assertEqual(discover.call_count, 2)

    def test_snapshot_survives_one_broken_postprocessing_file(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            broken = case.path / "postProcessing/broken/0/data.dat"
            broken.parent.mkdir(parents=True)
            broken.write_bytes(b"\xff\xfe\x00")
            snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["solver"]["currentTime"], 0.25)
        self.assertTrue(
            any("broken" in notice["source"] for notice in snapshot["notices"])
        )

    def test_external_config_change_reapplies_stationarity_thresholds(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            collector = WatcherCollector(case.path)
            first = collector.snapshot()
            series_id = first["seriesCatalog"][0]["id"]
            payload = {
                "version": 1,
                "selectedLog": None,
                "selectedSeries": [series_id],
                "overrides": {
                    series_id: {
                        "maxMeanShiftFraction": 0.5,
                        "label": "Configured lift",
                    }
                },
                "acceptedStates": ["plateau", "statistically_stationary"],
            }
            collector.update_config(payload)
            changed = collector.snapshot()
            save_config(
                case.path,
                WatcherConfig(
                    version=1,
                    selected_log=None,
                    selected_series=(series_id,),
                    overrides={},
                    accepted_states=frozenset({"plateau"}),
                ),
            )
            reloaded = collector.snapshot()

        self.assertEqual(
            changed["physical"]["results"][series_id]["thresholds"][
                "maxMeanShiftFraction"
            ],
            0.5,
        )
        self.assertEqual(changed["seriesCatalog"][0]["label"], "Configured lift")
        self.assertEqual(
            reloaded["physical"]["results"][series_id]["thresholds"][
                "maxMeanShiftFraction"
            ],
            0.02,
        )

    def test_series_reload_reflects_external_config_without_snapshot(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            collector = WatcherCollector(case.path)
            series_id = collector.snapshot()["seriesCatalog"][0]["id"]
            save_config(
                case.path,
                WatcherConfig(
                    version=1,
                    selected_log=None,
                    selected_series=(series_id,),
                    overrides={
                        series_id: SeriesOverride(label="Externally configured")
                    },
                    accepted_states=frozenset({"plateau"}),
                ),
            )

            detail = collector.series(series_id)

        self.assertEqual(detail["label"], "Externally configured")
        self.assertTrue(detail["selected"])

    def test_unknown_series_and_invalid_config_are_rejected(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            collector = WatcherCollector(case.path)
            collector.snapshot()
            with self.assertRaises(KeyError):
                collector.series("does-not-exist")
            with self.assertRaises(ConfigValidationError):
                collector.update_config(
                    {
                        "version": 1,
                        "selectedSeries": ["does-not-exist"],
                    }
                )

    def test_invalid_saved_configuration_becomes_notice(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write(".foam-watcher.json", "{broken")
            snapshot = WatcherCollector(case.path).snapshot()

        self.assertTrue(
            any(
                notice["source"] == ".foam-watcher.json"
                for notice in snapshot["notices"]
            )
        )


class ProcessAndProgressTests(TestCase):
    def _snapshot(self, log_text: str | None, *, old: bool = False) -> dict[str, object]:
        with TemporaryCase() as case:
            _populate_case(case, end_time=1.0)
            log = case.path / "log.pimpleFoam"
            if log_text is None:
                log.unlink()
            else:
                log.write_text(log_text, encoding="utf-8")
                if old:
                    _old(log)
            with patch("watcher.snapshot._matching_process", return_value=None):
                return WatcherCollector(case.path).snapshot()

    def test_process_states_use_log_fallback(self) -> None:
        cases = {
            "running": ("Time = 0.2\n", False),
            "stopped": ("Time = 0.2\n", True),
            "completed": ("Time = 0.2\nEnd\n", True),
            "failed": ("Time = 0.2\nFloating point exception (core dumped)\n", True),
            "not_started": (None, False),
        }
        for expected, (text, old) in cases.items():
            with self.subTest(expected=expected):
                snapshot = self._snapshot(text, old=old)
                self.assertEqual(snapshot["process"]["state"], expected)

    def test_active_proc_match_takes_precedence_over_stale_log(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            _old(case.path / "log.pimpleFoam")
            with patch(
                "watcher.snapshot._matching_process",
                return_value=(4321, "pimpleFoam -parallel"),
            ):
                snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["process"]["state"], "running")
        self.assertEqual(snapshot["process"]["pid"], 4321)
        self.assertEqual(snapshot["process"]["source"], "proc")

    def test_missing_proc_uses_file_activity_fallback(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            _old(case.path / "log.pimpleFoam")
            with patch("watcher.snapshot._PROC_ROOT", case.path / "missing-proc"):
                snapshot = WatcherCollector(case.path).snapshot()

        self.assertEqual(snapshot["process"]["state"], "stopped")
        self.assertEqual(snapshot["process"]["source"], "log")

    def test_end_time_progress_rate_and_eta(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case, end_time=1.0)
            with patch("watcher.snapshot._matching_process", return_value=None):
                snapshot = WatcherCollector(case.path).snapshot()

        progress = snapshot["solver"]["progress"]
        self.assertEqual(progress["fraction"], 0.25)
        self.assertEqual(progress["percent"], 25.0)
        self.assertEqual(progress["simulatedSecondsPerWallSecond"], 0.025)
        self.assertEqual(progress["etaSeconds"], 30.0)

    def test_configured_end_reached_is_completed_without_end_record(self) -> None:
        snapshot = self._snapshot("Time = 1\n", old=True)
        self.assertEqual(snapshot["process"]["state"], "completed")


class SeriesAndJsonTests(TestCase):
    def test_relaxed_and_tightened_stale_thresholds_override_cached_age_flag(
        self,
    ) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            table = case.path / "postProcessing/lift/0/coefficient.dat"
            modified = time.time() - 400.0
            os.utime(table, (modified, modified))
            collector = WatcherCollector(case.path)
            initial = collector.snapshot()
            series_id = initial["seriesCatalog"][0]["id"]
            self.assertTrue(initial["seriesCatalog"][0]["stale"])

            collector.update_config(
                {
                    "version": 1,
                    "selectedLog": None,
                    "selectedSeries": [series_id],
                    "overrides": {series_id: {"staleAfterSeconds": 600}},
                    "acceptedStates": ["plateau"],
                }
            )
            with patch("watcher.snapshot.time.time", return_value=modified + 400):
                relaxed = collector.snapshot()

            collector.update_config(
                {
                    "version": 1,
                    "selectedLog": None,
                    "selectedSeries": [series_id],
                    "overrides": {series_id: {"staleAfterSeconds": 100}},
                    "acceptedStates": ["plateau"],
                }
            )
            with patch("watcher.snapshot.time.time", return_value=modified + 400):
                tightened = collector.snapshot()

        self.assertFalse(relaxed["seriesCatalog"][0]["stale"])
        self.assertNotEqual(
            relaxed["physical"]["results"][series_id]["state"],
            "indeterminate",
        )
        self.assertNotIn(
            "The selected series is stale.",
            relaxed["physical"]["results"][series_id]["notices"],
        )
        self.assertTrue(tightened["seriesCatalog"][0]["stale"])
        self.assertEqual(
            tightened["physical"]["results"][series_id]["state"],
            "indeterminate",
        )

    def test_catalog_staleness_ages_without_postprocessing_refresh(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            collector = WatcherCollector(case.path)
            initial = collector.snapshot()
            series_id = initial["seriesCatalog"][0]["id"]
            collector.update_config(
                {
                    "version": 1,
                    "selectedLog": None,
                    "selectedSeries": [series_id],
                    "overrides": {series_id: {"staleAfterSeconds": 30}},
                    "acceptedStates": ["plateau"],
                }
            )
            modified = (
                case.path / "postProcessing/lift/0/coefficient.dat"
            ).stat().st_mtime
            with patch("watcher.snapshot.time.time", return_value=modified + 10):
                fresh = collector.snapshot()
            with patch("watcher.snapshot.time.time", return_value=modified + 40):
                stale = collector.snapshot()

        self.assertFalse(fresh["seriesCatalog"][0]["stale"])
        self.assertTrue(stale["seriesCatalog"][0]["stale"])
        self.assertEqual(
            stale["physical"]["results"][series_id]["state"],
            "indeterminate",
        )
        self.assertTrue(
            any(
                notice["source"] == "postProcessing/lift/coefficient.dat"
                and "stale" in notice["message"].lower()
                for notice in stale["notices"]
            )
        )

    def test_stale_postprocessing_is_visible_in_catalog_and_notices(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            table = case.path / "postProcessing/lift/0/coefficient.dat"
            modified = time.time() - 360.0
            os.utime(table, (modified, modified))
            snapshot = WatcherCollector(case.path).snapshot()

        self.assertTrue(snapshot["seriesCatalog"][0]["stale"])
        self.assertTrue(
            any(
                notice["source"] == "postProcessing/lift/coefficient.dat"
                and "stale" in notice["message"].lower()
                for notice in snapshot["notices"]
            )
        )

    def test_catalog_and_series_use_bounded_min_max_envelopes(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            values = [0.0] * 1001
            values[517] = 99.0
            rows = "\n".join(f"{index} {value}" for index, value in enumerate(values))
            case.write("postProcessing/lift/0/coefficient.dat", f"# Time Cl\n{rows}\n")
            collector = WatcherCollector(case.path)
            snapshot = collector.snapshot()
            series_id = snapshot["seriesCatalog"][0]["id"]
            detail = collector.series(series_id, limit=20)
            capped = collector.series(series_id, limit=20_000)

        self.assertLessEqual(len(snapshot["seriesCatalog"][0]["preview"]["times"]), 300)
        self.assertLessEqual(len(detail["times"]), 20)
        self.assertIn(99.0, detail["values"])
        self.assertEqual(detail["times"][0], 0.0)
        self.assertEqual(detail["times"][-1], 1000.0)
        self.assertLessEqual(len(capped["times"]), 2000)

    def test_envelope_limits_preserve_defined_endpoints_and_extrema(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            values = (0.0, 5.0, -5.0, 1.0, 8.0, -9.0, 0.0)
            rows = "\n".join(
                f"{index} {value}" for index, value in enumerate(values)
            )
            case.write(
                "postProcessing/lift/0/coefficient.dat",
                f"# Time Cl\n{rows}\n",
            )
            collector = WatcherCollector(case.path)
            series_id = collector.snapshot()["seriesCatalog"][0]["id"]
            limited = {
                limit: collector.series(series_id, limit=limit)
                for limit in (1, 2, 3, 5, 6)
            }

        self.assertEqual(limited[1]["times"], [6.0])
        self.assertEqual(limited[2]["times"], [0.0, 6.0])
        self.assertEqual(limited[3]["times"], [0.0, 5.0, 6.0])
        for limit in (3, 5, 6):
            self.assertEqual(limited[limit]["times"][0], 0.0)
            self.assertEqual(limited[limit]["times"][-1], 6.0)
            self.assertLessEqual(len(limited[limit]["times"]), limit)
        self.assertIn(-9.0, limited[5]["values"])
        self.assertIn(8.0, limited[6]["values"])

    def test_envelope_ties_are_deterministic(self) -> None:
        with TemporaryCase() as case:
            _populate_case(case)
            case.write(
                "postProcessing/lift/0/coefficient.dat",
                "# Time Cl\n0 0\n1 5\n2 -5\n3 5\n4 -5\n5 0\n",
            )
            collector = WatcherCollector(case.path)
            series_id = collector.snapshot()["seriesCatalog"][0]["id"]
            first = collector.series(series_id, limit=3)
            second = collector.series(series_id, limit=3)

        self.assertEqual(first["times"], [0.0, 1.0, 5.0])
        self.assertEqual(second, first)

    def test_json_conversion_is_recursive_camel_case_and_finite(self) -> None:
        @dataclass(frozen=True)
        class Payload:
            sample_path: Path
            values: tuple[object, ...]

        converted = to_json_safe(
            {
                "payload": Payload(
                    Path("case/log"),
                    (math.inf, -math.inf, math.nan, {3, 1}),
                ),
                "series_with_underscore": {"value": math.inf},
            }
        )

        self.assertEqual(
            converted,
            {
                "payload": {
                    "samplePath": str(Path("case/log")),
                    "values": [None, None, None, [1, 3]],
                },
                "series_with_underscore": {"value": None},
            },
        )
        json.dumps(converted, allow_nan=False)
