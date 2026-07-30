from __future__ import annotations

import hashlib
import math
import shlex
import time
from collections import Counter
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from watcher.models import CandidateInfo, CaseInspection, FunctionObjectSpec, ParsedTable, SeriesData


_STALE_SECONDS = 300
_VECTOR_COMPONENTS = ("x", "y", "z")
_TENSOR_COMPONENTS = ("xx", "xy", "xz", "yx", "yy", "yz", "zx", "zy", "zz")


def parse_numeric_table(path: Path) -> ParsedTable:
    """Parse finite numeric rows from a post-processing table without modifying it."""
    resolved = path.resolve()
    headers: tuple[str, ...] = ()
    probes = 0
    notices: list[str] = []
    raw_rows: list[tuple[float, tuple[float, ...], tuple[int, ...]]] = []
    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        modified_ns = resolved.stat().st_mtime_ns
    except OSError as error:
        return ParsedTable(resolved, (), (), (), 0, (f"Could not read table: {error}",), 0, ())

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            tokens = _header_tokens(stripped[1:].strip())
            if tokens and tokens[0].lower() == "time":
                headers = tokens
            elif tokens and tokens[0].lower() == "probe":
                probes += 1
            continue
        parsed = _row_values(stripped)
        if parsed is not None:
            raw_rows.append(parsed)

    expected = _expected_groups(headers, raw_rows, probes)
    rows = [(time_value, values) for time_value, values, groups in raw_rows if groups == expected]
    if len(rows) != len(raw_rows):
        notices.append("Ignored incomplete or inconsistent table rows.")
    return ParsedTable(
        resolved,
        headers,
        tuple(item[0] for item in rows),
        tuple(item[1] for item in rows),
        modified_ns,
        tuple(notices),
        probes,
        expected,
    )


def discover_series(inspection: CaseInspection, now: float | None = None) -> Mapping[str, SeriesData]:
    """Discover declared and generic post-processing tables contained in a case."""
    case_dir = inspection.case_dir.resolve()
    root = case_dir / "postProcessing"
    grouped: dict[str, list[tuple[float, Path, str, str | None, FunctionObjectSpec | None]]] = {}
    if not root.is_dir():
        return MappingProxyType({})

    for path in _table_paths(root, case_dir):
        source, function_name, region, spec, start = _source_details(path, root, inspection.function_objects)
        grouped.setdefault(source, []).append((start, path, function_name, region, spec))

    result: dict[str, SeriesData] = {}
    reference_ns = time.time_ns() if now is None else int(now * 1_000_000_000)
    for source, files in grouped.items():
        ordered = sorted(files, key=lambda item: (item[0], item[1].as_posix()))
        tables = [(item, parse_numeric_table(item[1])) for item in ordered]
        if not tables:
            continue
        _, _, function_name, region, spec = ordered[-1]
        canonical_shape = _canonical_shape(table for _, table in tables)
        template = next(
            (table for _, table in tables if table.values and table.column_widths == canonical_shape),
            tables[-1][1],
        )
        compatible = [table for _, table in tables if table.values and table.column_widths == canonical_shape]
        merged: dict[float, tuple[float, ...]] = {}
        notices: list[str] = []
        modified_ns = 0
        for _, table in tables:
            if table not in compatible:
                notices.append("Ignored incompatible restart table.")
                continue
            merged.update(zip(table.times, table.values))
            notices.extend(table.notices)
            modified_ns = max(modified_ns, table.modified_ns)
        if not merged:
            continue
        times = tuple(sorted(merged))
        rows = tuple(merged[item] for item in times)
        function_type = spec.type_name if spec is not None else "generic_table"
        for field, operation, component, column in _descriptors(template, function_type, spec):
            if column >= len(rows[0]):
                continue
            values = (
                tuple(math.sqrt(sum(value * value for value in row[column : column + 3])) for row in rows)
                if component == "magnitude"
                else tuple(row[column] for row in rows)
            )
            stale = reference_ns - modified_ns > _STALE_SECONDS * 1_000_000_000
            candidate = _candidate(function_name, function_type, field, operation, values, stale)
            identity = "|".join(
                (function_name, function_type, region or "", field, operation or "", component or "", source)
            )
            series_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            label = " ".join(part for part in (function_name, operation, field, component) if part)
            result[series_id] = SeriesData(
                series_id=series_id,
                label=label,
                function_name=function_name,
                function_type=function_type,
                source_relative=source,
                region=region,
                field=field,
                operation=operation,
                component=component,
                units=None,
                times=times,
                values=values,
                modified_ns=modified_ns,
                stale=stale,
                candidate=candidate,
                notices=tuple(notices),
            )
    return MappingProxyType(dict(sorted(result.items())))


