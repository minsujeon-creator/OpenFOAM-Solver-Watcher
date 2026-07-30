from __future__ import annotations

from collections import defaultdict
from math import isfinite
from statistics import median
import re

from watcher.models import AssessmentCheck, CaseInspection, NumericalAssessment, ResidualSample, SolverTelemetry, TimeStepSample


_STEADY_MODES = {"steady_simple", "pseudo_transient"}
_TRANSIENT_MODES = {"transient_pimple", "transient_piso"}


def evaluate_numerics(inspection: CaseInspection, telemetry: SolverTelemetry) -> NumericalAssessment:
    """Assess configured solver numerics without inferring any missing threshold."""
    if inspection.mode in _STEADY_MODES:
        return _evaluate_steady(inspection, telemetry)
    if inspection.mode in _TRANSIENT_MODES:
        return _evaluate_transient(inspection, telemetry)
    return _unsupported_assessment(inspection, telemetry)


def _evaluate_steady(inspection: CaseInspection, telemetry: SolverTelemetry) -> NumericalAssessment:
    if not inspection.residual_targets:
        checks = [
            AssessmentCheck(
                "residual_targets",
                "Residual-control targets",
                None,
                None,
                None,
                "No residualControl targets are configured; no steady threshold is inferred.",
            )
        ]
        return _steady_result("not_configured", "Residual convergence is not configured.", checks, telemetry)

    groups = _residual_groups(_finalized_residuals(telemetry))
    window = list(groups.values())[-3:]
    if len(window) < 3:
        checks = [
            AssessmentCheck(
                "residual_history",
                "Sustained residual history",
                None,
                len(window),
                3.0,
                "Three complete iterations are required for a sustained residual assessment.",
            )
        ]
        return _steady_result("insufficient_data", "Too few residual iterations for a sustained assessment.", checks, telemetry)

    checks = [_steady_target_check(target.pattern, target.threshold, window) for target in inspection.residual_targets]
    status = "passing" if all(check.passed for check in checks) else "failing"
    return _steady_result(status, "Residual-control targets were assessed across the last three iterations.", checks, telemetry)


def _steady_result(
    status: str,
    summary: str,
    checks: list[AssessmentCheck],
    telemetry: SolverTelemetry,
) -> NumericalAssessment:
    if telemetry.solver_declared_converged:
        status = "passing"
        summary = "The solver declared its convergence criteria satisfied."
    if telemetry.failure is not None:
        status = "failing"
        summary = f"Fatal solver failure: {telemetry.failure.label}."
        checks.append(_failure_check(telemetry))
    return NumericalAssessment("steady_convergence", status, summary, tuple(checks), None)


def _residual_groups(residuals: tuple[ResidualSample, ...]) -> dict[tuple[int, float], list[ResidualSample]]:
    groups: dict[tuple[int, float], list[ResidualSample]] = defaultdict(list)
    for residual in residuals:
        groups[(residual.segment, residual.simulation_time)].append(residual)
    return dict(sorted(groups.items()))


def _finalized_residuals(telemetry: SolverTelemetry) -> tuple[ResidualSample, ...]:
    if telemetry.current_time is None or telemetry.completed:
        return telemetry.residuals
    return tuple(
        residual
        for residual in telemetry.residuals
        if not (residual.segment == telemetry.current_segment and residual.simulation_time == telemetry.current_time)
    )


def _steady_target_check(pattern: str, threshold: float, window: list[list[ResidualSample]]) -> AssessmentCheck:
    try:
        matcher = re.compile(pattern)
    except re.error:
        return AssessmentCheck(
            "residual_target",
            pattern,
            False,
            None,
            threshold,
            "The configured residual target is not a valid regular expression.",
        )

    values: list[float] = []
    complete = True
    for iteration in window:
        samples = [sample for sample in iteration if _matches_target(matcher, pattern, sample.field)]
        if not samples:
            complete = False
            continue
        values.extend(sample.initial for sample in samples)

    if not values:
        return AssessmentCheck(
            "residual_target",
            pattern,
            False,
            None,
            threshold,
            "The configured target did not match any observed residual field.",
        )

    observed = max(values)
    passed = complete and all(isfinite(value) and value <= threshold for value in values)
    explanation = "Every matched initial residual stayed within the target across the last three iterations."
    if not complete:
        explanation = "The configured target was absent from at least one of the last three iterations."
    elif not passed:
        explanation = "At least one matched initial residual exceeded the configured target."
    return AssessmentCheck("residual_target", pattern, passed, observed, threshold, explanation)


def _matches_target(matcher: re.Pattern[str], pattern: str, field: str) -> bool:
    if matcher.fullmatch(field) is not None:
        return True
    return _is_literal_field(pattern) and field in {f"{pattern}x", f"{pattern}y", f"{pattern}z"}


def _is_literal_field(pattern: str) -> bool:
    return re.fullmatch(r"[A-Za-z_]\w*", pattern) is not None


