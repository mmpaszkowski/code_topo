"""Merge verified compiler facts into existing immutable model records."""
from collections import defaultdict
from dataclasses import replace
from fnmatch import fnmatchcase
from hashlib import sha1, sha256
import logging
from pathlib import Path
import subprocess
from time import monotonic
from urllib.parse import unquote, urlsplit

from index_model import (IndexBuilder, CollectionStage, CollectionState,
                         Position, ReferenceKind, SourceRangeRole)
from index_pipeline import Step, SummaryItem
from progress import NO_PROGRESS, ProgressReporter
from .client import Extractor
from .commands import commands
from .records import Issue, Report
from .structural import StructuralFacts

logger = logging.getLogger(__name__)
METRICS_VERSION = 'clang-ast-body-v1'


def symbol_key(usr: str) -> str:
    return sha1(usr.encode('utf-8')).hexdigest()[:16].upper() if usr else ''


def file_path(uri):
    value = urlsplit(uri)
    if value.scheme != 'file' or value.netloc not in ('', 'localhost'):
        raise ValueError('AST enrichment requires local file URIs')
    return str(Path(unquote(value.path)).resolve())


class ClangAstStep(Step):
    def __init__(self, extractor: Extractor, compile_commands_dir: Path, *,
                 symbol_ids_provider, remove_compile_flags=(), exclude_paths=(),
                 progress: ProgressReporter = NO_PROGRESS):
        self.progress = progress
        self.extractor = extractor
        self.directory = Path(compile_commands_dir)
        self.ids_provider = symbol_ids_provider
        self.remove_flags = tuple(remove_compile_flags)
        self.exclude = tuple(exclude_paths)
        self.report = None

    def summary(self):
        if self.report is None:
            return ()
        r = self.report
        return tuple(SummaryItem(label, value) for label, value in (
            ('AST contexts', r.contexts_processed), ('AST failed contexts', r.contexts_failed),
            ('AST matched symbols', r.symbols_matched), ('AST unmatched symbols', r.symbols_unmatched),
            ('AST bodies added', r.bodies_added), ('AST calls confirmed', r.calls_confirmed),
            ('AST placeholders resolved', r.placeholders_resolved),
            ('AST scope owners filled', r.scope_owners_filled), ('AST symbol fields filled', r.symbol_fields_filled),
            ('AST inheritance enriched', r.inheritance_enriched), ('AST overrides added', r.overrides_added),
            ('AST conflicts', r.conflicts), ('AST seconds', f'{r.elapsed_seconds:.2f}')))

    def run(self, model):
        self.report = None
        self.progress.phase('setting up AST extraction')
        start = monotonic()
        if model.metadata.metrics_version not in (None, METRICS_VERSION):
            raise ValueError('Cannot mix AST body metrics versions')
        if self.ids_provider is None:
            raise ValueError('AST enrichment requires the YAML symbol identity provider')
        mapping = dict(self.ids_provider())
        if not mapping:
            raise ValueError('AST enrichment requires a completed YAML import')
        self.extractor.check()
        work = _Merge(model, mapping, self.exclude)
        selected_commands = [(context, command) for context, command in commands(self.directory, self.remove_flags)
                             if work.selected(command['file'])]
        failed = 0
        self.progress.phase('compiling', total=len(selected_commands), unit='contexts')
        for number, (context, command) in enumerate(selected_commands, 1):
            self.progress.update(detail=command['file'])
            try:
                result = self.extractor.run(command, tuple(work.files), context,
                                            metadata_files=tuple(work.all_files), ids=tuple(mapping))
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                failed += 1
                # Without a complete include inventory every selected file may be affected.
                work.fail(context, tuple(work.files), f'Extractor failed: {error}')
                self.progress.update(number)
                continue
            work.diagnostics.extend(result.issues)
            if not result.ok:
                failed += 1
                work.fail(context, tuple(work.files), 'Compilation failed; context observations discarded')
                self.progress.update(number)
                continue
            work.accept(context, result)
            self.progress.update(number)
        if not selected_commands:
            work.fail('', tuple(work.files), 'No project compilation commands selected')
        self.progress.phase('merging and validating')
        result = work.finish()
        self.progress.phase('building report')
        self.report = Report(len(selected_commands), failed, len(work.matched), len(work.unmatched),
                             work.bodies_added, work.calls_confirmed, work.conflicts,
                             monotonic() - start, tuple(dict.fromkeys(work.diagnostics)),
                             **{key: work.structural.counts[key] for key in ('placeholders_resolved', 'scope_owners_filled',
                                  'symbol_fields_filled', 'inheritance_enriched', 'overrides_added')},
                             eligibility=tuple(sorted(work.eligibility_counts().items())))
        return result


