from __future__ import annotations

import io
from pathlib import Path
import subprocess
from threading import Event, Thread
import time
from unittest import TestCase

from tests.helpers import TemporaryCase
from watcher.checkmesh import (
    CheckMeshMonitor,
    MeshProbe,
    mesh_probe,
    parse_checkmesh_output,
)


PASSING_OUTPUT = """\
Mesh stats
    points:           120
    faces:            330
    internal faces:   270
    cells:            100
    hexahedra:        100
    Number of regions: 1 (OK).

Checking geometry...
    Overall domain bounding box (0 0 0) (1 1 1)
    Mesh has 3 geometric (non-empty/wedge) directions (1 1 1)
    Mesh has 3 solution (non-empty) directions (1 1 1)
    Max aspect ratio = 12.4 OK.
    Minimum face area = 0.001. Maximum face area = 0.25.  Face area magnitudes OK.
    Min volume = 1e-06. Max volume = 0.01.  Total volume = 1.  Cell volumes OK.
    Mesh non-orthogonality Max: 61.2 average: 8.3
    Non-orthogonality check OK.
    Max skewness = 2.47 OK.
    Min determinant = 0.12. Mesh (non-empty, non-wedge) directions (1 1 1)
    Min face weight = 0.08
    Min volume ratio = 0.05

Mesh OK.
"""


FAILING_OUTPUT = """\
Mesh stats
    points: 120
    faces: 330
    internal faces: 270
    cells: 100
    Number of regions: 2
Checking geometry...
    Mesh non-orthogonality Max: 81 average: 12
   *Number of severely non-orthogonal (> 70 degrees) faces: 17.
    Max skewness = 5.2
   *Number of faces with skewness > 4 = 3
   *Number of cells with negative volume: 2
Failed 3 mesh checks.
"""


MESH_QUALITY_COUNTS = """\
Checking faces in error :
    non-orthogonality > 65 degrees                        : 0
    faces with face pyramid volume < 1e-13                : 0
    faces with concavity > 80 degrees                     : 4
    faces with skewness > 4 (internal) or 20 (boundary)   : 2
    faces with interpolation weights (0..1) < 0.05        : 0
    faces with volume ratio of neighbour cells < 0.01     : 1
    faces with face twist < 0.02                          : 0
    faces on cells with determinant < 0.001               : 0
Failed 3 mesh checks.
"""


