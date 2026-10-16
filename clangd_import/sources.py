"""Local source access and conversion from clangd columns to UTF-8 byte columns."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

from index_model import Position

from .records import ExportPosition


class LocalSourceReader:
    def read(self, uri: str) -> bytes:
        parts = urlsplit(uri)
        if (parts.scheme != "file" or parts.netloc not in ("", "localhost")
                or parts.query or parts.fragment):
            raise ValueError(f"Expected a local file URI: {uri!r}")
        path = Path(unquote(parts.path, encoding="utf-8", errors="strict"))
        if not path.is_absolute():
            raise ValueError(f"Expected an absolute file URI: {uri!r}")
        return path.read_bytes()


def utf8_position(lines: tuple[bytes, ...], position: ExportPosition) -> Position:
    """Convert an endpoint, rejecting overflow, out-of-range and split surrogates.

    Lines are split on LF only; CR and tabs remain bytes, as the model requires.
    LLVM's packed positions use 20 bits for lines and 12 for columns. Saturated
    coordinates cannot be distinguished from overflow and are not guessed.
    """
    if not 0 <= position.line < (1 << 20) - 1 or not 0 <= position.column < (1 << 12) - 1:
        raise ValueError("Invalid or saturated clangd coordinate")
    if position.line >= len(lines):
        raise ValueError("Line is outside the current source file")
    line = lines[position.line]
    if line.isascii():
        if position.column > len(line):
            raise ValueError("Column is outside the current source line")
        return Position(position.line, position.column)
    utf16 = line.decode("utf-8").encode("utf-16-le")
    end = position.column * 2
    if end > len(utf16):
        raise ValueError("Column is outside the current source line")
    prefix = utf16[:end].decode("utf-16-le")
    return Position(position.line, len(prefix.encode("utf-8")))
