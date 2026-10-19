"""Translate clangd export records into one validated application snapshot."""

from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import logging
from pathlib import Path

from index_model import (
    CollectionStage, CollectionState, FileId, GLOBAL_SCOPE_ID, IndexBuilder,
    IndexModel, InheritanceId, KindId, LanguageId, LocationId, OverrideId,
    Position, ReferenceId, ReferenceKind, ScopeId, SnapshotStatus, SymbolId,
)
from index_pipeline import Step, SummaryItem
from progress import NO_PROGRESS, ProgressReporter

from .reader import ClangdYamlReader, IndexParseError
from .records import (
    ClangdReferences, ClangdRelation, ClangdSymbol, DeferredRelation, Diagnostic,
    ExportedSymbol, ExportLocation, ImportCounts, ImportReport, ReferenceImport,
    SourceDocument, SourceRecord,
)
from .sources import LocalSourceReader, utf8_position


logger = logging.getLogger(__name__)

_LANGUAGES = {"C": "C", "Cpp": "C++"}
_KINDS = {
    "Constructor": "constructor", "Function": "function", "Field": "field",
    "Class": "class", "StaticMethod": "static_method", "InstanceMethod": "instance_method",
    "Using": "using", "StaticProperty": "static_property", "TypeAlias": "type_alias",
    "Variable": "variable", "Destructor": "destructor", "EnumConstant": "enum_constant",
    "Enum": "enum", "Struct": "struct", "ConversionFunction": "conversion_function",
    "Namespace": "namespace", "Union": "union", "NamespaceAlias": "namespace_alias",
}
_CALLABLE_KINDS = {
    "Function", "InstanceMethod", "StaticMethod", "Constructor", "Destructor", "ConversionFunction",
}
# LLVM 21's bit 16 identifies occurrences of callable targets, including their
# declarations. It is deliberately absent here: the model's CALL means a call.
_REFERENCE_KINDS = (
    (1, ReferenceKind.DECLARATION), (2, ReferenceKind.DEFINITION),
    (4, ReferenceKind.REFERENCE), (8, ReferenceKind.SPELLED),
)


class ClangdYamlStep(Step):
    def __init__(self, reader: ClangdYamlReader, sources: LocalSourceReader, *,
                 progress: ProgressReporter = NO_PROGRESS) -> None:
        self._reader = reader
        self._progress = progress
        self._sources = sources
        self._report: ImportReport | None = None

    @property
    def report(self) -> ImportReport | None:
        return self._report

    def summary(self) -> tuple[SummaryItem, ...]:
        report = self._report
        if report is None:
            return ()
        return (
            SummaryItem("clangd_yaml diagnostics", len(report.diagnostics)),
            SummaryItem("Unresolved symbols", report.counts.placeholders),
            SummaryItem("Recovered symbols", report.counts.recovered_symbols),
            SummaryItem("Skipped references", report.counts.skipped_references),
            SummaryItem("Deferred relations", report.counts.deferred_relations),
            SummaryItem("Source assumption", report.source_assumption),
        )

    def run(self, model: IndexModel) -> IndexModel:
        self._report = None
        self._progress.phase("preparing import")
        importer = _ClangdImport(model, self._sources, self._reader.path)
        self._progress.phase("importing", total=self._reader.path.stat().st_size, unit="bytes")
        completed = 0
        # Close the reader even when translation fails partway through iteration.
        with closing(self._reader.read()) as records:
            for record in records:
                importer.add_record(record)
                completed += len(record.source.raw_yaml)
                self._progress.update(completed)
        self._progress.phase("building and validating model")
        result = importer.build_model()
        self._progress.phase("building report")
        report = importer.build_report()
        self._log_diagnostics(report)
        self._report = report
        return result

    @staticmethod
    def _log_diagnostics(report: ImportReport) -> None:
        for diagnostic in report.diagnostics:
            level = logging.ERROR if diagnostic.severity == "error" else logging.WARNING
            message = diagnostic.message
            if diagnostic.recovery_action:
                message += f" ({diagnostic.recovery_action})"
            logger.log(level, "%s:%d: %s: %s", report.yaml_path, diagnostic.source_line,
                       diagnostic.field_path, message)


