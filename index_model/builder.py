"""ID-based staging followed by iterative construction of a canonical object graph."""

from collections import defaultdict, deque
from dataclasses import dataclass, fields
from typing import Self

from . import records as r
from .errors import ModelValidationError, ValidationIssue, instance, integer, require
from .snapshot import IndexModel, _BY_TYPE, _COLLECTIONS, _key
from .types import (
    AccessId, CollectionStage, CollectionState, DocumentationStatus, FileId,
    GLOBAL_SCOPE_ID, IncludeId, InheritanceId, KindId, LanguageId, LibraryId,
    LocationId, OverrideId, ReferenceId, ReferenceKind, ScopeId, SourceRangeRole, SymbolId,
)


_Node = tuple[str, object]


@dataclass
class _Draft:
    values: dict[str, object]
    links: dict[str, _Node]


class IndexBuilder:
    """Collect records in any order. Only build() publishes an immutable model.

    Mutations are confined to this staging object. Database-local IDs are supplied
    by the caller; no external identities, SQL, or source file access are involved.
    """

    def __init__(self) -> None:
        self._metadata: r.IndexMetadata | None = None
        self._drafts: dict[_Node, _Draft] = {}
        self.add_scope(scope_id=GLOBAL_SCOPE_ID, qualified_name="")

    @classmethod
    def from_model(cls, model: IndexModel) -> Self:
        """Stage a snapshot's records, preserving IDs without retaining entity links."""
        builder = cls()
        instance(builder, "model", model, IndexModel)
        builder.set_metadata(model.metadata)
        for collection in _COLLECTIONS:
            for record in getattr(model, collection):
                values = {}
                links = {}
                for field in fields(record):
                    value = getattr(record, field.name)
                    if type(value) in _BY_TYPE:
                        links[field.name] = (_BY_TYPE[type(value)][0], _key(value))
                    else:
                        values[field.name] = value
                builder._drafts[collection, _key(record)] = _Draft(values, links)
        return builder

    def set_metadata(self, metadata: r.IndexMetadata) -> None:
        instance(self, "metadata", metadata, r.IndexMetadata)
        require(self._metadata is None, self, "metadata", "already supplied")
        self._metadata = metadata

    def update_metadata(self, metadata: r.IndexMetadata) -> None:
        """Explicitly replace existing metadata, for example to record completion."""
        instance(self, "metadata", metadata, r.IndexMetadata)
        require(self._metadata is not None, self, "metadata", "has not been supplied")
        self._metadata = metadata

    def _add(self, collection: str, key: object, values: dict[str, object],
             links: dict[str, tuple[str, int | None]] | None = None,
             *, replace_placeholder: bool = False) -> None:
        node = (collection, key)
        if not isinstance(key, tuple):
            integer(self, "identity", key)
        if not replace_placeholder and node in self._drafts:
            raise ModelValidationError((ValidationIssue(collection, "identity", f"duplicate key {key!r}"),))
        copied = dict(values)
        references = {}
        for name, (target_collection, target_id) in (links or {}).items():
            if target_id is None:
                copied[name] = None
            else:
                integer(self, name, target_id)
                references[name] = (target_collection, target_id)
        self._drafts[node] = _Draft(copied, references)

    def add_library(self, *, library_id: LibraryId, name: str) -> None:
        self._add("libraries", library_id, dict(library_id=library_id, name=name))

    def add_file(self, *, file_id: FileId, uri: str, library_id: LibraryId | None = None,
                 content_hash: str | None = None) -> None:
        self._add("files", file_id, dict(file_id=file_id, uri=uri, content_hash=content_hash),
                  {"library": ("libraries", library_id)})

    def add_location(self, *, location_id: LocationId, file_id: FileId,
                     start: r.Position, end: r.Position) -> None:
        self._add("locations", location_id, dict(location_id=location_id, start=start, end=end),
                  {"file": ("files", file_id)})

    def add_symbol_kind(self, *, kind_id: KindId, name: str) -> None:
        self._add("symbol_kinds", kind_id, dict(kind_id=kind_id, name=name))

    def add_language(self, *, language_id: LanguageId, name: str) -> None:
        self._add("languages", language_id, dict(language_id=language_id, name=name))

    def add_access_specifier(self, *, access_id: AccessId, name: str) -> None:
        self._add("access_specifiers", access_id, dict(access_id=access_id, name=name))

    def add_reference_kind_bit(self, *, bit: int, name: str) -> None:
        self._add("reference_kind_bits", bit, dict(bit=bit, name=name))

    def add_scope(self, *, scope_id: ScopeId, qualified_name: str,
                  owner_symbol_id: SymbolId | None = None) -> None:
        self._add("scopes", scope_id, dict(scope_id=scope_id, qualified_name=qualified_name),
                  {"owner": ("symbols", owner_symbol_id)})

    def add_symbol(self, *, symbol_id: SymbolId, name: str | None = None,
                   scope_id: ScopeId | None = None, kind_id: KindId | None = None,
                   language_id: LanguageId | None = None,
                   declaration_location_id: LocationId | None = None,
                   definition_location_id: LocationId | None = None,
                   signature: str | None = None, template_specialization_args: str | None = None,
                   documentation: str | None = None, return_type: str | None = None) -> None:
        self._symbol(symbol_id, name, scope_id, kind_id, language_id, declaration_location_id,
                     definition_location_id, signature, template_specialization_args,
                     documentation, return_type, replace_placeholder=False)

    def resolve_symbol(self, *, symbol_id: SymbolId, name: str, scope_id: ScopeId,
                       language_id: LanguageId, kind_id: KindId | None = None,
                       declaration_location_id: LocationId | None = None,
                       definition_location_id: LocationId | None = None,
                       signature: str | None = None, template_specialization_args: str | None = None,
                       documentation: str | None = None, return_type: str | None = None) -> None:
        integer(self, "symbol_id", symbol_id)
        draft = self._drafts.get(("symbols", symbol_id))
        require(draft is not None and not draft.links and
                all(value is None for key, value in draft.values.items() if key != "symbol_id"),
                self, "symbol_id", "resolve_symbol requires an existing, unresolved placeholder")
        require(name is not None and scope_id is not None and language_id is not None,
                self, "symbol_id", "resolution requires name, scope_id, and language_id")
        self._symbol(symbol_id, name, scope_id, kind_id, language_id, declaration_location_id,
                     definition_location_id, signature, template_specialization_args,
                     documentation, return_type, replace_placeholder=True)

    def enrich_symbol(self, *, symbol_id: SymbolId, kind_id: KindId | None = None,
                      declaration_location_id: LocationId | None = None,
                      definition_location_id: LocationId | None = None,
                      signature: str | None = None, template_specialization_args: str | None = None,
                      documentation: str | None = None, return_type: str | None = None) -> None:
        """Fill missing details of a resolved symbol; None leaves a field unchanged.

        Repeating a known value is allowed. A conflicting value rejects the whole
        update. Use resolve_symbol() for an unresolved placeholder.
        """
        integer(self, "symbol_id", symbol_id)
        node = ("symbols", symbol_id)
        draft = self._drafts.get(node)
        require(draft is not None and draft.values["name"] is not None, self, "symbol_id",
                "enrich_symbol requires an existing, resolved symbol")
        values = dict(draft.values)
        links = dict(draft.links)
        for name, value in (
            ("signature", signature), ("template_specialization_args", template_specialization_args),
            ("documentation", documentation), ("return_type", return_type),
        ):
            if value is not None:
                require(values[name] is None or values[name] == value, self, name,
                        f"conflicting value for symbol {symbol_id}")
                values[name] = value
        for name, collection, target_id in (
            ("kind", "symbol_kinds", kind_id),
            ("declaration", "locations", declaration_location_id),
            ("definition", "locations", definition_location_id),
        ):
            if target_id is not None:
                integer(self, name, target_id)
                target = (collection, target_id)
                require(name not in links or links[name] == target, self, name,
                        f"conflicting value for symbol {symbol_id}")
                values.pop(name, None)
                links[name] = target
        self._drafts[node] = _Draft(values, links)

    def _symbol(self, symbol_id: SymbolId, name: str | None, scope_id: ScopeId | None,
                kind_id: KindId | None, language_id: LanguageId | None,
                declaration_location_id: LocationId | None, definition_location_id: LocationId | None,
                signature: str | None, template_specialization_args: str | None,
                documentation: str | None, return_type: str | None,
                *, replace_placeholder: bool) -> None:
        self._add("symbols", symbol_id, dict(
            symbol_id=symbol_id, name=name, signature=signature,
            template_specialization_args=template_specialization_args,
            documentation=documentation, return_type=return_type), {
                "scope": ("scopes", scope_id), "kind": ("symbol_kinds", kind_id),
                "language": ("languages", language_id),
                "declaration": ("locations", declaration_location_id),
                "definition": ("locations", definition_location_id),
            }, replace_placeholder=replace_placeholder)

    def add_file_include(self, *, include_id: IncludeId, includer_file_id: FileId,
                         included_file_id: FileId) -> None:
        self._add("file_includes", include_id, dict(include_id=include_id), {
            "includer": ("files", includer_file_id), "included": ("files", included_file_id)})

    def add_symbol_reference(self, *, reference_id: ReferenceId, target_symbol_id: SymbolId,
                             location_id: LocationId, kind: ReferenceKind,
                             container_symbol_id: SymbolId | None = None) -> None:
        self._add("symbol_references", reference_id, dict(reference_id=reference_id, kind=kind), {
            "target": ("symbols", target_symbol_id), "location": ("locations", location_id),
            "container": ("symbols", container_symbol_id)})

    def add_class_inheritance(self, *, inheritance_id: InheritanceId, base_symbol_id: SymbolId,
                              derived_symbol_id: SymbolId, access_id: AccessId | None = None,
                              is_virtual: bool | None = None) -> None:
        self._add("class_inheritance", inheritance_id,
                  dict(inheritance_id=inheritance_id, is_virtual=is_virtual), {
                      "base": ("symbols", base_symbol_id), "derived": ("symbols", derived_symbol_id),
                      "access": ("access_specifiers", access_id)})

    def add_method_override(self, *, override_id: OverrideId, overridden_symbol_id: SymbolId,
                            overriding_symbol_id: SymbolId) -> None:
        self._add("method_overrides", override_id, dict(override_id=override_id), {
            "overridden": ("symbols", overridden_symbol_id),
            "overriding": ("symbols", overriding_symbol_id)})

    def add_symbol_source_range(self, *, symbol_id: SymbolId, location_id: LocationId,
                                role: SourceRangeRole) -> None:
        integer(self, "symbol_id", symbol_id)
        integer(self, "location_id", location_id)
        instance(self, "role", role, SourceRangeRole)
        self._add("symbol_source_ranges", (symbol_id, location_id, role), dict(role=role), {
            "symbol": ("symbols", symbol_id), "location": ("locations", location_id)})

    def add_symbol_body(self, *, symbol_id: SymbolId, body_location_id: LocationId,
                        metrics: r.BodyMetrics) -> None:
        self._add("symbol_bodies", symbol_id, dict(metrics=metrics), {
            "symbol": ("symbols", symbol_id), "body": ("locations", body_location_id)})

    def add_collection_status(self, *, file_id: FileId, stage: CollectionStage,
                              status: CollectionState, error: str | None = None) -> None:
        integer(self, "file_id", file_id)
        instance(self, "stage", stage, CollectionStage)
        self._add("collection_statuses", (file_id, stage), dict(stage=stage, status=status, error=error),
                  {"file": ("files", file_id)})

    def update_collection_status(self, *, file_id: FileId, stage: CollectionStage,
                                 status: CollectionState, error: str | None = None) -> None:
        """Replace an existing stage outcome and its error; validate on build()."""
        integer(self, "file_id", file_id)
        instance(self, "stage", stage, CollectionStage)
        node = ("collection_statuses", (file_id, stage))
        require(node in self._drafts, self, "collection_status", "does not exist")
        self._drafts[node] = _Draft(dict(stage=stage, status=status, error=error),
                                   {"file": ("files", file_id)})

    def add_generated_documentation(self, *, symbol_id: SymbolId, comment: str | None = None,
                                    status: DocumentationStatus = DocumentationStatus.PENDING,
                                    model: str | None = None, prompt_version: str | None = None,
                                    source_input_hash: str | None = None,
                                    dependency_context_hash: str | None = None,
                                    created_at: str | None = None, completed_at: str | None = None,
                                    error: str | None = None) -> None:
        self._add("generated_documentation", symbol_id, dict(
            comment=comment, status=status, model=model, prompt_version=prompt_version,
            source_input_hash=source_input_hash, dependency_context_hash=dependency_context_hash,
            created_at=r.utc_now() if created_at is None else created_at,
            completed_at=completed_at, error=error), {"symbol": ("symbols", symbol_id)})

    def build(self) -> IndexModel:
        issues = []
        if self._metadata is None:
            issues.append(ValidationIssue("IndexBuilder", "metadata", "metadata is required"))
        dependents = defaultdict(set)
        remaining = {}
        for node, draft in self._drafts.items():
            dependencies = set(draft.links.values())
            remaining[node] = len(dependencies)
            for name, target in draft.links.items():
                if target not in self._drafts:
                    issues.append(ValidationIssue(f"{node[0]}[{node[1]!r}]", name,
                                                  f"missing referenced {target[0]}[{target[1]!r}]"))
            for target in dependencies:
                dependents[target].add(node)
        if issues:
            raise ModelValidationError(tuple(issues))

        ready = deque(sorted((node for node, count in remaining.items() if count == 0), key=repr))
        resolved = {}
        processed = set()
        while ready:
            node = ready.popleft()
            draft = self._drafts[node]
            label = f"{node[0]}[{node[1]!r}]"
            if any(target not in resolved for target in draft.links.values()):
                issues.append(ValidationIssue(label, "relationships", "depends on an invalid record"))
            else:
                values = dict(draft.values)
                values.update((name, resolved[target]) for name, target in draft.links.items())
                cls = _COLLECTIONS[node[0]][0]
                try:
                    resolved[node] = cls(**values)
                except ModelValidationError as error:
                    issues.extend(ValidationIssue(label, issue.field, issue.message) for issue in error.issues)
            processed.add(node)
            for dependent in sorted(dependents[node], key=repr):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
        blocked = sorted(set(self._drafts) - processed, key=repr)
        if blocked:
            issues.append(ValidationIssue("IndexBuilder", "relationships",
                f"scope ownership cycle blocks {len(blocked)} records; examples: {blocked[:5]!r}"))
        if issues:
            raise ModelValidationError(tuple(issues))
        collections = {name: [] for name in _COLLECTIONS}
        for (collection, _), record in resolved.items():
            collections[collection].append(record)
        return IndexModel(metadata=self._metadata, **collections)
