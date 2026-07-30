from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import platform
import socket
import time
from typing import Callable, Mapping, Sequence, TypeVar

from watcher.case_config import inspect_case
from watcher.convergence import evaluate_numerics
from watcher.log_parser import OpenFOAMLogParser
from watcher.log_reader import IncrementalLogReader, discover_logs
from watcher.models import (
    LogCandidate,
    ResidualSample,
    SeriesData,
    SeriesOverride,
    SolverTelemetry,
    StationarityResult,
    StationaritySettings,
    TimeStepSample,
    WatcherConfig,
    to_json_safe,
)
from watcher.persistence import load_config, save_config, validate_config_payload
from watcher.postprocessing import discover_series, parse_numeric_table
from watcher.stationarity import analyze_series, aggregate_stationarity


_REFRESH_SECONDS = 2.0
_RECENT_LOG_SECONDS = 90.0
_PREVIEW_LIMIT = 300
_SERIES_LIMIT = 2_000
_SNAPSHOT_RESIDUAL_LIMIT = 1_000
_SNAPSHOT_TIME_STEP_LIMIT = 1_000
_SNAPSHOT_MESSAGE_LIMIT = 200
_SNAPSHOT_NOTICE_LIMIT = 300
_PROC_ROOT = Path("/proc")
_Record = TypeVar("_Record")


