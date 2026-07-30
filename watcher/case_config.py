from __future__ import annotations

import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Iterator

from watcher.models import CaseInspection, FunctionObjectSpec, ResidualTarget


_TOKEN_PATTERN = re.compile(r'"(?:\\.|[^"\\])*"|[{}();]|#[A-Za-z][A-Za-z0-9_]*|[^\s{}();]+')
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_PROCESSOR_PATTERN = re.compile(r"processor\d+$")


def _strip_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    in_quote = False
    while index < len(text):
        char = text[index]
        if in_quote and char == "\\" and index + 1 < len(text):
            result.extend((char, text[index + 1]))
            index += 2
            continue
        if char == '"':
            in_quote = not in_quote
            result.append(char)
            index += 1
        elif not in_quote and text.startswith("//", index):
            newline = text.find("\n", index)
            if newline == -1:
                break
            result.append("\n")
            index = newline + 1
        elif not in_quote and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                break
            result.append(" ")
            index = end + 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(_strip_comments(text)):
        token = match.group(0)
        if token.startswith('"') and token.endswith('"'):
            token = token[1:-1]
        tokens.append(token)
    return tokens


def _as_value(token: str) -> object:
    if _NUMBER_PATTERN.match(token):
        return float(token)
    if token == "yes":
        return True
    if token == "no":
        return False
    return token


