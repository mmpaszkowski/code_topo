from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

import index_model as m
from clangd_import import ClangdYamlReader, ClangdYamlStep, IndexParseError, LocalSourceReader
from clangd_import.records import ExportPosition
from clangd_import.sources import utf8_position
from index_pipeline import SummaryItem


FIRST = "0000000000000001"
SECOND = "FFFFFFFFFFFFFFFF"
THIRD = "0000000000000003"
URI = "file:///example/main.cpp"


def location(uri=URI, start=0, end=1, line=0):
    return (f"{{FileURI: '{uri}', Start: {{Line: {line}, Column: {start}}}, "
            f"End: {{Line: {line}, Column: {end}}}}}")


def symbol(symbol_id=FIRST, name="f", scope="", kind="Function", lang="Cpp", extra="", where=None):
    info = f"  Kind: {kind}\n  Lang: {lang}" if kind is not None else "  Kind:            Lang:            C"
    return (f"--- !Symbol\nID: '{symbol_id}'\nName: '{name}'\nScope: '{scope}'\nSymInfo:\n{info}\n"
            f"CanonicalDeclaration: {where or location()}\nFlags: 9\nSignature: ''\n"
            "TemplateSpecializationArgs: ''\nCompletionSnippetSuffix: ''\n"
            f"Documentation: ''\nReturnType: int\nType: c:I\n{extra}...\n").encode()


def references(target=FIRST, container=SECOND, kinds=(20,), where=None):
    entries = "".join(f"  - Kind: {kind}\n    Location: {where or location()}\n"
                      f"    Container: {{ID: '{container}'}}\n" for kind in kinds)
    return f"--- !Refs\nID: '{target}'\nReferences:\n{entries}...\n".encode()


def relation(subject=FIRST, predicate=0, object=SECOND):
    return (f"--- !Relations\nSubject: {{ID: '{subject}'}}\nPredicate: {predicate}\n"
            f"Object: {{ID: '{object}'}}\n...\n").encode()


def seed():
    builder = m.IndexBuilder()
    builder.set_metadata(m.IndexMetadata(project_root="/example", created_at="2026-10-16T00:00:00Z"))
    return builder.build()


class ImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "index.yaml"
        self.sources = Mock(spec=LocalSourceReader)
        self.sources.read.return_value = b"f\n"
        self.step = ClangdYamlStep(ClangdYamlReader(self.path), self.sources)
        self.initial = seed()

    def run_export(self, raw):
        self.path.write_bytes(raw)
        return self.step.run(self.initial)

    def test_step_owns_summary_and_diagnostic_logging(self):
        self.assertEqual(self.step.summary(), ())
        with self.assertLogs("clangd_import.step", level="WARNING") as logs:
            self.run_export(symbol(kind=None))
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f"{self.path}:", logs.records[0].getMessage())
        self.assertIn("SymInfo.Kind", logs.records[0].getMessage())
        self.assertIn(self.step.report.diagnostics[0].recovery_action, logs.records[0].getMessage())
        with self.assertNoLogs("clangd_import.step"):
            summary = self.step.summary()
            self.assertEqual(summary, self.step.summary())
        self.assertEqual(summary, (
            SummaryItem("clangd_yaml diagnostics", 1),
            SummaryItem("Unresolved symbols", 0),
            SummaryItem("Recovered symbols", 1),
            SummaryItem("Skipped references", 0),
            SummaryItem("Deferred relations", 0),
            SummaryItem("Source assumption", self.step.report.source_assumption),
        ))

    def test_forward_references_and_relations_resolve_with_consistent_ids(self):
        raw = (references() + relation() + relation(predicate=1, object=THIRD)
               + symbol(extra=f"Definition: {location()}\n"))
        model = self.run_export(raw)
        report = self.step.report
        resolved = model.symbol(report.symbol_ids[FIRST])
        placeholder = model.symbol(report.symbol_ids[SECOND])
        self.assertEqual(resolved.name, "f")
        self.assertEqual(resolved.return_type, "int")
        self.assertIsNone(resolved.signature)
        self.assertIs(resolved.declaration, resolved.definition)
        self.assertEqual(placeholder.resolution_state, m.ResolutionState.PLACEHOLDER)
        self.assertIs(model.symbol_references[0].target, resolved)
        self.assertIs(model.symbol_references[0].container, placeholder)
        self.assertIs(model.class_inheritance[0].base, resolved)
        self.assertIs(model.class_inheritance[0].derived, placeholder)
        self.assertIsNone(model.class_inheritance[0].access)
        self.assertIsNone(model.class_inheritance[0].is_virtual)
        self.assertIs(model.method_overrides[0].overridden, resolved)
        self.assertEqual(model.method_overrides[0].overriding.symbol_id, report.symbol_ids[THIRD])
        self.assertEqual(report.counts.placeholders, 2)
        self.assertEqual(report.yaml_sha256, sha256(raw).hexdigest())
        self.assertEqual(report.source_hashes[URI], sha256(b"f\n").hexdigest())
        self.assertEqual(model.files[0].content_hash, report.source_hashes[URI])
        self.assertEqual(self.sources.read.call_count, 1)
        self.assertEqual(self.initial.symbols, ())
        self.assertEqual(model.metadata.status, m.SnapshotStatus.BUILDING)
        for stage in m.CollectionStage:
            expected = (m.CollectionState.COMPLETE if stage in (m.CollectionStage.SYMBOLS, m.CollectionStage.REFERENCES)
                        else m.CollectionState.NOT_REQUESTED)
            self.assertEqual(model.collection_status(model.files[0].file_id, stage).status, expected)
        self.assertEqual(m.validate(model), ())

    def test_callable_flags_are_not_calls_and_normalized_duplicates_keep_export_flags(self):
        model = self.run_export(references(container="0" * 16, kinds=(4, 20, 17, 18, 28)))
        report = self.step.report
        self.assertEqual([r.original_kind for r in report.references], [4, 20, 17, 18, 28])
        self.assertEqual(report.references[0].reference_id, report.references[1].reference_id)
        self.assertEqual(report.counts.duplicate_references, 1)
        self.assertEqual(report.counts.references, 4)
        self.assertEqual(report.counts.reference_occurrences, 5)
        self.assertEqual({int(r.kind) for r in model.symbol_references}, {4, 1, 2, 12})
        self.assertTrue(all(not r.kind & m.ReferenceKind.CALL for r in model.symbol_references))
        self.assertTrue(all(r.container is None for r in model.symbol_references))
        self.assertNotIn("0" * 16, report.symbol_ids)

    def test_scope_labels_do_not_prove_identity_or_ownership(self):
        model = self.run_export(symbol(name="ns", kind="Namespace")
                                + symbol(SECOND, scope="ns::") + symbol(THIRD, scope="ns::"))
        a = model.symbol(self.step.report.symbol_ids[SECOND])
        b = model.symbol(self.step.report.symbol_ids[THIRD])
        self.assertEqual(a.scope_name, "ns::")
        self.assertEqual(b.scope_name, "ns::")
        self.assertIsNot(a.scope, b.scope)
        self.assertIsNone(a.parent)
        self.assertIsNone(b.parent)

    def test_export_only_fields_and_variable_types_stay_in_report(self):
        model = self.run_export(symbol(kind="Variable", extra="References: 2\nIncludeHeaders:\n"
                                      "  - {Header: '<example.h>', References: 2}\n"))
        result = model.symbols[0]
        self.assertIsNone(result.return_type)
        original = self.step.report.exported_symbols[result.symbol_id]
        self.assertEqual(original.symbol.return_type, "int")
        self.assertEqual(original.symbol.type, "c:I")
        self.assertEqual(original.symbol.flags, 9)
        self.assertEqual(original.symbol.signature, "")
        self.assertEqual(original.symbol.include_headers[0].header, "<example.h>")
        self.assertEqual(original.document_ordinal, 0)
        self.assertEqual(original.source_line, 1)
        self.assertEqual(model.file_includes, ())
        self.assertEqual(model.symbol_source_ranges, ())
        self.assertEqual(model.symbol_bodies, ())

    def test_recovery_and_deferred_self_relation_have_diagnostics_and_partial_coverage(self):
        model = self.run_export(relation(object=FIRST) + symbol(kind=None))
        report = self.step.report
        self.assertEqual(report.counts.recovered_symbols, 1)
        self.assertEqual(report.counts.deferred_relations, 1)
        self.assertEqual(len(report.diagnostics), 2)
        self.assertIsNone(model.symbols[0].kind)
        self.assertEqual(model.symbols[0].language.name, "C")
        self.assertEqual(model.class_inheritance, ())
        self.assertEqual(report.deferred_relations[0].relation.subject_symbol_id, FIRST)
        self.assertEqual(report.deferred_relations[0].document_ordinal, 0)
        self.assertEqual(report.diagnostics[1].field_path, "SymInfo.Kind")
        self.assertEqual(report.diagnostics[1].document_ordinal, 1)
        coverage = model.collection_status(model.files[0].file_id, m.CollectionStage.SYMBOLS)
        self.assertEqual(coverage.status, m.CollectionState.PARTIAL)
        self.assertIn("Unknown exported symbol kind", coverage.error)
        self.assertIn("identical endpoints", coverage.error)

    def test_unknown_kind_is_preserved_without_inventing_a_domain_kind(self):
        model = self.run_export(symbol(kind="FutureKind"))
        self.assertIsNone(model.symbols[0].kind)
        self.assertEqual(self.step.report.exported_symbols[model.symbols[0].symbol_id].symbol.sym_info.kind,
                         "FutureKind")
        self.assertEqual(len(self.step.report.diagnostics), 1)

    def test_missing_source_preserves_symbols_and_placeholder_ids(self):
        self.sources.read.side_effect = FileNotFoundError("missing file")
        model = self.run_export(symbol() + references(target=SECOND, container=THIRD))
        self.assertEqual(len(model.symbols), 3)
        self.assertIsNone(model.symbol(self.step.report.symbol_ids[FIRST]).declaration)
        self.assertEqual(model.symbol_references, ())
        self.assertEqual(model.locations, ())
        self.assertIsNone(model.files[0].content_hash)
        self.assertIsNone(self.step.report.references[0].reference_id)
        self.assertEqual(self.step.report.references[0].document_ordinal, 1)
        self.assertEqual(self.step.report.counts.skipped_references, 1)
        self.assertEqual(len(self.step.report.diagnostics), 1)
        self.assertEqual(self.sources.read.call_count, 1)
        for stage in (m.CollectionStage.SYMBOLS, m.CollectionStage.REFERENCES):
            self.assertEqual(model.collection_status(model.files[0].file_id, stage).status, m.CollectionState.PARTIAL)

    def test_unicode_columns_are_converted_against_cached_bytes(self):
        self.sources.read.return_value = "é😀foo\r\n".encode()
        where = location(start=3, end=6)
        model = self.run_export(symbol(where=where) + references(where=where))
        self.assertEqual(len(model.locations), 1)
        self.assertEqual(model.locations[0].start, m.Position(0, 6))
        self.assertEqual(model.locations[0].end, m.Position(0, 9))
        self.assertEqual(model.files[0].content_hash, sha256("é😀foo\r\n".encode()).hexdigest())
        self.assertEqual(self.sources.read.call_count, 1)

    def test_bad_locations_are_omitted_and_partial_without_losing_valid_locations(self):
        for where, data in ((location(start=3, end=4), b"f\n"),
                            (location(line=9), b"f\n"),
                            (location(start=0, end=1), "😀x".encode()),
                            (location(start=4095, end=4095), b"x" * 5000),
                            (location(start=0, end=1), b"\xffx")):
            with self.subTest(where=where, data=data[:10]):
                self.sources.read.return_value = data
                model = self.run_export(symbol(where=where) + references(where=where))
                self.assertEqual(model.locations, ())
                self.assertEqual(model.symbol_references, ())
                self.assertIsNone(model.symbols[0].declaration)
                self.assertEqual(self.step.report.counts.skipped_references, 1)
                self.assertEqual(len(self.step.report.diagnostics), 1)
                for stage in (m.CollectionStage.SYMBOLS, m.CollectionStage.REFERENCES):
                    self.assertEqual(model.collection_status(model.files[0].file_id, stage).status,
                                     m.CollectionState.PARTIAL)
        self.sources.read.return_value = b"f\n"
        model = self.run_export(symbol() + references(where=location(line=9)))
        self.assertEqual(len(model.locations), 1)
        self.assertEqual(model.collection_status(model.files[0].file_id, m.CollectionStage.SYMBOLS).status,
                         m.CollectionState.COMPLETE)

    def test_failures_clear_report_preserve_input_and_close_reader(self):
        self.run_export(symbol())
        previous_report = self.step.report
        stream = BytesIO(symbol() + symbol(name="conflict") + relation())
        with patch.object(Path, "open", return_value=stream):
            with self.assertRaisesRegex(IndexParseError, "duplicate symbol record"):
                self.step.run(self.initial)
        self.assertTrue(stream.closed)
        self.assertIsNone(self.step.report)
        self.assertEqual(self.step.summary(), ())
        self.assertEqual(self.initial.symbols, ())
        self.assertEqual(previous_report.counts.symbol_records, 1)

    def test_build_failure_does_not_publish_report(self):
        self.path.write_bytes(symbol())
        error = m.ModelValidationError((m.ValidationIssue("test", "build", "failed"),))
        with patch("clangd_import.step.IndexBuilder.build", side_effect=error):
            with self.assertRaises(m.ModelValidationError) as caught:
                self.step.run(self.initial)
        self.assertIs(caught.exception, error)
        self.assertIsNone(self.step.report)
        self.assertEqual(self.step.summary(), ())

    def test_unsupported_language_predicate_flags_and_corruption_stop_import(self):
        for raw in (symbol(lang="FutureLanguage"), relation(predicate=3), references(kinds=(32,)),
                    references(kinds=(16,)), symbol().replace(b"Flags: 9", b"Flags: -1")):
            with self.subTest(raw=raw), self.assertRaises(IndexParseError):
                self.run_export(raw)
            self.assertIsNone(self.step.report)

    def test_repeated_runs_are_independent_and_reports_are_immutable(self):
        first = self.run_export(symbol())
        report = self.step.report
        self.sources.read.return_value = b"g\n"
        second = self.step.run(self.initial)
        self.assertEqual(dict(report.symbol_ids), dict(self.step.report.symbol_ids))
        self.assertEqual(report.counts, self.step.report.counts)
        self.assertIsNot(first.symbols[0], second.symbols[0])
        self.assertNotEqual(first.files[0].content_hash, second.files[0].content_hash)
        self.assertEqual(self.sources.read.call_count, 2)
        with self.assertRaises(TypeError):
            report.symbol_ids[FIRST] = m.SymbolId(9)
        with self.assertRaises(TypeError):
            report.source_hashes[URI] = "changed"
        with self.assertRaises(FrozenInstanceError):
            report.exported_symbols[first.symbols[0].symbol_id].symbol.name = "changed"
        with self.assertRaises(FrozenInstanceError):
            report.counts.symbols = 0
        supplied = dict(report.symbol_ids)
        copied = replace(report, symbol_ids=supplied)
        supplied.clear()
        self.assertEqual(copied.symbol_ids[FIRST], m.SymbolId(1))

    def test_seed_can_supply_lookups_but_not_existing_indexed_entities(self):
        b = m.IndexBuilder.from_model(self.initial)
        b.add_language(language_id=m.LanguageId(7), name="C++")
        b.add_symbol_kind(kind_id=m.KindId(8), name="function")
        self.initial = b.build()
        model = self.run_export(symbol())
        self.assertEqual(model.symbols[0].language.language_id, 7)
        self.assertEqual(model.symbols[0].kind.kind_id, 8)
        with self.assertRaisesRegex(ValueError, "fresh seed"):
            self.step.run(model)
        self.assertIsNone(self.step.report)
        failed = replace(self.initial, metadata=replace(self.initial.metadata,
                         status=m.SnapshotStatus.FAILED, error="failed"))
        with self.assertRaisesRegex(ValueError, "building metadata"):
            self.step.run(failed)

    def test_duplicate_relations_are_counted_and_not_inserted_twice(self):
        model = self.run_export(relation() * 2 + relation(predicate=1) * 2)
        self.assertEqual(len(model.class_inheritance), 1)
        self.assertEqual(len(model.method_overrides), 1)
        self.assertEqual(self.step.report.counts.relation_records, 4)
        self.assertEqual(self.step.report.counts.duplicate_relations, 2)