def _header_tokens(text: str) -> tuple[str, ...]:
    try:
        raw_tokens = shlex.split(text)
    except ValueError:
        raw_tokens = text.split()
    tokens: list[str] = []
    pending: list[str] = []
    depth = 0
    for token in raw_tokens:
        pending.append(token)
        depth += token.count("(") - token.count(")")
        if depth <= 0:
            tokens.append(" ".join(pending))
            pending = []
            depth = 0
    if pending:
        tokens.extend(pending)
    return tuple(tokens)


def _row_values(line: str) -> tuple[float, tuple[float, ...], tuple[int, ...]] | None:
    tokens = _top_level_tokens(line)
    if len(tokens) < 2:
        return None
    parsed_tokens: list[tuple[float, ...]] = []
    for token in tokens:
        numbers: list[float] = []
        inner = token[1:-1] if token.startswith("(") and token.endswith(")") else token
        for value in inner.replace("(", " ").replace(")", " ").split():
            try:
                number = float(value)
            except ValueError:
                return None
            if not math.isfinite(number):
                return None
            numbers.append(number)
        parsed_tokens.append(tuple(numbers))
    if len(parsed_tokens[0]) != 1 or not parsed_tokens[1:]:
        return None
    values = tuple(number for group in parsed_tokens[1:] for number in group)
    groups = tuple(len(group) for group in parsed_tokens[1:])
    return parsed_tokens[0][0], values, groups


def _top_level_tokens(line: str) -> tuple[str, ...]:
    tokens: list[str] = []
    token: list[str] = []
    depth = 0
    for char in line:
        if char.isspace() and depth == 0:
            if token:
                tokens.append("".join(token))
                token = []
            continue
        token.append(char)
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return ()
    if depth or not token:
        return ()
    tokens.append("".join(token))
    return tuple(tokens)


def _expected_groups(
    headers: tuple[str, ...],
    rows: list[tuple[float, tuple[float, ...], tuple[int, ...]]],
    probe_count: int,
) -> tuple[int, ...]:
    shapes = Counter(groups for _, _, groups in rows)
    header_count = len(headers) - 1
    if header_count:
        required_groups = probe_count if probe_count else header_count
        shapes = Counter({shape: count for shape, count in shapes.items() if len(shape) == required_groups})
    return shapes.most_common(1)[0][0] if shapes else ()


def _canonical_shape(tables: Iterable[ParsedTable]) -> tuple[int, ...]:
    shapes = [table.column_widths for table in tables if table.values]
    return max(shapes, key=lambda shape: (sum(shape), shape), default=())


