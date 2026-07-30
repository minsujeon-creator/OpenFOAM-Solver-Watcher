from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipIf
from unittest.mock import patch

from tests.helpers import TemporaryCase
from watcher.persistence import (
    ConfigValidationError,
    UnsafeConfigPath,
    default_config,
    load_config,
    save_config,
    validate_config_payload,
)
from watcher.models import SeriesOverride, WatcherConfig


class ConfigurationValidationTests(TestCase):
    def test_round_trip_configuration(self) -> None:
        with TemporaryCase() as case:
            config = WatcherConfig(
                version=1,
                selected_log="log.pimpleFoam",
                selected_series=("abc123",),
                overrides={"abc123": SeriesOverride(label="Lift", units="N")},
                accepted_states=frozenset(
                    {"plateau", "statistically_stationary", "periodic"}
                ),
            )

            save_config(case.path, config)
            loaded = load_config(case.path)

        self.assertEqual(loaded.config, config)
        self.assertIsNone(loaded.error)

    def test_rejects_unknown_top_level_and_override_keys(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config_payload({"version": 1, "extra": True}, set())

        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {
                    "version": 1,
                    "overrides": {"x": {"label": "X", "extra": 1}},
                },
                {"x"},
            )

    def test_rejects_booleans_and_non_finite_numbers(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config_payload({"version": True}, set())

        for value in (True, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(ConfigValidationError):
                    validate_config_payload(
                        {
                            "version": 1,
                            "overrides": {"x": {"absoluteFloor": value}},
                        },
                        {"x"},
                    )

    def test_rejects_unbounded_minimum_cycles(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {
                    "version": 1,
                    "overrides": {"x": {"minimumCycles": 10**1_000}},
                },
                {"x"},
            )

    def test_rejects_unsafe_selected_log_paths(self) -> None:
        unsafe_paths = (
            "/tmp/log.solver",
            "../log.solver",
            "logs/../../log.solver",
            r"C:\outside\log.solver",
        )
        for selected_log in unsafe_paths:
            with self.subTest(selected_log=selected_log):
                with self.assertRaises(ConfigValidationError):
                    validate_config_payload(
                        {"version": 1, "selectedLog": selected_log},
                        set(),
                    )

    def test_rejects_unsupported_versions(self) -> None:
        for version in (0, 2, "1", None):
            with self.subTest(version=version):
                with self.assertRaises(ConfigValidationError):
                    validate_config_payload({"version": version}, set())

    def test_rejects_unknown_and_duplicate_series_ids(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "selectedSeries": ["unknown"]},
                {"known"},
            )
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "overrides": {"unknown": {"label": "X"}}},
                {"known"},
            )
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "selectedSeries": ["known", "known"]},
                {"known"},
            )

    def test_rejects_invalid_accepted_states(self) -> None:
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "acceptedStates": ["passing"]},
                set(),
            )
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "acceptedStates": ["plateau", "plateau"]},
                set(),
            )

    def test_bounds_strings_and_selections(self) -> None:
        for key in ("label", "units"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigValidationError):
                    validate_config_payload(
                        {
                            "version": 1,
                            "overrides": {"known": {key: "x" * 101}},
                        },
                        {"known"},
                    )

        known = {f"series-{index}" for index in range(1001)}
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {
                    "version": 1,
                    "selectedSeries": [f"series-{index}" for index in range(1001)],
                },
                known,
            )
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "selectedLog": "x" * 4097},
                set(),
            )
        with self.assertRaises(ConfigValidationError):
            validate_config_payload(
                {"version": 1, "selectedSeries": ["x" * 257]},
                {"x" * 257},
            )

    def test_builds_immutable_configuration_models(self) -> None:
        source_overrides = {"x": SeriesOverride(label="X")}
        config = WatcherConfig(
            version=1,
            selected_log=None,
            selected_series=["x"],  # type: ignore[arg-type]
            overrides=source_overrides,
            accepted_states={"plateau"},  # type: ignore[arg-type]
        )
        source_overrides["y"] = SeriesOverride(label="Y")

        self.assertEqual(config.selected_series, ("x",))
        self.assertEqual(config.accepted_states, frozenset({"plateau"}))
        self.assertNotIn("y", config.overrides)
        with self.assertRaises(TypeError):
            config.overrides["y"] = SeriesOverride(label="Y")  # type: ignore[index]


