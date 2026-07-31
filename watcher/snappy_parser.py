from __future__ import annotations

import math
from pathlib import Path
import re

from watcher.case_config import parse_foam_file
from watcher.models import FailureRecord, LogChunk, SnappySettings, SnappyTelemetry


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
_MORPH = re.compile(r"\bMorph\s+iteration\s+(?P<iteration>\d+)\b", re.IGNORECASE)
_SMOOTHING = re.compile(
    r"\b(?:smooth(?:ing)?\s+displacement|displacement\s+smoothing)"
    r"(?:\s*[:=-]?\s*iteration)?\s+(?P<iteration>\d+)\b",
    re.IGNORECASE,
)
_POINT_DISPLACEMENT = re.compile(
    r"\bSolving for\s+pointDisplacement\w*\b.*\bNo Iterations\s*(?:=\s*)?(?P<iteration>\d+)\b",
    re.IGNORECASE,
)
_MESH_COUNT = re.compile(
    r"\b(?P<label>cells|faces|points)\s*:\s*(?P<count>\d+)\b",
    re.IGNORECASE,
)
_EXECUTION_TIME = re.compile(
    rf"^ExecutionTime\s*=\s*(?P<execution>{_NUMBER})\s+s\s+"
    rf"ClockTime\s*=\s*(?P<clock>{_NUMBER})\s+s\s*$",
    re.IGNORECASE,
)
_WARNING = re.compile(r"^(?:-->)?\s*(?:FOAM\s+)?Warning\b", re.IGNORECASE)
_FATAL = (
    ("FOAM fatal error", re.compile(r"^\s*(?:\[\d+\]\s*)?(?:-->\s*)?FOAM FATAL (?:IO )?ERROR\s*:\s*$", re.IGNORECASE)),
    ("floating point exception", re.compile(r"^\s*(?:\[\d+\]\s*)?Floating point exception(?:\s+\(core dumped\))?\s*$", re.IGNORECASE)),
    ("segmentation fault", re.compile(r"^\s*(?:\[\d+\]\s*)?Segmentation fault(?:\s+\(core dumped\))?\s*$", re.IGNORECASE)),
    ("MPI abort", re.compile(r"^\s*(?:\[\d+\]\s*)?MPI_ABORT was invoked on rank \d+\b", re.IGNORECASE)),
)
_CASTELLATION = re.compile(
    r"\b(?:castellated mesh generation|mesh refinement|refinement phase)\b",
    re.IGNORECASE,
)
_SNAPPING = re.compile(r"\b(?:snapping phase|mesh snapping|morph iteration)\b", re.IGNORECASE)
_LAYERS = re.compile(r"\b(?:layer addition|adding layers|add layers)\b", re.IGNORECASE)
_FINALIZING = re.compile(
    r"\b(?:finalis(?:e|ing)|finaliz(?:e|ing)|checking final mesh|final mesh)\b",
    re.IGNORECASE,
)
_END = re.compile(r"^End\s*$")
_MAX_GLOBAL = re.compile(
    r"\bmaxGlobalCells\b.*\b(?:reached|exceeded|stopp(?:ed|ing))\b",
    re.IGNORECASE,
)


def read_snappy_settings(case_dir: Path) -> SnappySettings:
    """Read only the snappy settings needed to explain phase-local progress."""
    resolved = case_dir.resolve()
    data, notices = parse_foam_file(resolved / "system" / "snappyHexMeshDict", resolved)
    snap = data.get("snapControls")
    castellated = data.get("castellatedMeshControls")
    snap_dict = snap if isinstance(snap, dict) else {}
    castellated_dict = castellated if isinstance(castellated, dict) else {}
    add_layers = _boolean(data.get("addLayers"))
    return SnappySettings(
        add_layers=add_layers,
        n_solve_iter=_integer(snap_dict.get("nSolveIter")),
        n_relax_iter=_integer(snap_dict.get("nRelaxIter")),
        max_global_cells=_integer(castellated_dict.get("maxGlobalCells")),
        stage_count=2 if add_layers is False else 3,
        notices=notices,
    )