class CheckMeshParserTests(TestCase):
    def test_parses_passing_thorough_report(self) -> None:
        report = parse_checkmesh_output(
            PASSING_OUTPUT,
            exit_code=0,
            command=("checkMesh", "-latestTime", "-allTopology", "-allGeometry", "-meshQuality"),
            started_at=10.0,
            finished_at=12.5,
        )

        self.assertEqual(report.status, "passing")
        self.assertTrue(report.mesh_ok)
        self.assertEqual(report.points, 120)
        self.assertEqual(report.faces, 330)
        self.assertEqual(report.internal_faces, 270)
        self.assertEqual(report.cells, 100)
        self.assertEqual(report.regions, 1)
        self.assertEqual(report.bounding_box_min, (0.0, 0.0, 0.0))
        self.assertEqual(report.bounding_box_max, (1.0, 1.0, 1.0))
        self.assertEqual(report.geometric_directions, (1, 1, 1))
        self.assertEqual(report.solution_directions, (1, 1, 1))
        self.assertEqual(report.execution_seconds, 2.5)
        metrics = {metric.code: metric for metric in report.metrics}
        self.assertEqual(metrics["aspect_ratio_max"].observed, 12.4)
        self.assertEqual(metrics["aspect_ratio_max"].status, "passing")
        self.assertEqual(metrics["non_orthogonality_max"].observed, 61.2)
        self.assertEqual(metrics["non_orthogonality_max"].status, "passing")
        self.assertEqual(metrics["skewness_max"].observed, 2.47)
        self.assertEqual(metrics["cell_volume_min"].observed, 1e-6)

    def test_parses_failed_checks_and_problem_counts(self) -> None:
        report = parse_checkmesh_output(FAILING_OUTPUT, exit_code=1)

        self.assertEqual(report.status, "failing")
        self.assertFalse(report.mesh_ok)
        self.assertEqual(report.failed_checks, 3)
        self.assertEqual(report.regions, 2)
        problems = {problem.code: problem.count for problem in report.problems}
        self.assertNotIn("regions", problems)
        self.assertEqual(problems["severely_non_orthogonal_faces"], 17)
        self.assertEqual(problems["highly_skew_faces"], 3)
        self.assertEqual(problems["negative_volume_cells"], 2)
        metrics = {metric.code: metric for metric in report.metrics}
        self.assertEqual(metrics["non_orthogonality_max"].status, "failing")
        self.assertEqual(metrics["skewness_max"].status, "failing")

    def test_parses_mesh_quality_dictionary_error_count_table(self) -> None:
        report = parse_checkmesh_output(MESH_QUALITY_COUNTS, exit_code=1)

        problems = {problem.code: problem for problem in report.problems}
        self.assertEqual(problems["non_orthogonality_faces"].count, 0)
        self.assertEqual(problems["non_orthogonality_faces"].limit, 65.0)
        self.assertEqual(problems["face_pyramid_faces"].count, 0)
        self.assertEqual(problems["concave_faces"].count, 4)
        self.assertEqual(problems["highly_skew_faces"].count, 2)
        self.assertEqual(problems["interpolation_weight_faces"].count, 0)
        self.assertEqual(problems["volume_ratio_faces"].count, 1)
        self.assertEqual(problems["face_twist_faces"].count, 0)
        self.assertEqual(problems["low_determinant_cells"].count, 0)

    def test_nonzero_or_fatal_incomplete_output_is_failing(self) -> None:
        report = parse_checkmesh_output(
            "--> FOAM FATAL ERROR:\nCannot find file points\n",
            exit_code=2,
        )

        self.assertEqual(report.status, "failing")
        self.assertIsNone(report.mesh_ok)
        self.assertIn("exit code 2", report.summary)
        self.assertTrue(any("FATAL" in line for line in report.diagnostics))

    def test_standard_trap_fpe_banner_does_not_override_mesh_ok(self) -> None:
        report = parse_checkmesh_output(
            "trapFpe : Floating point exception trapping enabled (FOAM_SIGFPE).\nMesh OK.\n",
            exit_code=0,
        )

        self.assertEqual(report.status, "passing")

    def test_problem_counts_update_related_metric_assessments(self) -> None:
        report = parse_checkmesh_output(
            """\
Min determinant = 0.0005
Min face weight = 0.01
Min volume ratio = 0.005
Max concavity = 85
faces on cells with determinant < 0.001 : 2
faces with interpolation weights (0..1) < 0.05 : 3
faces with volume ratio of neighbour cells < 0.01 : 4
faces with concavity > 80 degrees : 5
Failed 4 mesh checks.
""",
            exit_code=0,
        )

        metrics = {metric.code: metric for metric in report.metrics}
        self.assertEqual(metrics["determinant_min"].status, "failing")
        self.assertEqual(metrics["face_weight_min"].status, "failing")
        self.assertEqual(metrics["volume_ratio_min"].status, "failing")
        self.assertEqual(metrics["concavity_max"].status, "failing")

    def test_incomplete_successful_output_is_indeterminate_and_diagnostics_are_bounded(self) -> None:
        lines = [f"ordinary output {index}" for index in range(250)]
        report = parse_checkmesh_output("\n".join(lines), exit_code=0)

        self.assertEqual(report.status, "indeterminate")
        self.assertEqual(len(report.diagnostics), 200)
        self.assertEqual(report.diagnostics[0], "ordinary output 50")


class _FakeProcess:
    def __init__(self, output: str, exit_code: int = 0) -> None:
        self.output = output
        self.returncode = exit_code
        self.stdout = io.StringIO(output)
        self.terminated = False
        self.killed = False
        self.communicated = Event()

    def communicate(self) -> tuple[str, None]:
        raise AssertionError("The monitor must stream bounded output instead of communicate().")

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.communicated.set()
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _BlockingStream:
    def __init__(self) -> None:
        self.released = Event()
        self.entered = Event()

    def read(self, size: int) -> str:
        del size
        self.entered.set()
        self.released.wait(timeout=1.0)
        return ""