class ConfigurationFilesystemTests(TestCase):
    def test_missing_configuration_uses_defaults(self) -> None:
        with TemporaryCase() as case:
            loaded = load_config(case.path)

        self.assertEqual(loaded.config, default_config())
        self.assertIsNone(loaded.error)

    def test_malformed_json_uses_defaults_without_modifying_file(self) -> None:
        with TemporaryCase() as case:
            target = case.write(".foam-watcher.json", "{broken")
            before = target.read_bytes()

            loaded = load_config(case.path)

            self.assertEqual(target.read_bytes(), before)

        self.assertEqual(loaded.config, default_config())
        self.assertIsNotNone(loaded.error)

    def test_overlong_integer_uses_defaults_without_escaping_loader(self) -> None:
        with TemporaryCase() as case:
            content = '{"version": ' + "9" * 5_000 + "}\n"
            target = case.write(".foam-watcher.json", content)
            before = target.read_bytes()

            loaded = load_config(case.path)

            self.assertEqual(target.read_bytes(), before)

        self.assertEqual(loaded.config, default_config())
        self.assertIsNotNone(loaded.error)

    def test_deep_json_uses_defaults_without_escaping_loader(self) -> None:
        with TemporaryCase() as case:
            content = "[" * 5_000 + "0" + "]" * 5_000
            target = case.write(".foam-watcher.json", content)
            before = target.read_bytes()

            loaded = load_config(case.path)

            self.assertEqual(target.read_bytes(), before)

        self.assertEqual(loaded.config, default_config())
        self.assertIsNotNone(loaded.error)

    def test_oversized_json_is_rejected_before_schema_processing(self) -> None:
        with TemporaryCase() as case:
            content = " " * (5 * 1_024 * 1_024) + '{"version": 1}\n'
            target = case.write(".foam-watcher.json", content)
            before = target.read_bytes()

            loaded = load_config(case.path)

            self.assertEqual(target.read_bytes(), before)

        self.assertEqual(loaded.config, default_config())
        self.assertIsNotNone(loaded.error)

    def test_invalid_existing_file_is_not_overwritten(self) -> None:
        with TemporaryCase() as case:
            target = case.write(".foam-watcher.json", '{"version": 2}\n')
            before = target.read_bytes()

            with self.assertRaises(ConfigValidationError):
                save_config(case.path, default_config())

            self.assertEqual(target.read_bytes(), before)

    def test_failed_atomic_replacement_preserves_original_and_removes_temp(self) -> None:
        with TemporaryCase() as case:
            original = WatcherConfig(
                version=1,
                selected_log="log.original",
                selected_series=(),
                overrides={},
                accepted_states=frozenset({"plateau"}),
            )
            save_config(case.path, original)
            target = case.path / ".foam-watcher.json"
            before = target.read_bytes()

            replacement = WatcherConfig(
                version=1,
                selected_log="log.replacement",
                selected_series=(),
                overrides={},
                accepted_states=frozenset({"periodic"}),
            )
            with patch("watcher.persistence.os.replace", side_effect=OSError("failed")):
                with self.assertRaises(OSError):
                    save_config(case.path, replacement)

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(
                sorted(path.name for path in case.path.glob(".foam-watcher.*")),
                [".foam-watcher.json"],
            )

    def test_target_mutated_during_fsync_is_preserved_and_temp_removed(self) -> None:
        with TemporaryCase() as case:
            save_config(case.path, default_config())
            target = case.path / ".foam-watcher.json"
            real_fsync = os.fsync

            def mutate_target(descriptor: int) -> None:
                target.write_text("{broken", encoding="utf-8")
                real_fsync(descriptor)

            with patch("watcher.persistence.os.fsync", side_effect=mutate_target):
                with self.assertRaises(UnsafeConfigPath):
                    save_config(case.path, default_config())

            self.assertEqual(target.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(
                sorted(path.name for path in case.path.glob(".foam-watcher.*")),
                [".foam-watcher.json"],
            )

    def test_target_created_during_fsync_is_not_overwritten(self) -> None:
        with TemporaryCase() as case:
            target = case.path / ".foam-watcher.json"
            real_fsync = os.fsync

            def create_target(descriptor: int) -> None:
                target.write_text("{broken", encoding="utf-8")
                real_fsync(descriptor)

            with patch("watcher.persistence.os.fsync", side_effect=create_target):
                with self.assertRaises(UnsafeConfigPath):
                    save_config(case.path, default_config())

            self.assertEqual(target.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(
                sorted(path.name for path in case.path.glob(".foam-watcher.*")),
                [".foam-watcher.json"],
            )

    def test_final_target_safety_check_can_refuse_a_symlink(self) -> None:
        with TemporaryCase() as case:
            save_config(case.path, default_config())
            target = case.path / ".foam-watcher.json"
            before = target.read_bytes()

            with patch(
                "watcher.persistence._reject_unsafe_target",
                side_effect=(None, UnsafeConfigPath("became a symlink")),
            ):
                with self.assertRaises(UnsafeConfigPath):
                    save_config(case.path, default_config())

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(
                sorted(path.name for path in case.path.glob(".foam-watcher.*")),
                [".foam-watcher.json"],
            )

    def test_writer_requests_user_only_tempfile_mode_on_all_platforms(self) -> None:
        with TemporaryCase() as case:
            real_chmod = os.chmod

            with patch("watcher.persistence.os.chmod", wraps=real_chmod) as chmod:
                save_config(case.path, default_config())

            self.assertEqual(chmod.call_count, 1)
            temporary_path, mode = chmod.call_args.args
            self.assertEqual(Path(temporary_path).parent, case.path)
            self.assertEqual(mode, 0o600)

    def test_oversized_serialized_config_preserves_existing_file(self) -> None:
        with TemporaryCase() as case:
            save_config(case.path, default_config())
            target = case.path / ".foam-watcher.json"
            before = target.read_bytes()
            oversized = WatcherConfig(
                version=1,
                selected_log="log.solver",
                selected_series=("x",),
                overrides={
                    "x": SeriesOverride(
                        label="L" * 100,
                        units="U" * 100,
                        minimum_cycles=3,
                    )
                },
                accepted_states=frozenset({"periodic"}),
            )

            with patch("watcher.persistence.MAX_CONFIG_BYTES", 256):
                with self.assertRaises(ConfigValidationError):
                    save_config(case.path, oversized)

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(
                sorted(path.name for path in case.path.glob(".foam-watcher.*")),
                [".foam-watcher.json"],
            )

    def test_serialized_config_at_byte_limit_round_trips(self) -> None:
        payload = {
            "version": 1,
            "selectedLog": None,
            "selectedSeries": [],
            "overrides": {},
            "acceptedStates": [
                "periodic",
                "plateau",
                "statistically_stationary",
            ],
        }
        expected = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

        with TemporaryCase() as case:
            with patch("watcher.persistence.MAX_CONFIG_BYTES", len(expected)):
                save_config(case.path, default_config())
                loaded = load_config(case.path)
                raw = (case.path / ".foam-watcher.json").read_bytes()

        self.assertEqual(raw, expected)
        self.assertEqual(loaded.config, default_config())
        self.assertIsNone(loaded.error)

    def test_serialized_config_one_byte_over_limit_is_refused(self) -> None:
        payload = {
            "version": 1,
            "selectedLog": None,
            "selectedSeries": [],
            "overrides": {},
            "acceptedStates": [
                "periodic",
                "plateau",
                "statistically_stationary",
            ],
        }
        serialized_size = len(
            (
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )

        with TemporaryCase() as case:
            target = case.path / ".foam-watcher.json"
            with patch(
                "watcher.persistence.MAX_CONFIG_BYTES",
                serialized_size - 1,
            ):
                with self.assertRaises(ConfigValidationError):
                    save_config(case.path, default_config())

            self.assertFalse(target.exists())
            self.assertFalse(tuple(case.path.glob(".foam-watcher.*")))

    def test_saved_json_is_finite_utf8_and_uses_closed_shape(self) -> None:
        with TemporaryCase() as case:
            config = WatcherConfig(
                version=1,
                selected_log="logs/솔버.log",
                selected_series=("x",),
                overrides={
                    "x": SeriesOverride(
                        label="양력",
                        units=None,
                        max_mean_shift_fraction=0.01,
                        minimum_cycles=4,
                    )
                },
                accepted_states=frozenset({"periodic"}),
            )

            save_config(case.path, config)
            raw = (case.path / ".foam-watcher.json").read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertEqual(
            payload,
            {
                "version": 1,
                "selectedLog": "logs/솔버.log",
                "selectedSeries": ["x"],
                "overrides": {
                    "x": {
                        "label": "양력",
                        "units": None,
                        "maxMeanShiftFraction": 0.01,
                        "minimumCycles": 4,
                    }
                },
                "acceptedStates": ["periodic"],
            },
        )
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)

    @skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_refuses_configuration_symlink(self) -> None:
        with TemporaryCase() as case, TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            (case.path / ".foam-watcher.json").symlink_to(outside)

            with self.assertRaises(UnsafeConfigPath):
                save_config(case.path, default_config())

            self.assertEqual(outside.read_text(encoding="utf-8"), "{}\n")

    @skipIf(os.name == "nt", "POSIX mode semantics")
    def test_configuration_mode_is_user_only(self) -> None:
        with TemporaryCase() as case:
            save_config(case.path, default_config())
            mode = (case.path / ".foam-watcher.json").stat().st_mode & 0o777

        self.assertEqual(mode, 0o600)