def _evaluate_transient(inspection: CaseInspection, telemetry: SolverTelemetry) -> NumericalAssessment:
    checks: list[AssessmentCheck] = []
    if inspection.max_co is None:
        checks.append(
            AssessmentCheck(
                "courant_limit",
                "Configured maxCo",
                None,
                None,
                None,
                "No maxCo is configured; a Courant limit is not inferred.",
            )
        )
        return _transient_result("not_configured", "Transient Courant health is not configured.", checks, None, telemetry)

    steps = _finalized_steps(telemetry)
    if len(steps) < 3:
        checks.append(
            AssessmentCheck(
                "finalized_steps",
                "Finalized time steps",
                None,
                len(steps),
                3.0,
                "At least three finalized time steps are required for transient health.",
            )
        )
        return _transient_result("insufficient_data", "Too few finalized time steps for a health assessment.", checks, None, telemetry)

    courant_ok = [step.courant_max is not None and isfinite(step.courant_max) and step.courant_max <= inspection.max_co for step in steps]
    continuity_ok = [_continuity_is_finite(step) for step in steps]
    residual_ok = [_step_residuals_converged(step.segment, step.simulation_time, telemetry.residuals) for step in steps]
    healthy = [all(values) for values in zip(courant_ok, continuity_ok, residual_ok)]
    healthy_percent = 100.0 * sum(healthy) / len(healthy)

    observed_courant = max((step.courant_max for step in steps if step.courant_max is not None and isfinite(step.courant_max)), default=None)
    checks.extend(
        (
            AssessmentCheck(
                "courant_limit",
                "Courant maximum",
                all(courant_ok),
                observed_courant,
                inspection.max_co,
                "Every finalized step remained within the configured maxCo.",
            ),
            AssessmentCheck(
                "continuity_values",
                "Finite continuity values",
                all(continuity_ok),
                None,
                "finite",
                "Every finalized step supplied finite local, global, and cumulative continuity errors.",
            ),
            AssessmentCheck(
                "residual_convergence",
                "Explicit residual convergence",
                all(residual_ok),
                None,
                "no explicit non-convergence",
                "No finalized step contained an explicitly non-converged residual record.",
            ),
            AssessmentCheck(
                "healthy_steps",
                "Healthy finalized steps",
                healthy_percent >= 95.0,
                healthy_percent,
                "95%",
                "A healthy step meets Courant, continuity, and residual-record requirements.",
            ),
        )
    )
    trend = _continuity_trend_check(steps)
    if trend is not None:
        checks.append(trend)

    if healthy_percent < 80.0:
        status = "failing"
    elif healthy_percent < 95.0:
        status = "warning"
    else:
        status = "passing"
    if trend is not None and trend.passed is False and status == "passing":
        status = "warning"
    return _transient_result(status, "Recent finalized time steps were assessed for numerical health.", checks, healthy_percent, telemetry)


def _finalized_steps(telemetry: SolverTelemetry) -> tuple[TimeStepSample, ...]:
    steps = telemetry.time_steps
    if telemetry.current_time is not None and not telemetry.completed:
        steps = tuple(
            step
            for step in steps
            if not (step.segment == telemetry.current_segment and step.simulation_time == telemetry.current_time)
        )
    return steps[-20:]


def _unsupported_assessment(inspection: CaseInspection, telemetry: SolverTelemetry) -> NumericalAssessment:
    checks = [
        AssessmentCheck(
            "unsupported_mode",
            "Case mode",
            None,
            inspection.mode,
            "known numerical mode",
            "Numerical assessment is not configured for the detected case mode.",
        )
    ]
    status = "not_configured"
    summary = "Numerical assessment is not configured for the detected case mode."
    if telemetry.failure is not None:
        status = "failing"
        summary = f"Fatal solver failure: {telemetry.failure.label}."
        checks.append(_failure_check(telemetry))
    return NumericalAssessment("unsupported", status, summary, tuple(checks), None)


def _continuity_is_finite(step: TimeStepSample) -> bool:
    values = (step.continuity_local, step.continuity_global, step.continuity_cumulative)
    return all(value is not None and isfinite(value) for value in values)


def _step_residuals_converged(segment: int, simulation_time: float, residuals: tuple[ResidualSample, ...]) -> bool:
    return not any(
        residual.segment == segment and residual.simulation_time == simulation_time and residual.converged is False
        for residual in residuals
    )


def _continuity_trend_check(steps: tuple[TimeStepSample, ...]) -> AssessmentCheck | None:
    errors = [abs(step.continuity_global) for step in steps if step.continuity_global is not None and isfinite(step.continuity_global)]
    if len(errors) < 2:
        return None
    split = len(errors) // 2
    first = median(errors[:split])
    second = median(errors[split:])
    degraded = second > 5.0 * first and second - first > 1e-12
    explanation = "The median absolute global continuity error did not materially degrade."
    if degraded:
        explanation = "The second-half median absolute global continuity error increased by more than fivefold."
    return AssessmentCheck("continuity_trend", "Continuity trend", not degraded, second, first, explanation)


def _transient_result(
    status: str,
    summary: str,
    checks: list[AssessmentCheck],
    healthy_step_percent: float | None,
    telemetry: SolverTelemetry,
) -> NumericalAssessment:
    if telemetry.failure is not None:
        status = "failing"
        summary = f"Fatal solver failure: {telemetry.failure.label}."
        checks.append(_failure_check(telemetry))
    return NumericalAssessment("transient_health", status, summary, tuple(checks), healthy_step_percent)


def _failure_check(telemetry: SolverTelemetry) -> AssessmentCheck:
    assert telemetry.failure is not None
    return AssessmentCheck(
        "fatal_failure",
        telemetry.failure.label,
        False,
        telemetry.failure.simulation_time,
        "no fatal failure",
        telemetry.failure.line,
    )
