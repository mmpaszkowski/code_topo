from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import index_model as m
from clangd_lsp import ClangdClient, ClangdLspStep, LspRequestError, LspSessionError
from clangd_lsp.records import (
    CollectionResult, CompilerDiagnostic, DecodeIssue, DiagnosticReason, DocumentLink,
    DocumentSymbol, LspPosition, LspRange, SymbolInfo,
)
from clangd_lsp.source import SourceText


def span(line, start, end):
    return LspRange(LspPosition(line, start), LspPosition(line, end))


class EnrichmentTests(unittest.TestCase):
    def test_progress_total_excludes_dependencies_and_counts_handled_failures(self):
        from progress import ProgressReporter
        dep = self.root / 'dep.hpp'
        dep.write_text('int x;')
        self.builder.add_file(file_id=11, uri=dep.as_uri(), content_hash=sha256(dep.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=11, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        progress = Mock(spec=ProgressReporter)
        self.path.write_text('changed since indexing')
        step = ClangdLspStep(self.client, exclude_paths=('dep.hpp',), progress=progress)
        with self.assertLogs('clangd_lsp.step', level='WARNING'):
            step.run(self.builder.build())
        progress.phase.assert_any_call('inspecting', total=1, unit='files')
        self.assertEqual([c.args[0] for c in progress.update.call_args_list if c.args], [1])
        self.assertEqual(step.report.files_excluded, 1)
        self.assertEqual(step.report.files_skipped, 1)
        progress.phase.assert_any_call('shutting down clangd')

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
        self.client.document_symbols.return_value = CollectionResult((
            DocumentSymbol("f", span(0, 4, 5), span(0, 0, 7)),
            DocumentSymbol("f", span(1, 4, 5), span(1, 0, 21)),
        ))
        self.client.document_links.return_value = CollectionResult()
        self.step = ClangdLspStep(self.client)

    def status(self, model, stage):
        return model.collection_status(10, stage).status

    def test_adds_ranges_and_includes_preserves_ids_and_is_repeatable(self):
        external = (self.root.parent / "external.hpp").as_uri()
        self.client.document_links.return_value = CollectionResult((DocumentLink(span(0, 0, 3), external),) * 2)
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
        self.assertEqual(len(self.step.summary()), 8)
        self.assertEqual(self.step.report.occurrences_matched, 2)
        self.assertEqual(self.step.report.occurrences_total, 2)
        self.assertEqual(self.step.report.occurrences_identity_resolved, 0)
        self.client.symbol_info.assert_not_called()
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
        original = self.client.document_symbols.return_value.items
        child = DocumentSymbol("extra", span(1, 14, 15), span(1, 12, 18))
        self.client.document_symbols.return_value = CollectionResult((replace(original[0], children=(child,)), original[1]))
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.symbol_source_ranges), 3)
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.COMPLETE)

    def test_exact_matches_only_and_ambiguous_selections_are_partial(self):
        self.builder.add_symbol(symbol_id=41, name="other", scope_id=1, language_id=1,
                                declaration_location_id=20)
        self.client.document_symbols.return_value = CollectionResult(
            self.client.document_symbols.return_value.items + (DocumentSymbol("nearby", span(0, 5, 6), span(0, 0, 7)),))
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.symbol_source_ranges), 1)
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.assertEqual(len(result.symbols), 2)
        ambiguous = [d for d in self.step.report.diagnostics if d.reason is DiagnosticReason.AMBIGUOUS_MATCH]
        self.assertEqual({d.symbol_id for d in ambiguous}, {40, 41})
        self.assertTrue(all(d.candidates == (40, 41) for d in ambiguous))
        self.assertTrue(all(d.source_range.start == m.Position(0, 4) for d in ambiguous))
        self.assertTrue(any(d.reason is DiagnosticReason.UNMATCHED_OUTLINE for d in self.step.report.diagnostics))
        self.client.symbol_info.assert_not_called()

    def test_missing_and_changed_sources_are_skipped(self):
        model = self.builder.build()
        for action in (lambda: self.path.write_bytes(b"changed"), self.path.unlink):
            action()
            result = self.step.run(model)
            self.assertEqual(self.step.report.files_skipped, 1)
            self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.FAILED)
            self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.FAILED)
        self.client.open_document.assert_not_called()

    def test_identity_resolves_only_verified_candidate_roles_and_caches_positions(self):
        self.builder.add_symbol(symbol_id=41, name="f", scope_id=1, language_id=1,
                                declaration_location_id=20)
        self.builder.add_symbol_reference(reference_id=1, target_symbol_id=40, location_id=20,
                                          kind=m.ReferenceKind.DEFINITION)
        original = self.builder.build()
        symbols = self.client.document_symbols.return_value.items
        self.client.document_symbols.return_value = CollectionResult(symbols + symbols[:1])
        identity = SymbolInfo("f", None, "usr", "0123456789abcdef")
        self.client.symbol_info.return_value = (identity, identity)
        provider = Mock(return_value={"0123456789ABCDEF": 40})
        step = ClangdLspStep(self.client, symbol_ids_provider=provider)
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            result = step.run(original)
        self.assertEqual(original.symbol_source_ranges, ())
        self.assertEqual(len(result.symbol_source_ranges), 3)
        self.assertTrue(all(r.symbol.symbol_id == 40 for r in result.symbol_source_ranges))
        self.assertEqual(step.report.occurrences_identity_resolved, 2)
        self.assertEqual(step.report.occurrences_matched, 3)
        self.assertEqual(step.report.occurrences_total, 4)
        self.client.symbol_info.assert_called_once_with(self.path.as_uri(), LspPosition(0, 4))
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.assertEqual([(d.reason, d.symbol_id) for d in step.report.diagnostics],
                         [(DiagnosticReason.AMBIGUOUS_MATCH, 41)])
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            again = step.run(result)
        self.assertEqual(len(again.symbol_source_ranges), 3)
        self.assertEqual(step.report.ranges_added, 0)
        self.assertEqual(step.report.occurrences_identity_resolved, 2)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(self.client.symbol_info.call_count, 2)  # cache is per run/file

    def test_identity_conflicts_missing_ids_and_lookup_errors_remain_partial(self):
        self.builder.add_symbol(symbol_id=41, name="f", scope_id=1, language_id=1,
                                declaration_location_id=20)
        self.builder.add_symbol(symbol_id=42, name="elsewhere", scope_id=1, language_id=1)
        known = SymbolInfo("f", None, "usr", "000000000000000A")
        other = replace(known, id="000000000000000B")
        unknown = replace(known, id="000000000000000C")
        noncandidate = replace(known, id="000000000000000D")
        cases = [(), (replace(known, id=None),), (unknown,), (known, unknown),
                 (known, other), (noncandidate,), (known, noncandidate),
                 ValueError("bad identity response"),
                 LspRequestError("symbolInfo", {"code": -32601, "message": "unsupported"})]
        step = ClangdLspStep(self.client, symbol_ids_provider=lambda: {
            known.id: 40, other.id: 41, noncandidate.id: 42,
        })
        symbols = self.client.document_symbols.return_value.items
        self.client.document_symbols.return_value = CollectionResult(symbols + symbols[:1])
        for response in cases:
            self.client.symbol_info.reset_mock()
            self.client.symbol_info.side_effect = response if isinstance(response, Exception) else None
            self.client.symbol_info.return_value = response
            with self.subTest(response=response), self.assertLogs("clangd_lsp.step", level="WARNING"):
                result = step.run(self.builder.build())
            self.assertEqual(len(result.symbol_source_ranges), 1)  # unambiguous definition
            self.assertEqual(step.report.occurrences_identity_resolved, 0)
            self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
            self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.COMPLETE)
            self.assertEqual({d.symbol_id for d in step.report.diagnostics}, {40, 41})
            self.client.symbol_info.assert_called_once()

    def test_invalid_identity_mapping_is_rejected_before_starting_clangd(self):
        self.builder.add_symbol(symbol_id=41, name="other", scope_id=1, language_id=1)
        for mapping in (None, {"external": 999}, {"external": True}, {"": 40},
                        {1: 40}, {"a": 40, "A": 41}):
            step = ClangdLspStep(self.client, symbol_ids_provider=lambda: mapping)
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                step.run(self.builder.build())
            self.assertIsNone(step.report)
            self.assertEqual(step.summary(), ())
        self.client.start.assert_not_called()

    def test_identity_transport_failure_aborts_and_closes_the_step(self):
        self.builder.add_symbol(symbol_id=41, name="f", scope_id=1, language_id=1,
                                declaration_location_id=20)
        step = ClangdLspStep(self.client, symbol_ids_provider=lambda: {"external": 40})
        self.client.symbol_info.side_effect = LspSessionError("timeout")
        original = self.builder.build()
        with self.assertRaises(LspSessionError):
            step.run(original)
        self.client.close.assert_called_once()
        self.client.close_document.assert_not_called()
        self.assertIsNone(step.report)
        self.assertEqual(original.symbol_source_ranges, ())

    def test_decode_issues_preserve_good_ranges_and_links_as_partial(self):
        self.client.document_symbols.return_value = replace(
            self.client.document_symbols.return_value, issues=(DecodeIssue("result[2]", "bad symbol"),))
        self.client.document_links.return_value = CollectionResult(
            (DocumentLink(span(0, 0, 3), (self.root / "header.hpp").as_uri()),),
            (DecodeIssue("result[1]", "bad link"),))
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            result = self.step.run(self.builder.build())
        self.assertEqual(len(result.symbol_source_ranges), 2)
        self.assertEqual(len(result.file_includes), 1)
        for stage in (m.CollectionStage.SOURCE_RANGES, m.CollectionStage.INCLUDES):
            self.assertEqual(self.status(result, stage), m.CollectionState.PARTIAL)
            self.assertEqual(result.collection_status(10, stage).error, "decode_error: 1")
        self.assertEqual({d.reason for d in self.step.report.diagnostics}, {DiagnosticReason.DECODE_ERROR})

    def test_top_level_decode_failure_fails_only_affected_stage(self):
        self.client.document_symbols.side_effect = ValueError("Expected an LSP array")
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            result = self.step.run(self.builder.build())
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.FAILED)
        self.assertEqual(self.status(result, m.CollectionStage.INCLUDES), m.CollectionState.COMPLETE)
        self.assertEqual(self.step.report.diagnostics[0].reason, DiagnosticReason.INVALID_RESULT)

    def test_partial_links_and_parse_errors_preserve_good_observations(self):
        self.client.open_document.return_value = (CompilerDiagnostic("header not found", span(0, 0, 3)),)
        self.client.document_links.return_value = CollectionResult((
            DocumentLink(span(0, 0, 3), (self.root / "new.hpp").as_uri()),
            DocumentLink(span(0, 0, 3), None), DocumentLink(span(0, 0, 3), "https://example.com"),
        ))
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

    def test_unicode_utf16_ranges_convert_to_bytes_but_identity_query_uses_lsp_columns(self):
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
        self.client.document_symbols.return_value = CollectionResult((DocumentSymbol("f", span(0, 10, 11), span(0, 6, 13)),))
        result = self.step.run(builder.build())
        self.assertEqual(result.symbol_source_ranges[0].location.start, m.Position(0, 8))
        self.assertEqual(result.symbol_source_ranges[0].location.end, m.Position(0, 15))
        builder.add_symbol(symbol_id=2, name="f", language_id=1, scope_id=1, declaration_location_id=1)
        self.client.symbol_info.return_value = (SymbolInfo("f", None, "usr", "000000000000000A"),)
        step = ClangdLspStep(self.client, symbol_ids_provider=lambda: {"000000000000000a": 1})
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            result = step.run(builder.build())
        self.client.symbol_info.assert_called_once_with(self.path.as_uri(), LspPosition(0, 10))
        self.assertEqual(len(result.symbol_source_ranges), 1)
        self.assertEqual(result.symbol_source_ranges[0].symbol.symbol_id, 1)
        self.assertEqual(result.symbol_source_ranges[0].location.start, m.Position(0, 8))

    def test_invalid_extent_is_reported_without_creating_locations(self):
        self.client.document_symbols.return_value = CollectionResult((DocumentSymbol("f", span(0, 4, 5), span(0, 0, 100)),))
        result = self.step.run(self.builder.build())
        self.assertEqual(len(result.locations), 2)
        self.assertEqual(result.symbol_source_ranges, ())
        self.assertEqual(self.status(result, m.CollectionStage.SOURCE_RANGES), m.CollectionState.PARTIAL)
        self.assertEqual({d.reason for d in self.step.report.diagnostics},
                         {DiagnosticReason.INVALID_RANGE, DiagnosticReason.MISSING_OUTLINE})

    def test_missing_occurrence_reports_symbol_role_location_and_one_based_logs(self):
        self.client.document_symbols.return_value = CollectionResult(self.client.document_symbols.return_value.items[:1])
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
        self.client.document_links.return_value = CollectionResult((DocumentLink(span(0, 0, 3), dependency.as_uri()),))
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
        self.assertEqual(source.lsp_position(m.Position(0, 6), "utf-16"), LspPosition(0, 3))
        self.assertEqual(source.lsp_position(m.Position(0, 6), "utf-8"), LspPosition(0, 6))
        with self.assertRaises(ValueError):
            source.lsp_position(m.Position(0, 3), "utf-16")
        for position, encoding in ((LspPosition(0, 2), "utf-16"), (LspPosition(0, 1), "utf-8"),
                                   (LspPosition(4, 0), "utf-8"), (LspPosition(0, 99), "utf-8")):
            with self.subTest(position=position, encoding=encoding), self.assertRaises(ValueError):
                source.position(position, encoding)


if __name__ == "__main__":
    unittest.main()