class SnappyHexMeshParser:
    """Incrementally parse bounded snappyHexMesh progress evidence."""

    def __init__(self, settings: SnappySettings) -> None:
        self.settings = settings
        self._current_segment = 0
        self._reset_run()

    def feed(self, chunk: LogChunk) -> None:
        if chunk.reset or chunk.segment != self._current_segment:
            self._current_segment = chunk.segment
            self._reset_run()
        for line in chunk.lines:
            self._feed_line(line.strip())

    def snapshot(self) -> SnappyTelemetry:
        stage_index = _stage_index(self._stage, self.settings.stage_count)
        phase_percent = None
        if (
            self._stage == "snapping"
            and self._completed_morph_iterations is not None
            and self.settings.n_relax_iter is not None
            and self.settings.n_relax_iter > 0
        ):
            phase_percent = min(
                100.0,
                self._completed_morph_iterations / self.settings.n_relax_iter * 100.0,
            )
        return SnappyTelemetry(
            current_segment=self._current_segment,
            stage=self._stage,
            stage_index=stage_index,
            stage_count=self.settings.stage_count,
            current_work=self._current_work,
            active_morph_iteration=self._active_morph_iteration,
            completed_morph_iterations=self._completed_morph_iterations,
            morph_total=self.settings.n_relax_iter,
            smoothing_iteration=self._smoothing_iteration,
            smoothing_total=self.settings.n_solve_iter,
            phase_progress_percent=phase_percent,
            mesh_cells=self._mesh_counts["cells"],
            mesh_faces=self._mesh_counts["faces"],
            mesh_points=self._mesh_counts["points"],
            max_global_cells_reached=self._max_global_cells_reached,
            execution_seconds=self._execution_seconds,
            clock_seconds=self._clock_seconds,
            completed=self._completed,
            warnings=tuple(self._warnings),
            warning_count=self._warning_count,
            failure=self._failure,
            notices=tuple(self._notices),
            notice_count=self._notice_count,
        )

    def _reset_run(self) -> None:
        self._stage = "initialization"
        self._current_work: str | None = None
        self._active_morph_iteration: int | None = None
        self._completed_morph_iterations: int | None = None
        self._smoothing_iteration: int | None = None
        self._mesh_counts: dict[str, int | None] = {"cells": None, "faces": None, "points": None}
        self._max_global_cells_reached = False
        self._execution_seconds: float | None = None
        self._clock_seconds: float | None = None
        self._completed = False
        self._warnings: list[str] = []
        self._warning_count = 0
        self._failure: FailureRecord | None = None
        self._notices: list[str] = list(self.settings.notices)
        self._notice_count = len(self._notices)

    def _feed_line(self, line: str) -> None:
        if not line:
            return
        for label, pattern in _FATAL:
            if pattern.search(line):
                self._failure = FailureRecord(label, line, self._current_segment, None)
                return
        if _WARNING.search(line):
            self._append_warning(line)
            self._append_notice(line)
        if _MAX_GLOBAL.search(line):
            self._max_global_cells_reached = True
            self._append_warning(line)
            self._append_notice(line)

        if _END.match(line):
            self._completed = True
            self._set_stage("completed")
            self._current_work = "snappyHexMesh completed"
            return
        if _FINALIZING.search(line):
            if self._set_stage("finalizing"):
                self._current_work = line
        elif _LAYERS.search(line):
            if self._set_stage("layers"):
                self._current_work = line
        elif _SNAPPING.search(line):
            if self._set_stage("snapping"):
                self._current_work = line
        elif _CASTELLATION.search(line):
            if self._set_stage("castellation"):
                self._current_work = line

        morph = _MORPH.search(line)
        if morph:
            zero_based = int(morph.group("iteration"))
            self._active_morph_iteration = zero_based + 1
            self._completed_morph_iterations = zero_based
            self._current_work = line
        smoothing = _SMOOTHING.search(line) or _POINT_DISPLACEMENT.search(line)
        if smoothing:
            self._smoothing_iteration = int(smoothing.group("iteration"))
            self._current_work = line
        for match in _MESH_COUNT.finditer(line):
            self._mesh_counts[match.group("label").lower()] = int(match.group("count"))
        timing = _EXECUTION_TIME.match(line)
        if timing:
            self._execution_seconds = float(timing.group("execution"))
            self._clock_seconds = float(timing.group("clock"))

    def _set_stage(self, stage: str) -> bool:
        order = {
            "initialization": 0,
            "castellation": 1,
            "snapping": 2,
            "layers": 3,
            "finalizing": 4,
            "completed": 5,
        }
        if order[stage] < order[self._stage]:
            return False
        self._stage = stage
        return True

    def _append_notice(self, notice: str) -> None:
        if self._notices and self._notices[-1] == notice:
            return
        self._notice_count += 1
        _append_bounded(self._notices, notice)

    def _append_warning(self, warning: str) -> None:
        if self._warnings and self._warnings[-1] == warning:
            return
        self._warning_count += 1
        _append_bounded(self._warnings, warning)


def _stage_index(stage: str, stage_count: int) -> int | None:
    if stage == "castellation":
        return 1
    if stage == "snapping":
        return 2
    if stage == "layers":
        return 3
    if stage in {"finalizing", "completed"}:
        return stage_count
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value).is_integer():
        integer = int(value)
        return integer if integer >= 0 else None
    return None


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
    return None


def _append_bounded(values: list[str], value: str, limit: int = 200) -> None:
    if not values or values[-1] != value:
        values.append(value)
    if len(values) > limit:
        del values[: len(values) - limit]
