from __future__ import annotations

import re
import time
from pathlib import Path

from watcher.models import CaseInspection, LogCandidate, LogChunk


_TAIL_BYTES = 64 * 1024
_RECENT_NS = 120 * 1_000_000_000
_SOLVER_RECORD = re.compile(
    r"(?:\bTime\s*=|\bSolving for\b|\bInitial residual\b|\bFinal residual\b)",
    re.IGNORECASE,
)
_PREPROCESSING_UTILITY = re.compile(
    r"\b(?:blockMesh|checkMesh|decomposePar|reconstructPar|foamToVTK|renumberMesh|snappyHexMesh)\b",
    re.IGNORECASE,
)


def discover_logs(
    inspection: CaseInspection,
    explicit: Path | None,
    saved_relative: str | None,
) -> tuple[LogCandidate, ...]:
    """Return contained solver-log candidates in explainable rank order."""
    case_dir = inspection.case_dir.resolve()
    selections: dict[Path, list[str]] = {}

    for path in _log_like_paths(case_dir):
        selections.setdefault(path, [])

    if explicit is not None:
        path = _contained_file(explicit, case_dir)
        if path is not None:
            selections.setdefault(path, []).append("explicit selection")

    if saved_relative is not None:
        saved = Path(saved_relative)
        if not saved.is_absolute():
            path = _contained_file(case_dir / saved, case_dir)
            if path is not None:
                selections.setdefault(path, []).append("saved selection")

    candidates: list[LogCandidate] = []
    for path, reasons in selections.items():
        try:
            candidates.append(_candidate(path, case_dir, inspection.application, reasons))
        except OSError:
            continue
    return tuple(sorted(candidates, key=lambda item: (-item.score, -item.modified_ns, item.relative_path)))


def _log_like_paths(case_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    try:
        files = case_dir.rglob("*")
        for path in files:
            if not _is_log_like(path.name):
                continue
            resolved = _contained_file(path, case_dir)
            if resolved is not None:
                paths.append(resolved)
    except OSError:
        pass
    return tuple(paths)


def _is_log_like(name: str) -> bool:
    lowered = name.lower()
    return lowered == "log" or lowered.startswith("log.") or lowered.endswith(".log")


def _contained_file(path: Path, case_dir: Path) -> Path | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(case_dir)
        if not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _candidate(
    path: Path,
    case_dir: Path,
    application: str | None,
    selection_reasons: list[str],
) -> LogCandidate:
    stat = path.stat()
    relative_path = path.relative_to(case_dir).as_posix()
    score = 0
    reasons = list(selection_reasons)
    filename = path.name

    if "explicit selection" in selection_reasons:
        score += 1000
    if "saved selection" in selection_reasons:
        score += 900
    if application and application.lower() in filename.lower():
        score += 200
        reasons.append("application-name match")

    prefix = _read_range(path, 0, min(stat.st_size, _TAIL_BYTES))
    tail_start = max(0, stat.st_size - _TAIL_BYTES)
    tail = _read_range(path, tail_start, stat.st_size - tail_start)
    prefix_text = prefix.decode("utf-8", errors="replace")
    tail_text = tail.decode("utf-8", errors="replace")
    if "openfoam" in prefix_text.lower():
        score += 50
        reasons.append("OpenFOAM banner")
    if _SOLVER_RECORD.search(tail_text):
        score += 50
        reasons.append("solver record")
    if stat.st_mtime_ns >= time.time_ns() - _RECENT_NS:
        score += 25
        reasons.append("recently modified")
    if path.parent == case_dir:
        score += 10
        reasons.append("case-root file")
    if _PREPROCESSING_UTILITY.search(filename) or _PREPROCESSING_UTILITY.search(prefix_text):
        score -= 300
        reasons.append("preprocessing utility")
    if stat.st_size == 0:
        score -= 25
        reasons.append("zero-length file")

    return LogCandidate(
        path=path,
        relative_path=relative_path,
        score=score,
        reasons=tuple(reasons),
        modified_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def _read_range(path: Path, offset: int, length: int) -> bytes:
    if length <= 0:
        return b""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)
    except OSError:
        return b""


class IncrementalLogReader:
    """Read complete UTF-8 log lines while recognizing solver restarts."""

    def __init__(self, case_dir: Path, path: Path) -> None:
        self._case_dir = case_dir.resolve()
        try:
            self.path = path.resolve()
            self.path.relative_to(self._case_dir)
        except ValueError as error:
            raise ValueError("log path must be inside the resolved case directory") from error
        self._offset = 0
        self._pending = b""
        self._identity: tuple[int, int] | None = None
        self._previous_size = 0
        self._segment = 0

    def read(self) -> LogChunk:
        path = _contained_file(self.path, self._case_dir)
        if path is None:
            return LogChunk(self.path, self._segment, False, (), 0, 0)

        try:
            stat = path.stat()
        except OSError:
            return LogChunk(self.path, self._segment, False, (), 0, 0)
        identity = _file_identity(stat)
        reset = self._is_reset(identity, stat.st_size)
        if reset:
            self._segment += 1
            self._offset = 0
            self._pending = b""

        data = _read_range(path, self._offset, max(0, stat.st_size - self._offset))
        self._offset += len(data)
        self._identity = identity
        self._previous_size = stat.st_size
        lines, self._pending = _complete_lines(self._pending + data)
        return LogChunk(
            path=path,
            segment=self._segment,
            reset=reset,
            lines=lines,
            file_size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
        )

    def _is_reset(self, identity: tuple[int, int] | None, size: int) -> bool:
        identity_changed = (
            self._identity is not None and identity is not None and identity != self._identity
        )
        return identity_changed or size < self._previous_size


def _file_identity(stat: object) -> tuple[int, int] | None:
    device = getattr(stat, "st_dev", None)
    inode = getattr(stat, "st_ino", None)
    if isinstance(device, int) and isinstance(inode, int):
        return device, inode
    return None


def _complete_lines(data: bytes) -> tuple[tuple[str, ...], bytes]:
    parts = data.split(b"\n")
    pending = parts.pop()
    lines = tuple(part.rstrip(b"\r").decode("utf-8", errors="replace") for part in parts)
    return lines, pending