class SourceTests(unittest.TestCase):
    def test_local_reader_decodes_uri_without_treating_plus_as_space(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "a+b 文.cpp"
            path.write_bytes(b"source")
            self.assertEqual(LocalSourceReader().read(path.as_uri()), b"source")
        for uri in ("https://example.com/source.cpp", "file://remote/a.cpp", "file:a.cpp",
                    "file:///a.cpp?query", "file:///a.cpp#fragment"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                LocalSourceReader().read(uri)

    def test_line_endings_tabs_empty_lines_and_unicode_boundaries(self):
        lines = tuple("\té😀x\r\n".encode().split(b"\n"))
        self.assertEqual(utf8_position(lines, ExportPosition(0, 0)), m.Position(0, 0))
        self.assertEqual(utf8_position(lines, ExportPosition(0, 4)), m.Position(0, 7))
        self.assertEqual(utf8_position(lines, ExportPosition(0, 6)), m.Position(0, 9))
        self.assertEqual(utf8_position(lines, ExportPosition(1, 0)), m.Position(1, 0))
        with self.assertRaises(ValueError):
            utf8_position(lines, ExportPosition(0, 3))

    def test_reader_can_be_closed_early(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.yaml"
            path.write_bytes(symbol() + relation())
            with closing(ClangdYamlReader(path).read()) as records:
                self.assertEqual(next(records).payload.symbol_id, FIRST)


if __name__ == "__main__":
    unittest.main()
