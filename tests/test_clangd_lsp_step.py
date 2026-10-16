from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import index_model as m
from clangd_lsp import ClangdClient, ClangdLspStep, LspRequestError, LspSessionError
from clangd_lsp.records import CompilerDiagnostic, DiagnosticReason, DocumentLink, DocumentSymbol, LspPosition, LspRange
from clangd_lsp.source import SourceText


def span(line, start, end):
    return LspRange(LspPosition(line, start), LspPosition(line, end))


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "main.cpp"
        self.path.write_bytes(b"int f();\nint f() { return 1; }\n")
        self.builder = m.IndexBuilder()
        self.builder.set_metadata(m.IndexMetadata(project_root=str(self.root)))
        self.builder.add_file(file_id=10, uri=self.path.as_uri(),
                              content_hash=sha256(self.path.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=10, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        self.builder.add_language(language_id=1, name="Cpp")
        self.builder.add_location(location_id=20, file_id=10, start=m.Position(0, 4), end=m.Position(0, 5))
        self.builder.add_location(location_id=30, file_id=10, start=m.Position(1, 4), end=m.Position(1, 5))
        self.builder.add_symbol(symbol_id=40, name="f", scope_id=1, language_id=1,
                                declaration_location_id=20, definition_location_id=30)
        self.client = Mock(spec=ClangdClient)
        self.client.position_encoding = "utf-8"
        self.client.open_document.return_value = ()
        self.client.document_symbols.return_value = (
            DocumentSymbol("f", span(0, 4, 5), span(0, 0, 7)),
            DocumentSymbol("f", span(1, 4, 5), span(1, 0, 21)),
        )
        self.client.document_links.return_value = ()
        self.step = ClangdLspStep(self.client)

    def status(self, model, stage):
        return model.collection_status(10, stage).status

    def test_adds_ranges_and_includes_preserves_ids_and_is_repeatable(self):
        external = (self.root.parent / "external.hpp").as_uri()
        self.client.document_links.return_value = (DocumentLink(span(0, 0, 3), external),) * 2
        original = self.builder.build()
        self.assertEqual(self.step.summary(), ())
        result = self.step.run(original)
        self.assertEqual(original.symbol_source_ranges, ())
        self.assertEqual(original.file_includes, ())
        self.assertEqual([f.file_id for f in result.files], [10, 11])
        self.assertEqual(result.symbol(40).declaration.location_id, 20)
        self.assertEqual(result.symbol(40).definition.location_id, 30)
        self.assertEqual({r.role for r in result.symbol_source_ranges},
                         {m.SourceRangeRole.DECLARATION, m.SourceRangeRole.DEFINITION})
        self.assertEqual(result.file_includes[0].included.uri, external)
        self.assertTrue(all(s.status is m.CollectionState.NOT_REQUESTED
                            for s in result.collection_statuses if s.file.file_id == 11))
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.COMPLETE)
        self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.COMPLETE)
        self.assertEqual(self.status(result, m.CollectionStage.METRICS), m.CollectionState.NOT_REQUESTED)
        self.assertEqual(result.metadata.status, m.SnapshotStatus.BUILDING)
        self.assertEqual(len(self.step.summary()), 7)
        self.assertEqual(self.step.report.occurrences_matched, 2)
        self.assertEqual(self.step.report.occurrences_total, 2)
        again = self.step.run(result)
        self.assertEqual(len(again.locations), len(result.locations))
        self.assertEqual(len(again.symbol_source_ranges), 2)
        self.assertEqual(len(again.file_includes), 1)
        self.assertEqual(self.step.report.ranges_added, 0)
        self.assertEqual(self.step.report.includes_added, 0)
        self.assertEqual(self.client.open_document.call_count, 2)  # external file is never opened

    def test_recurses_and_matches_additional_reference_occurrences(self):
        self.builder.add_location(location_id=31, file_id=10, start=m.Position(1, 14), end=m.Position(1, 15))
        self.builder.add_symbol_reference(reference_id=1, target_symbol_id=40, location_id=31,
                                          kind=m.ReferenceKind.DECLARATION)
        original = self.client.document_symbols.return_value
        child = DocumentSymbol("extra", span(1, 14, 15), span(1, 12, 18))
        self.client.document_symbols.return_value = (replace(original[0], children=(child,)), original[1])
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.symbol_source_ranges), 3)
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.COMPLETE)

    def test_exact_matches_only_and_ambiguous_selections_are_partial(self):
        self.builder.add_symbol(symbol_id=41, name="other", scope_id=1, language_id=1,
                                declaration_location_id=20)
        self.client.document_symbols.return_value += (DocumentSymbol("nearby", span(0, 5, 6), span(0, 0, 7)),)
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.symbol_source_ranges), 1)
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.assertEqual(len(result.symbols), 2)
        ambiguous = [d for d in self.step.report.diagnostics if d.reason is DiagnosticReason.AMBIGUOUS_MATCH]
        self.assertEqual({d.symbol_id for d in ambiguous}, {40, 41})
        self.assertTrue(all(d.candidates == (40, 41) for d in ambiguous))
        self.assertTrue(all(d.source_range.start == m.Position(0, 4) for d in ambiguous))
        self.assertTrue(any(d.reason is DiagnosticReason.UNMATCHED_OUTLINE for d in self.step.report.diagnostics))

    def test_missing_and_changed_sources_are_skipped(self):
        model = self.builder.build()
        for action in (lambda: self.path.write_bytes(b"changed"), self.path.unlink):
            action()
            result = self.step.run(model)
            self.assertEqual(self.step.report.files_skipped, 1)
            self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.FAILED)
            self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.FAILED)
        self.client.open_document.assert_not_called()

    def test_partial_links_and_parse_errors_preserve_good_observations(self):
        self.client.open_document.return_value = (CompilerDiagnostic("header not found", span(0, 0, 3)),)
        self.client.document_links.return_value = (
            DocumentLink(span(0, 0, 3), (self.root / "new.hpp").as_uri()),
            DocumentLink(span(0, 0, 3), None), DocumentLink(span(0, 0, 3), "https://example.com"),
        )
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.file_includes), 1)
        self.assertEqual(len(result.symbol_source_ranges), 2)
        self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.PARTIAL)
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.client.open_document.assert_called_once()  # newly discovered project file not opened

    def test_request_failures_do_not_discard_other_stage(self):
        self.client.document_symbols.side_effect = LspRequestError("symbols", {"code": -1, "message": "failed"})
        result = self.step.run(self.builder.build())
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.FAILED)
        self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.COMPLETE)
        self.client.close_document.assert_called_once()

    def test_session_failure_resets_report_and_always_closes(self):
        model = self.builder.build()
        self.step.run(model)
        self.client.reset_mock()
        self.client.document_symbols.side_effect = LspSessionError("timeout")
        with self.assertRaises(LspSessionError):
            self.step.run(model)
        self.assertEqual(model.symbol_source_ranges, ())
        self.assertIsNone(self.step.report)
        self.assertEqual(self.step.summary(), ())
        self.client.close_document.assert_not_called()
        self.client.close.assert_called_once()

    def test_unicode_utf16_ranges_convert_to_byte_columns(self):
        self.path.write_text("/*😀*/int f();\n", encoding="utf-8")
        builder = m.IndexBuilder()
        builder.set_metadata(m.IndexMetadata(project_root=str(self.root)))
        builder.add_file(file_id=1, uri=self.path.as_uri(), content_hash=sha256(self.path.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            builder.add_collection_status(file_id=1, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        builder.add_language(language_id=1, name="Cpp")
        builder.add_location(location_id=1, file_id=1, start=m.Position(0, 12), end=m.Position(0, 13))
        builder.add_symbol(symbol_id=1, name="f", language_id=1, scope_id=1, declaration_location_id=1)
        self.client.position_encoding = "utf-16"
        self.client.document_symbols.return_value = (DocumentSymbol("f", span(0, 10, 11), span(0, 6, 13)),)
        result = self.step.run(builder.build())
        self.assertEqual(result.symbol_source_ranges[0].location.start, m.Position(0, 8))
        self.assertEqual(result.symbol_source_ranges[0].location.end, m.Position(0, 15))

    def test_invalid_extent_is_reported_without_creating_locations(self):
        self.client.document_symbols.return_value = (DocumentSymbol("f", span(0, 4, 5), span(0, 0, 100)),)
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.locations), 2)
        self.assertEqual(result.symbol_source_ranges, ())
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.assertEqual({d.reason for d in self.step.report.diagnostics},
                         {DiagnosticReason.INVALID_RANGE, DiagnosticReason.MISSING_OUTLINE})

    def test_missing_occurrence_reports_symbol_role_location_and_one_based_logs(self):
        self.client.document_symbols.return_value = self.client.document_symbols.return_value[:1]
        with self.assertLogs("clangd_lsp.step", level="WARNING") as logs:
            result = self.step.run(self.builder.build())
        diagnostic, = self.step.report.diagnostics
        self.assertEqual(diagnostic.reason, DiagnosticReason.MISSING_OUTLINE)
        self.assertEqual(diagnostic.symbol_id, 40)
        self.assertEqual(diagnostic.source_range.start, m.Position(1, 4))
        self.assertEqual(diagnostic.source_range.end, m.Position(1, 5))
        self.assertIn("'f', definition", diagnostic.message)
        self.assertIn(":2:5-2:6 (1-based lines and UTF-8 byte columns)", logs.output[0])
        self.assertEqual(result.collection_status(10, m.CollectionStage.SOURCE_RANGES).error, "missing_outline: 1")
        with self.assertNoLogs("clangd_lsp.step"):
            self.assertEqual(self.step.summary(), self.step.summary())

    def test_exclusions_preserve_dependency_edges_and_existing_statuses(self):
        dependency = self.root / "build" / "_deps" / "library" / "header.hpp"
        self.builder.add_file(file_id=11, uri=dependency.as_uri())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=11, stage=stage,
                                               status=m.CollectionState.PARTIAL, error="previous outcome")
        self.client.document_links.return_value = (DocumentLink(span(0, 0, 3), dependency.as_uri()),)
        patterns = ["build/_deps/*"]
        step = ClangdLspStep(self.client, exclude_paths=patterns)
        patterns.clear()  # the step owns a copy
        result = step.run(self.builder.build())
        self.assertEqual(step.report.files_excluded, 1)
        self.assertEqual(step.report.files_skipped, 0)
        self.assertEqual(step.report.files_processed, 1)
        self.client.open_document.assert_called_once()
        self.assertEqual(result.file_includes[0].included.file_id, 11)
        for stage in m.CollectionStage:
            self.assertEqual(result.collection_status(11, stage).error, "previous outcome")
        self.client.reset_mock()
        other = ClangdLspStep(self.client, exclude_paths=["BUILD/_deps/*"])
        other.run(self.builder.build())
        self.assertEqual(other.report.files_excluded, 0)
        self.assertEqual(other.report.files_skipped, 1)  # the missing dependency was selected


class SourceTextTests(unittest.TestCase):
    def test_encodings_and_invalid_endpoints(self):
        source = SourceText("é😀f\r\n".encode())
        self.assertEqual(source.position(LspPosition(0, 3), "utf-16"), m.Position(0, 6))
        self.assertEqual(source.position(LspPosition(0, 6), "utf-8"), m.Position(0, 6))
        self.assertEqual(source.position(LspPosition(1, 0), "utf-8"), m.Position(1, 0))
        for position, encoding in ((LspPosition(0, 2), "utf-16"), (LspPosition(0, 1), "utf-8"),
                                   (LspPosition(4, 0), "utf-8"), (LspPosition(0, 99), "utf-8")):
            with self.subTest(position=position, encoding=encoding), self.assertRaises(ValueError):
                source.position(position, encoding)


if __name__ == "__main__":
    unittest.main()
