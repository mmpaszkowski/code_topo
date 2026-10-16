"""Immutable clangd export records and the report accompanying an imported model."""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from index_model import ReferenceId, SymbolId


@dataclass(frozen=True, slots=True)
class ExportPosition:
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class ExportLocation:
    file_uri: str
    start: ExportPosition
    end: ExportPosition


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    kind: str | None
    language: str


@dataclass(frozen=True, slots=True)
class IncludeHeader:
    header: str
    exported_reference_count: int


@dataclass(frozen=True, slots=True)
class ClangdSymbol:
    symbol_id: str
    name: str
    scope: str
    sym_info: SymbolInfo
    canonical_declaration: ExportLocation
    definition: ExportLocation | None
    flags: int
    signature: str
    template_specialization_args: str
    completion_snippet_suffix: str
    documentation: str
    return_type: str
    type: str
    exported_reference_count: int | None
    include_headers: tuple[IncludeHeader, ...]


@dataclass(frozen=True, slots=True)
class ClangdReference:
    kind: int
    location: ExportLocation
    container_symbol_id: str | None


@dataclass(frozen=True, slots=True)
class ClangdReferences:
    target_symbol_id: str
    references: tuple[ClangdReference, ...]


@dataclass(frozen=True, slots=True)
class ClangdRelation:
    subject_symbol_id: str
    predicate: int
    object_symbol_id: str


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: Literal["warning", "error"]
    source_line: int
    field_path: str
    message: str
    recovery_action: str | None = None
    document_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class SourceDocument:
    document_ordinal: int
    yaml_tag: str
    start_line: int
    raw_yaml: bytes
    parse_status: Literal["parsed", "recovered"]


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source: SourceDocument
    payload: ClangdSymbol | ClangdReferences | ClangdRelation
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportedSymbol:
    symbol: ClangdSymbol
    document_ordinal: int
    source_line: int


@dataclass(frozen=True, slots=True)
class ReferenceImport:
    """One exported occurrence; None means its source location was unavailable.

    Several occurrences can map to one model reference after normalization.
    Document position and reference_index locate the original record in the YAML.
    """

    reference_id: ReferenceId | None
    original_kind: int
    document_ordinal: int
    source_line: int
    reference_index: int


@dataclass(frozen=True, slots=True)
class DeferredRelation:
    relation: ClangdRelation
    document_ordinal: int
    source_line: int
    reason: str


@dataclass(frozen=True, slots=True)
class ImportCounts:
    documents: int
    symbol_records: int
    reference_groups: int
    reference_occurrences: int
    relation_records: int
    recovered_symbols: int
    symbols: int
    placeholders: int
    files: int
    locations: int
    references: int
    skipped_references: int
    duplicate_references: int
    inheritance: int
    overrides: int
    deferred_relations: int
    duplicate_relations: int


@dataclass(frozen=True, slots=True)
class ImportReport:
    yaml_path: Path
    yaml_sha256: str
    counts: ImportCounts
    symbol_ids: Mapping[str, SymbolId]
    exported_symbols: Mapping[SymbolId, ExportedSymbol]
    references: tuple[ReferenceImport, ...]
    deferred_relations: tuple[DeferredRelation, ...]
    diagnostics: tuple[Diagnostic, ...]
    source_hashes: Mapping[str, str | None]
    source_assumption: str = "Current source files are assumed to match the indexed snapshot."

    def __post_init__(self) -> None:
        # Copy before wrapping so caller-owned dictionaries cannot change a report.
        object.__setattr__(self, "symbol_ids", MappingProxyType(dict(self.symbol_ids)))
        object.__setattr__(self, "exported_symbols", MappingProxyType(dict(self.exported_symbols)))
        object.__setattr__(self, "source_hashes", MappingProxyType(dict(self.source_hashes)))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "deferred_relations", tuple(self.deferred_relations))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
