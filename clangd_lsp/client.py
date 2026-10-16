"""The clangd-specific protocol boundary. No model or persistence operations."""

import math
from collections.abc import Sequence
from pathlib import Path

from .compilation_database import CompilationDatabase
from .records import CompilerDiagnostic, DocumentLink, DocumentSymbol, LspPosition, LspRange
from .transport import JsonRpcProcess, LspRequestError, LspSessionError


def _mapping(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Expected an LSP object")
    return value


def _array(value) -> list:
    if not isinstance(value, list):
        raise ValueError("Expected an LSP array")
    return value


def _position(value) -> LspPosition:
    value = _mapping(value)
    line, character = value.get("line"), value.get("character")
    if type(line) is not int or type(character) is not int or min(line, character) < 0:
        raise ValueError("Invalid LSP position")
    return LspPosition(line, character)


def _range(value) -> LspRange:
    value = _mapping(value)
    start, end = _position(value.get("start")), _position(value.get("end"))
    if end < start:
        raise ValueError("Invalid LSP range")
    return LspRange(start, end)


def _symbol(value) -> DocumentSymbol:
    value = _mapping(value)
    name = value.get("name")
    if not isinstance(name, str):
        raise ValueError("Document symbol has no name")
    # Flat SymbolInformation has no full extent: do not manufacture one.
    selection, extent = _range(value.get("selectionRange")), _range(value.get("range"))
    if not extent.start <= selection.start <= selection.end <= extent.end:
        raise ValueError("Document symbol selection is outside its extent")
    children = tuple(_symbol(child) for child in _array(value.get("children", [])))
    return DocumentSymbol(name, selection, extent, children)


class ClangdClient:
    def __init__(self, compile_commands_dir: Path, *, executable: str = "clangd",
                 timeout_seconds: float = 60, remove_compile_flags: Sequence[str] = ()):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._database = CompilationDatabase(compile_commands_dir, remove_compile_flags)
        self._executable = executable
        self._timeout = timeout_seconds
        self._rpc: JsonRpcProcess | None = None
        self._capabilities: dict = {}
        self._diagnostics: dict[str, tuple[CompilerDiagnostic, ...]] = {}
        self.position_encoding = "utf-16"

    def start(self, project_root: Path) -> None:
        if self._rpc is not None:
            raise RuntimeError("clangd is already started")
        self._diagnostics.clear()
        try:
            directory = self._database.prepare()
            self._rpc = JsonRpcProcess(
                [self._executable, f"--compile-commands-dir={directory}",
                 "--background-index=false", "--log=error"], project_root, self._timeout, self._notification)
            result = _mapping(self._rpc.request("initialize", {
                "processId": None,
                "rootUri": project_root.as_uri(),
                "capabilities": {
                    "general": {"positionEncodings": ["utf-8", "utf-16"]},
                    "offsetEncoding": ["utf-8", "utf-16"],
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"versionSupport": True},
                    },
                },
            }))
            self._capabilities = _mapping(result.get("capabilities"))
            self.position_encoding = self._capabilities.get(
                "positionEncoding", result.get("offsetEncoding", "utf-16"))
            if self.position_encoding not in ("utf-8", "utf-16"):
                raise LspSessionError(f"Unsupported position encoding: {self.position_encoding}")
            self._rpc.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def _notification(self, method: str, params: dict) -> None:
        if method != "textDocument/publishDiagnostics":
            return
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise LspSessionError("Diagnostics have no document URI")
        errors = []
        for diagnostic in _array(params.get("diagnostics")):
            diagnostic = _mapping(diagnostic)
            if diagnostic.get("severity", 1) == 1:
                message = diagnostic.get("message")
                if not isinstance(message, str):
                    raise LspSessionError("Diagnostic has no message")
                source_range = _range(diagnostic["range"]) if "range" in diagnostic else None
                errors.append(CompilerDiagnostic(message, source_range))
        self._diagnostics[uri] = tuple(errors)

    def open_document(self, uri: str, text: str) -> tuple[CompilerDiagnostic, ...]:
        self._diagnostics.pop(uri, None)
        suffix = Path(uri).suffix.lower()
        language = {".c": "c", ".m": "objective-c", ".mm": "objective-cpp"}.get(suffix, "cpp")
        self._rpc.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language, "version": 1, "text": text,
        }})
        # Initial diagnostics are a parsing barrier, including for broken files.
        self._rpc.wait_until(lambda: uri in self._diagnostics)
        return self._diagnostics[uri]

    def close_document(self, uri: str) -> None:
        self._rpc.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self._diagnostics.pop(uri, None)

    def document_symbols(self, uri: str) -> tuple[DocumentSymbol, ...]:
        self._require("documentSymbolProvider", "textDocument/documentSymbol")
        result = self._rpc.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return tuple(_symbol(value) for value in _array([] if result is None else result))

    def document_links(self, uri: str) -> tuple[DocumentLink, ...]:
        self._require("documentLinkProvider", "textDocument/documentLink")
        result = self._rpc.request("textDocument/documentLink", {"textDocument": {"uri": uri}})
        links = []
        for value in _array([] if result is None else result):
            value = _mapping(value)
            target = value.get("target")
            if target is not None and not isinstance(target, str):
                raise ValueError("Document link target is not a URI")
            links.append(DocumentLink(_range(value.get("range")), target))
        return tuple(links)

    def _require(self, capability: str, method: str) -> None:
        if self._capabilities.get(capability) in (None, False):
            raise LspRequestError(method, {"code": -32601, "message": "Capability unavailable"})

    def close(self) -> None:
        try:
            if self._rpc is not None:
                self._rpc.close()
        finally:
            self._rpc = None
            self._database.close()
