from __future__ import annotations

from dataclasses import fields, FrozenInstanceError
import math
import random
import unittest
from unittest.mock import patch

from watcher.models import CandidateInfo, SeriesData
from watcher import stationarity
from watcher.stationarity import StationaritySettings, aggregate_stationarity, analyze_series


def series(
    values: list[float],
    *,
    times: list[float] | None = None,
    series_id: str = "selected",
    stale: bool = False,
    modified_at: float | None = None,
) -> SeriesData:
    sample_times = times if times is not None else [float(index) for index in range(len(values))]
    if modified_at is None:
        modified_at = sample_times[-1] if sample_times else 0.0
    return SeriesData(
        series_id=series_id,
        label=series_id,
        function_name="monitor",
        function_type="volFieldValue",
        source_relative="postProcessing/monitor/0/value.dat",
        region=None,
        field="value",
        operation="volAverage",
        component=None,
        units=None,
        times=tuple(sample_times),
        values=tuple(values),
        modified_ns=int(modified_at * 1_000_000_000),
        stale=stale,
        candidate=CandidateInfo(100, "high", True, "test signal"),
        notices=(),
    )


class StationarityAnalysisTests(unittest.TestCase):
    def test_default_settings_are_exact_and_immutable(self) -> None:
        settings = StationaritySettings()

        self.assertEqual(
            (
                settings.minimum_effective_samples,
                settings.max_mean_shift_fraction,
                settings.max_mean_shift_standard_errors,
                settings.max_normalized_slope,
                settings.minimum_cycles,
                settings.max_period_variation_fraction,
                settings.max_amplitude_variation_fraction,
                settings.absolute_floor,
                settings.stale_after_seconds,
            ),
            (20.0, 0.02, 2.0, 0.02, 3, 0.05, 0.10, 1e-12, 120.0),
        )
        with self.assertRaises(FrozenInstanceError):
            settings.absolute_floor = 1.0  # type: ignore[misc]

    def test_flat_noisy_signal_is_statistically_stationary(self) -> None:
        rng = random.Random(31)
        values = [10.0 + rng.gauss(0.0, 0.05) for _ in range(600)]

        result = analyze_series(series(values), StationaritySettings(), now=599.0)

        self.assertIn(result.state, {"plateau", "statistically_stationary"})
        self.assertGreaterEqual(result.evidence.effective_samples, 20.0)

    def test_drifting_signal_is_evolving(self) -> None:
        values = [2.0 + 0.002 * index for index in range(600)]

        result = analyze_series(series(values), StationaritySettings(), now=599.0)

        self.assertEqual(result.state, "evolving")
        self.assertGreater(
            result.evidence.normalized_slope,
            result.thresholds["max_normalized_slope"],
        )

    def test_stable_sinusoid_is_periodic(self) -> None:
        values = [4.0 + 0.8 * math.sin(2.0 * math.pi * index / 40.0) for index in range(800)]

        result = analyze_series(series(values), StationaritySettings(), now=799.0)

        self.assertEqual(result.state, "periodic")
        self.assertIsNotNone(result.evidence.period)
        assert result.evidence.period is not None
        self.assertLess(abs(result.evidence.period - 40.0), 1.0)
        self.assertGreaterEqual(result.evidence.complete_cycles, 3)

    def test_short_period_sinusoid_selects_its_fundamental_cycle(self) -> None:
        values = [
            4.0 + 0.8 * math.sin(2.0 * math.pi * index / 7.0 + 0.7)
            for index in range(600)
        ]

        result = analyze_series(series(values), StationaritySettings(), now=599.0)

        self.assertEqual(result.state, "periodic")
        self.assertIsNotNone(result.evidence.period)
        assert result.evidence.period is not None
        self.assertLess(abs(result.evidence.period - 7.0), 1.0)
        self.assertGreaterEqual(result.evidence.complete_cycles, 3)

    def test_period_thirteen_sinusoid_selects_its_fundamental_cycle(self) -> None:
        values = [
            4.0 + 0.8 * math.sin(2.0 * math.pi * index / 13.0 + 1.1)
            for index in range(600)
        ]

        result = analyze_series(series(values), StationaritySettings(), now=599.0)

        self.assertEqual(result.state, "periodic")
        self.assertIsNotNone(result.evidence.period)
        assert result.evidence.period is not None
        self.assertLess(abs(result.evidence.period - 13.0), 1.0)
        self.assertGreaterEqual(result.evidence.complete_cycles, 3)

    def test_exact_sine_period_and_phase_sweep_selects_fundamentals(self) -> None:
        for period in (5.0, 7.0, 11.0, 13.0, 17.0, 19.0, 29.0):
            for phase in (0.0, 0.7, 1.1):
                with self.subTest(period=period, phase=phase):
                    values = [
                        4.0 + 0.8 * math.sin(2.0 * math.pi * index / period + phase)
                        for index in range(600)
                    ]

                    result = analyze_series(series(values), StationaritySettings(), now=599.0)

                    self.assertEqual(result.state, "periodic")
                    self.assertIsNotNone(result.evidence.period)
                    assert result.evidence.period is not None
                    self.assertLess(abs(result.evidence.period - period), 1.0)
                    self.assertGreaterEqual(result.evidence.complete_cycles, 3)

    def test_phase_zero_sinusoid_has_stable_cycle_boundaries(self) -> None:
        values = [4.0 + 0.8 * math.sin(2.0 * math.pi * index / 6.0) for index in range(600)]

        result = analyze_series(series(values), StationaritySettings(), now=599.0)

        self.assertEqual(result.state, "periodic")
        self.assertIsNotNone(result.evidence.period)
        assert result.evidence.period is not None
        self.assertLess(abs(result.evidence.period - 6.0), 1.0)
        self.assertLessEqual(
            result.evidence.period_variation_fraction,
            result.thresholds["max_period_variation_fraction"],
        )

    def test_analysis_horizon_bounds_very_long_series_work(self) -> None:
        sample_count = 100_000
        values = [4.0 + 0.8 * math.sin(2.0 * math.pi * index / 40.0) for index in range(sample_count)]
        calls: list[int] = []
        autocorrelation = stationarity._autocorrelation

        def record_autocorrelation(
            centered: list[float],
            lag: int,
            variance: float,
        ) -> float:
            calls.append(len(centered))
            return autocorrelation(centered, lag, variance)

        with patch.object(stationarity, "_autocorrelation", side_effect=record_autocorrelation):
            result = analyze_series(series(values), StationaritySettings(), now=float(sample_count - 1))

        self.assertEqual(result.evidence.segment_samples, sample_count)
        self.assertLessEqual(
            result.evidence.window_samples,
            stationarity._MAXIMUM_ANALYSIS_SAMPLES // 2,
        )
        self.assertLessEqual(max(calls), stationarity._MAXIMUM_ANALYSIS_SAMPLES)
        self.assertLessEqual(
            len(calls),
            2 * stationarity._MAXIMUM_ANALYSIS_SAMPLES
            + 2 * stationarity._MAXIMUM_AUTOCORRELATION_LAG,
        )

    def test_bounded_analysis_retains_long_period_periodicity(self) -> None:
        values = [4.0 + 0.8 * math.sin(2.0 * math.pi * index / 1200.0) for index in range(12_000)]

        result = analyze_series(series(values), StationaritySettings(), now=11_999.0)

        self.assertEqual(result.state, "periodic")
        self.assertIsNotNone(result.evidence.period)
        assert result.evidence.period is not None
        self.assertLess(abs(result.evidence.period - 1200.0), 5.0)
        self.assertGreaterEqual(result.evidence.complete_cycles, 3)

    def test_recent_amplitude_ramp_is_not_hidden_by_bounded_overview(self) -> None:
        values = [
            4.0
            + (
                0.8 if index < 11_000 else 0.8 + 3.0 * (index - 11_000) / 999.0
            )
            * math.sin(2.0 * math.pi * index / 40.0)
            for index in range(12_000)
        ]

        result = analyze_series(series(values), StationaritySettings(), now=11_999.0)

        self.assertEqual(result.state, "evolving")
        self.assertGreater(
            result.evidence.amplitude_variation_fraction,
            result.thresholds["max_amplitude_variation_fraction"],
        )

    def test_recent_period_ramp_vetoes_stable_overview_fallback(self) -> None:
        phase = 0.0
        values: list[float] = []
        for index in range(12_000):
            period = 40.0 if index < 9_952 else 40.0 + 20.0 * (index - 9_952) / 2_047.0
            values.append(4.0 + 0.8 * math.sin(phase))
            phase += 2.0 * math.pi / period

        result = analyze_series(series(values), StationaritySettings(), now=11_999.0)

        self.assertEqual(result.state, "evolving")
        self.assertGreater(
            result.evidence.period_variation_fraction,
            result.thresholds["max_period_variation_fraction"],
        )

    def test_material_cycle_evolution_truth_table(self) -> None:
        settings = StationaritySettings()
        for period_drift, amplitude_drift, mean_drift in (
            (False, False, False),
            (True, False, False),
            (False, True, False),
            (False, False, True),
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ):
            with self.subTest(
                period_drift=period_drift,
                amplitude_drift=amplitude_drift,
                mean_drift=mean_drift,
            ):
                evidence = (
                    40.0,
                    10,
                    0.06 if period_drift else 0.01,
                    0.11 if amplitude_drift else 0.01,
                    0.03 if mean_drift else 0.01,
                )
                self.assertEqual(
                    stationarity._material_cycle_evolution(evidence, settings),
                    period_drift or amplitude_drift or mean_drift,
                )

    def test_periodic_amplitude_drift_is_evolving(self) -> None:
        values = [
            4.0 + (0.4 + 0.0015 * index) * math.sin(2.0 * math.pi * index / 40.0)
            for index in range(800)
        ]

        result = analyze_series(series(values), StationaritySettings(), now=799.0)

        self.assertEqual(result.state, "evolving")
        self.assertGreater(
            result.evidence.amplitude_variation_fraction,
            result.thresholds["max_amplitude_variation_fraction"],
        )

    def test_periodic_mean_drift_is_evolving(self) -> None:
        values = [
            4.0 + 0.001 * index + 0.8 * math.sin(2.0 * math.pi * index / 40.0)
            for index in range(800)
        ]

        result = analyze_series(series(values), StationaritySettings(), now=799.0)

        self.assertEqual(result.state, "evolving")
        self.assertGreater(
            result.evidence.normalized_slope,
            result.thresholds["max_normalized_slope"],
        )

    def test_near_zero_scale_remains_finite_and_can_plateau(self) -> None:
        rng = random.Random(101)
        window = [rng.gauss(0.0, 1e-15) for _ in range(100)]
        values = window + window

        result = analyze_series(series(values), StationaritySettings(), now=199.0)

        self.assertEqual(result.state, "plateau")
        self.assertTrue(math.isfinite(result.evidence.normalized_mean_shift))
        self.assertTrue(math.isfinite(result.evidence.normalized_slope))

    def test_autocorrelation_reduces_effective_sample_count(self) -> None:
        rng = random.Random(17)
        values = [0.0]
        for _ in range(399):
            values.append(0.98 * values[-1] + rng.gauss(0.0, 0.05))

        result = analyze_series(series(values), StationaritySettings(), now=399.0)

        self.assertGreater(result.evidence.autocorrelation_time, 1.0)
        self.assertLess(result.evidence.effective_samples, result.evidence.window_samples)

    def test_irregular_sample_times_use_elapsed_time_for_slope(self) -> None:
        times = [float(index) + 0.15 * (index % 3) for index in range(300)]
        values = [7.0 + 0.001 * math.sin(index) for index in range(300)]

        result = analyze_series(series(values, times=times), StationaritySettings(), now=times[-1])

        self.assertIn(result.state, {"plateau", "statistically_stationary"})
        self.assertLessEqual(
            result.evidence.normalized_slope,
            result.thresholds["max_normalized_slope"],
        )

    def test_large_gap_with_short_latest_segment_is_indeterminate(self) -> None:
        times = [float(index) for index in range(200)] + [1000.0 + index for index in range(40)]
        values = [3.0] * len(times)

        result = analyze_series(series(values, times=times), StationaritySettings(), now=1039.0)

        self.assertEqual(result.state, "indeterminate")
        self.assertTrue(result.evidence.discontinuous)
        self.assertEqual(result.evidence.segment_samples, 40)

    def test_stale_flag_or_expired_modification_time_is_indeterminate(self) -> None:
        values = [3.0] * 100

        flagged = analyze_series(series(values, stale=True), StationaritySettings(), now=99.0)
        expired = analyze_series(
            series(values, modified_at=0.0),
            StationaritySettings(),
            now=121.0,
        )

        self.assertEqual(flagged.state, "indeterminate")
        self.assertEqual(expired.state, "indeterminate")

    def test_nonfinite_samples_are_removed_before_analysis(self) -> None:
        values = [5.0] * 100
        values[5] = math.nan
        values[15] = math.inf
        times = [float(index) for index in range(100)]
        times[25] = math.nan

        result = analyze_series(series(values, times=times), StationaritySettings(), now=99.0)

        self.assertEqual(result.state, "plateau")
        self.assertEqual(result.evidence.raw_samples, 97)

    def test_fewer_than_twenty_effective_samples_is_indeterminate(self) -> None:
        rng = random.Random(23)
        fluctuations = [0.0]
        for _ in range(59):
            fluctuations.append(0.99 * fluctuations[-1] + rng.gauss(0.0, 0.01))
        values = [2.0 + value for value in fluctuations]

        result = analyze_series(
            series(values),
            StationaritySettings(minimum_effective_samples=20.0),
            now=59.0,
        )

        self.assertEqual(result.state, "indeterminate")
        self.assertLess(result.evidence.effective_samples, 20.0)

    def test_unavailable_evidence_never_uses_nonfinite_json_numbers(self) -> None:
        result = analyze_series(series([2.0] * 10), StationaritySettings(), now=9.0)

        numbers = (
            getattr(result.evidence, field.name)
            for field in fields(result.evidence)
            if isinstance(getattr(result.evidence, field.name), float)
        )
        self.assertTrue(all(math.isfinite(value) for value in numbers))


