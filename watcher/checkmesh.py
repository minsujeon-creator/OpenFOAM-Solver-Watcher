from __future__ import annotations

from dataclasses import dataclass, replace
from collections import deque
import math
from pathlib import Path
import re
import shutil
import subprocess
from threading import RLock, Thread
import time
from typing import Callable, Sequence

from watcher.models import (
    MeshQualityMetric,
    MeshQualityProblem,
    MeshQualityReport,
    MeshQualityStatus,
)


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
_CORE_MESH_FILES = ("points", "faces", "owner", "neighbour", "boundary")
_DIAGNOSTIC_LIMIT = 200
_EVIDENCE_LIMIT = 200
_READ_CHUNK = 8_192
_MAX_LINE_CHARS = 16_384
_BASE_COMMAND = ("-latestTime", "-allTopology", "-allGeometry")
_ADVISORY = (
    "Advisory only: checkMesh evidence must be judged against the physics, numerics, "
    "and OpenFOAM version used by this case."
)


@dataclass(frozen=True)
class MeshProbe:
    signature: tuple[object, ...] | None
    source: str | None
    reason: str | None
    latest_modified_at: float | None = None


def parse_checkmesh_output(
    output: str,
    *,
    exit_code: int,
    command: Sequence[str] = (),
    started_at: float | None = None,
    finished_at: float | None = None,
) -> MeshQualityReport:
    """Parse important, explicitly reported checkMesh evidence across common dialects."""
    counts: dict[str, int | None] = {
        "points": None,
        "faces": None,
        "internal_faces": None,
        "cells": None,
        "regions": None,
    }
    metric_data: dict[str, dict[str, object]] = {}
    problems: list[MeshQualityProblem] = []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    mesh_ok: bool | None = None
    failed_checks: int | None = None
    fatal = False
    bounding_box_min: tuple[float, float, float] | None = None
    bounding_box_max: tuple[float, float, float] | None = None
    geometric_directions: tuple[int, int, int] | None = None
    solution_directions: tuple[int, int, int] | None = None

    def metric(
        code: str,
        label: str,
        observed: float,
        unit: str | None,
        line: str,
        *,
        status: str = "informational",
        target: float | str | None = None,
    ) -> None:
        existing = metric_data.get(code)
        if existing is not None and existing.get("status") == "failing":
            status = "failing"
        metric_data[code] = {
            "code": code,
            "label": label,
            "observed": observed,
            "unit": unit,
            "target": target,
            "status": status,
            "explanation": line,
        }

    def mark_metric(code: str, status: str, target: float | str | None = None) -> None:
        existing = metric_data.get(code)
        if existing is None:
            return
        existing["status"] = status
        if target is not None:
            existing["target"] = target

    for line in lines:
        lowered = line.lower()
        if any(
            pattern.search(line)
            for pattern in (
                re.compile(r"^(?:-->\s*)?FOAM FATAL (?:IO )?ERROR\s*:?$", re.IGNORECASE),
                re.compile(r"^Floating point exception(?:\s+\(core dumped\))?$", re.IGNORECASE),
                re.compile(r"^Segmentation fault(?:\s+\(core dumped\))?$", re.IGNORECASE),
                re.compile(r"^MPI_ABORT was invoked on rank \d+\b", re.IGNORECASE),
            )
        ):
            fatal = True

        count_match = re.match(
            r"^(points|faces|internal\s+faces|cells)\s*:\s*(\d+)\b",
            line,
            re.IGNORECASE,
        )
        if count_match:
            key = count_match.group(1).lower().replace(" ", "_")
            counts[key] = int(count_match.group(2))
        regions = re.search(r"\bNumber of regions\s*:\s*(\d+)\b", line, re.IGNORECASE)
        if regions:
            counts["regions"] = int(regions.group(1))

        bounds = re.search(
            rf"\bOverall domain bounding box\s*\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)\s*"
            rf"\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)",
            line,
            re.IGNORECASE,
        )
        if bounds:
            bounding_box_min = tuple(float(bounds.group(index)) for index in range(1, 4))  # type: ignore[assignment]
            bounding_box_max = tuple(float(bounds.group(index)) for index in range(4, 7))  # type: ignore[assignment]
        official_directions = re.search(
            r"\bMesh has \d+\s+(geometric|solution)\s+\([^)]*\) directions\s*"
            r"\(\s*([01])\s+([01])\s+([01])\s*\)",
            line,
            re.IGNORECASE,
        )
        if official_directions:
            values = tuple(int(official_directions.group(index)) for index in range(2, 5))
            if official_directions.group(1).lower() == "geometric":
                geometric_directions = values  # type: ignore[assignment]
            else:
                solution_directions = values  # type: ignore[assignment]
        legacy_directions = re.search(
            r"\bMesh \((non-empty(?:, non-wedge)?)\) directions\s*\(\s*([01])\s+([01])\s+([01])\s*\)",
            line,
            re.IGNORECASE,
        )
        if legacy_directions:
            values = tuple(int(legacy_directions.group(index)) for index in range(2, 5))
            if "non-wedge" in legacy_directions.group(1).lower():
                geometric_directions = values  # type: ignore[assignment]
            else:
                solution_directions = values  # type: ignore[assignment]

        match = re.search(rf"\bMax aspect ratio\s*=\s*({_NUMBER})", line, re.IGNORECASE)
        if match:
            metric("aspect_ratio_max", "Maximum aspect ratio", float(match.group(1)), None, line, status=_ok_status(line))

        match = re.search(
            rf"\bMinimum face area\s*=\s*({_NUMBER}).*?Maximum face area\s*=\s*({_NUMBER})",
            line,
            re.IGNORECASE,
        )
        if match:
            status = _ok_status(line)
            metric("face_area_min", "Minimum face area", float(match.group(1)), "m²", line, status=status)
            metric("face_area_max", "Maximum face area", float(match.group(2)), "m²", line, status=status)

        match = re.search(
            rf"\bMin(?:imum)? volume\s*=\s*({_NUMBER}).*?Max(?:imum)? volume\s*=\s*({_NUMBER})",
            line,
            re.IGNORECASE,
        )
        if match:
            status = _ok_status(line)
            metric("cell_volume_min", "Minimum cell volume", float(match.group(1)), "m³", line, status=status)
            metric("cell_volume_max", "Maximum cell volume", float(match.group(2)), "m³", line, status=status)

        match = re.search(
            rf"\bMesh non-orthogonality Max\s*:\s*({_NUMBER})\s+average\s*:\s*({_NUMBER})",
            line,
            re.IGNORECASE,
        )
        if match:
            metric("non_orthogonality_max", "Maximum non-orthogonality", float(match.group(1)), "°", line)
            metric("non_orthogonality_average", "Average non-orthogonality", float(match.group(2)), "°", line)
        if "non-orthogonality check ok" in lowered:
            mark_metric("non_orthogonality_max", "passing")
            mark_metric("non_orthogonality_average", "passing")

        match = re.search(rf"\bMax skewness\s*=\s*({_NUMBER})", line, re.IGNORECASE)
        if match:
            metric("skewness_max", "Maximum skewness", float(match.group(1)), None, line, status=_ok_status(line))

        for code, label, pattern, unit in (
            ("determinant_min", "Minimum determinant", r"\bMin(?:imum)? determinant\s*=", None),
            ("face_weight_min", "Minimum face weight", r"\bMin(?:imum)? face weight\s*=", None),
            ("volume_ratio_min", "Minimum volume ratio", r"\bMin(?:imum)? volume ratio\s*=", None),
            ("tet_quality_min", "Minimum tet quality", r"\bMin(?:imum)? tet quality\s*=", None),
            ("concavity_max", "Maximum concavity", r"\bMax(?:imum)? concav(?:e|ity)\s*=", "°"),
            ("cell_openness_max", "Maximum cell openness", r"\bMax(?:imum)? cell openness\s*=", None),
        ):
            match = re.search(rf"{pattern}\s*({_NUMBER})", line, re.IGNORECASE)
            if match:
                metric(code, label, float(match.group(1)), unit, line, status=_ok_status(line))

        problem = _problem_from_line(line)
        if problem is not None:
            problems.append(problem)
            criterion_status = "failing" if problem.count > 0 else "passing"
            related_metric = {
                "severely_non_orthogonal_faces": "non_orthogonality_max",
                "non_orthogonality_faces": "non_orthogonality_max",
                "highly_skew_faces": "skewness_max",
                "concave_faces": "concavity_max",
                "interpolation_weight_faces": "face_weight_min",
                "volume_ratio_faces": "volume_ratio_min",
                "low_determinant_cells": "determinant_min",
                "negative_volume_cells": "cell_volume_min",
            }.get(problem.code)
            if related_metric is not None:
                mark_metric(related_metric, criterion_status, problem.limit)

        if re.search(r"\bMesh OK\s*\.", line, re.IGNORECASE):
            mesh_ok = True
        failed = re.search(r"\bFailed\s+(\d+)\s+mesh checks?\s*\.", line, re.IGNORECASE)
        if failed:
            failed_checks = int(failed.group(1))
            mesh_ok = False

    if exit_code != 0 or fatal or mesh_ok is False:
        status = "failing"
        if failed_checks is not None:
            summary = f"checkMesh failed {failed_checks} mesh checks."
        elif exit_code != 0:
            summary = f"checkMesh did not complete successfully (exit code {exit_code})."
        else:
            summary = "checkMesh reported a fatal failure."
    elif mesh_ok is True:
        status = "passing"
        summary = "checkMesh reports Mesh OK."
    else:
        status = "indeterminate"
        summary = "checkMesh finished without an authoritative Mesh OK or failed-checks result."

    elapsed = None
    if started_at is not None and finished_at is not None:
        candidate = finished_at - started_at
        if math.isfinite(candidate) and candidate >= 0.0:
            elapsed = candidate
    metrics = tuple(MeshQualityMetric(**item) for item in metric_data.values())
    return MeshQualityReport(
        status=status,
        summary=summary,
        mesh_ok=mesh_ok,
        failed_checks=failed_checks,
        exit_code=exit_code,
        command=tuple(command),
        started_at=started_at,
        finished_at=finished_at,
        execution_seconds=elapsed,
        points=counts["points"],
        faces=counts["faces"],
        internal_faces=counts["internal_faces"],
        cells=counts["cells"],
        regions=counts["regions"],
        bounding_box_min=bounding_box_min,
        bounding_box_max=bounding_box_max,
        geometric_directions=geometric_directions,
        solution_directions=solution_directions,
        metrics=metrics,
        problems=tuple(problems),
        diagnostics=tuple(lines[-_DIAGNOSTIC_LIMIT:]),
    )