class _ClangdImport:
    """Mutable working state belonging to exactly one step execution."""

    def __init__(self, model: IndexModel, sources: LocalSourceReader, yaml_path: Path) -> None:
        self._check_seed(model)
        self._builder = IndexBuilder.from_model(model)
        self._sources = sources
        self._yaml_path = yaml_path
        self._yaml_hash = sha256()
        self._counts: Counter[str] = Counter()
        self._symbol_ids: dict[str, SymbolId] = {}
        self._symbols: dict[SymbolId, ExportedSymbol] = {}
        self._languages = {row.name: row.language_id for row in model.languages}
        self._kinds = {row.name: row.kind_id for row in model.symbol_kinds}
        self._next_scope = 2
        self._files: dict[str, FileId] = {}
        self._source_lines: dict[str, tuple[bytes, ...]] = {}
        self._source_hashes: dict[str, str | None] = {}
        self._source_errors: dict[str, str] = {}
        self._reported_source_errors: set[str] = set()
        self._locations: dict[ExportLocation, LocationId] = {}
        self._location_errors: dict[ExportLocation, str] = {}
        self._normalized_locations: dict[tuple[FileId, Position, Position], LocationId] = {}
        self._reference_ids: dict[tuple[SymbolId, LocationId, ReferenceKind, SymbolId | None], ReferenceId] = {}
        self._references: list[ReferenceImport] = []
        self._relations: set[ClangdRelation] = set()
        self._deferred_relations: list[DeferredRelation] = []
        self._diagnostics: list[Diagnostic] = []
        self._partial: dict[tuple[FileId, CollectionStage], set[str]] = defaultdict(set)
        self._result: IndexModel | None = None

    @staticmethod
    def _check_seed(model: IndexModel) -> None:
        if not isinstance(model, IndexModel):
            raise TypeError("ClangdYamlStep requires an IndexModel")
        if model.metadata.status is not SnapshotStatus.BUILDING:
            raise ValueError("ClangdYamlStep requires building metadata")
        # Lookup rows may be supplied by the caller. Indexed entities may not.
        for name in (
            "libraries", "files", "locations", "symbols", "file_includes", "symbol_references",
            "class_inheritance", "method_overrides", "symbol_source_ranges", "symbol_bodies",
            "collection_statuses", "generated_documentation",
        ):
            if getattr(model, name):
                raise ValueError(f"ClangdYamlStep requires a fresh seed; {name} is populated")
        if len(model.scopes) != 1 or model.scopes[0].scope_id != GLOBAL_SCOPE_ID:
            raise ValueError("ClangdYamlStep requires only the global scope in its seed")

    def add_record(self, record: SourceRecord) -> None:
        self._counts["documents"] += 1
        self._yaml_hash.update(record.source.raw_yaml)
        self._diagnostics.extend(replace(issue, document_ordinal=record.source.document_ordinal)
                                 for issue in record.diagnostics)
        payload = record.payload
        if isinstance(payload, ClangdSymbol):
            self._add_symbol(payload, record.source)
        elif isinstance(payload, ClangdReferences):
            self._add_references(payload, record.source)
        elif isinstance(payload, ClangdRelation):
            self._add_relation(payload, record.source)
        else:
            raise TypeError(f"Unsupported export record: {type(payload).__name__}")
        if self._counts["documents"] % 5000 == 0:
            logger.debug("Imported %d YAML documents", self._counts["documents"])

    def _ensure_symbol_id(self, external_id: str) -> SymbolId:
        if external_id not in self._symbol_ids:
            symbol_id = SymbolId(len(self._symbol_ids) + 1)
            self._symbol_ids[external_id] = symbol_id
            self._builder.add_symbol(symbol_id=symbol_id)
        return self._symbol_ids[external_id]

    def _ensure_file(self, uri: str) -> FileId:
        if uri not in self._files:
            file_id = FileId(len(self._files) + 1)
            self._files[uri] = file_id
            content_hash = None
            try:
                data = self._sources.read(uri)
            except (OSError, ValueError) as error:
                self._source_errors[uri] = f"Source unavailable for {uri}: {error}"
            else:
                content_hash = sha256(data).hexdigest()
                self._source_lines[uri] = tuple(data.split(b"\n"))
            self._source_hashes[uri] = content_hash
            self._builder.add_file(file_id=file_id, uri=uri, content_hash=content_hash)
        return self._files[uri]

    def _warn(self, source: SourceDocument, field: str, message: str) -> None:
        self._diagnostics.append(Diagnostic("warning", source.start_line, field, message,
                                            document_ordinal=source.document_ordinal))

    def _mark_partial(self, uri: str, stage: CollectionStage, reason: str) -> None:
        if uri:
            self._partial[self._ensure_file(uri), stage].add(reason)

    def _add_location(self, location: ExportLocation, stage: CollectionStage,
                      source: SourceDocument, field: str) -> LocationId | None:
        uri = location.file_uri
        if not uri:
            self._warn(source, field, "Location has no file URI; omitted from the model")
            return None
        file_id = self._ensure_file(uri)
        if uri in self._source_errors:
            reason = self._source_errors[uri]
            self._mark_partial(uri, stage, reason)
            if uri not in self._reported_source_errors:
                self._warn(source, field, reason)
                self._reported_source_errors.add(uri)
            return None
        if location in self._locations:
            return self._locations[location]
        if location not in self._location_errors:
            try:
                start = utf8_position(self._source_lines[uri], location.start)
                end = utf8_position(self._source_lines[uri], location.end)
            except ValueError as error:
                reason = f"Unavailable location in {uri}: {error}"
                self._location_errors[location] = reason
                self._warn(source, field, reason)
            else:
                key = (file_id, start, end)
                if key not in self._normalized_locations:
                    location_id = LocationId(len(self._normalized_locations) + 1)
                    self._builder.add_location(location_id=location_id, file_id=file_id, start=start, end=end)
                    self._normalized_locations[key] = location_id
                self._locations[location] = self._normalized_locations[key]
                return self._locations[location]
        self._mark_partial(uri, stage, self._location_errors[location])
        return None

    def _add_symbol(self, symbol: ClangdSymbol, source: SourceDocument) -> None:
        symbol_id = self._ensure_symbol_id(symbol.symbol_id)
        if symbol_id in self._symbols:
            raise IndexParseError(source.document_ordinal, source.start_line, "ID",
                                  f"duplicate symbol record {symbol.symbol_id}")
        self._counts["symbol_records"] += 1
        self._symbols[symbol_id] = ExportedSymbol(symbol, source.document_ordinal, source.start_line)
        language = _LANGUAGES.get(symbol.sym_info.language)
        if language is None:
            raise IndexParseError(source.document_ordinal, source.start_line, "SymInfo.Lang",
                                  f"unsupported language {symbol.sym_info.language!r}")
        if language not in self._languages:
            language_id = LanguageId(max((0, *self._languages.values())) + 1)
            self._languages[language] = language_id
            self._builder.add_language(language_id=language_id, name=language)

        kind_id = None
        kind_name = _KINDS.get(symbol.sym_info.kind)
        if kind_name is not None:
            if kind_name not in self._kinds:
                self._kinds[kind_name] = KindId(max((0, *self._kinds.values())) + 1)
                self._builder.add_symbol_kind(kind_id=self._kinds[kind_name], name=kind_name)
            kind_id = self._kinds[kind_name]
        else:
            reason = f"Unknown exported symbol kind: {symbol.sym_info.kind!r}"
            if source.parse_status == "recovered":
                self._counts["recovered_symbols"] += 1
            else:
                self._warn(source, "SymInfo.Kind", reason)
            self._mark_partial(symbol.canonical_declaration.file_uri, CollectionStage.SYMBOLS, reason)
            if symbol.definition:
                self._mark_partial(symbol.definition.file_uri, CollectionStage.SYMBOLS, reason)

        scope_id = GLOBAL_SCOPE_ID
        if symbol.scope:
            scope_id = ScopeId(self._next_scope)
            self._next_scope += 1
            self._builder.add_scope(scope_id=scope_id, qualified_name=symbol.scope)
        declaration = self._add_location(symbol.canonical_declaration, CollectionStage.SYMBOLS,
                                         source, "CanonicalDeclaration")
        definition = (self._add_location(symbol.definition, CollectionStage.SYMBOLS, source, "Definition")
                      if symbol.definition else None)
        self._builder.resolve_symbol(
            symbol_id=symbol_id, name=symbol.name, scope_id=scope_id,
            language_id=self._languages[language], kind_id=kind_id,
            declaration_location_id=declaration, definition_location_id=definition,
            signature=symbol.signature or None,
            template_specialization_args=symbol.template_specialization_args or None,
            documentation=symbol.documentation or None,
            return_type=(symbol.return_type or None) if symbol.sym_info.kind in _CALLABLE_KINDS else None,
        )

    def _add_references(self, group: ClangdReferences, source: SourceDocument) -> None:
        self._counts["reference_groups"] += 1
        target_id = self._ensure_symbol_id(group.target_symbol_id)
        for number, reference in enumerate(group.references):
            self._counts["reference_occurrences"] += 1
            if reference.kind & ~31 or not reference.kind & 7:
                raise IndexParseError(source.document_ordinal, source.start_line, f"References[{number}].Kind",
                                      f"unsupported occurrence flags {reference.kind}")
            kind = ReferenceKind(0)
            for exported_bit, model_bit in _REFERENCE_KINDS:
                if reference.kind & exported_bit:
                    kind |= model_bit
            container_id = (self._ensure_symbol_id(reference.container_symbol_id)
                            if reference.container_symbol_id is not None else None)
            location_id = self._add_location(reference.location, CollectionStage.REFERENCES,
                                             source, f"References[{number}].Location")
            reference_id = None
            if location_id is None:
                self._counts["skipped_references"] += 1
            else:
                key = (target_id, location_id, kind, container_id)
                reference_id = self._reference_ids.get(key)
                if reference_id is None:
                    reference_id = ReferenceId(len(self._reference_ids) + 1)
                    self._reference_ids[key] = reference_id
                    self._builder.add_symbol_reference(reference_id=reference_id, target_symbol_id=target_id,
                        location_id=location_id, kind=kind, container_symbol_id=container_id)
                else:
                    self._counts["duplicate_references"] += 1
            self._references.append(ReferenceImport(reference_id, reference.kind, source.document_ordinal,
                                                     source.start_line, number))

    def _add_relation(self, relation: ClangdRelation, source: SourceDocument) -> None:
        self._counts["relation_records"] += 1
        if relation.predicate not in (0, 1):
            raise IndexParseError(source.document_ordinal, source.start_line, "Predicate",
                                  f"unsupported relation predicate {relation.predicate}")
        subject_id = self._ensure_symbol_id(relation.subject_symbol_id)
        object_id = self._ensure_symbol_id(relation.object_symbol_id)
        if subject_id == object_id:
            reason = "Relation has identical endpoints; retained in report, not representable in the model"
            self._deferred_relations.append(DeferredRelation(relation, source.document_ordinal,
                                                               source.start_line, reason))
            self._warn(source, "Subject/Object", reason)
            return
        if relation in self._relations:
            self._counts["duplicate_relations"] += 1
            return
        self._relations.add(relation)
        if relation.predicate == 0:
            self._counts["inheritance"] += 1
            self._builder.add_class_inheritance(inheritance_id=InheritanceId(self._counts["inheritance"]),
                                                base_symbol_id=subject_id, derived_symbol_id=object_id)
        else:
            self._counts["overrides"] += 1
            self._builder.add_method_override(override_id=OverrideId(self._counts["overrides"]),
                                               overridden_symbol_id=subject_id, overriding_symbol_id=object_id)

    def build_model(self) -> IndexModel:
        # Relations may precede symbols, so attribute deferred relations only now.
        for deferred in self._deferred_relations:
            symbol_id = self._symbol_ids[deferred.relation.subject_symbol_id]
            exported = self._symbols.get(symbol_id)
            if exported:
                self._mark_partial(exported.symbol.canonical_declaration.file_uri,
                                   CollectionStage.SYMBOLS, deferred.reason)
                if exported.symbol.definition:
                    self._mark_partial(exported.symbol.definition.file_uri,
                                       CollectionStage.SYMBOLS, deferred.reason)
        for file_id in self._files.values():
            for stage in CollectionStage:
                reasons = self._partial.get((file_id, stage), set())
                requested = stage in (CollectionStage.SYMBOLS, CollectionStage.REFERENCES)
                state = (CollectionState.PARTIAL if reasons else CollectionState.COMPLETE if requested
                         else CollectionState.NOT_REQUESTED)
                self._builder.add_collection_status(file_id=file_id, stage=stage, status=state,
                                                     error="; ".join(sorted(reasons)) if reasons else None)
        self._result = self._builder.build()
        return self._result

    def build_report(self) -> ImportReport:
        if self._result is None:
            raise RuntimeError("Build and validate the model before creating its report")
        counts = ImportCounts(
            documents=self._counts["documents"], symbol_records=self._counts["symbol_records"],
            reference_groups=self._counts["reference_groups"],
            reference_occurrences=self._counts["reference_occurrences"],
            relation_records=self._counts["relation_records"], recovered_symbols=self._counts["recovered_symbols"],
            symbols=len(self._result.symbols), placeholders=len(self._symbol_ids) - len(self._symbols),
            files=len(self._result.files), locations=len(self._result.locations),
            references=len(self._result.symbol_references), skipped_references=self._counts["skipped_references"],
            duplicate_references=self._counts["duplicate_references"], inheritance=len(self._result.class_inheritance),
            overrides=len(self._result.method_overrides), deferred_relations=len(self._deferred_relations),
            duplicate_relations=self._counts["duplicate_relations"],
        )
        return ImportReport(
            yaml_path=self._yaml_path, yaml_sha256=self._yaml_hash.hexdigest(), counts=counts,
            symbol_ids=self._symbol_ids, exported_symbols=self._symbols,
            references=tuple(self._references), deferred_relations=tuple(self._deferred_relations),
            diagnostics=tuple(self._diagnostics), source_hashes=self._source_hashes,
        )
