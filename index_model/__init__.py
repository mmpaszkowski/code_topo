"""Tool-independent immutable model for code-index schema version 3 (Python 3.11+)."""

from .builder import IndexBuilder
from .errors import ModelValidationError, ValidationIssue
from .records import (
    AccessSpecifier, BodyMetrics, ClassInheritance, CollectionStatus, File,
    FileInclude, GeneratedDocumentation, IndexMetadata, Language, Library,
    Location, MethodOverride, Position, ReferenceKindBit, Scope, Symbol,
    SymbolBody, SymbolKind, SymbolReference, SymbolSourceRange,
)
from .snapshot import IndexModel, validate
from .types import (
    AccessId, CollectionStage, CollectionState, DocumentationStatus, FileId,
    GLOBAL_SCOPE_ID, IncludeId, InheritanceId, KindId, LanguageId, LibraryId,
    LocationId, OverrideId, ReferenceId, ReferenceKind, ResolutionState,
    SCHEMA_VERSION, ScopeId, SnapshotStatus, SourceRangeRole, SymbolId,
)

__all__ = [
    "IndexBuilder", "IndexModel", "ModelValidationError", "ValidationIssue", "validate",
    "AccessSpecifier", "BodyMetrics", "ClassInheritance", "CollectionStatus", "File",
    "FileInclude", "GeneratedDocumentation", "IndexMetadata", "Language", "Library",
    "Location", "MethodOverride", "Position", "ReferenceKindBit", "Scope", "Symbol",
    "SymbolBody", "SymbolKind", "SymbolReference", "SymbolSourceRange", "AccessId",
    "CollectionStage", "CollectionState", "DocumentationStatus", "FileId", "GLOBAL_SCOPE_ID",
    "IncludeId", "InheritanceId", "KindId", "LanguageId", "LibraryId", "LocationId",
    "OverrideId", "ReferenceId", "ReferenceKind", "ResolutionState", "SCHEMA_VERSION",
    "ScopeId", "SnapshotStatus", "SourceRangeRole", "SymbolId",
]
