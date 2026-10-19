"""Small immutable values returned by the clangd adapter."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from index_model import CollectionStage, Position, SymbolId


@dataclass(frozen=True, slots=True, order=True)
class LspPosition:
    line: int
    character: int


@dataclass(frozen=True, slots=True)
class LspRange:
    start: LspPosition
    end: LspPosition


@dataclass(frozen=True, slots=True)
class DocumentSymbol:
    name: str
    selection: LspRange
    extent: LspRange
    children: tuple["DocumentSymbol", ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentLink:
    extent: LspRange
    target: str | None


@dataclass(frozen=True, slots=True)
class DecodeIssue:
    path: str
    message: str


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class CollectionResult(Generic[_T]):
    items: tuple[_T, ...] = ()
    issues: tuple[DecodeIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    name: str
    container_name: str | None
    usr: str
    id: str | None = None
    declaration: "LspLocation | None" = None
    definition: "LspLocation | None" = None


@dataclass(frozen=True, slots=True)
class LspLocation:
    uri: str
    extent: LspRange


@dataclass(frozen=True, slots=True)
class WorkspaceSymbol:
    name: str
    location: LspLocation


@dataclass(frozen=True, slots=True)
class HierarchyItem:
    name: str
    kind: int
    location: LspLocation
    extent: LspRange
    # JSON preserves opaque continuation data without exposing mutable mappings.
    data_json: str
    symbol_id: str | None = None


@dataclass(frozen=True, slots=True)
class HierarchyCall:
    other: HierarchyItem
    ranges: tuple[LspRange, ...]


@dataclass(frozen=True, slots=True)
class CompilerDiagnostic:
    message: str
    source_range: LspRange | None = None


@dataclass(frozen=True, slots=True)
class SourceRange:
    """Zero-based lines, UTF-8 byte columns, exclusive end."""

    start: Position
    end: Position


class DiagnosticReason(StrEnum):
    MISSING_OUTLINE = "missing_outline"
    AMBIGUOUS_MATCH = "ambiguous_match"
    UNMATCHED_OUTLINE = "unmatched_outline"
    INVALID_RANGE = "invalid_range"
    SOURCE_FAILURE = "source_failure"
    REQUEST_FAILURE = "request_failure"
    INVALID_RESULT = "invalid_result"
    COMPILER_ERROR = "compiler_error"
    UNRESOLVED_INCLUDE = "unresolved_include"
    DECODE_ERROR = "decode_error"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    CONFLICT = "conflict"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True, slots=True)
class StructuralReport:
    symbols_queried: int = 0
    locations_filled: int = 0
    references_added: int = 0
    references_enriched: int = 0
    call_candidates: int = 0
    inheritance_added: int = 0
    hierarchy_queries: int = 0
    hierarchy_partial: int = 0
    queries_failed: int = 0


@dataclass(frozen=True, slots=True)
class Diagnostic:
    uri: str
    stage: CollectionStage | None
    message: str
    reason: DiagnosticReason
    symbol_id: SymbolId | None = None
    source_range: SourceRange | None = None
    candidates: tuple[SymbolId, ...] = ()


@dataclass(frozen=True, slots=True)
class EnrichmentReport:
    files_processed: int
    files_skipped: int
    files_excluded: int
    ranges_added: int
    includes_added: int
    occurrences_matched: int
    occurrences_total: int
    diagnostics: tuple[Diagnostic, ...]
    occurrences_identity_resolved: int = 0
    structural: StructuralReport | None = None
    normalized_index_records: int = 0
    elapsed_seconds: float = 0
