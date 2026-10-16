"""Small immutable values returned by the clangd adapter."""

from dataclasses import dataclass
from enum import StrEnum

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
