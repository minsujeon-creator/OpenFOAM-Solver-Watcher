from __future__ import annotations

from dataclasses import dataclass
import re

from watcher.models import FailureRecord, LogChunk, ResidualSample, SolverTelemetry, TimeStepSample


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
_REGION_PREFIX = r"(?:(?P<region>[A-Za-z_][\w.-]*)\s*:\s*)?"

_TIME = re.compile(rf"^{_REGION_PREFIX}Time\s*=\s*(?P<time>{_NUMBER})\s*$")
_DELTA_T = re.compile(rf"^{_REGION_PREFIX}deltaT\s*=\s*(?P<delta_t>{_NUMBER})\s*$")
_REGION = re.compile(r"^Region\s*:?[ \t]*(?P<region>[A-Za-z_][\w.-]*)\s*$", re.IGNORECASE)
_SOLVER_PERFORMANCE = re.compile(
    rf"^{_REGION_PREFIX}(?P<solver>[^:]+):\s+Solving for\s+(?P<field>[^,]+),\s*"
    rf"Initial residual\s*=\s*(?P<initial>{_NUMBER}),\s*"
    rf"Final residual\s*=\s*(?P<final>{_NUMBER}),\s*"
    rf"No Iterations\s*(?:=\s*)?(?P<iterations>\d+)(?P<suffix>.*)$",
    re.IGNORECASE,
)
_COURANT = re.compile(
    rf"^{_REGION_PREFIX}Courant Number\s+mean:\s*(?P<mean>{_NUMBER})\s+max:\s*(?P<max>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_MESH_COURANT = re.compile(
    rf"^{_REGION_PREFIX}Mesh Courant Number\s+mean:\s*(?P<mean>{_NUMBER})\s+max:\s*(?P<max>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_CONTINUITY = re.compile(
    rf"^{_REGION_PREFIX}time step continuity errors\s*:\s*sum local\s*=\s*(?P<local>{_NUMBER}),\s*"
    rf"global\s*=\s*(?P<global>{_NUMBER}),\s*cumulative\s*=\s*(?P<cumulative>{_NUMBER})\s*$",
    re.IGNORECASE,
)
_CORRECTOR = re.compile(
    rf"^{_REGION_PREFIX}(?:PIMPLE|PISO|SIMPLE)\s*:\s*(?:outer\s+)?iteration\s+(?P<iteration>\d+)\s*$",
    re.IGNORECASE,
)
_EXECUTION_TIME = re.compile(
    rf"^ExecutionTime\s*=\s*(?P<execution>{_NUMBER})\s+s\s+ClockTime\s*=\s*(?P<clock>{_NUMBER})\s+s\s*$",
    re.IGNORECASE,
)
_CONVERGENCE = re.compile(
    r"(?:\b(?:converged|satisfied|met)\b.*\bconvergence criteria\b|"
    r"\bconvergence criteria\b.*\b(?:converged|satisfied|met)\b)",
    re.IGNORECASE,
)
_NEGATED_CONVERGENCE = re.compile(r"\b(?:not|failed|failure|unsatisfied|unmet)\b", re.IGNORECASE)
_END = re.compile(r"^End\s*$")
_WARNING = re.compile(r"^(?:-->)?\s*(?:FOAM\s+)?WARNING\b", re.IGNORECASE)
_RANK_PREFIX = r"(?:\[\d+\]\s*)?"
_FPE_FATAL = re.compile(
    rf"^\s*{_RANK_PREFIX}Floating point exception(?:\s+\(core dumped\))?\s*$",
    re.IGNORECASE,
)
_SEGMENTATION_FAULT = re.compile(
    rf"^\s*{_RANK_PREFIX}Segmentation fault(?:\s+\(core dumped\))?\s*$",
    re.IGNORECASE,
)
_MPI_ABORT = re.compile(
    rf"^\s*{_RANK_PREFIX}MPI_ABORT was invoked on rank \d+\b",
    re.IGNORECASE,
)
_FOAM_FATAL = re.compile(
    rf"^\s*{_RANK_PREFIX}(?:-->\s*)?FOAM FATAL (?:IO )?ERROR\s*:\s*$",
    re.IGNORECASE,
)
_FATAL_ERROR = re.compile(rf"^\s*{_RANK_PREFIX}FATAL ERROR\s*:\s*$", re.IGNORECASE)


@dataclass
class _TimeStepAccumulator:
    simulation_time: float
    segment: int
    delta_t: float | None = None
    courant_mean: float | None = None
    courant_max: float | None = None
    mesh_courant_mean: float | None = None
    mesh_courant_max: float | None = None
    continuity_local: float | None = None
    continuity_global: float | None = None
    continuity_cumulative: float | None = None
    outer_correctors: int = 0

    def sample(self) -> TimeStepSample:
        return TimeStepSample(
            simulation_time=self.simulation_time,
            segment=self.segment,
            delta_t=self.delta_t,
            courant_mean=self.courant_mean,
            courant_max=self.courant_max,
            mesh_courant_mean=self.mesh_courant_mean,
            mesh_courant_max=self.mesh_courant_max,
            continuity_local=self.continuity_local,
            continuity_global=self.continuity_global,
            continuity_cumulative=self.continuity_cumulative,
            outer_correctors=self.outer_correctors,
        )


