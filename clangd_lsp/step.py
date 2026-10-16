"""Add ranges and includes without discovering symbols or depending on YAML."""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import replace
from fnmatch import fnmatchcase
from hashlib import sha256
import logging
from pathlib import Path

from index_model import (
    CollectionStage, CollectionState, File, FileId, IncludeId, IndexBuilder,
    IndexModel, Location, LocationId, Position, ReferenceKind, SnapshotStatus, SourceRangeRole, SymbolId,
)
from index_pipeline import Step, SummaryItem

from .client import ClangdClient
from .records import CompilerDiagnostic, Diagnostic, DiagnosticReason, EnrichmentReport, SourceRange
from .source import SourceText, local_path
from .transport import LspRequestError


logger = logging.getLogger(__name__)
_STAGES = (CollectionStage.SOURCE_RANGES, CollectionStage.INCLUDES)


class ClangdLspStep(Step):
    def __init__(self, client: ClangdClient, *, exclude_paths: Sequence[str] = ()):
        self._client = client
        self._exclude_paths = tuple(exclude_paths)
        self._report: EnrichmentReport | None = None

    @property
    def report(self) -> EnrichmentReport | None:
        return self._report

    def summary(self) -> tuple[SummaryItem, ...]:
        if self._report is None:
            return ()
        report = self._report
        reasons = Counter(d.reason for d in report.diagnostics)
        return (
            SummaryItem("LSP files processed", report.files_processed),
            SummaryItem("LSP files skipped", report.files_skipped),
            SummaryItem("LSP files excluded", report.files_excluded),
            SummaryItem("LSP source ranges added", report.ranges_added),
            SummaryItem("LSP includes added", report.includes_added),
            SummaryItem("LSP occurrences matched", f"{report.occurrences_matched}/{report.occurrences_total}"),
            SummaryItem("clangd_lsp diagnostics", len(report.diagnostics)),
        ) + tuple(SummaryItem(f"LSP {reason.value}", count) for reason, count in sorted(reasons.items()))

    def run(self, model: IndexModel) -> IndexModel:
        self._report = None
        work = _Enrichment(model, self._client, self._exclude_paths)
        try:
            self._client.start(Path(model.metadata.project_root).resolve())
            result = work.run()
        finally:
            self._client.close()
        report = work.report()
        for diagnostic in report.diagnostics:
            stage = diagnostic.stage.value if diagnostic.stage else "file"
            where = diagnostic.uri
            if diagnostic.source_range is not None:
                start, end = diagnostic.source_range.start, diagnostic.source_range.end
                where += (f":{start.line + 1}:{start.column + 1}-{end.line + 1}:{end.column + 1}"
                          " (1-based lines and UTF-8 byte columns)")
            logger.warning("%s: %s: %s: %s", where, stage, diagnostic.reason.value, diagnostic.message)
        self._report = report
        return result


