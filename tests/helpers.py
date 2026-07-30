from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory


class TemporaryCase:
    def __enter__(self) -> "TemporaryCase":
        self._directory = TemporaryDirectory()
        self.path = Path(self._directory.name)
        (self.path / "system").mkdir()
        (self.path / "constant").mkdir()
        return self

    def __exit__(self, *args: object) -> None:
        self._directory.cleanup()

    def write(self, relative_path: str, content: str) -> Path:
        path = self.path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def append(self, relative_path: str, content: str) -> Path:
        path = self.path / relative_path
        with path.open("a", encoding="utf-8") as stream:
            stream.write(content)
        return path

    def touch(self, relative_path: str, seconds_after: int = 0) -> Path:
        path = self.path / relative_path
        modified_ns = path.stat().st_mtime_ns + seconds_after * 1_000_000_000
        os.utime(path, ns=(modified_ns, modified_ns))
        return path

    def mkdir(self, relative_path: str) -> Path:
        path = self.path / relative_path
        path.mkdir(parents=True, exist_ok=True)
        return path