class WatcherCollector:
    """Assemble one advisory, JSON-safe view of an OpenFOAM case."""

    def __init__(self, case_dir: Path, explicit_log: Path | None = None) -> None:
        self.inspection = inspect_case(case_dir)
        self.case_dir = self.inspection.case_dir
        self.explicit_log = explicit_log
        self._selected_log: Path | None = None
        self._reader: IncrementalLogReader | None = None
        self._parser = OpenFOAMLogParser()
        self._post_signature: tuple[int, int, int] | None = None
        self._series: dict[str, SeriesData] = {}
        self._post_notices: tuple[dict[str, object], ...] = ()
        self._config = load_config(self.case_dir).config

    def snapshot(self) -> dict[str, object]:
        now = time.time()
        loaded = load_config(self.case_dir)
        self._config = loaded.config
        candidates, selected = self._refresh_log(self._config)
        telemetry = self._parser.snapshot()
        self._refresh_postprocessing()

        results = self._stationarity_results(self._config, now)
        aggregate = aggregate_stationarity(
            self._config.selected_series,
            results,
            self._config.accepted_states,
        )
        numerics = evaluate_numerics(self.inspection, telemetry)
        process = self._process_model(selected, telemetry, now)

        notices = list(self._post_notices)
        notices.extend(
            _notice("case", message, "warning")
            for message in self.inspection.notices
        )
        visible_solver_notices = telemetry.notices[-_SNAPSHOT_MESSAGE_LIMIT:]
        notices.extend(
            _notice("solver log", message, "warning")
            for message in visible_solver_notices
        )
        if loaded.error is not None:
            notices.append(_notice(".foam-watcher.json", loaded.error, "error"))
        if selected is None and process["state"] == "not_started":
            notices.append(_notice("solver log", "No solver log was found.", "info"))
        for series_id, series in self._series.items():
            if _series_is_stale(
                series,
                _settings(self._config.overrides.get(series_id)),
                now,
            ):
                notices.append(
                    _notice(series.source_relative, "Post-processing series is stale.", "warning")
                )

        bounded_notices, notice_count = _bounded_notices(
            notices,
            _SNAPSHOT_NOTICE_LIMIT,
            omitted_count=len(telemetry.notices) - len(visible_solver_notices),
        )
        solver_model = self._solver_model(telemetry)
        solver_model["snapshotNoticeCount"] = notice_count
        solver_model["snapshotNoticesTruncated"] = (
            notice_count > len(bounded_notices)
        )
        model = {
            "generatedAt": _timestamp(now),
            "refreshSeconds": _REFRESH_SECONDS,
            "case": to_json_safe(self.inspection),
            "process": process,
            "host": _host_model(),
            "solver": solver_model,
            "numerics": to_json_safe(numerics),
            "physical": {
                "aggregate": to_json_safe(aggregate),
                "results": _stationarity_models(results),
            },
            "seriesCatalog": self._series_catalog(self._config, now),
            "logSelection": {
                "selected": selected.relative_path if selected is not None else None,
                "explicit": str(self.explicit_log) if self.explicit_log is not None else None,
                "saved": self._config.selected_log,
                "candidates": to_json_safe(candidates),
            },
            "notices": bounded_notices,
            "configuration": _configuration_model(self._config, loaded.error),
        }
        return to_json_safe(model)  # type: ignore[return-value]

    def series(self, series_id: str, limit: int = _SERIES_LIMIT) -> dict[str, object]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("series limit must be a positive integer")
        self._config = load_config(self.case_dir).config
        self._refresh_postprocessing()
        series = self._series.get(series_id)
        if series is None:
            raise KeyError(f"Unknown series ID: {series_id}")

        limit = min(limit, _SERIES_LIMIT)
        times, values = _min_max_envelope(series.times, series.values, limit)
        override = self._config.overrides.get(series_id)
        result = {
            "id": series.series_id,
            "label": override.label if override and override.label is not None else series.label,
            "units": override.units if override and override.units is not None else series.units,
            "source": series.source_relative,
            "field": series.field,
            "component": series.component,
            "selected": series_id in self._config.selected_series,
            "totalSamples": min(len(series.times), len(series.values)),
            "returnedSamples": len(times),
            "downsampled": len(times) < min(len(series.times), len(series.values)),
            "times": times,
            "values": values,
        }
        return to_json_safe(result)  # type: ignore[return-value]

    def update_config(self, payload: object) -> dict[str, object]:
        self._refresh_postprocessing()
        config = validate_config_payload(payload, frozenset(self._series))
        save_config(self.case_dir, config)
        self._config = config
        return _configuration_model(config, None)

    def _refresh_log(
        self,
        config: WatcherConfig,
    ) -> tuple[tuple[LogCandidate, ...], LogCandidate | None]:
        candidates = discover_logs(
            self.inspection,
            explicit=self.explicit_log,
            saved_relative=config.selected_log,
        )
        selected = _select_candidate(candidates)
        selected_path = selected.path if selected is not None else None
        if selected_path != self._selected_log:
            self._selected_log = selected_path
            self._parser = OpenFOAMLogParser()
            self._reader = (
                IncrementalLogReader(self.case_dir, selected_path)
                if selected_path is not None
                else None
            )
        if self._reader is not None:
            self._parser.feed(self._reader.read())
        return candidates, selected

    def _refresh_postprocessing(self) -> None:
        signature, paths, scan_notices = _postprocessing_signature(self.case_dir)
        if signature == self._post_signature:
            return

        notices = list(scan_notices)
        try:
            discovered = discover_series(self.inspection)
        except Exception as error:
            notices.append(
                _notice("postProcessing", f"Could not refresh post-processing: {error}", "error")
            )
        else:
            self._series = dict(discovered)
            notices.extend(_table_notices(self.case_dir, paths))
            for series in discovered.values():
                notices.extend(
                    _notice(series.source_relative, message, "warning")
                    for message in series.notices
                )
            self._post_signature = signature
        self._post_notices = tuple(_deduplicate_notices(notices))

    def _stationarity_results(
        self,
        config: WatcherConfig,
        now: float,
    ) -> dict[str, StationarityResult]:
        return {
            series_id: analyze_series(
                replace(series, stale=False),
                _settings(config.overrides.get(series_id)),
                now,
            )
            for series_id, series in self._series.items()
        }

    def _series_catalog(
        self,
        config: WatcherConfig,
        now: float,
    ) -> list[dict[str, object]]:
        catalog: list[dict[str, object]] = []
        selected = frozenset(config.selected_series)
        for series_id, series in self._series.items():
            override = config.overrides.get(series_id)
            times, values = _min_max_envelope(
                series.times,
                series.values,
                _PREVIEW_LIMIT,
            )
            catalog.append(
                {
                    "id": series_id,
                    "label": (
                        override.label
                        if override is not None and override.label is not None
                        else series.label
                    ),
                    "units": (
                        override.units
                        if override is not None and override.units is not None
                        else series.units
                    ),
                    "source": series.source_relative,
                    "functionName": series.function_name,
                    "functionType": series.function_type,
                    "region": series.region,
                    "field": series.field,
                    "operation": series.operation,
                    "component": series.component,
                    "modifiedAt": _timestamp(series.modified_ns / 1_000_000_000),
                    "stale": _series_is_stale(
                        series,
                        _settings(override),
                        now,
                    ),
                    "selected": series_id in selected,
                    "sampleCount": min(len(series.times), len(series.values)),
                    "currentValue": series.values[-1] if series.values else None,
                    "candidate": to_json_safe(series.candidate),
                    "preview": {"times": times, "values": values},
                }
            )
        return catalog

    def _process_model(
        self,
        selected: LogCandidate | None,
        telemetry: SolverTelemetry,
        now: float,
    ) -> dict[str, object]:
        process = _matching_process(self.case_dir, self.inspection.application)
        age = (
            max(0.0, now - selected.modified_ns / 1_000_000_000)
            if selected is not None
            else None
        )
        end_reached = (
            telemetry.current_time is not None
            and self.inspection.end_time is not None
            and telemetry.current_time >= self.inspection.end_time
        )
        if process is not None:
            state = "running"
            source = "proc"
            pid, command = process
        else:
            pid = None
            command = None
            source = "log"
            if telemetry.failure is not None:
                state = "failed"
            elif telemetry.completed or end_reached:
                state = "completed"
            elif selected is not None and age is not None and age <= _RECENT_LOG_SECONDS:
                state = "running"
            elif selected is not None and _has_parsed_log(telemetry):
                state = "stopped"
            elif selected is None:
                state = "not_started"
            else:
                state = "not_started"
        return {
            "state": state,
            "pid": pid,
            "command": command,
            "source": source if state != "not_started" else "none",
            "lastActivity": (
                _timestamp(selected.modified_ns / 1_000_000_000)
                if selected is not None
                else None
            ),
            "logAgeSeconds": age,
        }

    def _solver_model(self, telemetry: SolverTelemetry) -> dict[str, object]:
        residuals = _bounded_records(
            telemetry.residuals,
            _SNAPSHOT_RESIDUAL_LIMIT,
            _residual_signal,
        )
        time_steps = _bounded_records(
            telemetry.time_steps,
            _SNAPSHOT_TIME_STEP_LIMIT,
            _time_step_signal,
        )
        result = {
            "currentTime": telemetry.current_time,
            "currentSegment": telemetry.current_segment,
            "currentDeltaT": telemetry.current_delta_t,
            "executionSeconds": telemetry.execution_seconds,
            "clockSeconds": telemetry.clock_seconds,
            "completed": telemetry.completed,
            "solverDeclaredConverged": telemetry.solver_declared_converged,
            "residualCount": len(telemetry.residuals),
            "timeStepCount": len(telemetry.time_steps),
            "residualsDownsampled": len(residuals) < len(telemetry.residuals),
            "timeStepsDownsampled": len(time_steps) < len(telemetry.time_steps),
            "residuals": to_json_safe(residuals),
            "timeSteps": to_json_safe(time_steps),
            "warnings": to_json_safe(telemetry.warnings[-_SNAPSHOT_MESSAGE_LIMIT:]),
            "failure": to_json_safe(telemetry.failure),
            "notices": to_json_safe(telemetry.notices[-_SNAPSHOT_MESSAGE_LIMIT:]),
            "noticeCount": len(telemetry.notices),
            "noticesTruncated": len(telemetry.notices) > _SNAPSHOT_MESSAGE_LIMIT,
        }
        result["progress"] = _progress(self.inspection.start_time, self.inspection.end_time, telemetry)
        return result


