"""Immutable nested records. Construction validates local fields, not graph membership."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone

from .errors import digest, instance, integer, require, text
from .types import (
    AccessId, CollectionStage, CollectionState, DocumentationStatus, FileId,
    GLOBAL_SCOPE_ID, IncludeId, InheritanceId, KindId, LanguageId, LibraryId,
    LocationId, OverrideId, ReferenceId, ReferenceKind, ResolutionState,
    SCHEMA_VERSION, ScopeId, SnapshotStatus, SourceRangeRole, SymbolId,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, order=True)
class Position:
    line: int
    column: int

    def __post_init__(self) -> None:
        integer(self, "line", self.line, 0)
        integer(self, "column", self.column, 0)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class IndexMetadata:
    project_root: str
    metadata_id: int = 1
    schema_version: int = SCHEMA_VERSION
    source_revision: str | None = None
    build_input_hash: str | None = None
    metrics_version: str | None = None
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    status: SnapshotStatus = SnapshotStatus.BUILDING
    error: str | None = None

    def __post_init__(self) -> None:
        integer(self, "metadata_id", self.metadata_id)
        integer(self, "schema_version", self.schema_version)
        require(self.metadata_id == 1, self, "metadata_id", "must be 1")
        require(self.schema_version == SCHEMA_VERSION, self, "schema_version", "must be 3")
        text(self, "project_root", self.project_root, nonempty=True)
        for name in ("source_revision", "metrics_version", "completed_at", "error"):
            text(self, name, getattr(self, name), optional=True, nonempty=True)
        text(self, "created_at", self.created_at)
        digest(self, "build_input_hash", self.build_input_hash)
        instance(self, "status", self.status, SnapshotStatus)
        if self.status is SnapshotStatus.COMPLETE:
            require(self.completed_at is not None and self.error is None, self, "status",
                    "complete snapshots need completed_at and no error")
        if self.status is SnapshotStatus.FAILED:
            require(self.error is not None, self, "error", "failed snapshots need an error")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class Library:
    library_id: LibraryId
    name: str

    def __post_init__(self) -> None:
        integer(self, "library_id", self.library_id)
        text(self, "name", self.name, nonempty=True)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class File:
    file_id: FileId
    uri: str
    library: Library | None = field(default=None, repr=False)
    content_hash: str | None = None

    def __post_init__(self) -> None:
        integer(self, "file_id", self.file_id)
        text(self, "uri", self.uri)
        instance(self, "library", self.library, Library, optional=True)
        digest(self, "content_hash", self.content_hash)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class Location:
    location_id: LocationId
    file: File = field(repr=False)
    start: Position
    end: Position

    def __post_init__(self) -> None:
        integer(self, "location_id", self.location_id)
        instance(self, "file", self.file, File)
        instance(self, "start", self.start, Position)
        instance(self, "end", self.end, Position)
        require(self.end >= self.start, self, "end", "must not precede start")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class SymbolKind:
    kind_id: KindId
    name: str

    def __post_init__(self) -> None:
        integer(self, "kind_id", self.kind_id)
        text(self, "name", self.name, nonempty=True)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class Language:
    language_id: LanguageId
    name: str

    def __post_init__(self) -> None:
        integer(self, "language_id", self.language_id)
        text(self, "name", self.name, nonempty=True)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class AccessSpecifier:
    access_id: AccessId
    name: str

    def __post_init__(self) -> None:
        integer(self, "access_id", self.access_id)
        text(self, "name", self.name)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class ReferenceKindBit:
    bit: int
    name: str

    def __post_init__(self) -> None:
        integer(self, "bit", self.bit, 1)
        require(self.bit & (self.bit - 1) == 0, self, "bit", "must be a single bit")
        text(self, "name", self.name)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class Scope:
    scope_id: ScopeId
    qualified_name: str
    owner: Symbol | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        integer(self, "scope_id", self.scope_id)
        text(self, "qualified_name", self.qualified_name)
        instance(self, "owner", self.owner, Symbol, optional=True)
        if self.scope_id == GLOBAL_SCOPE_ID:
            require(self.qualified_name == "" and self.owner is None, self, "scope_id",
                    "global scope must have an empty name and no owner")
        else:
            require(self.qualified_name != "", self, "qualified_name", "must not be empty")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class Symbol:
    symbol_id: SymbolId
    name: str | None = None
    scope: Scope | None = field(default=None, repr=False)
    kind: SymbolKind | None = field(default=None, repr=False)
    language: Language | None = field(default=None, repr=False)
    declaration: Location | None = field(default=None, repr=False)
    definition: Location | None = field(default=None, repr=False)
    signature: str | None = None
    template_specialization_args: str | None = None
    documentation: str | None = None
    return_type: str | None = None

    def __post_init__(self) -> None:
        integer(self, "symbol_id", self.symbol_id)
        text(self, "name", self.name, optional=True)
        for name, cls in (("scope", Scope), ("kind", SymbolKind), ("language", Language),
                          ("declaration", Location), ("definition", Location)):
            instance(self, name, getattr(self, name), cls, optional=True)
        for name in ("signature", "template_specialization_args", "documentation", "return_type"):
            text(self, name, getattr(self, name), optional=True, nonempty=True)
        if self.name is None:
            require(all(getattr(self, f.name) is None for f in fields(self)
                        if f.name != "symbol_id"), self, "name",
                    "a placeholder must have no attributes")
        else:
            require(self.scope is not None and self.language is not None, self, "name",
                    "a resolved symbol requires scope and language")

    @property
    def resolution_state(self) -> ResolutionState:
        return ResolutionState.PLACEHOLDER if self.name is None else ResolutionState.RESOLVED

    @property
    def parent(self) -> Symbol | None:
        return self.scope.owner if self.scope is not None else None

    @property
    def scope_name(self) -> str | None:
        return self.scope.qualified_name if self.scope is not None else None


@dataclass(frozen=True, slots=True, kw_only=True)
class BodyMetrics:
    statement_count: int
    if_count: int
    switch_count: int
    case_count: int
    loop_count: int
    max_nesting_depth: int
    cyclomatic_complexity: int
    return_count: int
    goto_count: int
    break_count: int
    continue_count: int
    try_count: int
    catch_count: int
    throw_count: int
    parameter_count: int
    local_variable_count: int
    lambda_count: int

    def __post_init__(self) -> None:
        for f in fields(self):
            integer(self, f.name, getattr(self, f.name), 1 if f.name == "cyclomatic_complexity" else 0)
        require(self.case_count == 0 or self.switch_count > 0, self, "case_count", "requires a switch")
        require(self.catch_count == 0 or self.try_count > 0, self, "catch_count", "requires a try")
        require(self.max_nesting_depth == 0 or
                self.if_count + self.switch_count + self.loop_count + self.try_count > 0,
                self, "max_nesting_depth", "requires a nesting construct")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class SymbolBody:
    symbol: Symbol = field(repr=False)
    body: Location = field(repr=False)
    metrics: BodyMetrics

    def __post_init__(self) -> None:
        instance(self, "symbol", self.symbol, Symbol)
        instance(self, "body", self.body, Location)
        instance(self, "metrics", self.metrics, BodyMetrics)

    @property
    def line_count(self) -> int:
        start, end = self.body.start, self.body.end
        return 0 if start == end else end.line - start.line + int(end.column > 0)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class SymbolSourceRange:
    symbol: Symbol = field(repr=False)
    location: Location = field(repr=False)
    role: SourceRangeRole

    def __post_init__(self) -> None:
        instance(self, "symbol", self.symbol, Symbol)
        instance(self, "location", self.location, Location)
        instance(self, "role", self.role, SourceRangeRole)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class SymbolReference:
    reference_id: ReferenceId
    target: Symbol = field(repr=False)
    location: Location = field(repr=False)
    kind: ReferenceKind
    container: Symbol | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        integer(self, "reference_id", self.reference_id)
        instance(self, "target", self.target, Symbol)
        instance(self, "location", self.location, Location)
        instance(self, "container", self.container, Symbol, optional=True)
        instance(self, "kind", self.kind, ReferenceKind)
        require(1 <= int(self.kind) <= 31, self, "kind", "requires known, nonzero flags")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class FileInclude:
    include_id: IncludeId
    includer: File = field(repr=False)
    included: File = field(repr=False)

    def __post_init__(self) -> None:
        integer(self, "include_id", self.include_id)
        instance(self, "includer", self.includer, File)
        instance(self, "included", self.included, File)


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class ClassInheritance:
    inheritance_id: InheritanceId
    base: Symbol = field(repr=False)
    derived: Symbol = field(repr=False)
    access: AccessSpecifier | None = field(default=None, repr=False)
    is_virtual: bool | None = None

    def __post_init__(self) -> None:
        integer(self, "inheritance_id", self.inheritance_id)
        instance(self, "base", self.base, Symbol)
        instance(self, "derived", self.derived, Symbol)
        instance(self, "access", self.access, AccessSpecifier, optional=True)
        instance(self, "is_virtual", self.is_virtual, bool, optional=True)
        require(self.base.symbol_id != self.derived.symbol_id, self, "derived", "cannot inherit itself")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class MethodOverride:
    override_id: OverrideId
    overridden: Symbol = field(repr=False)
    overriding: Symbol = field(repr=False)

    def __post_init__(self) -> None:
        integer(self, "override_id", self.override_id)
        instance(self, "overridden", self.overridden, Symbol)
        instance(self, "overriding", self.overriding, Symbol)
        require(self.overridden.symbol_id != self.overriding.symbol_id, self, "overriding",
                "cannot override itself")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class CollectionStatus:
    file: File = field(repr=False)
    stage: CollectionStage
    status: CollectionState
    error: str | None = None

    def __post_init__(self) -> None:
        instance(self, "file", self.file, File)
        instance(self, "stage", self.stage, CollectionStage)
        instance(self, "status", self.status, CollectionState)
        text(self, "error", self.error, optional=True, nonempty=True)
        needs_error = self.status in (CollectionState.PARTIAL, CollectionState.FAILED)
        require((self.error is not None) == needs_error, self, "error",
                "partial/failed stages require an error; other stages must not have one")


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class GeneratedDocumentation:
    symbol: Symbol = field(repr=False)
    comment: str | None = None
    status: DocumentationStatus = DocumentationStatus.PENDING
    model: str | None = None
    prompt_version: str | None = None
    source_input_hash: str | None = None
    dependency_context_hash: str | None = None
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        instance(self, "symbol", self.symbol, Symbol)
        instance(self, "status", self.status, DocumentationStatus)
        for name in ("comment", "model", "prompt_version", "completed_at", "error"):
            text(self, name, getattr(self, name), optional=True, nonempty=True)
        require(self.comment is None or self.comment.strip(" ") != "", self, "comment",
                "must not consist only of spaces")
        text(self, "created_at", self.created_at)
        digest(self, "source_input_hash", self.source_input_hash)
        digest(self, "dependency_context_hash", self.dependency_context_hash)
        if self.status is DocumentationStatus.COMPLETE:
            require(all(getattr(self, name) is not None for name in (
                "comment", "model", "prompt_version", "source_input_hash",
                "dependency_context_hash", "completed_at")) and self.error is None,
                self, "status", "complete documents require content, provenance, completion time, and no error")
            require(self.symbol.name is not None, self, "symbol", "completed documentation needs a resolved symbol")
        if self.status is DocumentationStatus.FAILED:
            require(self.error is not None, self, "error", "failed generation requires an error")
