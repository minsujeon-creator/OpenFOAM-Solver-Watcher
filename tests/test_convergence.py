from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from types import MappingProxyType
import unittest

from watcher.convergence import evaluate_numerics
from watcher.models import (
    CaseInspection,
    FailureRecord,
    ResidualSample,
    ResidualTarget,
    SolverTelemetry,
    TimeStepSample,
)


def steady_inspection(targets: dict[str, float]) -> CaseInspection:
    return inspection(mode="steady_simple", targets=targets, max_co=None)


def transient_inspection(max_co: float | None) -> CaseInspection:
    return inspection(mode="transient_pimple", targets={}, max_co=max_co)


def inspection(mode: str, targets: dict[str, float], max_co: float | None) -> CaseInspection:
    return CaseInspection(
        case_dir=Path("case"),
        application="solver",
        openfoam_version=None,
        mode=mode,
        mode_confidence="high",
        mode_evidence=(),
        start_time=0.0,
        end_time=None,
        delta_t=1.0,
        adjust_time_step=None,
        max_co=max_co,
        max_delta_t=None,
        parallel_ranks=1,
        multi_region=False,
        residual_targets=tuple(ResidualTarget(pattern, threshold) for pattern, threshold in targets.items()),
        function_objects=MappingProxyType({}),
        notices=(),
    )


def steady_telemetry(
    iterations: list[dict[str, float]] | None = None,
    *,
    declared_converged: bool = False,
    failure: FailureRecord | None = None,
) -> SolverTelemetry:
    residuals = tuple(
        ResidualSample(
            simulation_time=float(index),
            segment=0,
            field=field,
            initial=value,
            final=value / 10,
            iterations=1,
            converged=None,
            region=None,
            outer_corrector=None,
        )
        for index, iteration in enumerate(iterations or (), start=1)
        for field, value in iteration.items()
    )
    return telemetry(residuals=residuals, declared_converged=declared_converged, failure=failure)


def transient_telemetry(
    courant_max: list[float],
    *,
    continuity_global: list[float] | None = None,
    current_step: TimeStepSample | None = None,
    residuals: tuple[ResidualSample, ...] = (),
    failure: FailureRecord | None = None,
) -> SolverTelemetry:
    continuity = continuity_global or [1e-12] * len(courant_max)
    steps = tuple(
        TimeStepSample(
            simulation_time=float(index),
            segment=0,
            delta_t=0.1,
            courant_mean=value / 2,
            courant_max=value,
            mesh_courant_mean=None,
            mesh_courant_max=None,
            continuity_local=continuity[index - 1] / 2,
            continuity_global=continuity[index - 1],
            continuity_cumulative=continuity[index - 1],
            outer_correctors=1,
        )
        for index, value in enumerate(courant_max, start=1)
    )
    return telemetry(
        current_time=current_step.simulation_time if current_step is not None else None,
        time_steps=steps + ((current_step,) if current_step is not None else ()),
        residuals=residuals,
        failure=failure,
    )


def telemetry(
    *,
    current_time: float | None = None,
    residuals: tuple[ResidualSample, ...] = (),
    time_steps: tuple[TimeStepSample, ...] = (),
    declared_converged: bool = False,
    failure: FailureRecord | None = None,
) -> SolverTelemetry:
    return SolverTelemetry(
        current_time=current_time,
        current_segment=0,
        current_delta_t=None,
        execution_seconds=None,
        clock_seconds=None,
        completed=False,
        solver_declared_converged=declared_converged,
        residuals=residuals,
        time_steps=time_steps,
        warnings=(),
        failure=failure,
        notices=(),
    )


