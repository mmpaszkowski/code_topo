"""Application-owned identifiers and vocabulary; no storage or producer types."""

from enum import IntFlag, StrEnum
from typing import NewType


LibraryId = NewType("LibraryId", int)
FileId = NewType("FileId", int)
LocationId = NewType("LocationId", int)
KindId = NewType("KindId", int)
LanguageId = NewType("LanguageId", int)
AccessId = NewType("AccessId", int)
ScopeId = NewType("ScopeId", int)
SymbolId = NewType("SymbolId", int)
IncludeId = NewType("IncludeId", int)
ReferenceId = NewType("ReferenceId", int)
InheritanceId = NewType("InheritanceId", int)
OverrideId = NewType("OverrideId", int)

SCHEMA_VERSION = 3
GLOBAL_SCOPE_ID = ScopeId(1)


class SnapshotStatus(StrEnum):
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"


class CollectionStage(StrEnum):
    SYMBOLS = "symbols"
    REFERENCES = "references"
    INCLUDES = "includes"
    SOURCE_RANGES = "source_ranges"
    METRICS = "metrics"


class CollectionState(StrEnum):
    NOT_REQUESTED = "not_requested"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceRangeRole(StrEnum):
    DECLARATION = "declaration"
    DEFINITION = "definition"
    BODY = "body"


class DocumentationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    STALE = "stale"


class ResolutionState(StrEnum):
    PLACEHOLDER = "placeholder"
    RESOLVED = "resolved"


class ReferenceKind(IntFlag):
    DECLARATION = 1
    DEFINITION = 2
    REFERENCE = 4
    SPELLED = 8
    CALL = 16
