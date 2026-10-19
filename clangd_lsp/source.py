"""Local URI normalization and conversion of LSP columns to model columns."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from index_model import Position

from .records import LspPosition, LspRange


def local_path(uri: str) -> Path:
    parts = urlsplit(uri)
    if (parts.scheme != "file" or parts.netloc not in ("", "localhost")
            or parts.query or parts.fragment):
        raise ValueError(f"Expected a local file URI: {uri!r}")
    path = Path(unquote(parts.path, encoding="utf-8", errors="strict"))
    if not path.is_absolute() or "\0" in str(path):
        raise ValueError(f"Invalid local file URI: {uri!r}")
    return path.resolve()


class SourceText:
    def __init__(self, data: bytes):
        self.text = data.decode("utf-8")
        self._lines = self.text.split("\n")

    def position(self, value: LspPosition, encoding: str) -> Position:
        if value.line < 0 or value.character < 0 or value.line >= len(self._lines):
            raise ValueError("LSP position is outside the source file")
        line = self._lines[value.line]
        codec, width = {"utf-8": ("utf-8", 1), "utf-16": ("utf-16-le", 2)}[encoding]
        encoded = line.encode(codec)
        offset = value.character * width
        if offset > len(encoded):
            raise ValueError("LSP position is outside the source line")
        # Decoding also rejects endpoints within a UTF-8 character or surrogate pair.
        prefix = encoded[:offset].decode(codec)
        return Position(value.line, len(prefix.encode("utf-8")))

    def range(self, value: LspRange, encoding: str) -> tuple[Position, Position]:
        start, end = self.position(value.start, encoding), self.position(value.end, encoding)
        if end < start:
            raise ValueError("LSP range ends before it starts")
        return start, end

    def lsp_position(self, value: Position, encoding: str) -> LspPosition:
        # Validate the byte endpoint, including multi-byte character boundaries.
        self.position(LspPosition(value.line, value.column), "utf-8")
        prefix = self._lines[value.line].encode("utf-8")[:value.column].decode("utf-8")
        column = len(prefix.encode("utf-16-le")) // 2 if encoding == "utf-16" else value.column
        return LspPosition(value.line, column)