def _select_candidate(candidates: tuple[LogCandidate, ...]) -> LogCandidate | None:
    explicit = next(
        (candidate for candidate in candidates if "explicit selection" in candidate.reasons),
        None,
    )
    if explicit is not None:
        return explicit
    saved = next(
        (candidate for candidate in candidates if "saved selection" in candidate.reasons),
        None,
    )
    return saved if saved is not None else (candidates[0] if candidates else None)


def _postprocessing_signature(
    case_dir: Path,
) -> tuple[tuple[int, int, int], tuple[Path, ...], tuple[dict[str, object], ...]]:
    root = case_dir / "postProcessing"
    if not root.is_dir():
        return (0, 0, 0), (), ()

    maximum_mtime_ns = 0
    total_size = 0
    paths: list[Path] = []
    notices: list[dict[str, object]] = []
    try:
        candidates = root.rglob("*")
        for path in candidates:
            try:
                resolved = path.resolve()
                resolved.relative_to(case_dir)
                if not resolved.is_file():
                    continue
                stat = resolved.stat()
            except (OSError, ValueError) as error:
                notices.append(_notice(_relative_source(path, case_dir), str(error), "warning"))
                continue
            paths.append(resolved)
            maximum_mtime_ns = max(maximum_mtime_ns, stat.st_mtime_ns)
            total_size += stat.st_size
    except OSError as error:
        notices.append(_notice("postProcessing", str(error), "error"))
    return (
        (maximum_mtime_ns, total_size, len(paths)),
        tuple(sorted(paths, key=lambda item: item.as_posix())),
        tuple(notices),
    )