def _table_paths(root: Path, case_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            resolved.relative_to(case_dir)
            paths.append(resolved)
    except (OSError, ValueError):
        pass
    return tuple(paths)


def _source_details(
    path: Path,
    root: Path,
    specs: Mapping[str, FunctionObjectSpec],
) -> tuple[str, str, str | None, FunctionObjectSpec | None, float]:
    parts = list(path.relative_to(root).parts)
    time_index = next((index for index in range(len(parts) - 2, -1, -1) if _number(parts[index]) is not None), None)
    if time_index is None:
        time_index = len(parts) - 1
        start = 0.0
        source_parts = ["postProcessing", *parts]
    else:
        start = _number(parts[time_index]) or 0.0
        source_parts = ["postProcessing", *parts[:time_index], *parts[time_index + 1 :]]
    before = parts[:time_index]
    matched_index = next((index for index, part in enumerate(before) if part in specs), None)
    if matched_index is not None:
        function_name = before[matched_index]
        spec = specs[function_name]
        region = spec.region or (before[matched_index - 1] if matched_index else None)
    else:
        function_name = before[-1] if before else path.stem
        spec = None
        region = before[-2] if len(before) > 1 else None
    return "/".join(source_parts), function_name, region, spec, start


def _number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _descriptors(
    table: ParsedTable,
    function_type: str,
    spec: FunctionObjectSpec | None,
) -> tuple[tuple[str, str | None, str | None, int], ...]:
    width = len(table.values[0]) if table.values else 0
    header_values = table.headers[1:]
    if header_values and len(header_values) == len(table.column_widths):
        descriptors: list[tuple[str, str | None, str | None, int]] = []
        offset = 0
        for value, group_width in zip(header_values, table.column_widths):
            field, operation = _field_operation(value, spec)
            descriptors.extend(_group_descriptors(field, operation, value, function_type, offset, group_width))
            offset += group_width
        return tuple(descriptors)
    base = header_values[0] if header_values else (spec.fields[0] if spec and spec.fields else "value")
    field, operation = _field_operation(base, spec)
    if table.probe_count and table.probe_count == width:
        return tuple((field, operation, f"probe {index}", index) for index in range(width))
    return _group_descriptors(field, operation, base, function_type, 0, width)


def _group_descriptors(
    field: str,
    operation: str | None,
    header: str,
    function_type: str,
    offset: int,
    width: int,
) -> tuple[tuple[str, str | None, str | None, int], ...]:
    if width == 1:
        return ((field, operation, None, offset),)
    if width == 3:
        descriptors = [(field, operation, component, offset + index) for index, component in enumerate(_VECTOR_COMPONENTS)]
        descriptors.append((field, operation, "magnitude", offset))
        return tuple(descriptors)
    kind = "moment" if "moment" in header.lower() else "force"
    if width == 9 and ("force" in function_type.lower() or kind in header.lower()):
        labels = ("pressure", "viscous", "porous")
        return tuple(
            (f"{kind} {labels[group]}", None, component, offset + group * 3 + component_offset)
            for group in range(3)
            for component_offset, component in enumerate(_VECTOR_COMPONENTS)
        )
    if width == 9:
        return tuple((field, operation, component, offset + index) for index, component in enumerate(_TENSOR_COMPONENTS))
    return tuple((f"{field}[{index}]", operation, None, offset + index) for index in range(width))


def _field_operation(value: str, spec: FunctionObjectSpec | None) -> tuple[str, str | None]:
    if "(" in value and value.endswith(")"):
        operation, field = value[:-1].split("(", 1)
        return field, operation
    if spec is not None and len(spec.fields) == 1 and value.lower() in {"value", "values"}:
        return spec.fields[0], spec.operation
    return value, spec.operation if spec is not None else None


def _candidate(
    function_name: str,
    function_type: str,
    field: str,
    operation: str | None,
    values: tuple[float, ...],
    stale: bool,
) -> CandidateInfo:
    text = " ".join((function_name, function_type, field, operation or "")).lower()
    score = 0
    explanations: list[str] = []
    if any(marker in text for marker in ("force", "flow", "flux", "pressuredifference", "pressure_difference", "power")):
        score += 80
        explanations.append("Force, flow, flux, pressure-difference, or power output (+80).")
    elif any(marker in text for marker in ("average", "integral", "heattransfer", "species", "phase", "interface")):
        score += 70
        explanations.append("Average, integral, heat-transfer, species, phase, or interface output (+70).")
    elif "probe" in text:
        score += 60
        explanations.append("Named probe output (+60).")
    if len(values) >= 50:
        score += 20
        explanations.append("At least 50 finite samples (+20).")
    elif len(values) >= 20:
        score += 10
        explanations.append("At least 20 finite samples (+10).")
    if any(marker in text for marker in ("solverinfo", "executiontime", "clocktime", "courant", "diagnostic")):
        score -= 60
        explanations.append("Numerical-only diagnostic (-60).")
    if len(values) >= 20 and len(set(values)) == 1:
        score -= 20
        explanations.append("Constant series after 20 samples (-20).")
    if stale:
        score -= 20
        explanations.append("Stale post-processing output (-20).")
    confidence = "high" if score >= 70 else "medium" if score >= 40 else "low"
    explanation = " ".join(explanations) or "No candidate-ranking factor applied."
    return CandidateInfo(score, confidence, score >= 70, explanation)