class _Parser:
    def __init__(self, path: Path, case_dir: Path, visited: set[Path], notices: list[str]) -> None:
        self.path = path
        self.case_dir = case_dir
        self.visited = visited
        self.notices = notices
        self.tokens: list[str] = []
        self.index = 0

    def parse(self) -> dict[str, object]:
        try:
            self.tokens = _tokens(self.path.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            self.notices.append(f"Could not read {self.path}: {error}")
            return {}
        return self._dictionary(None)

    def _dictionary(self, closing: str | None) -> dict[str, object]:
        result: dict[str, object] = {}
        while self.index < len(self.tokens):
            token = self._next()
            if token == closing:
                return result
            if token == ";":
                continue
            if token.startswith("#"):
                self._directive(token, result)
                continue
            if token in ("{", "}", "(", ")"):
                self.notices.append(f"Unexpected token {token!r} in {self.path}")
                continue
            if self.index >= len(self.tokens):
                self.notices.append(f"Missing value for {token!r} in {self.path}")
                break
            next_token = self._next()
            if next_token == "{":
                result[token] = self._dictionary("}")
            elif next_token == "(":
                result[token] = self._list()
                self._consume_semicolon()
            else:
                result[token] = _as_value(next_token)
                self._consume_to_semicolon()
        if closing is not None:
            self.notices.append(f"Unclosed {closing!r} in {self.path}")
        return result

    def _list(self) -> tuple[object, ...]:
        values: list[object] = []
        while self.index < len(self.tokens):
            token = self._next()
            if token == ")":
                return tuple(values)
            if token == "(":
                values.append(self._list())
            elif token == "{":
                values.append(self._dictionary("}"))
            else:
                values.append(_as_value(token))
        self.notices.append(f"Unclosed ')' in {self.path}")
        return tuple(values)

    def _directive(self, directive: str, result: dict[str, object]) -> None:
        if directive in ("#include", "#includeIfPresent") and self.index < len(self.tokens):
            target = self._next()
            self._consume_semicolon()
            included = self._include(target, directive == "#includeIfPresent")
            result.update(included)
            return
        self.notices.append(f"Skipped unevaluated directive {directive} in {self.path}")
        if self.index < len(self.tokens) and self.tokens[self.index] == "{":
            self.index += 1
            self._skip_braces()

    def _include(self, target: str, optional: bool) -> dict[str, object]:
        candidate = (self.path.parent / target).resolve()
        if not _allowed_include(candidate, self.case_dir):
            self.notices.append(f"Skipped include outside allowed directories: {target}")
            return {}
        if not candidate.exists():
            if not optional:
                self.notices.append(f"Included file does not exist: {candidate}")
            return {}
        if candidate in self.visited:
            self.notices.append(f"Skipped cyclic include: {candidate}")
            return {}
        self.visited.add(candidate)
        return _Parser(candidate, self.case_dir, self.visited, self.notices).parse()

    def _skip_braces(self) -> None:
        depth = 1
        while self.index < len(self.tokens) and depth:
            token = self._next()
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
        if depth:
            self.notices.append(f"Unclosed directive block in {self.path}")

    def _consume_to_semicolon(self) -> None:
        while self.index < len(self.tokens) and self.tokens[self.index] != ";":
            if self.tokens[self.index] in ("{", "}"):
                return
            self.index += 1
        self._consume_semicolon()

    def _consume_semicolon(self) -> None:
        if self.index < len(self.tokens) and self.tokens[self.index] == ";":
            self.index += 1

    def _next(self) -> str:
        token = self.tokens[self.index]
        self.index += 1
        return token


def _allowed_include(candidate: Path, case_dir: Path) -> bool:
    if _is_relative_to(candidate, case_dir):
        return True
    project_dir = os.environ.get("WM_PROJECT_DIR")
    if not project_dir:
        return False
    return _is_relative_to(candidate, Path(project_dir).resolve())


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def parse_foam_file(path: Path, case_dir: Path) -> tuple[dict[str, object], tuple[str, ...]]:
    """Read a Foam dictionary without expanding variables or running directives."""
    resolved_case = case_dir.resolve()
    resolved_path = path.resolve()
    notices: list[str] = []
    parsed = _Parser(resolved_path, resolved_case, {resolved_path}, notices).parse()
    return parsed, tuple(notices)


def inspect_case(case_dir: Path) -> CaseInspection:
    case_dir = case_dir.resolve()
    control, control_notices = parse_foam_file(case_dir / "system" / "controlDict", case_dir)
    solution, solution_notices = parse_foam_file(case_dir / "system" / "fvSolution", case_dir)
    schemes, schemes_notices = parse_foam_file(case_dir / "system" / "fvSchemes", case_dir)
    regions, region_notices = parse_foam_file(case_dir / "constant" / "regionProperties", case_dir)
    notices = list(control_notices + solution_notices + schemes_notices + region_notices)

    mode, evidence = _mode(solution, schemes)
    function_objects = _function_objects(control)
    residual_targets = _residual_targets(solution)
    version = _version(case_dir / "system" / "controlDict")
    processor_count = sum(
        child.is_dir() and bool(_PROCESSOR_PATTERN.fullmatch(child.name))
        for child in case_dir.iterdir()
    )
    multi_region = _region_count(regions.get("regions")) > 1
    return CaseInspection(
        case_dir=case_dir,
        application=_string(control.get("application")),
        openfoam_version=version,
        mode=mode,
        mode_confidence="high" if mode != "unknown" else "low",
        mode_evidence=evidence,
        start_time=_number(control.get("startTime")),
        end_time=_number(control.get("endTime")),
        delta_t=_number(control.get("deltaT")),
        adjust_time_step=_boolean(control.get("adjustTimeStep")),
        max_co=_number(control.get("maxCo")),
        max_delta_t=_number(control.get("maxDeltaT")),
        parallel_ranks=processor_count,
        multi_region=multi_region,
        residual_targets=residual_targets,
        function_objects=MappingProxyType(function_objects),
        notices=tuple(notices),
    )


def _mode(solution: dict[str, object], schemes: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    if isinstance(solution.get("PIMPLE"), dict):
        return "transient_pimple", ("fvSolution:PIMPLE",)
    if isinstance(solution.get("PISO"), dict):
        return "transient_piso", ("fvSolution:PISO",)
    if isinstance(solution.get("SIMPLE"), dict):
        flattened = " ".join(_strings(schemes)) + " " + " ".join(_strings(solution))
        if any(marker in flattened for marker in ("Euler", "backward", "CrankNicolson", "localEuler", "rDeltaT")):
            return "pseudo_transient", ("fvSolution:SIMPLE", "transient time marker")
        return "steady_simple", ("fvSolution:SIMPLE",)
    return "unknown", ()


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, tuple):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _residual_targets(solution: dict[str, object]) -> tuple[ResidualTarget, ...]:
    targets: list[ResidualTarget] = []
    for algorithm in ("PIMPLE", "PISO", "SIMPLE"):
        section = solution.get(algorithm)
        if not isinstance(section, dict):
            continue
        residual_control = section.get("residualControl")
        if not isinstance(residual_control, dict):
            continue
        for pattern, threshold in residual_control.items():
            number = _number(threshold)
            if number is None and isinstance(threshold, dict):
                number = _number(threshold.get("tolerance"))
            if number is not None:
                targets.append(ResidualTarget(pattern, number))
    return tuple(targets)


def _function_objects(control: dict[str, object]) -> dict[str, FunctionObjectSpec]:
    functions = control.get("functions")
    if not isinstance(functions, dict):
        return {}
    result: dict[str, FunctionObjectSpec] = {}
    for name, configuration in functions.items():
        if not isinstance(configuration, dict):
            continue
        fields = configuration.get("fields", configuration.get("field", ()))
        if isinstance(fields, str):
            fields_tuple = (fields,)
        elif isinstance(fields, tuple):
            fields_tuple = tuple(item for item in fields if isinstance(item, str))
        else:
            fields_tuple = ()
        result[name] = FunctionObjectSpec(
            name=name,
            type_name=_string(configuration.get("type")) or "",
            fields=fields_tuple,
            region=_string(configuration.get("region")),
            operation=_string(configuration.get("operation")),
        )
    return result


def _region_count(value: object) -> int:
    if not isinstance(value, tuple):
        return 0
    count = 0
    index = 0
    while index < len(value):
        if isinstance(value[index], str):
            count += 1
            index += 1
            if index < len(value) and isinstance(value[index], tuple):
                index += 1
        else:
            index += 1
    return count


def _version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"(?:Version|OpenFOAM)[^0-9]*(v?\d+(?:\.\d+)*)", text, re.IGNORECASE)
    return match.group(1) if match else None


def _number(value: object) -> float | None:
    return value if isinstance(value, float) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
