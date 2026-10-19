"""Collect structural LSP facts using only existing model identities."""

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from hashlib import sha256
import logging

from index_model import CollectionStage, CollectionState, ReferenceKind

from .records import Diagnostic, DiagnosticReason, LspLocation, LspRange, StructuralReport
from .source import SourceText, local_path
from .transport import LspRequestError, LspSessionError


logger = logging.getLogger(__name__)
_CALLABLE = {"function", "constructor", "destructor", "instance_method", "static_method", "conversion_function"}
_CLASS = {"class", "struct", "union"}


@dataclass(frozen=True, slots=True)
class _Reference:
    target: int
    location: int
    kind: ReferenceKind
    container: int | None


class StructuralEnrichment:
    """Adapter-local observations; the builder and immutable records stay generic."""

    def __init__(self, work):
        self.work = work
        self.client = work.client
        self.model = work.model
        self.sources = {}
        self.queries = defaultdict(list)
        self.selections = defaultdict(set)
        self.identities = {}
        self.requests = {}
        self.active_uri = None
        self.current_file = None
        self.requested = set()
        self.problems = defaultdict(set)
        self.counts = Counter()
        for symbol in self.model.symbols:
            for loc in (symbol.declaration, symbol.definition):
                if loc is not None:
                    self.selections[loc.file.file_id, loc.start, loc.end].add(symbol.symbol_id)
            if symbol.name is None:
                continue
            for anchor in (symbol.definition, symbol.declaration):
                if anchor is not None and work.selected(anchor.file):
                    self.queries[anchor.file.file_id].append((symbol, anchor))
                    break
        for reference in self.model.symbol_references:
            if reference.kind & (ReferenceKind.DECLARATION | ReferenceKind.DEFINITION):
                loc = reference.location
                self.selections[loc.file.file_id, loc.start, loc.end].add(reference.target.symbol_id)
        self.references = {}
        self.by_site = defaultdict(list)
        for ref in self.model.symbol_references:
            fact = _Reference(ref.target.symbol_id, ref.location.location_id, ref.kind,
                              ref.container.symbol_id if ref.container else None)
            self.references[ref.reference_id] = fact
            self.by_site[fact.target, fact.location].append(ref.reference_id)
        self.next_reference = max(self.references, default=0) + 1
        self.new_references = set()
        self.changed_references = set()
        self.call_candidates = set()
        self.inheritance = {(edge.base.symbol_id, edge.derived.symbol_id) for edge in self.model.class_inheritance}
        self.next_inheritance = max((edge.inheritance_id for edge in self.model.class_inheritance), default=0) + 1

    def verify_index(self):
        # No documents are open yet, so a match proves the static index is loaded.
        candidates = [(symbol, anchor) for entries in self.queries.values() for symbol, anchor in entries
                      if symbol.name.isidentifier() and symbol.kind and
                      symbol.kind.name in (_CALLABLE | _CLASS | {"variable", "enum"})]
        candidates.sort(key=lambda pair: (pair[0].kind.name not in _CLASS, pair[0].name.startswith("_"),
                                          -len(pair[0].name), pair[0].symbol_id))
        for symbol, _ in candidates[:3]:
            result = self.client.workspace_symbols(symbol.name)
            for found in result.items:
                if found.name != symbol.name:
                    continue
                for loc in (symbol.declaration, symbol.definition):
                    if loc is None or not self.work.selected(loc.file):
                        continue
                    try:
                        if local_path(found.location.uri).as_uri() != local_path(loc.file.uri).as_uri():
                            continue
                        source = self._source(loc.file)
                        start, end = source.range(found.location.extent, self.client.position_encoding)
                    except (OSError, ValueError):
                        continue
                    if (start, end) == (loc.start, loc.end):
                        logger.debug("LSP static index verified using %s", symbol.name)
                        return
        raise LspSessionError("Static index could not be verified against an exported project symbol")

    def _source(self, file):
        if file.file_id not in self.sources:
            data = local_path(file.uri).read_bytes()
            if file.content_hash is None or sha256(data).hexdigest() != file.content_hash:
                raise ValueError("Source hash is missing or differs from the imported file")
            self.sources[file.file_id] = SourceText(data)
        return self.sources[file.file_id]

    def _problem(self, stage, reason, message, symbol_id=None, *, uri=None):
        diagnostic = Diagnostic(uri or self.current_file.uri, stage, message, reason, symbol_id)
        self.work.diagnostics.append(diagnostic)
        if reason is not DiagnosticReason.OUT_OF_SCOPE and stage is not None:
            self.problems[self.current_file.file_id, stage].add((reason, message))

    def _checked(self, location, stage, symbol_id):
        try:
            path = local_path(location.uri)
            uri = path.as_uri()
            if not self.work.path_selected(path):
                self._problem(stage, DiagnosticReason.OUT_OF_SCOPE, "Returned location is outside the selected scope",
                              symbol_id, uri=uri)
                return None
            ids = self.work.files.get(uri, ())
            if len(ids) != 1:
                raise ValueError("Returned location has no unique existing model file")
            file = self.model.file(ids[0])
            source = self._source(file)
            start, end = source.range(location.extent, self.client.position_encoding)
            return file, source, start, end
        except (OSError, ValueError, KeyError) as error:
            self._problem(stage, DiagnosticReason.SOURCE_FAILURE, str(error), symbol_id, uri=location.uri)
            return None

    def _identity(self, location, stage, symbol_id, identifier=None):
        checked = self._checked(location, stage, symbol_id)
        if checked is None:
            return None
        file, source, start, end = checked
        candidates = self.selections[file.file_id, start, end]
        uri = local_path(location.uri).as_uri()
        if identifier is None:
            key = (uri, location.extent.start)
            if key not in self.identities:
                opened = uri != self.active_uri
                try:
                    if opened:
                        errors = self.client.open_document(uri, source.text)
                        if errors:
                            self._problem(stage, DiagnosticReason.COMPILER_ERROR,
                                          "Identity target has compiler errors", symbol_id, uri=uri)
                    self.identities[key] = self.client.symbol_info(uri, location.extent.start)
                except (LspRequestError, ValueError) as error:
                    self.identities[key] = error
                except LspSessionError:
                    # A broken transport must only be closed by the step owner.
                    opened = False
                    raise
                finally:
                    if opened:
                        self.client.close_document(uri)
            result = self.identities[key]
            if isinstance(result, Exception):
                self._problem(stage, DiagnosticReason.REQUEST_FAILURE, str(result), symbol_id)
                return None
            identifiers = {info.id for info in result}
            if identifiers and None not in identifiers:
                mapped = {self.work.symbol_ids.get(value.upper()) for value in identifiers}
            elif not self.work.symbol_ids and len(candidates) == 1:
                mapped = set(candidates)
            else:
                mapped = set()
        else:
            mapped = {self.work.symbol_ids.get(identifier.upper())}
        if len(mapped) != 1 or None in mapped:
            self._problem(stage, DiagnosticReason.UNRESOLVED_IDENTITY,
                          "No unique mapped clangd identity", symbol_id, uri=uri)
            return None
        resolved = next(iter(mapped))
        if candidates and resolved not in candidates:
            self._problem(stage, DiagnosticReason.CONFLICT,
                          "Clangd identity conflicts with imported name location", symbol_id, uri=uri)
            return None
        return resolved

    def _request(self, method, stage, symbol_id, *args):
        key = (method, *args)
        if key not in self.requests:
            try:
                self.requests[key] = getattr(self.client, method)(*args)
            except (LspRequestError, ValueError) as error:
                self.requests[key] = error
                self.counts["queries_failed"] += 1
        result = self.requests[key]
        if isinstance(result, Exception):
            self._problem(stage, DiagnosticReason.REQUEST_FAILURE, f"{method}: {result}", symbol_id)
            return ()
        for issue in result.issues:
            self._problem(stage, DiagnosticReason.DECODE_ERROR, f"{method} {issue.path}: {issue.message}", symbol_id)
        return result.items

    def process_file(self, file, uri, source, parse_errors):
        self.current_file, self.active_uri = file, uri
        self.sources[file.file_id] = source
        for stage in (CollectionStage.SYMBOLS, CollectionStage.REFERENCES):
            self.requested.add((file.file_id, stage))
            for error in parse_errors:
                self._problem(stage, DiagnosticReason.COMPILER_ERROR, error.message)
        entries = self.queries[file.file_id]
        for index, (symbol, anchor) in enumerate(entries, 1):
            if index == 1 or index % 25 == 0:
                logger.debug("LSP structural %s: symbol %d/%d (%s)", file.uri, index, len(entries), symbol.name)
            try:
                start = source.lsp_position(anchor.start, self.client.position_encoding)
                end = source.lsp_position(anchor.end, self.client.position_encoding)
            except ValueError as error:
                for stage in (CollectionStage.SYMBOLS, CollectionStage.REFERENCES):
                    self._problem(stage, DiagnosticReason.INVALID_RANGE, str(error), symbol.symbol_id)
                continue
            seed = LspLocation(uri, LspRange(start, end))
            resolved = self._identity(seed, CollectionStage.SYMBOLS, symbol.symbol_id)
            if resolved != symbol.symbol_id:
                self._problem(CollectionStage.REFERENCES, DiagnosticReason.UNRESOLVED_IDENTITY,
                              "Symbol query anchor does not verify the imported identity", symbol.symbol_id)
                continue
            self.counts["symbols_queried"] += 1
            self._navigation(symbol, uri, start)
            for location in self._request("references", CollectionStage.REFERENCES, symbol.symbol_id, uri, start):
                self._reference(symbol.symbol_id, location)
            kind = symbol.kind.name if symbol.kind else None
            if kind in _CALLABLE:
                self._calls(symbol, uri, start)
            if kind in _CLASS:
                self._bases(symbol, uri, start)
        self.active_uri = None

    def failed_file(self, file, message):
        self.current_file = file
        for stage in (CollectionStage.SYMBOLS, CollectionStage.REFERENCES):
            self.requested.add((file.file_id, stage))
            self._problem(stage, DiagnosticReason.SOURCE_FAILURE, message)

    def _navigation(self, symbol, uri, position):
        for field, method in (("declaration", "declarations"), ("definition", "definitions")):
            if getattr(symbol, field) is not None:
                continue
            matches = set()
            for target in self._request(method, CollectionStage.SYMBOLS, symbol.symbol_id, uri, position):
                if self._identity(target, CollectionStage.SYMBOLS, symbol.symbol_id) != symbol.symbol_id:
                    continue
                if field == "definition":
                    # clangd's definition navigation can fall back to a declaration.
                    # Only symbolInfo's explicit definition location proves this role.
                    key = (local_path(target.uri).as_uri(), target.extent.start)
                    infos = self.identities.get(key, ())
                    confirmed = False
                    for info in infos:
                        try:
                            confirmed |= (info.definition is not None and info.definition.extent == target.extent
                                          and local_path(info.definition.uri) == local_path(target.uri))
                        except (OSError, ValueError):
                            pass
                    if not confirmed:
                        continue
                checked = self._checked(target, CollectionStage.SYMBOLS, symbol.symbol_id)
                if checked is not None:
                    file, _, start, end = checked
                    matches.add((file.file_id, start, end))
            if len(matches) > 1:
                self._problem(CollectionStage.SYMBOLS, DiagnosticReason.CONFLICT,
                              f"Multiple verified {field} locations; canonical location left unchanged", symbol.symbol_id)
            elif matches:
                file_id, start, end = next(iter(matches))
                location_id = self.work._location(file_id, start, end)
                self.work.builder.enrich_symbol(symbol_id=symbol.symbol_id, **{field + "_location_id": location_id})
                self.selections[file_id, start, end].add(symbol.symbol_id)
                self.counts["locations_filled"] += 1

    def _reference(self, target, location, caller=None):
        checked = self._checked(location, CollectionStage.REFERENCES, target)
        if checked is None:
            return
        file, _, start, end = checked
        location_id = self.work._location(file.file_id, start, end)
        site = (target, location_id)
        if caller is not None:
            self.call_candidates.add((target, location_id, caller))
        candidates = self.by_site[site]
        usable = [key for key in candidates if self.references[key].kind & ReferenceKind.REFERENCE
                  and not self.references[key].kind & (ReferenceKind.DECLARATION | ReferenceKind.DEFINITION)]
        if caller is None and usable:
            return
        if caller is not None and any(self.references[key].container == caller for key in usable):
            return
        compatible = [key for key in usable if self.references[key].container == caller]
        if caller is not None and not compatible:
            compatible = [key for key in usable if self.references[key].container is None]
        # clangd 21 reports address-taking in BOTH incoming and outgoing calls.
        # The hierarchy proves a reference and container, not the model's CALL bit.
        kind = ReferenceKind.REFERENCE
        if len(compatible) == 1:
            key = compatible[0]
            original = self.references[key]
            updated = replace(original, kind=original.kind | kind, container=caller if caller is not None else original.container)
            if updated != original:
                self.references[key] = updated
                self.changed_references.add(key)
            return
        if candidates:
            self._problem(CollectionStage.REFERENCES, DiagnosticReason.CONFLICT,
                          "Reference site has incompatible or ambiguous existing observations; retained separately", target)
        key = self.next_reference
        self.next_reference += 1
        self.references[key] = _Reference(target, location_id, kind, caller)
        self.by_site[site].append(key)
        self.new_references.add(key)
        self.work.builder.add_symbol_reference(reference_id=key, target_symbol_id=target,
                                               location_id=location_id, kind=kind, container_symbol_id=caller)

    def _calls(self, symbol, uri, position):
        stage = CollectionStage.REFERENCES
        for item in self._request("prepare_calls", stage, symbol.symbol_id, uri, position):
            if self._identity(item.location, stage, symbol.symbol_id, item.symbol_id) != symbol.symbol_id:
                continue
            for method in ("incoming_calls", "outgoing_calls"):
                for call in self._request(method, stage, symbol.symbol_id, item):
                    other = self._identity(call.other.location, stage, symbol.symbol_id, call.other.symbol_id)
                    if other is None:
                        continue
                    caller, callee = ((other, symbol.symbol_id) if method == "incoming_calls"
                                      else (symbol.symbol_id, other))
                    source_uri = call.other.location.uri if method == "incoming_calls" else item.location.uri
                    for extent in call.ranges:
                        self._reference(callee, LspLocation(source_uri, extent), caller)

    def _bases(self, symbol, uri, position):
        before = len(self.work.diagnostics)
        self.counts["hierarchy_queries"] += 1
        for item in self._request("prepare_types", None, symbol.symbol_id, uri, position):
            if self._identity(item.location, None, symbol.symbol_id, item.symbol_id) != symbol.symbol_id:
                continue
            for parent in self._request("supertypes", None, symbol.symbol_id, item):
                base = self._identity(parent.location, None, symbol.symbol_id, parent.symbol_id)
                if base is None:
                    continue
                edge = (base, symbol.symbol_id)
                if base == symbol.symbol_id:
                    self._problem(None, DiagnosticReason.CONFLICT, "Clangd reported self-inheritance", symbol.symbol_id)
                elif edge not in self.inheritance:
                    self.work.builder.add_class_inheritance(inheritance_id=self.next_inheritance,
                                                            base_symbol_id=base, derived_symbol_id=symbol.symbol_id)
                    self.next_inheritance += 1
                    self.inheritance.add(edge)
                    self.counts["inheritance_added"] += 1
        if any(d.reason is not DiagnosticReason.OUT_OF_SCOPE for d in self.work.diagnostics[before:]):
            self.counts["hierarchy_partial"] += 1

    def finish_statuses(self):
        for file_id, stage in sorted(self.requested):
            old = self.model.collection_status(file_id, stage)
            problems = self.problems[file_id, stage]
            if not problems:
                # Additional observations cannot erase gaps in the imported data.
                if old.status is CollectionState.NOT_REQUESTED:
                    self.work.builder.update_collection_status(file_id=file_id, stage=stage,
                                                               status=CollectionState.COMPLETE)
                continue
            counts = Counter(reason for reason, _ in problems)
            details = "; ".join(f"{reason.value}: {count}" for reason, count in sorted(counts.items()))
            error = f"{old.error}; LSP: {details}" if old.error else f"LSP: {details}"
            self.work.builder.update_collection_status(file_id=file_id, stage=stage,
                                                       status=CollectionState.PARTIAL, error=error)

    def finish_references(self, model):
        if not self.changed_references:
            return model
        references = []
        for ref in model.symbol_references:
            if ref.reference_id in self.changed_references:
                fact = self.references[ref.reference_id]
                ref = replace(ref, kind=fact.kind, container=model.symbol(fact.container) if fact.container else None)
            references.append(ref)
        return replace(model, symbol_references=tuple(references))

    def report(self):
        return StructuralReport(**self.counts, references_added=len(self.new_references),
                                references_enriched=len(self.changed_references - self.new_references),
                                call_candidates=len(self.call_candidates))
