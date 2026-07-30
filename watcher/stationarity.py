from __future__ import annotations

from math import isfinite, sqrt
from statistics import median
from sys import float_info
from types import MappingProxyType
from typing import AbstractSet, Mapping, Sequence

from watcher.models import (
    AggregateStationarity,
    SeriesData,
    StationarityEvidence,
    StationarityResult,
    StationaritySettings,
)


_MINIMUM_WINDOW_SAMPLES = 30
_MAXIMUM_AUTOCORRELATION_LAG = 200
_MAXIMUM_ANALYSIS_SAMPLES = 2048


def analyze_series(
    series: SeriesData,
    settings: StationaritySettings,
    now: float,
) -> StationarityResult:
    """Classify one scalar history using only its physical time-series evidence."""
    times, values, removed = _finite_samples(series)
    segment_start = _latest_segment_start(times)
    discontinuous = segment_start > 0
    segment_times = times[segment_start:]
    segment_values = values[segment_start:]
    analysis_times, analysis_values = _analysis_horizon(segment_times, segment_values)
    window_samples = len(analysis_values) // 2
    notices = list(series.notices)
    if removed:
        notices.append(f"Removed {removed} non-finite or duplicate samples.")

    if window_samples < _MINIMUM_WINDOW_SAMPLES:
        evidence = _empty_evidence(len(times), len(segment_values), window_samples, discontinuous)
    else:
        first_times = analysis_times[-2 * window_samples : -window_samples]
        first_values = analysis_values[-2 * window_samples : -window_samples]
        latest_times = analysis_times[-window_samples:]
        latest_values = analysis_values[-window_samples:]
        evidence = _window_evidence(
            len(times),
            len(segment_values),
            len(segment_values) // 2,
            analysis_times,
            analysis_values,
            first_times,
            first_values,
            latest_times,
            latest_values,
            segment_times,
            segment_values,
            discontinuous,
            settings,
        )

    stale = series.stale or now - series.modified_ns / 1_000_000_000 > settings.stale_after_seconds
    state, summary = _decision(evidence, stale, settings)
    if stale:
        notices.append("The selected series is stale.")
    elif window_samples < _MINIMUM_WINDOW_SAMPLES:
        notices.append("The latest continuous segment does not contain two 30-sample windows.")

    return StationarityResult(
        series_id=series.series_id,
        state=state,
        summary=summary,
        evidence=evidence,
        thresholds=_thresholds(settings),
        notices=tuple(notices),
    )


def aggregate_stationarity(
    selected_ids: Sequence[str],
    results: Mapping[str, StationarityResult],
    accepted_states: AbstractSet[str],
) -> AggregateStationarity:
    """Require every selected series to have fresh evidence in an accepted state."""
    selected = tuple(dict.fromkeys(selected_ids))
    accepted = frozenset(accepted_states)
    missing = tuple(series_id for series_id in selected if series_id not in results)
    indeterminate = tuple(
        series_id
        for series_id in selected
        if series_id in results and results[series_id].state == "indeterminate"
    )
    rejected = tuple(
        series_id
        for series_id in selected
        if series_id in results
        and results[series_id].state != "indeterminate"
        and results[series_id].state not in accepted
    )

    if not selected:
        state = "indeterminate"
        summary = "No physical series are selected."
    elif missing:
        state = "indeterminate"
        summary = "At least one selected physical series is missing."
    elif indeterminate:
        state = "indeterminate"
        summary = "At least one selected physical series lacks sufficient current evidence."
    elif rejected:
        state = "failing"
        summary = "At least one selected physical series is outside the accepted states."
    else:
        state = "passing"
        summary = "Every selected physical series has an accepted state."

    return AggregateStationarity(
        state=state,
        passing=state == "passing",
        selected_ids=selected,
        accepted_states=accepted,
        missing_ids=missing,
        indeterminate_ids=indeterminate,
        rejected_ids=rejected,
        summary=summary,
    )


def _finite_samples(series: SeriesData) -> tuple[list[float], list[float], int]:
    latest_by_time: dict[float, float] = {}
    for sample_time, value in zip(series.times, series.values):
        if isfinite(sample_time) and isfinite(value):
            latest_by_time[sample_time] = value
    ordered = sorted(latest_by_time.items())
    removed = max(len(series.times), len(series.values)) - len(ordered)
    return [item[0] for item in ordered], [item[1] for item in ordered], removed


