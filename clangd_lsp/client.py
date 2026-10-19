"""The clangd-specific protocol boundary. No model or persistence operations."""

import json
import math
from collections.abc import Sequence
from pathlib import Path

from .compilation_database import CompilationDatabase
from .records import (
    CollectionResult, CompilerDiagnostic, DecodeIssue, DocumentLink, DocumentSymbol,
    HierarchyCall, HierarchyItem, LspLocation, LspPosition, LspRange, SymbolInfo, WorkspaceSymbol,
)
from .static_index import StaticIndex, StaticIndexSource
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


def _symbol(value, children: tuple[DocumentSymbol, ...]) -> DocumentSymbol:
    value = _mapping(value)
    name = value.get("name")
    if not isinstance(name, str):
        raise ValueError("Document symbol has no name")
    # Flat SymbolInformation has no full extent: do not manufacture one.
    selection, extent = _range(value.get("selectionRange")), _range(value.get("range"))
    if not extent.start <= selection.start <= selection.end <= extent.end:
        raise ValueError("Document symbol selection is outside its extent")
    return DocumentSymbol(name, selection, extent, children)


def _symbols(values: list, path: str, issues: list[DecodeIssue]) -> tuple[DocumentSymbol, ...]:
    symbols = []
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        children = ()
        if isinstance(value, dict) and "children" in value:
            try:
                child_values = _array(value["children"])
            except ValueError as error:
                issues.append(DecodeIssue(f"{item_path}.children", str(error)))
            else:
                children = _symbols(child_values, f"{item_path}.children", issues)
        try:
            symbols.append(_symbol(value, children))
        except ValueError as error:
            issues.append(DecodeIssue(item_path, str(error)))
            # A child's own ranges remain usable even if its parent is malformed.
            symbols.extend(children)
    return tuple(symbols)


def _location(value) -> LspLocation:
    value = _mapping(value)
    uri = value.get("targetUri", value.get("uri"))
    if not isinstance(uri, str):
        raise ValueError("Location has no URI")
    extent = _range(value.get("targetSelectionRange", value.get("range")))
    if "targetUri" in value:
        target = _range(value.get("targetRange"))
        if not target.start <= extent.start <= extent.end <= target.end:
            raise ValueError("Target selection is outside target range")
    return LspLocation(uri, extent)


def _hierarchy_item(value) -> HierarchyItem:
    value = _mapping(value)
    name, kind = value.get("name"), value.get("kind")
    if not isinstance(name, str) or type(kind) is not int or not 1 <= kind <= 26:
        raise ValueError("Invalid hierarchy item name or kind")
    location = _location({"uri": value.get("uri"), "range": value.get("selectionRange")})
    extent = _range(value.get("range"))
    if not extent.start <= location.extent.start <= location.extent.end <= extent.end:
        raise ValueError("Hierarchy selection is outside its extent")
    data = value.get("data")
    identifier = data.get("symbolID") if isinstance(data, dict) else data
    if identifier is not None and not isinstance(identifier, str):
        identifier = None
    return HierarchyItem(name, kind, location, extent, json.dumps(data),
                         identifier.upper() if identifier else None)


def _collection(result, decoder) -> CollectionResult:
    items, issues = [], []
    for index, value in enumerate(_array([] if result is None else result)):
        try:
            items.append(decoder(value))
        except ValueError as error:
            issues.append(DecodeIssue(f"result[{index}]", str(error)))
    return CollectionResult(tuple(items), tuple(issues))


def _item_params(item: HierarchyItem) -> dict:
    def position(value):
        return {"line": value.line, "character": value.character}

    def extent(value):
        return {"start": position(value.start), "end": position(value.end)}

    return {"item": {"name": item.name, "kind": item.kind, "uri": item.location.uri,
                     "range": extent(item.extent), "selectionRange": extent(item.location.extent),
                     "data": json.loads(item.data_json)}}