class NumericalAssessmentTests(unittest.TestCase):
    def test_steady_requires_every_configured_target(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5, "p_rgh": 1e-4}),
            steady_telemetry(
                [
                    {"U": 8e-6, "p_rgh": 9e-5},
                    {"U": 7e-6, "p_rgh": 8e-5},
                    {"U": 6e-6, "p_rgh": 7e-5},
                ]
            ),
        )

        self.assertEqual(result.kind, "steady_convergence")
        self.assertEqual(result.status, "passing")
        self.assertEqual(
            [(check.code, check.label, check.passed, check.observed, check.target) for check in result.checks],
            [("residual_target", "U", True, 8e-6, 1e-5), ("residual_target", "p_rgh", True, 9e-5, 1e-4)],
        )

    def test_missing_steady_targets_is_not_configured(self) -> None:
        result = evaluate_numerics(steady_inspection({}), steady_telemetry())

        self.assertEqual(result.status, "not_configured")

    def test_steady_expands_regex_targets_over_all_matching_fields(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"(k|omega)": 1e-4}),
            steady_telemetry(
                [
                    {"k": 8e-5, "omega": 9e-5},
                    {"k": 7e-5, "omega": 8e-5},
                    {"k": 6e-5, "omega": 7e-5},
                ]
            ),
        )

        self.assertEqual(result.status, "passing")
        self.assertEqual(result.checks[0].observed, 9e-5)

    def test_steady_groups_literal_vector_components(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5}),
            steady_telemetry(
                [
                    {"Ux": 8e-6, "Uy": 9e-6, "Uz": 1.1e-5},
                    {"Ux": 7e-6, "Uy": 8e-6, "Uz": 9e-6},
                    {"Ux": 6e-6, "Uy": 7e-6, "Uz": 8e-6},
                ]
            ),
        )

        self.assertEqual(result.status, "failing")
        self.assertEqual((result.checks[0].label, result.checks[0].passed, result.checks[0].observed), ("U", False, 1.1e-5))

    def test_steady_fails_when_a_required_target_is_unobserved(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5, "p_rgh": 1e-4}),
            steady_telemetry([{"U": 8e-6}, {"U": 7e-6}, {"U": 6e-6}]),
        )

        self.assertEqual(result.status, "failing")
        self.assertEqual(
            (result.checks[1].code, result.checks[1].label, result.checks[1].passed, result.checks[1].observed),
            ("residual_target", "p_rgh", False, None),
        )

    def test_steady_excludes_an_incomplete_current_iteration(self) -> None:
        finalized = steady_telemetry([{"U": 8e-6}, {"U": 7e-6}, {"U": 6e-6}])
        active = ResidualSample(4.0, 0, "U", 1e-2, 1e-3, 1, None, None, None)
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5}),
            telemetry(current_time=4.0, residuals=finalized.residuals + (active,)),
        )

        self.assertEqual((result.status, result.checks[0].observed), ("passing", 8e-6))

    def test_completed_steady_includes_a_final_failing_iteration(self) -> None:
        prior = steady_telemetry([{"U": 8e-6}, {"U": 7e-6}, {"U": 6e-6}])
        final = ResidualSample(4.0, 0, "U", 1e-2, 1e-3, 1, None, None, None)
        completed = replace(prior, current_time=4.0, completed=True, residuals=prior.residuals + (final,))

        result = evaluate_numerics(steady_inspection({"U": 1e-5}), completed)

        self.assertEqual((result.status, result.checks[0].observed), ("failing", 1e-2))

    def test_solver_declared_convergence_overrides_residual_result(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5}),
            steady_telemetry([{"U": 2e-5}, {"U": 2e-5}, {"U": 2e-5}], declared_converged=True),
        )

        self.assertEqual(result.status, "passing")
        self.assertFalse(result.checks[0].passed)

    def test_fatal_failure_overrides_steady_declaration(self) -> None:
        result = evaluate_numerics(
            steady_inspection({"U": 1e-5}),
            steady_telemetry(
                [{"U": 8e-6}, {"U": 7e-6}, {"U": 6e-6}],
                declared_converged=True,
                failure=failure_record(),
            ),
        )

        self.assertEqual(result.status, "failing")
        self.assertEqual(result.checks[-1].code, "fatal_failure")

    def test_transient_health_fails_recent_courant_limit(self) -> None:
        result = evaluate_numerics(transient_inspection(1.0), transient_telemetry([0.4, 0.8, 1.4]))

        self.assertEqual(result.kind, "transient_health")
        self.assertEqual(result.status, "failing")
        self.assertTrue(any(check.code == "courant_limit" and not check.passed for check in result.checks))

    def test_transient_excludes_incomplete_current_step(self) -> None:
        current_step = TimeStepSample(4.0, 0, 0.1, 1.0, 2.0, None, None, 1e-12, 1e-12, 1e-12, 1)
        result = evaluate_numerics(transient_inspection(1.0), transient_telemetry([0.4, 0.8, 0.6], current_step=current_step))

        self.assertEqual((result.status, result.healthy_step_percent), ("passing", 100.0))

    def test_completed_transient_includes_a_final_unhealthy_step(self) -> None:
        prior = transient_telemetry([0.4, 0.5, 0.6])
        final = TimeStepSample(4.0, 0, 0.1, 1.0, 1.4, None, None, 1e-12, 1e-12, 1e-12, 1)
        completed = replace(prior, current_time=4.0, completed=True, time_steps=prior.time_steps + (final,))

        result = evaluate_numerics(transient_inspection(1.0), completed)

        self.assertEqual((result.status, result.healthy_step_percent), ("failing", 75.0))

    def test_transient_warns_when_continuity_global_error_degrades(self) -> None:
        result = evaluate_numerics(
            transient_inspection(1.0),
            transient_telemetry([0.5] * 6, continuity_global=[1e-14, 1e-14, 1e-14, 1e-10, 1e-10, 1e-10]),
        )

        self.assertEqual(result.status, "warning")
        self.assertEqual(
            [(check.code, check.passed) for check in result.checks if check.code == "continuity_trend"],
            [("continuity_trend", False)],
        )

    def test_transient_residual_non_convergence_makes_steps_unhealthy(self) -> None:
        residuals = tuple(
            ResidualSample(float(index), 0, "U", 1e-4, 1e-5, 1, False, None, None)
            for index in range(1, 4)
        )
        result = evaluate_numerics(transient_inspection(1.0), transient_telemetry([0.4, 0.5, 0.6], residuals=residuals))

        self.assertEqual((result.status, result.healthy_step_percent), ("failing", 0.0))

    def test_transient_requires_at_least_three_finalized_steps(self) -> None:
        result = evaluate_numerics(transient_inspection(1.0), transient_telemetry([0.4, 0.5]))

        self.assertEqual(result.status, "insufficient_data")
        self.assertEqual(result.healthy_step_percent, None)

    def test_transient_missing_max_co_is_not_configured(self) -> None:
        result = evaluate_numerics(transient_inspection(None), transient_telemetry([0.4, 0.5, 0.6]))

        self.assertEqual(result.status, "not_configured")

    def test_transient_failure_overrides_healthy_history(self) -> None:
        result = evaluate_numerics(
            transient_inspection(1.0),
            transient_telemetry([0.4, 0.5, 0.6], failure=failure_record()),
        )

        self.assertEqual(result.status, "failing")
        self.assertEqual(result.checks[-1].code, "fatal_failure")

    def test_transient_requires_finite_continuity_values(self) -> None:
        result = evaluate_numerics(
            transient_inspection(1.0),
            transient_telemetry([0.4, 0.5, 0.6], continuity_global=[1e-12, math.nan, 1e-12]),
        )

        self.assertEqual((result.status, result.healthy_step_percent), ("failing", 66.66666666666667))

    def test_unknown_mode_cannot_report_passing_numerics(self) -> None:
        result = evaluate_numerics(
            inspection(mode="unknown", targets={}, max_co=1.0),
            transient_telemetry([0.4, 0.5, 0.6]),
        )

        self.assertEqual((result.kind, result.status), ("unsupported", "not_configured"))


def failure_record() -> FailureRecord:
    return FailureRecord("Floating point exception", "Floating point exception", 0, 3.0)


if __name__ == "__main__":
    unittest.main()
