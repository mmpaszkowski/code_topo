"""Read the tagged UTF-8 document stream emitted by clangd-indexer.

The caller owns the binary stream. Memory use is proportional to the largest
document, unless the caller retains results. Unknown fields remain in raw_yaml.
Only YAML nodes are composed: no YAML object constructors are executed.
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
import re
from typing import BinaryIO, NoReturn

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .records import (
    Diagnostic, IncludeHeader, SourceRecord, ExportLocation, ExportPosition, ClangdReference,
    ClangdReferences, ClangdRelation, SourceDocument, ClangdSymbol, SymbolInfo,
)


_LOADER = getattr(yaml, "CBaseLoader", yaml.BaseLoader)
_DOCUMENT_START = re.compile(rb"^---(?:[ \t\r\n]|$)")
_BROKEN_KIND = re.compile(rb"(?m)^SymInfo:(\r?\n)  Kind: +Lang: +(C)(?=\r?$)")
_SYMBOL_ID = re.compile(r"[0-9A-Fa-f]{16}\Z")
_INTEGER = re.compile(r"[0-9]+\Z")
_ZERO_ID = "0000000000000000"


class IndexParseError(ValueError):
    """A syntax or validation error with an absolute input line and field path."""

    def __init__(self, document_ordinal: int, source_line: int,
                 field_path: str, message: str):
        self.document_ordinal = document_ordinal
        self.source_line = source_line
        self.field_path = field_path
        self.message = message
        super().__init__(
            f"document {document_ordinal}, line {source_line}, {field_path}: {message}"
        )


def _fail(source: SourceDocument, node: Node, path: str, message: str) -> NoReturn:
    raise IndexParseError(
        source.document_ordinal, source.start_line + node.start_mark.line,
        path, message,
    )


def _check_tree(root: Node, source: SourceDocument) -> None:
    """Reject duplicate keys and cycles, including inside unknown fields.

    An iterative walk avoids recursion limits. Shared alias nodes are checked once;
    recursive aliases are invalid for these tree-shaped index records.
    """
    active: set[int] = set()
    checked: set[int] = set()
    pending = [(root, "", False)]
    while pending:
        node, path, exiting = pending.pop()
        identity = id(node)
        if exiting:
            active.remove(identity)
            checked.add(identity)
            continue
        if identity in active:
            _fail(source, node, path or "$", "recursive YAML alias")
        if identity in checked:
            continue
        active.add(identity)
        pending.append((node, path, True))
        children = []
        if isinstance(node, MappingNode):
            keys: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode):
                    _fail(source, key, path or "$", "mapping keys must be scalars")
                child_path = f"{path}.{key.value}" if path else key.value
                if key.value in keys:
                    _fail(source, key, child_path, "duplicate mapping key")
                keys.add(key.value)
                children.append((value, child_path, False))
        elif isinstance(node, SequenceNode):
            children = [(value, f"{path}[{i}]", False)
                        for i, value in enumerate(node.value)]
        pending.extend(reversed(children))


class _Fields:
    """Small typed access boundary over a YAML mapping and its source marks."""

    def __init__(self, source: SourceDocument, node: Node, path: str = ""):
        if not isinstance(node, MappingNode):
            _fail(source, node, path or "$", "expected a mapping")
        self.source = source
        self.node = node
        self.path = path
        self.values = {key.value: value for key, value in node.value}

    def field_path(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def required(self, key: str) -> Node:
        if key not in self.values:
            _fail(self.source, self.node, self.field_path(key), "missing required field")
        return self.values[key]

    def text(self, key: str) -> str:
        node = self.required(key)
        if not isinstance(node, ScalarNode):
            _fail(self.source, node, self.field_path(key), "expected a scalar string")
        return node.value

    def integer(self, key: str) -> int:
        value = self.text(key)
        if not _INTEGER.fullmatch(value):
            _fail(self.source, self.required(key), self.field_path(key),
                  "expected a nonnegative decimal integer")
        try:
            return int(value, 10)
        except ValueError:
            _fail(self.source, self.required(key), self.field_path(key), "integer is too large")

    def symbol_id(self, key: str, *, allow_zero: bool = False) -> str:
        value = self.text(key)
        if not _SYMBOL_ID.fullmatch(value):
            _fail(self.source, self.required(key), self.field_path(key),
                  "expected a 16-character hexadecimal symbol ID")
        if value == _ZERO_ID and not allow_zero:
            _fail(self.source, self.required(key), self.field_path(key), "zero symbol ID is only valid for a container")
        return value.upper()

    def object(self, key: str) -> "_Fields":
        return _Fields(self.source, self.required(key), self.field_path(key))

    def objects(self, key: str) -> Iterator["_Fields"]:
        node = self.required(key)
        if not isinstance(node, SequenceNode):
            _fail(self.source, node, self.field_path(key), "expected a sequence")
        for i, item in enumerate(node.value):
            yield _Fields(self.source, item, f"{self.field_path(key)}[{i}]")


def _location(fields: _Fields) -> ExportLocation:
    start = fields.object("Start")
    end = fields.object("End")
    result = ExportLocation(
        fields.text("FileURI"),
        ExportPosition(start.integer("Line"), start.integer("Column")),
        ExportPosition(end.integer("Line"), end.integer("Column")),
    )
    if (result.end.line, result.end.column) < (result.start.line, result.start.column):
        _fail(fields.source, end.node, end.path, "location ends before it starts")
    return result


def _symbol(fields: _Fields, recovered: bool) -> ClangdSymbol:
    info = fields.object("SymInfo")
    return ClangdSymbol(
        symbol_id=fields.symbol_id("ID"), name=fields.text("Name"),
        scope=fields.text("Scope"),
        sym_info=SymbolInfo(None if recovered else info.text("Kind"), info.text("Lang")),
        canonical_declaration=_location(fields.object("CanonicalDeclaration")),
        definition=_location(fields.object("Definition")) if "Definition" in fields.values else None,
        flags=fields.integer("Flags"), signature=fields.text("Signature"),
        template_specialization_args=fields.text("TemplateSpecializationArgs"),
        completion_snippet_suffix=fields.text("CompletionSnippetSuffix"),
        documentation=fields.text("Documentation"), return_type=fields.text("ReturnType"),
        type=fields.text("Type"),
        exported_reference_count=fields.integer("References") if "References" in fields.values else None,
        include_headers=tuple(
            IncludeHeader(item.text("Header"), item.integer("References"))
            for item in fields.objects("IncludeHeaders")
        ) if "IncludeHeaders" in fields.values else (),
    )


def _references(fields: _Fields) -> ClangdReferences:
    references = []
    for item in fields.objects("References"):
        container = item.object("Container").symbol_id("ID", allow_zero=True)
        references.append(ClangdReference(
            item.integer("Kind"), _location(item.object("Location")),
            None if container == _ZERO_ID else container,
        ))
    return ClangdReferences(fields.symbol_id("ID"), tuple(references))


def _compose(raw: bytes, source: SourceDocument) -> Node | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        line = source.start_line + raw[:error.start].count(b"\n")
        raise IndexParseError(source.document_ordinal, line, "$", "expected UTF-8 input") from error
    try:
        return yaml.compose(text, Loader=_LOADER)
    except yaml.reader.ReaderError as error:
        # LibYAML reports byte offsets; the Python reader reports character offsets.
        if hasattr(yaml, "CBaseLoader") and issubclass(_LOADER, yaml.CBaseLoader):
            preceding_lines = raw[:error.position].count(b"\n")
        else:
            preceding_lines = text[:error.position].count("\n")
        raise IndexParseError(
            source.document_ordinal, source.start_line + preceding_lines, "$",
            f"invalid YAML character: {error.reason}",
        ) from error


def _parse_record(raw: bytes, ordinal: int, start_line: int,
                  recover_known_errors: bool) -> SourceRecord:
    source = SourceDocument(ordinal, "", start_line, raw, "parsed")
    diagnostic = None
    try:
        try:
            root = _compose(raw, source)
        except yaml.YAMLError:
            matches = list(_BROKEN_KIND.finditer(raw)) if recover_known_errors else []
            if len(matches) != 1:
                raise
            match = matches[0]
            # Remove only the missing Kind field, keeping line numbers unchanged.
            fixed = raw[:match.start()] + b"SymInfo:" + match[1] + b"  Lang: " + match[2] + raw[match.end():]
            root = _compose(fixed, source)
            if root is None or root.tag != "!Symbol":
                raise
            diagnostic = Diagnostic(
                "warning", start_line + raw[:match.start()].count(b"\n") + 1,
                "SymInfo.Kind", "Kind and Lang share a malformed field line",
                "Keep Lang; set missing Kind to None; retain original bytes",
            )
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = start_line + (mark.line if mark else 0)
        raise IndexParseError(ordinal, line, "$", getattr(error, "problem", None) or str(error)) from error
    if root is None or root.tag.startswith("tag:yaml.org,2002:"):
        raise IndexParseError(ordinal, start_line, "$", "expected a tagged clangd-indexer document")
    source = replace(source, yaml_tag=root.tag, parse_status="recovered" if diagnostic else "parsed")
    _check_tree(root, source)
    if root.tag not in ("!Symbol", "!Refs", "!Relations"):
        raise IndexParseError(ordinal, start_line, "$", f"Unsupported document tag {root.tag}")
    fields = _Fields(source, root)
    if root.tag == "!Symbol":
        payload = _symbol(fields, diagnostic is not None)
    elif root.tag == "!Refs":
        payload = _references(fields)
    else:
        payload = ClangdRelation(fields.object("Subject").symbol_id("ID"), fields.integer("Predicate"),
                           fields.object("Object").symbol_id("ID"))
    return SourceRecord(source, payload, (diagnostic,) if diagnostic else ())


def iter_index_records(stream: BinaryIO, *, recover_known_errors: bool = True) -> Iterator[SourceRecord]:
    """Yield validated records in file order; never close the caller's stream.

    Document ordinals are zero-based; source lines are one-based. Known Kind/Lang
    corruption is recovered by default. Other invalid input raises IndexParseError;
    already yielded records do not imply the remaining file is valid. A storage
    adapter must wrap consumption in a transaction if atomic replacement is needed.
    """
    buffer = bytearray()
    started = False
    ordinal = 0
    start_line = 1
    for line_number, line in enumerate(stream, 1):
        if not isinstance(line, bytes):
            raise TypeError("iter_index_records requires a binary stream")
        marker = line.removeprefix(b"\xef\xbb\xbf") if line_number == 1 else line
        if _DOCUMENT_START.match(marker):
            if started:
                yield _parse_record(bytes(buffer), ordinal, start_line, recover_known_errors)
                ordinal += 1
                buffer.clear()
                start_line = line_number
            started = True
        buffer.extend(line)
    if buffer:
        yield _parse_record(bytes(buffer), ordinal, start_line, recover_known_errors)
    else:
        raise IndexParseError(0, 1, "$", "expected a tagged clangd-indexer document")


class ClangdYamlReader:
    """Own the YAML file; each read() opens a fresh, streaming iterator."""

    def __init__(self, path: Path, *, recover_known_errors: bool = True) -> None:
        self.path = Path(path).absolute()
        self._recover_known_errors = recover_known_errors

    def read(self) -> Iterator[SourceRecord]:
        with self.path.open("rb") as stream:
            yield from iter_index_records(stream, recover_known_errors=self._recover_known_errors)