class ClangdClient:
    def __init__(self, compile_commands_dir: Path, *, executable: str = "clangd",
                 timeout_seconds: float = 60, remove_compile_flags: Sequence[str] = ()):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be a positive finite number")
        self._database = CompilationDatabase(compile_commands_dir, remove_compile_flags)
        self._index = StaticIndex()
        self._executable = executable
        self._timeout = timeout_seconds
        self._rpc: JsonRpcProcess | None = None
        self._capabilities: dict = {}
        self._diagnostics: dict[str, tuple[CompilerDiagnostic, ...]] = {}
        self._open_versions: dict[str, int] = {}
        self._next_version = 0
        self.position_encoding = "utf-16"

    def start(self, project_root: Path, *, index_source: StaticIndexSource | None = None) -> None:
        if self._rpc is not None:
            raise RuntimeError("clangd is already started")
        self._diagnostics.clear()
        self._open_versions.clear()
        self._next_version = 0
        try:
            directory = self._database.prepare()
            command = [self._executable, f"--compile-commands-dir={directory}",
                       "--background-index=false", "--log=error"]
            if index_source is not None:
                path = self._index.prepare(index_source)
                command.extend([f"--index-file={path}", "--sync", "--limit-references=0", "--limit-results=0"])
            self._rpc = JsonRpcProcess(
                command, project_root, self._timeout, self._notification)
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
        if uri not in self._open_versions:
            return
        version = params.get("version")
        if version is not None:
            if type(version) is not int:
                raise LspSessionError("Invalid diagnostic document version")
            if version != self._open_versions[uri]:
                return
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
        if uri in self._open_versions:
            raise RuntimeError("Document is already open")
        self._diagnostics.pop(uri, None)
        self._next_version += 1
        self._open_versions[uri] = self._next_version
        suffix = Path(uri).suffix.lower()
        language = {".c": "c", ".m": "objective-c", ".mm": "objective-cpp"}.get(suffix, "cpp")
        self._rpc.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": language, "version": self._next_version, "text": text,
        }})
        # Initial diagnostics are a parsing barrier, including for broken files.
        self._rpc.wait_until(lambda: uri in self._diagnostics)
        return self._diagnostics[uri]

    def close_document(self, uri: str) -> None:
        self._open_versions.pop(uri, None)
        self._diagnostics.pop(uri, None)
        self._rpc.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def document_symbols(self, uri: str) -> CollectionResult[DocumentSymbol]:
        self._require("documentSymbolProvider", "textDocument/documentSymbol")
        result = self._rpc.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        issues = []
        symbols = _symbols(_array([] if result is None else result), "result", issues)
        return CollectionResult(symbols, tuple(issues))

    def document_links(self, uri: str) -> CollectionResult[DocumentLink]:
        self._require("documentLinkProvider", "textDocument/documentLink")
        result = self._rpc.request("textDocument/documentLink", {"textDocument": {"uri": uri}})
        links = []
        issues = []
        for index, value in enumerate(_array([] if result is None else result)):
            try:
                value = _mapping(value)
                target = value.get("target")
                if target is not None and not isinstance(target, str):
                    raise ValueError("Document link target is not a URI")
                links.append(DocumentLink(_range(value.get("range")), target))
            except ValueError as error:
                issues.append(DecodeIssue(f"result[{index}]", str(error)))
        return CollectionResult(tuple(links), tuple(issues))

    def symbol_info(self, uri: str, position: LspPosition) -> tuple[SymbolInfo, ...]:
        # clangd advertises no capability for this extension. Unsupported servers
        # return a recoverable method-not-found error.
        result = self._rpc.request("textDocument/symbolInfo", {
            "textDocument": {"uri": uri},
            "position": {"line": position.line, "character": position.character},
        })
        symbols = []
        for value in _array(result):
            value = _mapping(value)
            name, container, usr = (value.get(key) for key in ("name", "containerName", "usr"))
            if (not isinstance(name, str) or not isinstance(usr, str)
                    or (container is not None and not isinstance(container, str))):
                raise ValueError("Invalid symbol info strings")
            identifier = value.get("id")
            if identifier is not None and (not isinstance(identifier, str) or not identifier):
                raise ValueError("Invalid clangd symbol ID")
            declaration = _location(value["declarationRange"]) if value.get("declarationRange") is not None else None
            definition = _location(value["definitionRange"]) if value.get("definitionRange") is not None else None
            symbols.append(SymbolInfo(name, container, usr, identifier.upper() if identifier else None,
                                      declaration, definition))
        return tuple(symbols)

    @property
    def normalized_index_records(self) -> int:
        return self._index.normalized_records

    def workspace_symbols(self, query: str) -> CollectionResult[WorkspaceSymbol]:
        self._require("workspaceSymbolProvider", "workspace/symbol")

        def decode(value):
            value = _mapping(value)
            if not isinstance(value.get("name"), str):
                raise ValueError("Workspace symbol has no name")
            return WorkspaceSymbol(value["name"], _location(value.get("location")))

        return _collection(self._rpc.request("workspace/symbol", {"query": query}), decode)

    def _at(self, capability: str, method: str, uri: str, position: LspPosition, **extra):
        self._require(capability, method)
        return self._rpc.request(method, {
            "textDocument": {"uri": uri},
            "position": {"line": position.line, "character": position.character}, **extra,
        })

    def declarations(self, uri: str, position: LspPosition) -> CollectionResult[LspLocation]:
        result = self._at("declarationProvider", "textDocument/declaration", uri, position)
        return _collection([result] if isinstance(result, dict) else result, _location)

    def definitions(self, uri: str, position: LspPosition) -> CollectionResult[LspLocation]:
        result = self._at("definitionProvider", "textDocument/definition", uri, position)
        return _collection([result] if isinstance(result, dict) else result, _location)

    def references(self, uri: str, position: LspPosition) -> CollectionResult[LspLocation]:
        return _collection(self._at("referencesProvider", "textDocument/references", uri, position,
                                    context={"includeDeclaration": False}), _location)

    def prepare_calls(self, uri: str, position: LspPosition) -> CollectionResult[HierarchyItem]:
        return _collection(self._at("callHierarchyProvider", "textDocument/prepareCallHierarchy", uri, position),
                           _hierarchy_item)

    def _calls(self, method: str, item: HierarchyItem, field: str) -> CollectionResult[HierarchyCall]:
        self._require("callHierarchyProvider", method)
        result = self._rpc.request(method, _item_params(item))
        calls, issues = [], []
        for index, value in enumerate(_array([] if result is None else result)):
            try:
                value = _mapping(value)
                other = _hierarchy_item(value.get(field))
                ranges = _collection(_array(value.get("fromRanges")), _range)
                issues.extend(DecodeIssue(f"result[{index}].fromRanges.{i.path}", i.message) for i in ranges.issues)
                calls.append(HierarchyCall(other, ranges.items))
            except ValueError as error:
                issues.append(DecodeIssue(f"result[{index}]", str(error)))
        return CollectionResult(tuple(calls), tuple(issues))

    def incoming_calls(self, item: HierarchyItem) -> CollectionResult[HierarchyCall]:
        return self._calls("callHierarchy/incomingCalls", item, "from")

    def outgoing_calls(self, item: HierarchyItem) -> CollectionResult[HierarchyCall]:
        return self._calls("callHierarchy/outgoingCalls", item, "to")

    def prepare_types(self, uri: str, position: LspPosition) -> CollectionResult[HierarchyItem]:
        return _collection(self._at("typeHierarchyProvider", "textDocument/prepareTypeHierarchy", uri, position),
                           _hierarchy_item)

    def supertypes(self, item: HierarchyItem) -> CollectionResult[HierarchyItem]:
        self._require("typeHierarchyProvider", "typeHierarchy/supertypes")
        return _collection(self._rpc.request("typeHierarchy/supertypes", _item_params(item)), _hierarchy_item)

    def _require(self, capability: str, method: str) -> None:
        if self._capabilities.get(capability) in (None, False):
            raise LspRequestError(method, {"code": -32601, "message": "Capability unavailable"})

    def close(self) -> None:
        try:
            if self._rpc is not None:
                self._rpc.close()
        finally:
            self._rpc = None
            self._open_versions.clear()
            self._diagnostics.clear()
            self._database.close()
            self._index.close()