def _ok_status(line: str) -> str:
    return "passing" if re.search(r"\bOK\b", line, re.IGNORECASE) else "informational"


def _problem_from_line(line: str) -> MeshQualityProblem | None:
    clean = line.lstrip("* ")
    lowered = clean.lower()
    if "number of regions" in lowered:
        return None
    failure_markers = (
        "non-orthogonal",
        "non-orthogonality",
        "skew",
        "negative volume",
        "face pyramid",
        "concav",
        "interpolation weight",
        "volume ratio",
        "face twist",
        "determinant",
        "illegal",
        "disconnected",
        "duplicate",
        "zero area",
    )
    if not line.lstrip().startswith("*") and not any(marker in lowered for marker in failure_markers):
        return None
    match = re.search(r"(?P<label>.+?)\s*[:=]\s*(?P<count>\d+)\.?$", clean)
    if match is None:
        return None
    raw_label = match.group("label").strip()
    limit_match = re.search(rf"[<>]\s*({_NUMBER})", raw_label)
    limit = float(limit_match.group(1)) if limit_match is not None else None
    if "non-orthogonal" in lowered:
        if "severely" in lowered:
            code = "severely_non_orthogonal_faces"
            label = "Severely non-orthogonal faces"
        else:
            code = "non_orthogonality_faces"
            label = "Faces above the non-orthogonality criterion"
    elif "non-orthogonality" in lowered:
        code = "non_orthogonality_faces"
        label = "Faces above the non-orthogonality criterion"
    elif "face pyramid" in lowered:
        code = "face_pyramid_faces"
        label = "Faces below the face-pyramid criterion"
    elif "concav" in lowered:
        code = "concave_faces"
        label = "Faces above the concavity criterion"
    elif "skew" in lowered:
        code = "highly_skew_faces"
        label = "Highly skew faces"
    elif "interpolation weight" in lowered:
        code = "interpolation_weight_faces"
        label = "Faces below the interpolation-weight criterion"
    elif "volume ratio" in lowered:
        code = "volume_ratio_faces"
        label = "Faces below the neighbour-volume-ratio criterion"
    elif "face twist" in lowered:
        code = "face_twist_faces"
        label = "Faces below the face-twist criterion"
    elif "determinant" in lowered:
        code = "low_determinant_cells"
        label = "Cells below the determinant criterion"
    elif "negative" in lowered and "volume" in lowered:
        code = "negative_volume_cells"
        label = "Negative-volume cells"
    else:
        code = re.sub(r"[^a-z0-9]+", "_", raw_label.lower()).strip("_")
        label = raw_label[:1].upper() + raw_label[1:]
    return MeshQualityProblem(
        code=code,
        label=label,
        count=int(match.group("count")),
        limit=limit,
        explanation=line,
    )


