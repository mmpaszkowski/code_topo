"""Immutable snapshot collections, canonical identity, and graph validation."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Mapping, TypeVar, cast

from . import records as r
from .errors import ModelValidationError, ValidationIssue, instance
from .types import (
    AccessId, CollectionStage, FileId, GLOBAL_SCOPE_ID, IncludeId, InheritanceId,
    KindId, LanguageId, LibraryId, LocationId, OverrideId, ReferenceId, ScopeId,
    SourceRangeRole, SymbolId,
)


# Collection names, record types, and identity paths. Nested paths retain the
# schema's composite keys without duplicating foreign-key fields on records.
_COLLECTIONS = {
    "libraries": (r.Library, ("library_id",)),
    "files": (r.File, ("file_id",)),
    "locations": (r.Location, ("location_id",)),
    "symbol_kinds": (r.SymbolKind, ("kind_id",)),
    "languages": (r.Language, ("language_id",)),
    "access_specifiers": (r.AccessSpecifier, ("access_id",)),
    "reference_kind_bits": (r.ReferenceKindBit, ("bit",)),
    "scopes": (r.Scope, ("scope_id",)),
    "symbols": (r.Symbol, ("symbol_id",)),
    "file_includes": (r.FileInclude, ("include_id",)),
    "symbol_references": (r.SymbolReference, ("reference_id",)),
    "class_inheritance": (r.ClassInheritance, ("inheritance_id",)),
    "method_overrides": (r.MethodOverride, ("override_id",)),
    "symbol_source_ranges": (r.SymbolSourceRange, ("symbol.symbol_id", "location.location_id", "role")),
    "symbol_bodies": (r.SymbolBody, ("symbol.symbol_id",)),
    "collection_statuses": (r.CollectionStatus, ("file.file_id", "stage")),
    "generated_documentation": (r.GeneratedDocumentation, ("symbol.symbol_id",)),
}
_BY_TYPE = {cls: (collection, paths) for collection, (cls, paths) in _COLLECTIONS.items()}
_T = TypeVar("_T")


def _path(record: object, path: str) -> object:
    for part in path.split("."):
        if record is None:
            return None
        record = getattr(record, part)
    return record


def _key(record: object) -> object:
    paths = _BY_TYPE[type(record)][1]
    values = tuple(_path(record, path) for path in paths)
    return values[0] if len(values) == 1 else values


@dataclass(frozen=True, slots=True, eq=False, kw_only=True)
class IndexModel:
    metadata: r.IndexMetadata
    libraries: tuple[r.Library, ...] = ()
    files: tuple[r.File, ...] = ()
    locations: tuple[r.Location, ...] = ()
    symbol_kinds: tuple[r.SymbolKind, ...] = ()
    languages: tuple[r.Language, ...] = ()
    access_specifiers: tuple[r.AccessSpecifier, ...] = ()
    reference_kind_bits: tuple[r.ReferenceKindBit, ...] = ()
    scopes: tuple[r.Scope, ...] = ()
    symbols: tuple[r.Symbol, ...] = ()
    file_includes: tuple[r.FileInclude, ...] = ()
    symbol_references: tuple[r.SymbolReference, ...] = ()
    class_inheritance: tuple[r.ClassInheritance, ...] = ()
    method_overrides: tuple[r.MethodOverride, ...] = ()
    symbol_source_ranges: tuple[r.SymbolSourceRange, ...] = ()
    symbol_bodies: tuple[r.SymbolBody, ...] = ()
    collection_statuses: tuple[r.CollectionStatus, ...] = ()
    generated_documentation: tuple[r.GeneratedDocumentation, ...] = ()
    _indices: Mapping[str, Mapping[object, object]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        instance(self, "metadata", self.metadata, r.IndexMetadata)
        indices = {}
        issues = []
        for name, (cls, _) in _COLLECTIONS.items():
            try:
                values = tuple(getattr(self, name))
            except TypeError as error:
                raise ModelValidationError((ValidationIssue(
                    "IndexModel", name, "expected an iterable of records"),)) from error
            index = {}
            for value in values:
                instance(self, name, value, cls)
                key = _key(value)
                if key in index:
                    issues.append(ValidationIssue(name, "identity", f"duplicate key {key!r}"))
                index[key] = value
            object.__setattr__(self, name, tuple(sorted(values, key=_key)))
            indices[name] = MappingProxyType(index)
        object.__setattr__(self, "_indices", MappingProxyType(indices))
        if issues:
            raise ModelValidationError(tuple(issues))
        issues = validate(self)
        if issues:
            raise ModelValidationError(issues)

    def library(self, key: LibraryId) -> r.Library:
        return self._lookup(r.Library, key)

    def file(self, key: FileId) -> r.File:
        return self._lookup(r.File, key)

    def location(self, key: LocationId) -> r.Location:
        return self._lookup(r.Location, key)

    def kind(self, key: KindId) -> r.SymbolKind:
        return self._lookup(r.SymbolKind, key)

    def language(self, key: LanguageId) -> r.Language:
        return self._lookup(r.Language, key)

    def access_specifier(self, key: AccessId) -> r.AccessSpecifier:
        return self._lookup(r.AccessSpecifier, key)

    def reference_kind_bit(self, bit: int) -> r.ReferenceKindBit:
        return self._lookup(r.ReferenceKindBit, bit)

    def scope(self, key: ScopeId) -> r.Scope:
        return self._lookup(r.Scope, key)

    def symbol(self, key: SymbolId) -> r.Symbol:
        return self._lookup(r.Symbol, key)

    def include(self, key: IncludeId) -> r.FileInclude:
        return self._lookup(r.FileInclude, key)

    def reference(self, key: ReferenceId) -> r.SymbolReference:
        return self._lookup(r.SymbolReference, key)

    def inheritance(self, key: InheritanceId) -> r.ClassInheritance:
        return self._lookup(r.ClassInheritance, key)

    def override(self, key: OverrideId) -> r.MethodOverride:
        return self._lookup(r.MethodOverride, key)

    def source_range(self, symbol: SymbolId, location: LocationId, role: SourceRangeRole) -> r.SymbolSourceRange:
        return self._lookup(r.SymbolSourceRange, (symbol, location, role))

    def body(self, symbol: SymbolId) -> r.SymbolBody:
        return self._lookup(r.SymbolBody, symbol)

    def collection_status(self, file: FileId, stage: CollectionStage) -> r.CollectionStatus:
        return self._lookup(r.CollectionStatus, (file, stage))

    def documentation(self, symbol: SymbolId) -> r.GeneratedDocumentation:
        return self._lookup(r.GeneratedDocumentation, symbol)

    def _lookup(self, cls: type[_T], key: object) -> _T:
        return cast(_T, self._indices[_BY_TYPE[cls][0]][key])


def validate(index: IndexModel) -> tuple[ValidationIssue, ...]:
    """Check canonical links and structural invariants, not publication readiness."""
    issues = []
    for collection in _COLLECTIONS:
        for record in getattr(index, collection):
            for f in fields(record):
                target = getattr(record, f.name)
                if type(target) in _BY_TYPE:
                    target_collection = _BY_TYPE[type(target)][0]
                    if index._indices[target_collection].get(_key(target)) is not target:
                        issues.append(ValidationIssue(f"{collection}[{_key(record)!r}]", f.name,
                                                      "must reference the canonical object in this snapshot"))

    # Natural uniqueness constraints are independent of the surrogate ID keys.
    unique_paths = {
        "libraries": ("name",), "files": ("uri",),
        "locations": ("file.file_id", "start", "end"),
        "symbol_kinds": ("name",), "languages": ("name",),
        "access_specifiers": ("name",), "reference_kind_bits": ("name",),
        "scopes": ("owner.symbol_id",),
        "file_includes": ("includer.file_id", "included.file_id"),
        "symbol_references": ("target.symbol_id", "location.location_id", "kind", "container.symbol_id"),
        "class_inheritance": ("base.symbol_id", "derived.symbol_id"),
        "method_overrides": ("overridden.symbol_id", "overriding.symbol_id"),
    }
    for collection, paths in unique_paths.items():
        seen = set()
        for record in getattr(index, collection):
            values = tuple(_path(record, path) for path in paths)
            if collection == "scopes" and values == (None,):
                continue
            if values in seen:
                issues.append(ValidationIssue(collection, ", ".join(paths),
                                              f"duplicate values {values!r}"))
            seen.add(values)

    if GLOBAL_SCOPE_ID not in index._indices["scopes"]:
        issues.append(ValidationIssue("IndexModel", "scopes", "global scope 1 is required"))

    parents = {s.symbol_id: s.parent.symbol_id if s.parent else None for s in index.symbols}
    visited = set()
    for start in parents:
        path = set()
        current = start
        while current in parents and current not in visited and current not in path:
            path.add(current)
            current = parents[current]
        if current in path:
            issues.append(ValidationIssue(f"Symbol[{current}]", "scope", "scope ownership cycle"))
        visited.update(path)

    for file in index.files:
        for stage in CollectionStage:
            if (file.file_id, stage) not in index._indices["collection_statuses"]:
                issues.append(ValidationIssue(f"File[{file.file_id}]", "collection_statuses",
                                              f"missing explicit status for {stage.value}"))

    if index.symbol_bodies and index.metadata.metrics_version is None:
        issues.append(ValidationIssue("IndexMetadata", "metrics_version", "required when metrics are present"))
    for body in index.symbol_bodies:
        key = (body.symbol.symbol_id, body.body.location_id, SourceRangeRole.BODY)
        if key not in index._indices["symbol_source_ranges"]:
            issues.append(ValidationIssue(f"SymbolBody[{body.symbol.symbol_id}]", "body",
                                          "requires a matching body source range"))
    for source in index.symbol_source_ranges:
        if source.role is SourceRangeRole.BODY and source.location.start == source.location.end:
            issues.append(ValidationIssue(f"SymbolSourceRange[{source.symbol.symbol_id}]", "location",
                                          "body range must not be empty"))
    return tuple(issues)