def _analysis_samples(times: list[float], values: list[float]) -> tuple[list[float], list[float]]:
    """Evenly sample one continuous segment while retaining its full time span."""
    if len(values) <= _MAXIMUM_ANALYSIS_SAMPLES:
        return times, values
    denominator = _MAXIMUM_ANALYSIS_SAMPLES - 1
    indices = [index * (len(values) - 1) // denominator for index in range(_MAXIMUM_ANALYSIS_SAMPLES)]
    return [times[index] for index in indices], [values[index] for index in indices]


def _analysis_horizon(times: list[float], values: list[float]) -> tuple[list[float], list[float]]:
    """Retain recent full-resolution samples for short-period cycle detection."""
    if len(values) <= _MAXIMUM_ANALYSIS_SAMPLES:
        return times, values
    return times[-_MAXIMUM_ANALYSIS_SAMPLES:], values[-_MAXIMUM_ANALYSIS_SAMPLES:]


def _latest_segment_start(times: list[float]) -> int:
    spacings = [right - left for left, right in zip(times, times[1:]) if right > left]
    if not spacings:
        return 0
    typical = median(spacings)
    start = 0
    for index, spacing in enumerate(
        (right - left for left, right in zip(times, times[1:])),
        start=1,
    ):
        if spacing > 5.0 * typical:
            start = index
    return start


def _window_evidence(
    raw_samples: int,
    segment_samples: int,
    raw_window_samples: int,
    segment_times: list[float],
    segment_values: list[float],
    first_times: list[float],
    first_values: list[float],
    latest_times: list[float],
    latest_values: list[float],
    periodic_times: list[float],
    periodic_values: list[float],
    discontinuous: bool,
    settings: StationaritySettings,
) -> StationarityEvidence:
    first_mean, first_std = _mean_std(first_values)
    latest_mean, latest_std = _mean_std(latest_values)
    scale = max(abs(latest_mean), latest_std, settings.absolute_floor)
    duration = latest_times[-1] - latest_times[0]
    slope = _slope(latest_times, latest_values)
    normalized_mean_shift = abs(latest_mean - first_mean) / scale
    normalized_slope = abs(slope) * duration / scale
    effective_times, effective_values = _analysis_samples(periodic_times, periodic_values)
    effective_window_samples = len(effective_values) // 2
    effective_first_times = effective_times[:effective_window_samples]
    effective_first_values = effective_values[:effective_window_samples]
    effective_latest_times = effective_times[-effective_window_samples:]
    effective_latest_values = effective_values[-effective_window_samples:]
    sample_weight = raw_window_samples / effective_window_samples
    first_tau = min(
        _MAXIMUM_AUTOCORRELATION_LAG,
        _autocorrelation_time(_detrend(effective_first_times, effective_first_values)) * sample_weight,
    )
    latest_tau = min(
        _MAXIMUM_AUTOCORRELATION_LAG,
        _autocorrelation_time(_detrend(effective_latest_times, effective_latest_values)) * sample_weight,
    )
    effective_samples = raw_window_samples / latest_tau
    standard_error = latest_std * sqrt(latest_tau / raw_window_samples)
    first_standard_error = first_std * sqrt(first_tau / raw_window_samples)
    shift_error = sqrt(standard_error * standard_error + first_standard_error * first_standard_error)
    mean_shift_standard_errors = _safe_ratio(abs(latest_mean - first_mean), shift_error)
    coefficient_of_variation = latest_std / scale
    periodic = _periodic_evidence(periodic_times, periodic_values, settings)

    return StationarityEvidence(
        raw_samples=raw_samples,
        segment_samples=segment_samples,
        window_samples=len(latest_values),
        discontinuous=discontinuous,
        earlier_mean=first_mean,
        latest_mean=latest_mean,
        latest_standard_deviation=latest_std,
        coefficient_of_variation=coefficient_of_variation,
        normalized_mean_shift=normalized_mean_shift,
        normalized_slope=normalized_slope,
        autocorrelation_time=latest_tau,
        effective_samples=effective_samples,
        standard_error=standard_error,
        mean_shift_standard_errors=mean_shift_standard_errors,
        period=periodic[0],
        complete_cycles=periodic[1],
        period_variation_fraction=periodic[2],
        amplitude_variation_fraction=periodic[3],
        cycle_mean_variation_fraction=periodic[4],
    )


def _empty_evidence(
    raw_samples: int,
    segment_samples: int,
    window_samples: int,
    discontinuous: bool,
) -> StationarityEvidence:
    return StationarityEvidence(
        raw_samples=raw_samples,
        segment_samples=segment_samples,
        window_samples=window_samples,
        discontinuous=discontinuous,
        earlier_mean=None,
        latest_mean=None,
        latest_standard_deviation=None,
        coefficient_of_variation=None,
        normalized_mean_shift=None,
        normalized_slope=None,
        autocorrelation_time=1.0,
        effective_samples=float(window_samples),
        standard_error=None,
        mean_shift_standard_errors=None,
        period=None,
        complete_cycles=0,
        period_variation_fraction=None,
        amplitude_variation_fraction=None,
        cycle_mean_variation_fraction=None,
    )


def _decision(
    evidence: StationarityEvidence,
    stale: bool,
    settings: StationaritySettings,
) -> tuple[str, str]:
    if (
        stale
        or evidence.window_samples < _MINIMUM_WINDOW_SAMPLES
        or evidence.effective_samples < settings.minimum_effective_samples
    ):
        return "indeterminate", "The current series lacks sufficient fresh, continuous evidence."

    periodic = (
        evidence.period is not None
        and evidence.complete_cycles >= settings.minimum_cycles
        and _at_most(evidence.period_variation_fraction, settings.max_period_variation_fraction)
        and _at_most(evidence.amplitude_variation_fraction, settings.max_amplitude_variation_fraction)
        and _at_most(evidence.cycle_mean_variation_fraction, settings.max_mean_shift_fraction)
    )
    if periodic:
        return "periodic", "A stable repeating cycle is present in the latest continuous segment."

    if _material_cycle_evolution(
        (
            evidence.period,
            evidence.complete_cycles,
            evidence.period_variation_fraction,
            evidence.amplitude_variation_fraction,
            evidence.cycle_mean_variation_fraction,
        ),
        settings,
    ):
        return "evolving", "The latest repeating cycle has material period, amplitude, or mean drift."

    stable = (
        _at_most(evidence.normalized_mean_shift, settings.max_mean_shift_fraction)
        and _at_most(evidence.normalized_slope, settings.max_normalized_slope)
        and _at_most(evidence.mean_shift_standard_errors, settings.max_mean_shift_standard_errors)
    )
    if stable and _at_most(evidence.coefficient_of_variation, 0.01):
        return "plateau", "The latest windows have negligible variation, shift, and trend."
    if (
        stable
        and evidence.coefficient_of_variation is not None
        and isfinite(evidence.coefficient_of_variation)
    ):
        return "statistically_stationary", "The latest windows have stable statistics without material trend."
    return "evolving", "The latest physical history still has material shift, trend, or cycle drift."


def _periodic_evidence(
    times: list[float],
    values: list[float],
    settings: StationaritySettings,
) -> tuple[float | None, int, float | None, float | None, float | None]:
    overview = _analysis_samples(times, values)
    recent = _analysis_horizon(times, values)
    overview_candidate = _periodic_evidence_for_view(*overview, settings)
    if recent == overview:
        return overview_candidate
    recent_candidate = _periodic_evidence_for_view(*recent, settings)
    if (
        recent_candidate[0] is not None
        and (
            not _stable_periodic_evidence(overview_candidate, settings)
            or _similar_periods(recent_candidate[0], overview_candidate[0])
            or (
                _material_cycle_evolution(recent_candidate, settings)
                and _related_periods(recent_candidate[0], overview_candidate[0])
            )
        )
    ):
        return recent_candidate
    return overview_candidate


def _periodic_evidence_for_view(
    times: list[float],
    values: list[float],
    settings: StationaritySettings,
) -> tuple[float | None, int, float | None, float | None, float | None]:
    count = len(values)
    if count < 2 * settings.minimum_cycles:
        return None, 0, None, None, None
    slope = _slope(times, values)
    intercept = sum(value - slope * sample_time for sample_time, value in zip(times, values)) / count
    detrended = [value - (intercept + slope * sample_time) for sample_time, value in zip(times, values)]
    variance = sum(value * value for value in detrended) / count
    if variance <= 0.0 or not isfinite(variance):
        return None, 0, None, None, None

    correlations = [0.0, _autocorrelation(detrended, 1, variance)]
    correlations.extend(
        _autocorrelation(detrended, lag, variance)
        for lag in range(2, count // 2 + 1)
    )
    peaks = [
        lag
        for lag in range(2, len(correlations) - 1)
        if correlations[lag] > 0.5
        and correlations[lag] >= correlations[lag - 1]
        and correlations[lag] >= correlations[lag + 1]
    ]
    if not peaks:
        return None, 0, None, None, None
    candidates = [(lag, _cycle_evidence(times, values, lag, settings)) for lag in peaks]
    stable = [candidate for _, candidate in candidates if _stable_periodic_evidence(candidate, settings)]
    if stable:
        return stable[0]
    period_samples, candidate = candidates[0]
    if _period_only_evolution(candidate, settings) and not _directional_period_drift(
        times,
        values,
        period_samples,
        settings,
    ):
        return None, 0, None, None, None
    return candidate


def _cycle_evidence(
    times: list[float],
    values: list[float],
    period_samples: int,
    settings: StationaritySettings,
) -> tuple[float | None, int, float | None, float | None, float | None]:
    count = len(values)
    complete_cycles = count // period_samples
    if complete_cycles < settings.minimum_cycles:
        return None, complete_cycles, None, None, None

    start = count - complete_cycles * period_samples
    cycles = [
        values[index : index + period_samples]
        for index in range(start, count, period_samples)
    ]
    cycle_times = [
        times[index : index + period_samples]
        for index in range(start, count, period_samples)
    ]
    amplitudes = [max(cycle) - min(cycle) for cycle in cycles]
    means = [sum(cycle) / len(cycle) for cycle in cycles]
    peak_times = [
        _cycle_peak_time(cycle_time, cycle)
        for cycle_time, cycle in zip(cycle_times, cycles)
    ]
    periods = [right - left for left, right in zip(peak_times, peak_times[1:])]
    if periods:
        period = sum(periods) / len(periods)
        period_variation = _coefficient_of_variation(periods)
    else:
        spacing = median(right - left for left, right in zip(times, times[1:]))
        period = period_samples * spacing
        period_variation = None
    amplitude_variation = _coefficient_of_variation(amplitudes)
    mean_scale = max(abs(sum(means) / len(means)), _population_std(means), settings.absolute_floor)
    mean_variation = (max(means) - min(means)) / mean_scale
    return period, complete_cycles, period_variation, amplitude_variation, mean_variation


def _stable_periodic_evidence(
    evidence: tuple[float | None, int, float | None, float | None, float | None],
    settings: StationaritySettings,
) -> bool:
    period, complete_cycles, period_variation, amplitude_variation, mean_variation = evidence
    return (
        period is not None
        and complete_cycles >= settings.minimum_cycles
        and _at_most(period_variation, settings.max_period_variation_fraction)
        and _at_most(amplitude_variation, settings.max_amplitude_variation_fraction)
        and _at_most(mean_variation, settings.max_mean_shift_fraction)
    )


def _similar_periods(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) / max(abs(left), abs(right), 1.0) <= 0.05


def _related_periods(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    ratio = left / right
    return 0.5 <= ratio <= 2.0


def _period_only_evolution(
    evidence: tuple[float | None, int, float | None, float | None, float | None],
    settings: StationaritySettings,
) -> bool:
    period, complete_cycles, period_variation, amplitude_variation, mean_variation = evidence
    return (
        period is not None
        and complete_cycles >= settings.minimum_cycles
        and not _at_most(period_variation, settings.max_period_variation_fraction)
        and _at_most(amplitude_variation, settings.max_amplitude_variation_fraction)
        and _at_most(mean_variation, settings.max_mean_shift_fraction)
    )


def _directional_period_drift(
    times: list[float],
    values: list[float],
    period_samples: int,
    settings: StationaritySettings,
) -> bool:
    complete_cycles = len(values) // period_samples
    start = len(values) - complete_cycles * period_samples
    peak_times = [
        _cycle_peak_time(
            times[index : index + period_samples],
            values[index : index + period_samples],
        )
        for index in range(start, len(values), period_samples)
    ]
    periods = [right - left for left, right in zip(peak_times, peak_times[1:])]
    if len(periods) < 2:
        return False
    mean_period = sum(periods) / len(periods)
    if mean_period == 0.0:
        return False
    trend = abs(_slope(list(range(len(periods))), periods)) * (len(periods) - 1) / abs(mean_period)
    return trend > settings.max_period_variation_fraction


def _material_cycle_evolution(
    evidence: tuple[float | None, int, float | None, float | None, float | None],
    settings: StationaritySettings,
) -> bool:
    period, complete_cycles, period_variation, amplitude_variation, mean_variation = evidence
    period_stable = _at_most(period_variation, settings.max_period_variation_fraction)
    amplitude_stable = _at_most(amplitude_variation, settings.max_amplitude_variation_fraction)
    mean_stable = _at_most(mean_variation, settings.max_mean_shift_fraction)
    return (
        period is not None
        and complete_cycles >= settings.minimum_cycles
        and not (period_stable and amplitude_stable and mean_stable)
    )


def _cycle_peak_time(times: list[float], values: list[float]) -> float:
    maximum = max(values)
    tolerance = max(abs(maximum), 1.0) * 1e-12
    tied_times = [sample_time for sample_time, value in zip(times, values) if maximum - value <= tolerance]
    return sum(tied_times) / len(tied_times)


def _mean_std(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    return mean, sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _population_std(values: list[float]) -> float:
    if not values:
        return 0.0
    return _mean_std(values)[1]


def _slope(times: list[float], values: list[float]) -> float:
    mean_time = sum(times) / len(times)
    mean_value = sum(values) / len(values)
    denominator = sum((sample_time - mean_time) ** 2 for sample_time in times)
    if denominator == 0.0:
        return 0.0
    return sum(
        (sample_time - mean_time) * (value - mean_value)
        for sample_time, value in zip(times, values)
    ) / denominator


def _detrend(times: list[float], values: list[float]) -> list[float]:
    slope = _slope(times, values)
    intercept = sum(value - slope * sample_time for sample_time, value in zip(times, values)) / len(values)
    residuals = [
        value - (intercept + slope * sample_time)
        for sample_time, value in zip(times, values)
    ]
    scale = max((abs(value) for value in values), default=0.0)
    if max((abs(value) for value in residuals), default=0.0) <= max(scale, 1.0) * 1e-14:
        return [0.0] * len(values)
    return residuals


def _autocorrelation_time(values: list[float]) -> float:
    mean, standard_deviation = _mean_std(values)
    if standard_deviation == 0.0:
        return 1.0
    centered = [value - mean for value in values]
    variance = standard_deviation * standard_deviation
    correlations = [
        _autocorrelation(centered, lag, variance)
        for lag in range(1, min(_MAXIMUM_AUTOCORRELATION_LAG, len(values) - 1) + 1)
    ]
    total = 0.0
    for index in range(0, len(correlations), 2):
        pair = correlations[index : index + 2]
        if sum(pair) <= 0.0:
            break
        total += sum(pair)
    return max(1.0, 1.0 + 2.0 * total)


def _autocorrelation(centered: list[float], lag: int, variance: float) -> float:
    return (
        sum(left * right for left, right in zip(centered[:-lag], centered[lag:]))
        / (len(centered) - lag)
        / variance
    )


def _coefficient_of_variation(values: list[float]) -> float | None:
    if not values:
        return None
    mean = sum(values) / len(values)
    if mean == 0.0:
        return 0.0 if all(value == 0.0 for value in values) else None
    return _population_std(values) / abs(mean)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0.0:
        return 0.0 if numerator == 0.0 else float_info.max
    return numerator / denominator


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and isfinite(value) and value <= threshold


def _thresholds(settings: StationaritySettings) -> Mapping[str, float]:
    return MappingProxyType(
        {
            "minimum_effective_samples": settings.minimum_effective_samples,
            "max_mean_shift_fraction": settings.max_mean_shift_fraction,
            "max_mean_shift_standard_errors": settings.max_mean_shift_standard_errors,
            "max_normalized_slope": settings.max_normalized_slope,
            "minimum_cycles": float(settings.minimum_cycles),
            "max_period_variation_fraction": settings.max_period_variation_fraction,
            "max_amplitude_variation_fraction": settings.max_amplitude_variation_fraction,
            "absolute_floor": settings.absolute_floor,
            "stale_after_seconds": settings.stale_after_seconds,
        }
    )