def _bounded_process_output(stream: object) -> str:
    """Read a text pipe without allowing total output or one line to grow unbounded."""
    evidence: deque[tuple[int, str]] = deque(maxlen=_EVIDENCE_LIMIT)
    tail: deque[tuple[int, str]] = deque(maxlen=_DIAGNOSTIC_LIMIT)
    pending = ""
    sequence = 0

    def retain(line: str) -> None:
        nonlocal sequence
        cleaned = line.rstrip("\r")[:_MAX_LINE_CHARS]
        if not cleaned:
            return
        item = (sequence, cleaned)
        sequence += 1
        tail.append(item)
        if _important_output_line(cleaned):
            evidence.append(item)

    while True:
        chunk = getattr(stream, "read")(_READ_CHUNK)
        if not chunk:
            break
        pending += str(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            retain(line)
        while len(pending) > _MAX_LINE_CHARS:
            retain(pending[:_MAX_LINE_CHARS])
            pending = pending[_MAX_LINE_CHARS:]
    if pending:
        retain(pending)
    merged = {index: line for index, line in evidence}
    merged.update({index: line for index, line in tail})
    return "\n".join(merged[index] for index in sorted(merged))


def _important_output_line(line: str) -> bool:
    lowered = line.lower()
    return any(
        marker in lowered
        for marker in (
            "points:",
            "faces:",
            "internal faces:",
            "cells:",
            "number of regions",
            "bounding box",
            "mesh (non-empty",
            "mesh has",
            "aspect ratio",
            "face area",
            "volume",
            "non-orthogon",
            "skew",
            "determinant",
            "face weight",
            "volume ratio",
            "tet quality",
            "concav",
            "openness",
            "face pyramid",
            "interpolation weight",
            "face twist",
            "mesh ok",
            "mesh check",
            "foam fatal",
            "floating point exception",
            "segmentation fault",
        )
    )


def mesh_probe(case_dir: Path) -> MeshProbe:
    """Return a signature for the latest complete undecomposed polyMesh."""
    case_dir = case_dir.resolve()
    candidates: list[tuple[float, Path]] = []
    constant = case_dir / "constant" / "polyMesh"
    if constant.is_dir():
        candidates.append((float("-inf"), constant))
    try:
        children = tuple(case_dir.iterdir())
    except OSError as error:
        return MeshProbe(None, None, f"Could not inspect the case mesh: {error}")
    for child in children:
        if not child.is_dir():
            continue
        try:
            value = float(child.name)
        except ValueError:
            continue
        if math.isfinite(value) and (child / "polyMesh").is_dir():
            candidates.append((value, child / "polyMesh"))

    for _, mesh_dir in sorted(candidates, key=lambda item: item[0], reverse=True):
        stats: list[object] = [mesh_dir.resolve().as_posix()]
        latest_modified_ns = 0
        complete = True
        for name in _CORE_MESH_FILES:
            path = mesh_dir / name
            if not path.is_file():
                compressed = mesh_dir / f"{name}.gz"
                path = compressed if compressed.is_file() else path
            try:
                stat = path.stat()
            except OSError:
                complete = False
                break
            stats.extend((path.name, stat.st_size, stat.st_mtime_ns))
            latest_modified_ns = max(latest_modified_ns, stat.st_mtime_ns)
        if complete:
            return MeshProbe(
                tuple(stats),
                mesh_dir.relative_to(case_dir).as_posix(),
                None,
                latest_modified_ns / 1_000_000_000,
            )

    decomposed = any(
        child.is_dir() and re.fullmatch(r"processor\d+", child.name)
        for child in children
    )
    if decomposed:
        return MeshProbe(
            None,
            None,
            "Only decomposed processor meshes were found; reconstruct an undecomposed mesh before the automatic check.",
        )
    return MeshProbe(None, None, "No complete undecomposed polyMesh was found yet.")


class CheckMeshMonitor:
    """Schedule one read-only background check per stable mesh signature."""

    def __init__(
        self,
        case_dir: Path,
        *,
        stable_seconds: float = 15.0,
        clock: Callable[[], float] = time.time,
        command_finder: Callable[[str], str | None] = shutil.which,
        probe_reader: Callable[[Path], MeshProbe] = mesh_probe,
        quality_dict_reader: Callable[[Path], object | None] | None = None,
        popen_factory: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self.case_dir = Path(case_dir)
        self.stable_seconds = max(0.0, stable_seconds)
        self._clock = clock
        self._command_finder = command_finder
        self._probe_reader = probe_reader
        self._quality_dict_reader = quality_dict_reader or _mesh_quality_marker
        self._popen_factory = popen_factory
        self._lock = RLock()
        self._observed_signature: tuple[object, ...] | None = None
        self._observed_at: float | None = None
        self._last_checked_signature: tuple[object, ...] | None = None
        self._worker: Thread | None = None
        self._process: object | None = None
        self._closed = False
        self._status = MeshQualityStatus(
            "waiting",
            "Waiting for a complete stable mesh.",
            None,
            None,
            None,
            None,
            _ADVISORY,
        )

    def update(self, *, mesh_busy: bool) -> MeshQualityStatus:
        probe = self._probe_reader(self.case_dir)
        now = self._clock()
        with self._lock:
            if self._closed:
                return self._status
            if probe.signature is None:
                self._status = replace(
                    self._status,
                    state="unavailable",
                    summary=probe.reason or "No checkable mesh was found.",
                    mesh_source=probe.source,
                    stable_for_seconds=None,
                    next_check_seconds=None,
                    report=None,
                )
                return self._status
            quality_marker = self._quality_dict_reader(self.case_dir)
            use_mesh_quality = quality_marker is not None and quality_marker is not False
            effective_signature = (probe.signature, quality_marker)
            if effective_signature != self._observed_signature:
                self._observed_signature = effective_signature
                self._observed_at = _latest_input_change(
                    now,
                    probe.latest_modified_at,
                    quality_marker,
                )
                self._status = replace(self._status, report=None)
            advisory = _ADVISORY
            if not use_mesh_quality:
                advisory += (
                    " system/meshQualityDict was not found, so user-defined "
                    "-meshQuality criteria are omitted while all topology and geometry checks remain enabled."
                )
            stable_for = max(0.0, now - (self._observed_at if self._observed_at is not None else now))
            if mesh_busy:
                self._status = replace(
                    self._status,
                    state="deferred",
                    summary="Automatic check deferred while snappyHexMesh is active.",
                    mesh_source=probe.source,
                    stable_for_seconds=stable_for,
                    next_check_seconds=None,
                    advisory=advisory,
                )
                return self._status
            if self._worker is not None and self._worker.is_alive():
                return replace(self._status, stable_for_seconds=stable_for)
            if effective_signature == self._last_checked_signature:
                self._status = replace(
                    self._status,
                    state="completed",
                    mesh_source=probe.source,
                    stable_for_seconds=stable_for,
                    next_check_seconds=None,
                    advisory=advisory,
                )
                return self._status
            remaining = max(0.0, self.stable_seconds - stable_for)
            if remaining > 0.0:
                self._status = replace(
                    self._status,
                    state="stabilizing",
                    summary=f"Mesh changed; waiting {remaining:.1f} s for stable files.",
                    mesh_source=probe.source,
                    stable_for_seconds=stable_for,
                    next_check_seconds=remaining,
                    advisory=advisory,
                )
                return self._status
            executable = self._command_finder("checkMesh")
            if executable is None:
                self._status = replace(
                    self._status,
                    state="unavailable",
                    summary="checkMesh is not on PATH; launch foam-watch from a sourced OpenFOAM shell.",
                    mesh_source=probe.source,
                    stable_for_seconds=stable_for,
                    next_check_seconds=None,
                    advisory=advisory,
                )
                return self._status
            signature = effective_signature
            command = (
                executable,
                *_BASE_COMMAND,
                *(("-meshQuality",) if use_mesh_quality else ()),
            )
            self._status = replace(
                self._status,
                state="running",
                summary="Running a thorough read-only checkMesh assessment.",
                mesh_source=probe.source,
                stable_for_seconds=stable_for,
                next_check_seconds=None,
                advisory=advisory,
            )
            self._worker = Thread(
                target=self._run,
                args=(signature, command),
                name="foam-watch-checkMesh",
                daemon=True,
            )
            self._worker.start()
            return self._status

    def snapshot(self) -> MeshQualityStatus:
        with self._lock:
            return self._status

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
            worker = self._worker
        if process is not None:
            _stop_owned_process(process)
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.5)

    def _run(self, signature: tuple[object, ...], command: tuple[str, ...]) -> None:
        started = self._clock()
        try:
            process = self._popen_factory(
                list(command),
                cwd=self.case_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
            with self._lock:
                self._process = process
                closed = self._closed
            if closed:
                _stop_owned_process(process)
            stream = getattr(process, "stdout")
            output = _bounded_process_output(stream)
            exit_code = int(getattr(process, "wait")())
            finished = self._clock()
            report = parse_checkmesh_output(
                output or "",
                exit_code=exit_code,
                command=command,
                started_at=started,
                finished_at=finished,
            )
            state = "completed"
            summary = report.summary
        except (OSError, ValueError) as error:
            finished = self._clock()
            report = parse_checkmesh_output(
                str(error),
                exit_code=1,
                command=command,
                started_at=started,
                finished_at=finished,
            )
            state = "failed"
            summary = f"Could not run checkMesh: {error}"
        with self._lock:
            self._process = None
            self._last_checked_signature = signature
            changed_during_check = signature != self._observed_signature
            self._status = replace(
                self._status,
                state="stabilizing" if changed_during_check else state,
                summary=(
                    "Mesh changed during checkMesh; a replacement check will run after stability."
                    if changed_during_check
                    else summary
                ),
                report=None if changed_during_check else report,
                next_check_seconds=None,
            )


def _stop_owned_process(process: object) -> None:
    try:
        if getattr(process, "poll")() is not None:
            return
        getattr(process, "terminate")()
        getattr(process, "wait")(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            getattr(process, "kill")()
        except OSError:
            pass
    except OSError:
        pass


def _mesh_quality_marker(case_dir: Path) -> tuple[int, int] | None:
    try:
        stat = (case_dir / "system" / "meshQualityDict").stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _latest_input_change(
    now: float,
    mesh_modified_at: float | None,
    quality_marker: object | None,
) -> float:
    candidates = [value for value in (mesh_modified_at,) if value is not None and math.isfinite(value)]
    if (
        isinstance(quality_marker, tuple)
        and len(quality_marker) >= 2
        and isinstance(quality_marker[1], int)
    ):
        candidates.append(quality_marker[1] / 1_000_000_000)
    return min(now, max(candidates)) if candidates else now