class _BlockingProcess:
    def __init__(self) -> None:
        self.stdout = _BlockingStream()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.released.set()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.released.set()


class CheckMeshMonitorTests(TestCase):
    def test_runs_immediately_when_startup_mesh_files_are_already_stable(self) -> None:
        calls: list[tuple[str, ...]] = []

        def popen(args: list[str], **kwargs: object) -> _FakeProcess:
            del kwargs
            calls.append(tuple(args))
            return _FakeProcess(PASSING_OUTPUT)

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=15.0,
            clock=lambda: 100.0,
            command_finder=lambda _: "checkMesh",
            probe_reader=lambda _: MeshProbe(
                ("mesh",),
                "constant/polyMesh",
                None,
                latest_modified_at=80.0,
            ),
            quality_dict_reader=lambda _: None,
            popen_factory=popen,
        )

        monitor.update(mesh_busy=False)
        self._wait_until(lambda: monitor.snapshot().state == "completed")

        self.assertEqual(len(calls), 1)

    def test_close_during_process_creation_still_terminates_the_late_child(self) -> None:
        factory_entered = Event()
        allow_process = Event()
        process = _BlockingProcess()

        def popen(args: list[str], **kwargs: object) -> _BlockingProcess:
            del args, kwargs
            factory_entered.set()
            allow_process.wait(timeout=1.0)
            return process

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=0.0,
            command_finder=lambda _: "checkMesh",
            probe_reader=lambda _: MeshProbe(("mesh",), "constant/polyMesh", None),
            popen_factory=popen,
        )
        monitor.update(mesh_busy=False)
        self.assertTrue(factory_entered.wait(timeout=1.0))
        closer = Thread(target=monitor.close)
        closer.start()
        allow_process.set()
        closer.join(timeout=2.0)

        self.assertFalse(closer.is_alive())
        self.assertTrue(process.terminated)

    def test_close_terminates_only_the_owned_running_check(self) -> None:
        started = Event()
        process = _BlockingProcess()

        def popen(args: list[str], **kwargs: object) -> _BlockingProcess:
            del args, kwargs
            started.set()
            return process

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=0.0,
            command_finder=lambda _: "checkMesh",
            probe_reader=lambda _: MeshProbe(("mesh",), "constant/polyMesh", None),
            popen_factory=popen,
        )
        monitor.update(mesh_busy=False)
        self.assertTrue(started.wait(timeout=1.0))
        self.assertTrue(process.stdout.entered.wait(timeout=1.0))

        monitor.close()

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_starts_once_after_stability_and_reruns_only_for_changed_mesh(self) -> None:
        now = [100.0]
        signature = [("constant/polyMesh", 1)]
        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def popen(args: list[str], **kwargs: object) -> _FakeProcess:
            calls.append((tuple(args), kwargs))
            return _FakeProcess(PASSING_OUTPUT)

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=15.0,
            clock=lambda: now[0],
            command_finder=lambda _: "/opt/openfoam/bin/checkMesh",
            probe_reader=lambda _: MeshProbe(tuple(signature), "constant/polyMesh", None),
            quality_dict_reader=lambda _: True,
            popen_factory=popen,
        )

        first = monitor.update(mesh_busy=False)
        now[0] = 114.9
        waiting = monitor.update(mesh_busy=False)
        self.assertEqual(first.state, "stabilizing")
        self.assertEqual(waiting.state, "stabilizing")
        self.assertEqual(calls, [])

        now[0] = 115.0
        monitor.update(mesh_busy=False)
        self._wait_until(lambda: monitor.snapshot().state == "completed")
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(
            args,
            (
                "/opt/openfoam/bin/checkMesh",
                "-latestTime",
                "-allTopology",
                "-allGeometry",
                "-meshQuality",
            ),
        )
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], Path("/case"))

        now[0] = 200.0
        monitor.update(mesh_busy=False)
        self.assertEqual(len(calls), 1)
        signature[0] = ("constant/polyMesh", 2)
        changed = monitor.update(mesh_busy=False)
        self.assertEqual(changed.state, "stabilizing")
        now[0] = 215.0
        monitor.update(mesh_busy=False)
        self._wait_until(lambda: len(calls) == 2 and monitor.snapshot().state == "completed")
        monitor.close()

    def test_omits_mesh_quality_option_when_its_required_dictionary_is_absent(self) -> None:
        calls: list[tuple[str, ...]] = []

        def popen(args: list[str], **kwargs: object) -> _FakeProcess:
            del kwargs
            calls.append(tuple(args))
            return _FakeProcess(PASSING_OUTPUT)

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=0.0,
            command_finder=lambda _: "checkMesh",
            probe_reader=lambda _: MeshProbe(("mesh",), "constant/polyMesh", None),
            quality_dict_reader=lambda _: False,
            popen_factory=popen,
        )
        monitor.update(mesh_busy=False)
        self._wait_until(lambda: monitor.snapshot().state == "completed")

        self.assertEqual(
            calls,
            [("checkMesh", "-latestTime", "-allTopology", "-allGeometry")],
        )
        self.assertIn("meshQualityDict", monitor.snapshot().advisory)

    def test_changed_mesh_quality_dictionary_schedules_a_new_check(self) -> None:
        marker: list[object | None] = [None]
        calls: list[tuple[str, ...]] = []

        def popen(args: list[str], **kwargs: object) -> _FakeProcess:
            del kwargs
            calls.append(tuple(args))
            return _FakeProcess(PASSING_OUTPUT)

        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=0.0,
            command_finder=lambda _: "checkMesh",
            probe_reader=lambda _: MeshProbe(("mesh",), "constant/polyMesh", None),
            quality_dict_reader=lambda _: marker[0],
            popen_factory=popen,
        )
        monitor.update(mesh_busy=False)
        self._wait_until(lambda: len(calls) == 1 and monitor.snapshot().state == "completed")
        marker[0] = (100, 123456)
        monitor.update(mesh_busy=False)
        self._wait_until(lambda: len(calls) == 2 and monitor.snapshot().state == "completed")

        self.assertNotIn("-meshQuality", calls[0])
        self.assertIn("-meshQuality", calls[1])

    def test_defers_for_active_mesher_and_reports_missing_command(self) -> None:
        now = [10.0]
        monitor = CheckMeshMonitor(
            Path("/case"),
            stable_seconds=0.0,
            clock=lambda: now[0],
            command_finder=lambda _: None,
            probe_reader=lambda _: MeshProbe(("mesh",), "constant/polyMesh", None),
        )

        deferred = monitor.update(mesh_busy=True)
        unavailable = monitor.update(mesh_busy=False)

        self.assertEqual(deferred.state, "deferred")
        self.assertEqual(unavailable.state, "unavailable")
        self.assertIn("checkMesh", unavailable.summary)

    def test_reports_decomposed_only_mesh_without_launching(self) -> None:
        monitor = CheckMeshMonitor(
            Path("/case"),
            probe_reader=lambda _: MeshProbe(None, None, "Only decomposed processor meshes were found."),
        )

        status = monitor.update(mesh_busy=False)

        self.assertEqual(status.state, "unavailable")
        self.assertIn("decomposed", status.summary)

    def test_default_probe_finds_latest_complete_undecomposed_mesh(self) -> None:
        with TemporaryCase() as case:
            for name in ("points", "faces", "owner", "neighbour", "boundary"):
                case.write(f"constant/polyMesh/{name}", name)
            for name in ("points", "faces", "owner", "neighbour", "boundary"):
                case.write(f"2.5/polyMesh/{name}", f"latest-{name}")

            probe = mesh_probe(case.path)

        self.assertIsNotNone(probe.signature)
        self.assertEqual(probe.source, "2.5/polyMesh")

    @staticmethod
    def _wait_until(predicate: object, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():  # type: ignore[operator]
            if time.monotonic() >= deadline:
                raise AssertionError("Timed out waiting for checkMesh worker")
            time.sleep(0.005)