def _table_notices(case_dir: Path, paths: tuple[Path, ...]) -> list[dict[str, object]]:
    notices: list[dict[str, object]] = []
    for path in paths:
        source = _relative_source(path, case_dir)
        try:
            table = parse_numeric_table(path)
        except (OSError, ValueError, UnicodeError) as error:
            notices.append(_notice(source, f"Could not parse table: {error}", "warning"))
            continue
        notices.extend(_notice(source, message, "warning") for message in table.notices)
        if not table.times or not table.values:
            notices.append(_notice(source, "No complete numeric table rows were found.", "warning"))
    return notices


def _settings(override: SeriesOverride | None) -> StationaritySettings:
    defaults = StationaritySettings()
    if override is None:
        return defaults
    values = {
        field.name: getattr(defaults, field.name)
        for field in fields(StationaritySettings)
    }
    for name in values:
        configured = getattr(override, name)
        if configured is not None:
            values[name] = configured
    return StationaritySettings(**values)


def _series_is_stale(
    series: SeriesData,
    settings: StationaritySettings,
    now: float,
) -> bool:
    return (
        now - series.modified_ns / 1_000_000_000
        > settings.stale_after_seconds
    )


def _configuration_model(config: WatcherConfig, error: str | None) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for series_id, override in config.overrides.items():
        serialized = to_json_safe(override)
        assert isinstance(serialized, dict)
        overrides[series_id] = {
            key: value for key, value in serialized.items() if value is not None
        }
    return {
        "version": config.version,
        "selectedLog": config.selected_log,
        "selectedSeries": list(config.selected_series),
        "overrides": overrides,
        "acceptedStates": sorted(config.accepted_states),
        "error": error,
    }


def _stationarity_models(
    results: Mapping[str, StationarityResult],
) -> dict[str, object]:
    models: dict[str, object] = {}
    for series_id, result in results.items():
        model = to_json_safe(result)
        assert isinstance(model, dict)
        thresholds = model.get("thresholds")
        if isinstance(thresholds, dict):
            model["thresholds"] = {
                _camel_case(key): value for key, value in thresholds.items()
            }
        models[series_id] = model
    return models


def _progress(
    start_time: float | None,
    end_time: float | None,
    telemetry: SolverTelemetry,
) -> dict[str, object]:
    current = telemetry.current_time
    wall_seconds = telemetry.clock_seconds or telemetry.execution_seconds
    fraction: float | None = None
    rate: float | None = None
    eta: float | None = None
    if (
        current is not None
        and start_time is not None
        and end_time is not None
        and math.isfinite(current)
        and math.isfinite(start_time)
        and math.isfinite(end_time)
        and end_time > start_time
    ):
        fraction = min(1.0, max(0.0, (current - start_time) / (end_time - start_time)))
        if wall_seconds is not None and math.isfinite(wall_seconds) and wall_seconds > 0.0:
            simulated = current - start_time
            if simulated >= 0.0:
                rate = simulated / wall_seconds
                if current < end_time and rate > 0.0:
                    eta = (end_time - current) / rate
    return {
        "startTime": start_time,
        "endTime": end_time,
        "fraction": fraction,
        "percent": fraction * 100.0 if fraction is not None else None,
        "simulatedSecondsPerWallSecond": rate,
        "wallSecondsPerSimulatedSecond": 1.0 / rate if rate else None,
        "realTimeFactor": rate,
        "etaSeconds": eta,
    }


def _min_max_envelope(
    raw_times: tuple[float, ...],
    raw_values: tuple[float, ...],
    limit: int,
) -> tuple[list[float], list[float]]:
    points = list(zip(raw_times, raw_values))
    indices = _envelope_indices([point[1] for point in points], limit)
    return [points[index][0] for index in indices], [points[index][1] for index in indices]


def _bounded_records(
    records: tuple[_Record, ...],
    limit: int,
    signal: Callable[[_Record], float],
) -> tuple[_Record, ...]:
    indices = _envelope_indices([signal(record) for record in records], limit)
    return tuple(records[index] for index in indices)