class _Enrichment:
    """Mutable staging state for one run; discarded if the session fails."""

    def __init__(self, model: IndexModel, client: ClangdClient, exclude_paths: tuple[str, ...]):
        self.model = model
        self.client = client
        self.exclude_paths = exclude_paths
        self.builder = IndexBuilder.from_model(model)
        self.builder.update_metadata(replace(model.metadata, status=SnapshotStatus.BUILDING,
                                             completed_at=None, error=None))
        self.files = defaultdict(list)
        for file in model.files:
            try:
                self.files[local_path(file.uri).as_uri()].append(file.file_id)
            except (OSError, ValueError):
                pass
        self.locations = {(loc.file.file_id, loc.start, loc.end): loc.location_id for loc in model.locations}
        self.ranges = {(r.symbol.symbol_id, r.location.location_id, r.role) for r in model.symbol_source_ranges}
        self.includes = {(e.includer.file_id, e.included.file_id) for e in model.file_includes}
        self.next_file = max((f.file_id for f in model.files), default=0) + 1
        self.next_location = max((loc.location_id for loc in model.locations), default=0) + 1
        self.next_include = max((e.include_id for e in model.file_includes), default=0) + 1
        self.occurrences = defaultdict(lambda: defaultdict(set))
        for symbol in model.symbols:
            for location, role in ((symbol.declaration, SourceRangeRole.DECLARATION),
                                   (symbol.definition, SourceRangeRole.DEFINITION)):
                if location is not None:
                    self._occurrence(symbol.symbol_id, location, role)
        for reference in model.symbol_references:
            for flag, role in ((ReferenceKind.DECLARATION, SourceRangeRole.DECLARATION),
                               (ReferenceKind.DEFINITION, SourceRangeRole.DEFINITION)):
                if reference.kind & flag:
                    self._occurrence(reference.target.symbol_id, reference.location, role)
        self.diagnostics = []
        self.files_processed = self.files_skipped = self.ranges_added = self.includes_added = 0
        self.files_excluded = self.occurrences_matched = self.occurrences_total = 0

    def _occurrence(self, symbol_id: SymbolId, location: Location, role: SourceRangeRole) -> None:
        self.occurrences[location.file.file_id][location.start, location.end].add((symbol_id, role))

    def run(self) -> IndexModel:
        root = Path(self.model.metadata.project_root).resolve()
        for file in sorted(self.model.files, key=lambda f: f.uri):
            try:
                path = local_path(file.uri)
            except (OSError, ValueError) as error:
                self.diagnostics.append(Diagnostic(file.uri, None, str(error), DiagnosticReason.SOURCE_FAILURE))
                continue
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                if any(fnmatchcase(relative, pattern) for pattern in self.exclude_paths):
                    self.files_excluded += 1
                    logger.info("LSP excluded %s", relative)
                    continue
                self.occurrences_total += sum(len(matches) for matches in self.occurrences[file.file_id].values())
                self._file(file, path)
        return self.builder.build()

    def _file(self, file: File, path: Path) -> None:
        uri = path.as_uri()
        logger.info("LSP inspecting %s", path)
        try:
            if len(self.files[uri]) != 1:
                raise ValueError("Multiple model files resolve to this URI")
            data = path.read_bytes()
            if file.content_hash is None or sha256(data).hexdigest() != file.content_hash:
                raise ValueError("Source hash is missing or differs from the imported file")
            source = SourceText(data)
        except (OSError, ValueError) as error:
            self.files_skipped += 1
            for stage in _STAGES:
                self._status(file, stage, [Diagnostic(file.uri, stage, str(error),
                                                      DiagnosticReason.SOURCE_FAILURE)], failed=True)
            return

        parse_errors = self.client.open_document(uri, source.text)
        self._ranges(file, uri, source, parse_errors)
        self._includes(file, uri, source, parse_errors)
        # Fatal errors skip this notification; run() closes the failed session.
        self.client.close_document(uri)
        self.files_processed += 1

    def _ranges(self, file: File, uri: str, source: SourceText,
                parse_errors: tuple[CompilerDiagnostic, ...]) -> None:
        stage = CollectionStage.SOURCE_RANGES
        try:
            symbols = self.client.document_symbols(uri)
        except (LspRequestError, ValueError) as error:
            self._request_failure(file, stage, error)
            return
        errors = self._compiler_errors(file, stage, source, parse_errors)
        expected = self.occurrences[file.file_id]
        matched = set()
        rejected = {}
        pending = list(reversed(symbols))
        while pending:
            symbol = pending.pop()
            pending.extend(reversed(symbol.children))
            selection = None
            try:
                selection = source.range(symbol.selection, self.client.position_encoding)
                extent = source.range(symbol.extent, self.client.position_encoding)
                if not extent[0] <= selection[0] <= selection[1] <= extent[1]:
                    raise ValueError("Selection is outside the full range")
            except ValueError as error:
                if selection in expected:
                    rejected[selection] = (DiagnosticReason.INVALID_RANGE, str(error), ())
                else:
                    errors.append(Diagnostic(file.uri, stage, f"{symbol.name}: {error}",
                                             DiagnosticReason.INVALID_RANGE,
                                             source_range=SourceRange(*selection) if selection else None))
                continue
            matches = expected.get(selection, set())
            candidates = tuple(sorted({symbol_id for symbol_id, _ in matches}))
            if not candidates:
                errors.append(Diagnostic(file.uri, stage, f"{symbol.name!r}: no existing symbol at this selection",
                                         DiagnosticReason.UNMATCHED_OUTLINE, source_range=SourceRange(*selection)))
                continue
            if len(candidates) > 1:
                rejected[selection] = (DiagnosticReason.AMBIGUOUS_MATCH,
                                       f"multiple candidate symbol IDs: {', '.join(map(str, candidates))}", candidates)
                continue
            location_id = self._location(file.file_id, *extent)
            for symbol_id, role in sorted(matches):
                key = (symbol_id, location_id, role)
                if key not in self.ranges:
                    self.builder.add_symbol_source_range(symbol_id=symbol_id, location_id=location_id, role=role)
                    self.ranges.add(key)
                    self.ranges_added += 1
                matched.add((selection, symbol_id, role))
        self.occurrences_matched += len(matched)
        for selection, matches in sorted(expected.items()):
            for symbol_id, role in sorted(matches):
                if (selection, symbol_id, role) in matched:
                    continue
                reason, detail, candidates = rejected.get(selection, (
                    DiagnosticReason.MISSING_OUTLINE, "no outline entry at the exact name range", ()))
                name = self.model.symbol(symbol_id).name
                message = f"symbol {symbol_id} {name!r}, {role.value}: {detail}"
                errors.append(Diagnostic(file.uri, stage, message, reason, symbol_id,
                                         SourceRange(*selection), candidates))
        self._status(file, stage, errors)

    def _includes(self, file: File, uri: str, source: SourceText,
                  parse_errors: tuple[CompilerDiagnostic, ...]) -> None:
        stage = CollectionStage.INCLUDES
        try:
            links = self.client.document_links(uri)
        except (LspRequestError, ValueError) as error:
            self._request_failure(file, stage, error)
            return
        errors = self._compiler_errors(file, stage, source, parse_errors)
        for link in links:
            try:
                extent = SourceRange(*source.range(link.extent, self.client.position_encoding))
            except ValueError as error:
                errors.append(Diagnostic(file.uri, stage, str(error), DiagnosticReason.INVALID_RANGE))
                continue
            try:
                if not link.target:
                    raise ValueError("Include link has no resolved target")
                target = local_path(link.target).as_uri()
                candidates = self.files[target]
                if len(candidates) > 1:
                    raise ValueError(f"Multiple model files resolve to include target {target}")
            except (OSError, ValueError) as error:
                errors.append(Diagnostic(file.uri, stage, str(error), DiagnosticReason.UNRESOLVED_INCLUDE,
                                         source_range=extent))
                continue
            if not candidates:
                file_id = FileId(self.next_file)
                self.next_file += 1
                self.builder.add_file(file_id=file_id, uri=target)
                for collection in CollectionStage:
                    self.builder.add_collection_status(file_id=file_id, stage=collection,
                                                       status=CollectionState.NOT_REQUESTED)
                candidates.append(file_id)
            edge = (file.file_id, candidates[0])
            if edge not in self.includes:
                self.builder.add_file_include(include_id=IncludeId(self.next_include),
                                              includer_file_id=edge[0], included_file_id=edge[1])
                self.next_include += 1
                self.includes.add(edge)
                self.includes_added += 1
        self._status(file, stage, errors)

    def _location(self, file_id: FileId, start: Position, end: Position) -> LocationId:
        key = (file_id, start, end)
        if key not in self.locations:
            location_id = LocationId(self.next_location)
            self.next_location += 1
            self.builder.add_location(location_id=location_id, file_id=file_id, start=start, end=end)
            self.locations[key] = location_id
        return self.locations[key]

    def _compiler_errors(self, file: File, stage: CollectionStage, source: SourceText,
                         parse_errors: tuple[CompilerDiagnostic, ...]) -> list[Diagnostic]:
        errors = []
        for error in parse_errors:
            extent = None
            if error.source_range is not None:
                try:
                    extent = SourceRange(*source.range(error.source_range, self.client.position_encoding))
                except ValueError as invalid:
                    errors.append(Diagnostic(file.uri, stage, f"Compiler diagnostic range: {invalid}",
                                             DiagnosticReason.INVALID_RANGE))
            errors.append(Diagnostic(file.uri, stage, error.message, DiagnosticReason.COMPILER_ERROR,
                                     source_range=extent))
        return errors

    def _request_failure(self, file: File, stage: CollectionStage, error: Exception) -> None:
        reason = DiagnosticReason.REQUEST_FAILURE if isinstance(error, LspRequestError) else DiagnosticReason.INVALID_RESULT
        self._status(file, stage, [Diagnostic(file.uri, stage, str(error), reason)], failed=True)

    def _status(self, file: File, stage: CollectionStage, errors: list[Diagnostic], *, failed: bool = False) -> None:
        errors = list(dict.fromkeys(errors))
        state = CollectionState.COMPLETE
        if errors:
            state = CollectionState.FAILED if failed else CollectionState.PARTIAL
        counts = Counter(error.reason for error in errors)
        summary = "; ".join(f"{reason.value}: {count}" for reason, count in sorted(counts.items()))
        self.builder.update_collection_status(file_id=file.file_id, stage=stage, status=state,
                                              error=summary or None)
        self.diagnostics.extend(errors)

    def report(self) -> EnrichmentReport:
        return EnrichmentReport(self.files_processed, self.files_skipped, self.files_excluded,
                                self.ranges_added, self.includes_added, self.occurrences_matched,
                                self.occurrences_total, tuple(self.diagnostics))