class OpenFOAMLogParser:
    """Convert complete OpenFOAM log lines into normalized solver telemetry."""

    def __init__(self) -> None:
        self._current_time: float | None = None
        self._current_segment = 0
        self._current_delta_t: float | None = None
        self._execution_seconds: float | None = None
        self._clock_seconds: float | None = None
        self._completed = False
        self._solver_declared_converged = False
        self._residuals: list[ResidualSample] = []
        self._time_steps: list[TimeStepSample] = []
        self._warnings: list[str] = []
        self._notices: list[str] = []
        self._failure: FailureRecord | None = None
        self._region: str | None = None
        self._active_step: _TimeStepAccumulator | None = None

    def feed(self, chunk: LogChunk) -> None:
        """Consume one incrementally-read, complete-line chunk."""
        if chunk.reset or chunk.segment != self._current_segment:
            self._start_segment(chunk.segment)
        for line in chunk.lines:
            self._feed_line(line)

    def snapshot(self) -> SolverTelemetry:
        """Return an immutable view which includes, but does not store, the active step."""
        time_steps = self._time_steps.copy()
        if self._active_step is not None:
            time_steps.append(self._active_step.sample())
        return SolverTelemetry(
            current_time=self._current_time,
            current_segment=self._current_segment,
            current_delta_t=self._current_delta_t,
            execution_seconds=self._execution_seconds,
            clock_seconds=self._clock_seconds,
            completed=self._completed,
            solver_declared_converged=self._solver_declared_converged,
            residuals=tuple(self._residuals),
            time_steps=tuple(time_steps),
            warnings=tuple(self._warnings),
            failure=self._failure,
            notices=tuple(self._notices),
        )

    def _start_segment(self, segment: int) -> None:
        self._finalize_active_step()
        self._current_segment = segment
        self._current_time = None
        self._current_delta_t = None
        self._execution_seconds = None
        self._clock_seconds = None
        self._completed = False
        self._solver_declared_converged = False
        self._failure = None
        self._region = None

    def _feed_line(self, line: str) -> None:
        if not line:
            return
        if self._record_failure(line):
            return
        if _WARNING.search(line):
            self._append_bounded(self._warnings, line, 200)

        match = _REGION.match(line)
        if match:
            self._region = match.group("region")
            return
        match = _TIME.match(line)
        if match:
            self._finalize_active_step()
            self._current_time = float(match.group("time"))
            self._current_delta_t = None
            self._active_step = _TimeStepAccumulator(self._current_time, self._current_segment)
            if match.group("region") is not None:
                self._region = match.group("region")
            return
        match = _DELTA_T.match(line)
        if match:
            self._current_delta_t = float(match.group("delta_t"))
            if self._active_step is not None:
                self._active_step.delta_t = self._current_delta_t
            return
        match = _SOLVER_PERFORMANCE.match(line)
        if match:
            self._record_residual(match)
            return
        match = _MESH_COURANT.match(line)
        if match:
            self._update_step(
                mesh_courant_mean=float(match.group("mean")),
                mesh_courant_max=float(match.group("max")),
            )
            return
        match = _COURANT.match(line)
        if match:
            self._update_step(courant_mean=float(match.group("mean")), courant_max=float(match.group("max")))
            return
        match = _CONTINUITY.match(line)
        if match:
            self._update_step(
                continuity_local=float(match.group("local")),
                continuity_global=float(match.group("global")),
                continuity_cumulative=float(match.group("cumulative")),
            )
            return
        match = _CORRECTOR.match(line)
        if match:
            self._update_step(outer_correctors=int(match.group("iteration")))
            return
        match = _EXECUTION_TIME.match(line)
        if match:
            self._execution_seconds = float(match.group("execution"))
            self._clock_seconds = float(match.group("clock"))
            return
        if _CONVERGENCE.search(line) and not _NEGATED_CONVERGENCE.search(line):
            self._solver_declared_converged = True
            self._append_notice(line)
            return
        if _END.match(line):
            self._completed = True

    def _record_residual(self, match: re.Match[str]) -> None:
        if self._current_time is None:
            return
        suffix = match.group("suffix").lower()
        converged: bool | None
        if re.search(r"\b(?:not\s+converged|failed|failure)\b", suffix):
            converged = False
        elif re.search(r"\bconverged\b", suffix):
            converged = True
        else:
            converged = None
        region = match.group("region") or self._region
        outer_corrector = self._active_step.outer_correctors if self._active_step is not None else None
        sample = ResidualSample(
            simulation_time=self._current_time,
            segment=self._current_segment,
            field=match.group("field").strip(),
            initial=float(match.group("initial")),
            final=float(match.group("final")),
            iterations=int(match.group("iterations")),
            converged=converged,
            region=region,
            outer_corrector=outer_corrector,
        )
        self._append_bounded(self._residuals, sample, 100_000)

    def _update_step(self, **values: float | int) -> None:
        if self._active_step is None:
            return
        for name, value in values.items():
            if name == "outer_correctors":
                value = max(self._active_step.outer_correctors, int(value))
            setattr(self._active_step, name, value)

    def _record_failure(self, line: str) -> bool:
        label: str | None = None
        if _FPE_FATAL.match(line):
            label = "Floating point exception"
        elif _SEGMENTATION_FAULT.match(line):
            label = "Segmentation fault"
        elif _MPI_ABORT.match(line):
            label = "MPI abort"
        elif _FOAM_FATAL.match(line) or _FATAL_ERROR.match(line):
            label = "Fatal error"
        if label is None:
            return False
        self._failure = FailureRecord(
            label=label,
            line=line,
            segment=self._current_segment,
            simulation_time=self._current_time,
        )
        return True

    def _finalize_active_step(self) -> None:
        if self._active_step is not None:
            self._time_steps.append(self._active_step.sample())
            self._active_step = None

    def _append_notice(self, notice: str) -> None:
        if notice not in self._notices:
            self._notices.append(notice)

    @staticmethod
    def _append_bounded(items: list[object], value: object, limit: int) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]