class _Merge:
    def __init__(self, model, mapping, exclude):
        self.model = model
        self.mapping = mapping
        counts = defaultdict(int)
        for identifier in mapping.values():
            counts[identifier] += 1
        self.unique_ids = {identifier for identifier, count in counts.items() if count == 1}
        self.root = Path(model.metadata.project_root).resolve()
        self.exclude = exclude
        self.files = {}
        self.all_files = {}
        for file in model.files:
            try:
                path = file_path(file.uri)
            except ValueError:
                continue
            self.all_files[path] = file
            if self.selected(path):
                self.files[path] = file
        self.symbols = {s.symbol_id: s for s in model.symbols if s.name is not None}
        self.all_symbols = {s.symbol_id for s in model.symbols}
        self.structural = StructuralFacts(self)
        self.matched, self.unmatched = set(), set()
        self.diagnostics = []
        self.problems = defaultdict(set)
        self.touched = set()
        self.sources = {}
        self.bodies = defaultdict(set)
        self.definitions = defaultdict(set)
        self.calls = defaultdict(set)
        self.conflicts = self.bodies_added = self.calls_confirmed = 0
        self.builder = IndexBuilder.from_model(model)
        self.locations = {(l.file.file_id, l.start, l.end): l.location_id for l in model.locations}
        self.next_location = max((l.location_id for l in model.locations), default=0) + 1
        self.ranges = {(r.symbol.symbol_id, r.location.location_id, r.role) for r in model.symbol_source_ranges}

    def selected(self, path):
        try:
            relative = Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return False
        return not any(fnmatchcase(relative, pattern) for pattern in self.exclude)

    def issue(self, context, path, message, stage='references'):
        stages = (stage,) if isinstance(stage, str) else stage
        for selected in stages:
            self.diagnostics.append(Issue(context, path, message, selected))
            if path in self.all_files:
                self.problems[path, selected].add(message)
                self.touched.add(path)

    def fail(self, context, paths, message):
        for path in paths:
            self.issue(context, path, message, ('symbols', 'metrics', 'source_ranges', 'references'))

    def identifier(self, usr):
        identifier = self.mapping.get(symbol_key(usr))
        return identifier if identifier in self.all_symbols and identifier in self.unique_ids else None

    def eligibility_counts(self):
        from collections import Counter
        result = Counter()
        for identifier, values in self.structural.eligibility.items():
            if values == {'not_applicable'}:
                continue
            if identifier in self.bodies and len(self.bodies[identifier]) == 1:
                result['analyzed'] += 1
            elif len(values) != 1:
                result['conflicting'] += 1
            else:
                result[next(iter(values)) if values != {'written'} else 'unsupported_or_unobserved'] += 1
        return result

    def identity(self, usr):
        identifier = self.mapping.get(symbol_key(usr))
        if identifier not in self.symbols or identifier not in self.unique_ids:
            self.unmatched.add(usr)
            return None
        self.matched.add(identifier)
        return identifier

    def accept(self, context, result):
        valid = set()
        for path, digest in result.files:
            if path not in self.all_files:
                continue
            if path in self.files:
                self.touched.add(path)
            try:
                data = Path(path).read_bytes()
                if self.all_files[path].content_hash != digest or sha256(data).hexdigest() != digest:
                    raise ValueError('Source hash differs from indexed or compiler bytes')
                self.sources[path] = data
                valid.add(path)
            except (OSError, ValueError) as error:
                self.issue(context, path, str(error), ('symbols', 'metrics', 'source_ranges', 'references'))
        for issue in result.issues:
            if issue.severity != 'warning':
                stages = ('metrics', 'source_ranges') if issue.stage == 'metrics' else issue.stage
                self.issue(context, issue.path, issue.message, stages)
        self.structural.accept(context, result, valid)
        for body in result.bodies:
            path = body.body.path
            if path not in valid:
                continue
            symbol = self.identifier(body.usr)
            if symbol is None:
                self.unmatched.add(body.usr)
                self.issue(context, path, f'Unmatched body identity: {body.usr}', ('symbols', 'metrics', 'source_ranges'))
                continue
            self.matched.add(symbol)
            try:
                self.coordinates(body.body)
                if body.definition is not None:
                    if body.definition.path not in valid:
                        raise ValueError('Definition has no verified indexed source')
                    self.coordinates(body.definition)
                    self.definitions[symbol].add(body.definition)
                self.bodies[symbol].add((body.body, body.metrics))
            except ValueError as error:
                self.issue(context, path, str(error), ('metrics', 'source_ranges'))
        for call in result.calls:
            if call.location.path not in valid:
                continue
            try:
                self.coordinates(call.location)
            except ValueError as error:
                self.issue(context, call.location.path, str(error))
                continue
            # Retain unresolved targets in the grouping, so a different context
            # cannot turn a conflicting site into an apparently verified call.
            self.calls[call.location].add((call.target_usr, call.container_usr))

    def coordinates(self, extent):
        data = self.sources[extent.path]
        if not 0 <= extent.start < extent.end <= len(data):
            raise ValueError('Source extent outside verified file')
        for offset in (extent.start, extent.end):
            if offset < len(data) and data[offset] & 0xc0 == 0x80:
                raise ValueError('Source extent splits UTF-8 codepoint')
        def position(offset):
            return Position(data.count(b'\n', 0, offset), offset - data.rfind(b'\n', 0, offset) - 1)
        return position(extent.start), position(extent.end)

    def location(self, extent):
        start, end = self.coordinates(extent)
        key = (self.all_files[extent.path].file_id, start, end)
        if key not in self.locations:
            self.locations[key] = self.next_location
            self.next_location += 1
            self.builder.add_location(location_id=self.locations[key], file_id=key[0], start=start, end=end)
        return self.locations[key]

    def source_range(self, symbol, extent, role):
        location = self.location(extent)
        key = (symbol, location, role)
        if key not in self.ranges:
            self.builder.add_symbol_source_range(symbol_id=symbol, location_id=location, role=role)
            self.ranges.add(key)
        return location

    def finish(self):
        for path, source in self.sources.items():
            if Path(path).read_bytes() != source:
                raise ValueError(f'Source changed during AST enrichment: {path}')
        self.model = self.structural.apply()
        self.builder = IndexBuilder.from_model(self.model)
        self.symbols = {s.symbol_id: s for s in self.model.symbols if s.name is not None}
        self.conflicts += self.structural.counts['conflicts']
        old_bodies = {b.symbol.symbol_id: b for b in self.model.symbol_bodies}
        for symbol, observations in sorted(self.bodies.items()):
            if symbol not in self.symbols:
                for extent, _ in observations:
                    self.issue('', extent.path, 'Body owner remains unresolved', ('metrics', 'source_ranges'))
                continue
            for extent, _ in sorted(observations, key=lambda pair: (pair[0].path, pair[0].start, pair[0].end)):
                self.source_range(symbol, extent, SourceRangeRole.BODY)
            for extent in sorted(self.definitions[symbol], key=lambda r: (r.path, r.start, r.end)):
                self.source_range(symbol, extent, SourceRangeRole.DEFINITION)
            if len(observations) != 1:
                self.conflicts += 1
                for extent, _ in observations:
                    self.issue('', extent.path, f'Conflicting body observations for symbol {symbol}', 'metrics')
                continue
            extent, metrics = next(iter(observations))
            location = self.location(extent)
            old = old_bodies.get(symbol)
            if old:
                if old.body.location_id != location or old.metrics != metrics:
                    self.conflicts += 1
                    self.issue('', extent.path, f'AST body conflicts with existing symbol {symbol}', 'metrics')
                continue
            self.builder.add_symbol_body(symbol_id=symbol, body_location_id=location, metrics=metrics)
            self.bodies_added += 1
        if self.bodies_added:
            self.builder.update_metadata(replace(self.model.metadata, metrics_version=METRICS_VERSION))
        replacements = {}
        by_site = defaultdict(list)
        for ref in self.model.symbol_references:
            by_site[ref.target.symbol_id, ref.location.location_id].append(ref)
        next_ref = max((r.reference_id for r in self.model.symbol_references), default=0) + 1
        for extent, observations in sorted(self.calls.items(), key=lambda pair: (pair[0].path, pair[0].start, pair[0].end)):
            targets = {target for target, _ in observations}
            containers = {owner for _, owner in observations}
            if len(targets) != 1 or len(containers) != 1:
                self.conflicts += 1
                self.issue('', extent.path, 'Conflicting call identities at source site')
                continue
            target = self.identity(next(iter(targets)))
            container = self.identity(next(iter(containers)))
            if target is None:
                self.issue('', extent.path, 'Call target is not a resolved indexed symbol')
                continue
            if container is None:
                self.issue('', extent.path, 'Call container is not a resolved indexed symbol')
            location = self.location(extent)
            existing = by_site[target, location]
            incompatible = [r for r in existing if r.container is not None and
                            (container is None or r.container.symbol_id != container)]
            if incompatible:
                self.conflicts += 1
                self.issue('', extent.path, 'Call container conflicts with existing reference')
                continue
            if existing:
                for ref in existing:
                    replacements[ref.reference_id] = (ref.kind | ReferenceKind.CALL | ReferenceKind.REFERENCE, container)
            else:
                self.builder.add_symbol_reference(reference_id=next_ref, target_symbol_id=target,
                    location_id=location, kind=ReferenceKind.CALL | ReferenceKind.REFERENCE,
                    container_symbol_id=container)
                next_ref += 1
            self.calls_confirmed += 1
        # A known written definition not observed cannot silently acquire complete metrics coverage.
        for symbol in self.symbols.values():
            if symbol.definition is None or symbol.kind is None or symbol.kind.name not in {
                    'function', 'instance_method', 'static_method', 'constructor', 'destructor', 'conversion_function'}:
                continue
            path = file_path(symbol.definition.file.uri)
            eligibility = self.structural.eligibility.get(symbol.symbol_id, set())
            expected_absence = eligibility and eligibility <= {'declaration_only', 'implicit', 'defaulted', 'deleted'}
            if path in self.files and symbol.symbol_id not in self.bodies and symbol.symbol_id not in old_bodies and not expected_absence:
                self.issue('', path, f'No analyzed body for defined symbol {symbol.symbol_id}', 'metrics')
        for path in sorted(self.touched):
            file = self.all_files[path]
            for stage in (CollectionStage.SYMBOLS, CollectionStage.METRICS, CollectionStage.SOURCE_RANGES, CollectionStage.REFERENCES):
                if path not in self.files and (path, stage.value) not in self.problems:
                    continue
                previous = self.model.collection_status(file.file_id, stage)
                problems = set(self.problems[path, stage.value])
                if previous and previous.error:
                    problems.add(previous.error)
                if stage is not CollectionStage.METRICS and previous and previous.status is CollectionState.NOT_REQUESTED and not problems:
                    # This adapter collects only callable ranges and calls, not all ranges/references.
                    problems.add('AST step covers callable bodies and explicit resolved calls only')
                status = CollectionState.PARTIAL if problems else CollectionState.COMPLETE
                args = dict(file_id=file.file_id, stage=stage, status=status,
                            error='; '.join(sorted(problems)) if problems else None)
                if previous:
                    self.builder.update_collection_status(**args)
                else:
                    self.builder.add_collection_status(**args)
        result = self.builder.build()
        if replacements:
            symbols = {s.symbol_id: s for s in result.symbols}
            refs = tuple(replace(r, kind=replacements[r.reference_id][0],
                                 container=symbols.get(replacements[r.reference_id][1]))
                         if r.reference_id in replacements else r for r in result.symbol_references)
            result = replace(result, symbol_references=refs)
        return result