def _envelope_indices(values: Sequence[float], limit: int) -> list[int]:
    count = len(values)
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [count - 1]
    if limit == 2:
        return [0, count - 1]

    capacity = limit - 2
    selected = {0, count - 1}
    interior_count = count - 2
    pair_buckets = capacity // 2
    for bucket in range(pair_buckets):
        start = 1 + bucket * interior_count // pair_buckets
        stop = 1 + (bucket + 1) * interior_count // pair_buckets
        indices = range(start, stop)
        selected.add(min(indices, key=lambda index: values[index]))
        selected.add(max(indices, key=lambda index: values[index]))

    remaining = [
        index for index in range(1, count - 1) if index not in selected
    ]
    remaining.sort(
        key=lambda index: (
            -_endpoint_deviation(values, index),
            index,
        )
    )
    selected.update(remaining[: limit - len(selected)])
    return sorted(selected)


def _endpoint_deviation(values: Sequence[float], index: int) -> float:
    start = values[0]
    end = values[-1]
    value = values[index]
    if all(math.isfinite(item) for item in (start, end, value)):
        baseline = start + (end - start) * index / (len(values) - 1)
        return abs(value - baseline)
    return abs(value) if math.isfinite(value) else 0.0


def _residual_signal(sample: ResidualSample) -> float:
    values = (sample.initial, sample.final)
    return max((abs(value) for value in values if math.isfinite(value)), default=0.0)


def _time_step_signal(sample: TimeStepSample) -> float:
    values = (
        sample.delta_t,
        sample.courant_mean,
        sample.courant_max,
        sample.mesh_courant_mean,
        sample.mesh_courant_max,
        sample.continuity_local,
        sample.continuity_global,
        sample.continuity_cumulative,
        float(sample.outer_correctors),
    )
    return max(
        (abs(value) for value in values if value is not None and math.isfinite(value)),
        default=0.0,
    )


def _matching_process(
    case_dir: Path,
    application: str | None,
) -> tuple[int, str] | None:
    if not _PROC_ROOT.is_dir():
        return None
    try:
        entries = tuple(_PROC_ROOT.iterdir())
    except OSError:
        return None
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
            cwd.relative_to(case_dir)
            raw_command = (entry / "cmdline").read_bytes()
        except (OSError, ValueError):
            continue
        command = raw_command.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        lowered = command.lower()
        if not command or "foam-watch" in lowered or "watcher.snapshot" in lowered:
            continue
        if application is not None and application.lower() not in lowered:
            continue
        return int(entry.name), command
    return None


def _host_model() -> dict[str, object]:
    try:
        load_average: object = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average = None
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpuCount": os.cpu_count(),
        "loadAverage": load_average,
    }


def _has_parsed_log(telemetry: SolverTelemetry) -> bool:
    return (
        telemetry.current_time is not None
        or bool(telemetry.residuals)
        or bool(telemetry.time_steps)
        or telemetry.execution_seconds is not None
        or telemetry.clock_seconds is not None
        or telemetry.failure is not None
        or telemetry.completed
    )


def _timestamp(seconds: float) -> str | None:
    if not math.isfinite(seconds):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return None


def _notice(source: str, message: str, severity: str) -> dict[str, object]:
    return {"severity": severity, "source": source, "message": message}


def _deduplicate_notices(
    notices: list[dict[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for notice in notices:
        identity = (
            notice.get("severity"),
            notice.get("source"),
            notice.get("message"),
        )
        if identity not in seen:
            seen.add(identity)
            result.append(notice)
    return result


def _bounded_notices(
    notices: list[dict[str, object]],
    limit: int,
    omitted_count: int = 0,
) -> tuple[list[dict[str, object]], int]:
    deduplicated = _deduplicate_notices(notices)
    total = len(deduplicated) + max(0, omitted_count)
    if len(deduplicated) <= limit:
        return deduplicated, total

    def priority(index: int) -> tuple[int, int]:
        notice = deduplicated[index]
        if notice.get("severity") == "error":
            rank = 0
        elif notice.get("source") != "solver log":
            rank = 1
        else:
            rank = 2
        return rank, -index

    selected = sorted(
        sorted(range(len(deduplicated)), key=priority)[:limit]
    )
    return [deduplicated[index] for index in selected], total


def _relative_source(path: Path, case_dir: Path) -> str:
    try:
        return path.resolve().relative_to(case_dir).as_posix()
    except (OSError, ValueError):
        return str(path)


def _camel_case(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)
