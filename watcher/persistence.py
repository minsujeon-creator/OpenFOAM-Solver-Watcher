"""Validated persistence for the watcher-owned per-case configuration.

On POSIX, target operations are pinned to an open case-directory descriptor.
All platforms compare target identity, metadata, and content immediately before
replacement. The standard library has no compare-and-swap rename, so the case
directory must remain under the launching user's control: a same-owner mutation
after that final comparison is outside the persistence guarantee.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import AbstractSet, BinaryIO, Iterator, Mapping

from watcher.models import ConfigLoadResult, SeriesOverride, WatcherConfig


CONFIG_NAME = ".foam-watcher.json"
MAX_SELECTED_SERIES = 1_000
MAX_SERIES_ID_LENGTH = 256
MAX_DISPLAY_STRING_LENGTH = 100
MAX_LOG_PATH_LENGTH = 4_096
MAX_MINIMUM_CYCLES = 1_000_000
# Four MiB is comfortably above the largest practical closed-schema payload
# while bounding parser work for a case-local file.
MAX_CONFIG_BYTES = 4 * 1_024 * 1_024

_TOP_LEVEL_KEYS = frozenset(
    {"version", "selectedLog", "selectedSeries", "overrides", "acceptedStates"}
)
_ACCEPTED_STATES = frozenset(
    {
        "evolving",
        "plateau",
        "statistically_stationary",
        "periodic",
        "indeterminate",
    }
)
_DEFAULT_ACCEPTED_STATES = frozenset(
    {"plateau", "statistically_stationary", "periodic"}
)
_OVERRIDE_FIELDS = {
    "label": "label",
    "units": "units",
    "maxMeanShiftFraction": "max_mean_shift_fraction",
    "maxMeanShiftStandardErrors": "max_mean_shift_standard_errors",
    "maxNormalizedSlope": "max_normalized_slope",
    "minimumEffectiveSamples": "minimum_effective_samples",
    "minimumCycles": "minimum_cycles",
    "maxPeriodVariationFraction": "max_period_variation_fraction",
    "maxAmplitudeVariationFraction": "max_amplitude_variation_fraction",
    "absoluteFloor": "absolute_floor",
    "staleAfterSeconds": "stale_after_seconds",
}
_POSITIVE_FLOAT_OVERRIDES = frozenset(
    {"minimumEffectiveSamples", "absoluteFloor"}
)
_NONNEGATIVE_FLOAT_OVERRIDES = frozenset(
    {
        "maxMeanShiftFraction",
        "maxMeanShiftStandardErrors",
        "maxNormalizedSlope",
        "maxPeriodVariationFraction",
        "maxAmplitudeVariationFraction",
        "staleAfterSeconds",
    }
)


@dataclass(frozen=True)
class _TargetSnapshot:
    metadata: tuple[int, ...] | None
    content: bytes | None

    @property
    def exists(self) -> bool:
        return self.metadata is not None


_ABSENT_TARGET = _TargetSnapshot(metadata=None, content=None)


class ConfigValidationError(ValueError):
    """The per-case configuration does not match the supported schema."""


class UnsafeConfigPath(OSError):
    """The fixed configuration target is unsafe to read or replace."""


def default_config() -> WatcherConfig:
    return WatcherConfig(
        version=1,
        selected_log=None,
        selected_series=(),
        overrides=MappingProxyType({}),
        accepted_states=_DEFAULT_ACCEPTED_STATES,
    )


def load_config(case_dir: Path) -> ConfigLoadResult:
    try:
        case_path = _resolve_case(case_dir)
        target = case_path / CONFIG_NAME
        with _pinned_case_directory(case_path) as directory_fd:
            _reject_unsafe_target(target)
            snapshot = _target_snapshot(case_path, directory_fd)
            if not snapshot.exists:
                return ConfigLoadResult(config=default_config(), error=None)
            return ConfigLoadResult(
                config=_config_from_snapshot(snapshot, target.name),
                error=None,
            )
    except (ConfigValidationError, UnsafeConfigPath, OSError) as error:
        return ConfigLoadResult(config=default_config(), error=str(error))


def validate_config_payload(
    payload: object,
    known_series: AbstractSet[str],
) -> WatcherConfig:
    return _validate_config_payload(payload, known_series)


def save_config(case_dir: Path, config: WatcherConfig) -> None:
    case_path = _resolve_case(case_dir)
    target = case_path / CONFIG_NAME
    with _pinned_case_directory(case_path) as directory_fd:
        _reject_unsafe_target(target)
        original = _target_snapshot(case_path, directory_fd)
        if original.exists:
            _config_from_snapshot(original, target.name)

        payload = _config_payload(config)
        validated = _validate_config_payload(
            payload,
            frozenset(config.selected_series) | frozenset(config.overrides),
        )
        payload = _config_payload(validated)
        serialized = _serialize_payload(payload)

        descriptor = -1
        temporary_path: Path | None = None
        temporary_identity: tuple[int, int] | None = None
        temporary_is_pinned = False
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".foam-watcher.",
                dir=case_path,
            )
            temporary_path = Path(temporary_name)
            temporary_identity = _file_identity(os.fstat(descriptor))
            os.chmod(temporary_path, 0o600)
            temporary_is_pinned = _verify_temporary_location(
                descriptor,
                temporary_path,
                directory_fd,
            )
            with os.fdopen(
                descriptor,
                "wb",
            ) as stream:
                descriptor = -1
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())

            _reject_unsafe_target(target)
            current = _target_snapshot(case_path, directory_fd)
            if current != original:
                raise UnsafeConfigPath(
                    "Configuration target changed while preferences were being saved"
                )
            _replace_temporary(temporary_path, target, directory_fd)
            temporary_path = None
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None and temporary_identity is not None:
                _remove_exact_temporary(
                    temporary_path,
                    temporary_identity,
                    directory_fd if temporary_is_pinned else None,
                )
            raise


def _resolve_case(case_dir: Path) -> Path:
    try:
        case_path = case_dir.resolve(strict=True)
    except OSError as error:
        raise UnsafeConfigPath(f"Cannot resolve case directory: {error}") from error
    if not case_path.is_dir():
        raise UnsafeConfigPath(f"Case path is not a directory: {case_path}")
    return case_path


def _reject_unsafe_target(target: Path) -> None:
    if target.is_symlink():
        raise UnsafeConfigPath(f"Refusing configuration symlink: {target}")
    if target.exists() and not target.is_file():
        raise UnsafeConfigPath(f"Configuration target is not a regular file: {target}")


@contextlib.contextmanager
def _pinned_case_directory(case_path: Path) -> Iterator[int | None]:
    if os.name != "posix":
        yield None
        return

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directory_fd = -1
    try:
        before = os.stat(case_path, follow_symlinks=False)
        directory_fd = os.open(case_path, flags)
        opened = os.fstat(directory_fd)
        after = os.stat(case_path, follow_symlinks=False)
    except OSError as error:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise UnsafeConfigPath(f"Cannot pin case directory: {error}") from error

    if (
        _file_identity(before) != _file_identity(opened)
        or _file_identity(opened) != _file_identity(after)
        or not stat.S_ISDIR(opened.st_mode)
    ):
        os.close(directory_fd)
        raise UnsafeConfigPath("Case directory changed while it was being opened")

    try:
        yield directory_fd
    finally:
        os.close(directory_fd)


def _target_snapshot(case_path: Path, directory_fd: int | None) -> _TargetSnapshot:
    target = case_path / CONFIG_NAME
    try:
        named_before = _target_lstat(target, directory_fd)
    except FileNotFoundError:
        return _ABSENT_TARGET

    if stat.S_ISLNK(named_before.st_mode):
        raise UnsafeConfigPath(f"Refusing configuration symlink: {target}")
    if not stat.S_ISREG(named_before.st_mode):
        raise UnsafeConfigPath(f"Configuration target is not a regular file: {target}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if directory_fd is not None:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if directory_fd is None:
            descriptor = os.open(target, flags)
        else:
            descriptor = os.open(CONFIG_NAME, flags, dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeConfigPath(
                f"Configuration target is not a regular file: {target}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = _read_bounded(stream, target.name)
            after_read = os.fstat(stream.fileno())
    except UnsafeConfigPath:
        raise
    except OSError as error:
        raise UnsafeConfigPath(f"Cannot inspect configuration target: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        named_after = _target_lstat(target, directory_fd)
    except FileNotFoundError as error:
        raise UnsafeConfigPath(
            "Configuration target changed while it was being inspected"
        ) from error
    if (
        _stat_signature(named_before) != _stat_signature(named_after)
        or _stat_signature(opened) != _stat_signature(after_read)
        or _cross_view_signature(named_before) != _cross_view_signature(opened)
        or _cross_view_signature(after_read) != _cross_view_signature(named_after)
    ):
        raise UnsafeConfigPath(
            "Configuration target changed while it was being inspected"
        )
    return _TargetSnapshot(metadata=_stat_signature(named_after), content=content)


def _target_lstat(target: Path, directory_fd: int | None) -> os.stat_result:
    if directory_fd is None:
        return os.stat(target, follow_symlinks=False)
    return os.stat(CONFIG_NAME, dir_fd=directory_fd, follow_symlinks=False)


def _read_bounded(stream: BinaryIO, name: str) -> bytes:
    content = stream.read(MAX_CONFIG_BYTES + 1)
    if len(content) > MAX_CONFIG_BYTES:
        raise ConfigValidationError(
            f"Invalid existing configuration {name}: exceeds the "
            f"{MAX_CONFIG_BYTES}-byte limit"
        )
    return content


def _serialize_payload(payload: Mapping[str, object]) -> bytes:
    try:
        serialized = (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    except (
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
    ) as error:
        raise ConfigValidationError(
            f"Configuration cannot be serialized safely: {error}"
        ) from error
    if len(serialized) > MAX_CONFIG_BYTES:
        raise ConfigValidationError(
            f"Serialized configuration exceeds the {MAX_CONFIG_BYTES}-byte limit"
        )
    return serialized


def _config_from_snapshot(snapshot: _TargetSnapshot, name: str) -> WatcherConfig:
    assert snapshot.content is not None
    try:
        text = snapshot.content.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except ConfigValidationError:
        raise
    except (
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        OverflowError,
    ) as error:
        raise ConfigValidationError(
            f"Invalid existing configuration {name}: {error}"
        ) from error
    return _validate_config_payload(payload, known_series=None)


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        getattr(value, "st_uid", 0),
        getattr(value, "st_gid", 0),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _cross_view_signature(value: os.stat_result) -> tuple[int, ...]:
    return _stat_signature(value)[:-1]


def _verify_temporary_location(
    descriptor: int,
    temporary_path: Path,
    directory_fd: int | None,
) -> bool:
    if directory_fd is None:
        return False
    opened = _file_identity(os.fstat(descriptor))
    try:
        named = os.stat(
            temporary_path.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise UnsafeConfigPath(
            "Temporary configuration file is not in the pinned case directory"
        ) from error
    if opened != _file_identity(named) or not stat.S_ISREG(named.st_mode):
        raise UnsafeConfigPath(
            "Temporary configuration file is not in the pinned case directory"
        )
    return True


def _replace_temporary(
    temporary_path: Path,
    target: Path,
    directory_fd: int | None,
) -> None:
    if directory_fd is None:
        os.replace(temporary_path, target)
        return
    os.replace(
        temporary_path.name,
        CONFIG_NAME,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _remove_exact_temporary(
    temporary_path: Path,
    expected_identity: tuple[int, int],
    directory_fd: int | None,
) -> None:
    try:
        if directory_fd is None:
            current = os.stat(temporary_path, follow_symlinks=False)
        else:
            current = os.stat(
                temporary_path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        if _file_identity(current) != expected_identity:
            return
        if directory_fd is None:
            temporary_path.unlink()
        else:
            os.unlink(temporary_path.name, dir_fd=directory_fd)
    except OSError:
        pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigValidationError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ConfigValidationError(f"Non-finite JSON number is not allowed: {value}")


def _validate_config_payload(
    payload: object,
    known_series: AbstractSet[str] | None,
) -> WatcherConfig:
    root = _object(payload, "configuration")
    _reject_unknown_keys(root, _TOP_LEVEL_KEYS, "configuration")

    version = root.get("version")
    if type(version) is not int or version != 1:
        raise ConfigValidationError("Configuration version must be the integer 1")

    selected_log = _selected_log(root.get("selectedLog"))
    selected_series = _selected_series(
        root.get("selectedSeries", []),
        known_series,
    )
    overrides = _overrides(root.get("overrides", {}), known_series)
    accepted_states = _accepted_states(
        root.get("acceptedStates", sorted(_DEFAULT_ACCEPTED_STATES))
    )

    return WatcherConfig(
        version=version,
        selected_log=selected_log,
        selected_series=selected_series,
        overrides=MappingProxyType(overrides),
        accepted_states=accepted_states,
    )


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ConfigValidationError(f"{label} must be a JSON object")
    return value


def _reject_unknown_keys(
    value: Mapping[str, object],
    allowed: AbstractSet[str],
    label: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ConfigValidationError(f"Unknown {label} key(s): {names}")


def _selected_log(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ConfigValidationError("selectedLog must be a string or null")
    if not value or len(value) > MAX_LOG_PATH_LENGTH or "\x00" in value:
        raise ConfigValidationError("selectedLog has an invalid length or character")

    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ConfigValidationError("selectedLog must be a relative non-parent path")
    return value


def _selected_series(
    value: object,
    known_series: AbstractSet[str] | None,
) -> tuple[str, ...]:
    if type(value) is not list:
        raise ConfigValidationError("selectedSeries must be a JSON array")
    if len(value) > MAX_SELECTED_SERIES:
        raise ConfigValidationError(
            f"selectedSeries cannot contain more than {MAX_SELECTED_SERIES} entries"
        )

    result: list[str] = []
    seen: set[str] = set()
    for series_id in value:
        checked_id = _series_id(series_id)
        if checked_id in seen:
            raise ConfigValidationError(f"Duplicate selected series ID: {checked_id}")
        _require_known_series(checked_id, known_series)
        seen.add(checked_id)
        result.append(checked_id)
    return tuple(result)


def _overrides(
    value: object,
    known_series: AbstractSet[str] | None,
) -> dict[str, SeriesOverride]:
    override_payload = _object(value, "overrides")
    if len(override_payload) > MAX_SELECTED_SERIES:
        raise ConfigValidationError(
            f"overrides cannot contain more than {MAX_SELECTED_SERIES} entries"
        )

    result: dict[str, SeriesOverride] = {}
    for raw_series_id, raw_override in override_payload.items():
        series_id = _series_id(raw_series_id)
        _require_known_series(series_id, known_series)
        override = _object(raw_override, f"override for {series_id}")
        _reject_unknown_keys(
            override,
            frozenset(_OVERRIDE_FIELDS),
            f"override for {series_id}",
        )
        values: dict[str, object] = {}
        for json_name, attribute_name in _OVERRIDE_FIELDS.items():
            if json_name not in override:
                continue
            raw_value = override[json_name]
            if json_name in {"label", "units"}:
                values[attribute_name] = _display_string(raw_value, json_name)
            elif json_name == "minimumCycles":
                values[attribute_name] = _positive_integer(raw_value, json_name)
            else:
                values[attribute_name] = _finite_float(raw_value, json_name)
                if json_name in _POSITIVE_FLOAT_OVERRIDES and values[attribute_name] <= 0:
                    raise ConfigValidationError(f"{json_name} must be greater than zero")
                if (
                    json_name in _NONNEGATIVE_FLOAT_OVERRIDES
                    and values[attribute_name] < 0
                ):
                    raise ConfigValidationError(f"{json_name} cannot be negative")
        result[series_id] = SeriesOverride(**values)
    return result


def _series_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_SERIES_ID_LENGTH
        or "\x00" in value
    ):
        raise ConfigValidationError("Series IDs must be non-empty bounded strings")
    return value


def _require_known_series(
    series_id: str,
    known_series: AbstractSet[str] | None,
) -> None:
    if known_series is not None and series_id not in known_series:
        raise ConfigValidationError(f"Unknown series ID: {series_id}")


def _display_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > MAX_DISPLAY_STRING_LENGTH:
        raise ConfigValidationError(
            f"{label} must be null or a string of at most "
            f"{MAX_DISPLAY_STRING_LENGTH} characters"
        )
    return value


def _positive_integer(value: object, label: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > MAX_MINIMUM_CYCLES
    ):
        raise ConfigValidationError(
            f"{label} must be an integer from 1 through {MAX_MINIMUM_CYCLES}"
        )
    return value


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ConfigValidationError(f"{label} must be a finite number") from error
    if not math.isfinite(result):
        raise ConfigValidationError(f"{label} must be a finite number")
    return result


def _accepted_states(value: object) -> frozenset[str]:
    if type(value) is not list:
        raise ConfigValidationError("acceptedStates must be a JSON array")
    result: set[str] = set()
    for state in value:
        if type(state) is not str or state not in _ACCEPTED_STATES:
            raise ConfigValidationError(f"Invalid accepted state: {state!r}")
        if state in result:
            raise ConfigValidationError(f"Duplicate accepted state: {state}")
        result.add(state)
    return frozenset(result)


def _config_payload(config: WatcherConfig) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for series_id, override in config.overrides.items():
        values: dict[str, object] = {
            "label": override.label,
            "units": override.units,
        }
        for json_name, attribute_name in _OVERRIDE_FIELDS.items():
            if json_name in {"label", "units"}:
                continue
            value = getattr(override, attribute_name)
            if value is not None:
                values[json_name] = value
        overrides[series_id] = values

    return {
        "version": config.version,
        "selectedLog": config.selected_log,
        "selectedSeries": list(config.selected_series),
        "overrides": overrides,
        "acceptedStates": sorted(config.accepted_states),
    }
