from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


def to_json_safe(value: object) -> object:
    """Recursively build camel-cased JSON primitives without non-finite floats."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            _camel_case(field.name): to_json_safe(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): to_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_json_safe(item) for item in sorted(value, key=repr)]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


@dataclass(frozen=True)
class ResidualTarget:
    pattern: str
    threshold: float


@dataclass(frozen=True)
class FunctionObjectSpec:
    name: str
    type_name: str
    fields: tuple[str, ...]
    region: str | None
    operation: str | None


@dataclass(frozen=True)
class CandidateInfo:
    score: int
    confidence: str
    recommended: bool
    explanation: str


@dataclass(frozen=True)
class ParsedTable:
    path: Path
    headers: tuple[str, ...]
    times: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]
    modified_ns: int
    notices: tuple[str, ...]
    probe_count: int
    column_widths: tuple[int, ...]


@dataclass(frozen=True)
class SeriesData:
    series_id: str
    label: str
    function_name: str
    function_type: str
    source_relative: str
    region: str | None
    field: str
    operation: str | None
    component: str | None
    units: str | None
    times: tuple[float, ...]
    values: tuple[float, ...]
    modified_ns: int
    stale: bool
    candidate: CandidateInfo
    notices: tuple[str, ...]


@dataclass(frozen=True)
class StationaritySettings:
    minimum_effective_samples: float = 20.0
    max_mean_shift_fraction: float = 0.02
    max_mean_shift_standard_errors: float = 2.0
    max_normalized_slope: float = 0.02
    minimum_cycles: int = 3
    max_period_variation_fraction: float = 0.05
    max_amplitude_variation_fraction: float = 0.10
    absolute_floor: float = 1e-12
    stale_after_seconds: float = 120.0


@dataclass(frozen=True)
class SeriesOverride:
    label: str | None = None
    units: str | None = None
    max_mean_shift_fraction: float | None = None
    max_mean_shift_standard_errors: float | None = None
    max_normalized_slope: float | None = None
    minimum_effective_samples: float | None = None
    minimum_cycles: int | None = None
    max_period_variation_fraction: float | None = None
    max_amplitude_variation_fraction: float | None = None
    absolute_floor: float | None = None
    stale_after_seconds: float | None = None


@dataclass(frozen=True)
class WatcherConfig:
    version: int
    selected_log: str | None
    selected_series: tuple[str, ...]
    overrides: Mapping[str, SeriesOverride]
    accepted_states: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_series", tuple(self.selected_series))
        object.__setattr__(self, "overrides", MappingProxyType(dict(self.overrides)))
        object.__setattr__(self, "accepted_states", frozenset(self.accepted_states))


@dataclass(frozen=True)
class ConfigLoadResult:
    config: WatcherConfig
    error: str | None


@dataclass(frozen=True)
class StationarityEvidence:
    raw_samples: int
    segment_samples: int
    window_samples: int
    discontinuous: bool
    earlier_mean: float | None
    latest_mean: float | None
    latest_standard_deviation: float | None
    coefficient_of_variation: float | None
    normalized_mean_shift: float | None
    normalized_slope: float | None
    autocorrelation_time: float
    effective_samples: float
    standard_error: float | None
    mean_shift_standard_errors: float | None
    period: float | None
    complete_cycles: int
    period_variation_fraction: float | None
    amplitude_variation_fraction: float | None
    cycle_mean_variation_fraction: float | None


@dataclass(frozen=True)
class StationarityResult:
    series_id: str
    state: str
    summary: str
    evidence: StationarityEvidence
    thresholds: Mapping[str, float]
    notices: tuple[str, ...]


@dataclass(frozen=True)
class AggregateStationarity:
    state: str
    passing: bool
    selected_ids: tuple[str, ...]
    accepted_states: frozenset[str]
    missing_ids: tuple[str, ...]
    indeterminate_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class CaseInspection:
    case_dir: Path
    application: str | None
    openfoam_version: str | None
    mode: str
    mode_confidence: str
    mode_evidence: tuple[str, ...]
    start_time: float | None
    end_time: float | None
    delta_t: float | None
    adjust_time_step: bool | None
    max_co: float | None
    max_delta_t: float | None
    parallel_ranks: int
    multi_region: bool
    residual_targets: tuple[ResidualTarget, ...]
    function_objects: Mapping[str, FunctionObjectSpec]
    notices: tuple[str, ...]


@dataclass(frozen=True)
class LogCandidate:
    path: Path
    relative_path: str
    workflow: str
    score: int
    reasons: tuple[str, ...]
    modified_ns: int
    size: int


@dataclass(frozen=True)
class LogChunk:
    path: Path
    segment: int
    reset: bool
    lines: tuple[str, ...]
    file_size: int
    modified_ns: int


@dataclass(frozen=True)
class LayerRequest:
    selector: str
    requested_layers: int


@dataclass(frozen=True)
class SnappySettings:
    add_layers: bool | None
    n_solve_iter: int | None
    n_relax_iter: int | None
    max_global_cells: int | None
    stage_count: int
    layer_requests: tuple[LayerRequest, ...]
    notices: tuple[str, ...]


@dataclass(frozen=True)
class LayerCoverageRow:
    patch: str
    faces: int
    average_layers: float
    average_thickness: float
    thickness_percent: float
    requested_layers: int | None
    matched_selector: str | None
    layer_fraction: float | None
    status: str
    summary: str


@dataclass(frozen=True)
class LayerCoverageReport:
    status: str
    summary: str
    rows: tuple[LayerCoverageRow, ...]
    requested_patch_count: int
    reported_patch_count: int
    unmatched_selectors: tuple[str, ...]
    advisory: str


@dataclass(frozen=True)
class MeshQualityMetric:
    code: str
    label: str
    observed: float | str | None
    unit: str | None
    target: float | str | None
    status: str
    explanation: str


@dataclass(frozen=True)
class MeshQualityProblem:
    code: str
    label: str
    count: int
    limit: float | None
    explanation: str


@dataclass(frozen=True)
class MeshQualityReport:
    status: str
    summary: str
    mesh_ok: bool | None
    failed_checks: int | None
    exit_code: int
    command: tuple[str, ...]
    started_at: float | None
    finished_at: float | None
    execution_seconds: float | None
    points: int | None
    faces: int | None
    internal_faces: int | None
    cells: int | None
    regions: int | None
    bounding_box_min: tuple[float, float, float] | None
    bounding_box_max: tuple[float, float, float] | None
    geometric_directions: tuple[int, int, int] | None
    solution_directions: tuple[int, int, int] | None
    metrics: tuple[MeshQualityMetric, ...]
    problems: tuple[MeshQualityProblem, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class MeshQualityStatus:
    state: str
    summary: str
    mesh_source: str | None
    stable_for_seconds: float | None
    next_check_seconds: float | None
    report: MeshQualityReport | None
    advisory: str


@dataclass(frozen=True)
class ResidualSample:
    simulation_time: float
    segment: int
    field: str
    initial: float
    final: float
    iterations: int
    converged: bool | None
    region: str | None
    outer_corrector: int | None


@dataclass(frozen=True)
class TimeStepSample:
    simulation_time: float
    segment: int
    delta_t: float | None
    courant_mean: float | None
    courant_max: float | None
    mesh_courant_mean: float | None
    mesh_courant_max: float | None
    continuity_local: float | None
    continuity_global: float | None
    continuity_cumulative: float | None
    outer_correctors: int


@dataclass(frozen=True)
class FailureRecord:
    label: str
    line: str
    segment: int
    simulation_time: float | None


@dataclass(frozen=True)
class SnappyTelemetry:
    current_segment: int
    stage: str
    stage_index: int | None
    stage_count: int
    current_work: str | None
    active_morph_iteration: int | None
    completed_morph_iterations: int | None
    morph_total: int | None
    smoothing_iteration: int | None
    smoothing_total: int | None
    phase_progress_percent: float | None
    mesh_cells: int | None
    mesh_faces: int | None
    mesh_points: int | None
    max_global_cells_reached: bool
    execution_seconds: float | None
    clock_seconds: float | None
    completed: bool
    warnings: tuple[str, ...]
    warning_count: int
    failure: FailureRecord | None
    notices: tuple[str, ...]
    notice_count: int
    layer_coverage: LayerCoverageReport


@dataclass(frozen=True)
class SolverTelemetry:
    current_time: float | None
    current_segment: int
    current_delta_t: float | None
    execution_seconds: float | None
    clock_seconds: float | None
    completed: bool
    solver_declared_converged: bool
    residuals: tuple[ResidualSample, ...]
    time_steps: tuple[TimeStepSample, ...]
    warnings: tuple[str, ...]
    failure: FailureRecord | None
    notices: tuple[str, ...]


@dataclass(frozen=True)
class AssessmentCheck:
    code: str
    label: str
    passed: bool | None
    observed: float | str | None
    target: float | str | None
    explanation: str


@dataclass(frozen=True)
class NumericalAssessment:
    kind: str
    status: str
    summary: str
    checks: tuple[AssessmentCheck, ...]
    healthy_step_percent: float | None
