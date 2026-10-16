"""Prepare clangd compilation inputs without changing the project's database."""

from collections.abc import Sequence
import json
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory


class CompilationDatabase:
    def __init__(self, directory: Path, remove_flags: Sequence[str] = ()):
        self._directory = Path(directory).resolve()
        self._remove_flags = frozenset(remove_flags)
        self._temporary: TemporaryDirectory | None = None

    def prepare(self) -> Path:
        """Return the original directory or a session-owned filtered copy."""
        if self._temporary is not None:
            raise RuntimeError("Compilation database is already prepared")
        path = self._directory / "compile_commands.json"
        if not path.is_file():
            raise FileNotFoundError(f"Compilation database does not exist: {path}")
        if not self._remove_flags:
            return self._directory
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError(f"{path}: expected an array of compile commands")
        filtered = []
        for number, entry in enumerate(entries):
            try:
                filtered.append(self._filter(entry))
            except ValueError as error:
                raise ValueError(f"{path}: entry {number}: {error}") from error
        self._temporary = TemporaryDirectory(prefix="indexer-clangd-commands-")
        directory = Path(self._temporary.name)
        try:
            (directory / "compile_commands.json").write_text(
                json.dumps(filtered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except BaseException:
            self.close()
            raise
        return directory

    def _filter(self, entry: object) -> dict:
        if not isinstance(entry, dict):
            raise ValueError("expected a compile command object")
        for field in ("directory", "file"):
            if not isinstance(entry.get(field), str) or not entry[field] or "\0" in entry[field]:
                raise ValueError(f"{field}: expected a nonempty path")
        if "arguments" in entry:
            arguments = entry["arguments"]
        else:
            command = entry.get("command")
            if not isinstance(command, str):
                raise ValueError("expected arguments or a command string")
            # Tokenize POSIX quoting only. No shell expansion or execution occurs.
            arguments = shlex.split(command, posix=True)
        if (not isinstance(arguments, list) or not arguments
                or any(not isinstance(arg, str) or "\0" in arg for arg in arguments)
                or not arguments[0]):
            raise ValueError("arguments: expected a nonempty array of argument strings")
        result = dict(entry)
        # Relative files and response files still resolve from the original working directory.
        result["directory"] = str((self._directory / entry["directory"]).resolve())
        result["arguments"] = [arguments[0], *(arg for arg in arguments[1:] if arg not in self._remove_flags)]
        result.pop("command", None)
        return result

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