class AggregateStationarityTests(unittest.TestCase):
    def setUp(self) -> None:
        flat = analyze_series(series([2.0] * 100, series_id="flat"), StationaritySettings(), now=99.0)
        periodic_values = [4.0 + math.sin(2.0 * math.pi * index / 20.0) for index in range(400)]
        periodic = analyze_series(
            series(periodic_values, series_id="periodic"),
            StationaritySettings(),
            now=399.0,
        )
        self.results = {"flat": flat, "periodic": periodic}

    def test_zero_selected_series_is_indeterminate(self) -> None:
        aggregate = aggregate_stationarity(
            [],
            self.results,
            {"plateau", "statistically_stationary", "periodic"},
        )

        self.assertEqual(aggregate.state, "indeterminate")
        self.assertFalse(aggregate.passing)

    def test_missing_selected_series_is_indeterminate(self) -> None:
        aggregate = aggregate_stationarity(
            ["flat", "missing"],
            self.results,
            {"plateau", "statistically_stationary", "periodic"},
        )

        self.assertEqual(aggregate.state, "indeterminate")
        self.assertFalse(aggregate.passing)
        self.assertEqual(aggregate.missing_ids, ("missing",))

    def test_every_selected_series_must_be_in_an_accepted_state(self) -> None:
        accepted = aggregate_stationarity(
            ["flat", "periodic"],
            self.results,
            {"plateau", "statistically_stationary", "periodic"},
        )
        rejected = aggregate_stationarity(
            ["flat", "periodic"],
            self.results,
            {"plateau", "statistically_stationary"},
        )

        self.assertEqual(accepted.state, "passing")
        self.assertTrue(accepted.passing)
        self.assertEqual(rejected.state, "failing")
        self.assertFalse(rejected.passing)
        self.assertEqual(rejected.rejected_ids, ("periodic",))


if __name__ == "__main__":
    unittest.main()
