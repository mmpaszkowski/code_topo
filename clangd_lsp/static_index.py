"""Session-owned static clangd index prepared from an already imported export."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory


@dataclass(frozen=True, slots=True)
class StaticIndexSource:
    path: Path
    sha256: str


_BROKEN_KIND = re.compile(rb"(?m)^SymInfo:(\r?\n)  Kind: +Lang: +(C)(?=\r?$)")


class StaticIndex:
    def __init__(self):
        self._temporary: TemporaryDirectory | None = None
        self.normalized_records = 0

    def prepare(self, source: StaticIndexSource) -> Path:
        if self._temporary is not None:
            raise RuntimeError("Static index is already prepared")
        raw = source.path.read_bytes()
        if sha256(raw).hexdigest() != source.sha256:
            raise ValueError("Static index export differs from the imported SHA-256")
        prepared, self.normalized_records = _BROKEN_KIND.subn(
            rb"SymInfo:\1  Kind: Unknown\1  Lang: \2", raw)
        # Always copy: clangd must read the bytes whose digest we checked, even if
        # the original export is replaced after preparation.
        self._temporary = TemporaryDirectory(prefix="indexer-clangd-index-")
        path = Path(self._temporary.name) / "index.yaml"
        try:
            path.write_bytes(prepared)
        except BaseException:
            self.close()
            raise
        return path

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
