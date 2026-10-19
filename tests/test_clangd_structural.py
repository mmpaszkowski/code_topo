from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import index_model as m
from clangd_lsp import ClangdClient, ClangdLspStep, LspRequestError, LspSessionError
from clangd_lsp.records import (
    CollectionResult, DiagnosticReason, HierarchyCall, HierarchyItem, LspLocation,
    LspPosition, LspRange, SymbolInfo, WorkspaceSymbol,
)
from clangd_lsp.static_index import StaticIndexSource


def span(line, start, end):
    return LspRange(LspPosition(line, start), LspPosition(line, end))


class StructuralTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "main.cpp"
        self.text = ("int callee();\nint caller() { return callee(); }\nint callee() { return 1; }\n"
                     "struct Base {};\nstruct Derived : Base {};\n")
        self.path.write_text(self.text)
        self.uri = self.path.as_uri()
        self.builder = m.IndexBuilder()
        self.builder.set_metadata(m.IndexMetadata(project_root=str(self.root)))
        self.builder.add_file(file_id=1, uri=self.uri, content_hash=sha256(self.path.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=1, stage=stage, status=m.CollectionState.COMPLETE)
        self.builder.add_language(language_id=1, name="C++")
        self.builder.add_symbol_kind(kind_id=1, name="function")
        self.builder.add_symbol_kind(kind_id=2, name="struct")
        names = ["callee", "caller", "Base", "Derived"]
        self.locations = [span(0, 4, 10), span(1, 4, 10), span(3, 7, 11), span(4, 7, 14)]
        self.ids = {f"{key:016X}": key for key in range(1, 5)}
        self.items = {}
        for key, (name, extent) in enumerate(zip(names, self.locations), 1):
            self.builder.add_location(location_id=key, file_id=1,
                                      start=m.Position(extent.start.line, extent.start.character),
                                      end=m.Position(extent.end.line, extent.end.character))
            self.builder.add_symbol(symbol_id=key, name=name, language_id=1, scope_id=1,
                                    kind_id=1 if key < 3 else 2, declaration_location_id=key,
                                    definition_location_id=key if key != 1 else None)
            self.items[key] = HierarchyItem(name, 12 if key < 3 else 23,
                LspLocation(self.uri, extent), extent, json.dumps(f"{key:016X}"), f"{key:016X}")
        self.call = LspLocation(self.uri, span(1, 22, 28))
        self.builder.add_location(location_id=50, file_id=1, start=m.Position(1, 22), end=m.Position(1, 28))
        self.builder.add_symbol_reference(reference_id=50, target_symbol_id=1, location_id=50,
                                          kind=m.ReferenceKind.REFERENCE | m.ReferenceKind.SPELLED)
        self.client = Mock(spec=ClangdClient)
        self.client.position_encoding = "utf-8"
        self.client.normalized_index_records = 0
        self.client.open_document.return_value = ()
        self.client.document_symbols.return_value = CollectionResult()
        self.client.document_links.return_value = CollectionResult()
        self.client.workspace_symbols.side_effect = lambda name: CollectionResult(tuple(
            WorkspaceSymbol(item.name, item.location) for item in self.items.values() if item.name == name))
        keys_by_line = {0: 1, 1: 2, 2: 1, 3: 3, 4: 4}
        self.client.symbol_info.side_effect = lambda uri, pos: (
            SymbolInfo(names[keys_by_line[pos.line] - 1], None, "usr", f"{keys_by_line[pos.line]:016X}",
                       definition=LspLocation(self.uri, span(2, 4, 10)) if keys_by_line[pos.line] == 1 else None),)
        self.client.definitions.return_value = CollectionResult((LspLocation(self.uri, span(2, 4, 10)),))
        self.client.declarations.return_value = CollectionResult()
        self.client.references.side_effect = lambda uri, pos: CollectionResult((self.call,)) if pos.line in (0, 2) else CollectionResult()
        self.client.prepare_calls.side_effect = lambda uri, pos: CollectionResult((self.items[keys_by_line[pos.line]],))
        self.client.incoming_calls.side_effect = lambda item: CollectionResult(
            (HierarchyCall(self.items[2], (self.call.extent,)),)) if item.name == "callee" else CollectionResult()
        self.client.outgoing_calls.side_effect = lambda item: CollectionResult(
            (HierarchyCall(self.items[1], (self.call.extent,)),)) if item.name == "caller" else CollectionResult()
        self.client.prepare_types.side_effect = lambda uri, pos: CollectionResult((self.items[keys_by_line[pos.line]],))
        self.client.supertypes.side_effect = lambda item: CollectionResult((self.items[3],)) if item.name == "Derived" else CollectionResult()
        self.step = ClangdLspStep(self.client, symbol_ids_provider=lambda: self.ids,
            index_source_provider=lambda: StaticIndexSource(self.root / "export.yaml", "0" * 64))

    def run_step(self, model=None):
        with self.assertLogs("clangd_lsp.step", level="WARNING"):
            return self.step.run(model or self.builder.build())

    def test_fills_locations_calls_and_inheritance_without_mutating_or_duplicating(self):
        original = self.builder.build()
        result = self.run_step(original)
        self.assertIsNone(original.symbol(1).definition)
        self.assertEqual(result.symbol(1).definition.start, m.Position(2, 4))
        ref, = result.symbol_references
        self.assertEqual(ref.reference_id, 50)
        self.assertEqual(ref.kind, m.ReferenceKind.REFERENCE | m.ReferenceKind.SPELLED)
        self.assertIs(ref.container, result.symbol(2))
        self.assertIsNone(original.symbol_references[0].container)
        base, = result.class_inheritance
        self.assertEqual((base.base.symbol_id, base.derived.symbol_id), (3, 4))
        self.assertIsNone(base.access)
        self.assertIsNone(base.is_virtual)
        report = self.step.report.structural
        self.assertEqual((report.locations_filled, report.call_candidates, report.inheritance_added), (1, 1, 1))
        self.assertEqual((report.references_added, report.references_enriched), (0, 1))
        again = self.run_step(result)
        self.assertEqual(len(again.locations), len(result.locations))
        self.assertEqual(len(again.symbol_references), 1)
        self.assertEqual(len(again.class_inheritance), 1)
        report = self.step.report.structural
        self.assertEqual((report.locations_filled, report.references_enriched, report.inheritance_added), (0, 0, 0))
        self.assertEqual(m.validate(again), ())

    def test_keeps_conflicting_containers_and_recursive_calls(self):
        self.builder.add_symbol_reference(reference_id=51, target_symbol_id=2, location_id=50,
                                          kind=m.ReferenceKind.REFERENCE, container_symbol_id=1)
        self.client.outgoing_calls.side_effect = lambda item: CollectionResult(
            (HierarchyCall(self.items[2], (self.call.extent,)),)) if item.name == "caller" else CollectionResult()
        result = self.run_step()
        refs = [r for r in result.symbol_references if r.target.symbol_id == 2]
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].container.symbol_id, 1)
        self.assertTrue(any(r.container is r.target for r in refs))
        self.assertFalse(any(r.kind & m.ReferenceKind.CALL for r in refs))
        self.assertTrue(any(d.reason is DiagnosticReason.CONFLICT for d in self.step.report.diagnostics))
        again = self.run_step(result)
        self.assertEqual(len(again.symbol_references), len(result.symbol_references))

    def test_ambiguous_navigation_and_unknown_identity_do_not_fill_fields(self):
        old_info = self.client.symbol_info.side_effect
        self.client.symbol_info.side_effect = lambda uri, pos: tuple(
            replace(info, definition=LspLocation(uri, span(pos.line, 4, 10))) for info in old_info(uri, pos))
        self.client.definitions.return_value = CollectionResult((LspLocation(self.uri, span(2, 4, 10)),
                                                                LspLocation(self.uri, span(0, 4, 10))))
        self.client.supertypes.side_effect = lambda item: CollectionResult((replace(self.items[3], symbol_id="UNKNOWN"),))
        result = self.run_step()
        self.assertIsNone(result.symbol(1).definition)
        self.assertEqual(result.class_inheritance, ())
        self.assertTrue(any(d.reason is DiagnosticReason.CONFLICT for d in self.step.report.diagnostics))
        self.assertGreater(self.step.report.structural.hierarchy_partial, 0)

    def test_definition_navigation_fallback_is_not_mistaken_for_a_definition(self):
        self.client.definitions.return_value = CollectionResult((self.items[1].location,))
        result = self.run_step()
        self.assertIsNone(result.symbol(1).definition)

    def test_failed_reference_query_keeps_facts_and_other_collections(self):
        self.client.references.side_effect = LspRequestError("references", {"code": -32601, "message": "unsupported"})
        result = self.run_step()
        self.assertEqual(result.collection_status(1, m.CollectionStage.REFERENCES).status, m.CollectionState.PARTIAL)
        self.assertEqual(len(result.class_inheritance), 1)
        self.assertIsNotNone(result.symbol(1).definition)
        self.assertEqual(self.step.report.structural.queries_failed, 4)

    def test_invalid_returned_source_is_not_added(self):
        returned = self.root / "returned.cpp"
        returned.write_text("int unrelated;\n")
        self.builder.add_file(file_id=2, uri=returned.as_uri(), content_hash="0" * 64)
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=2, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        self.client.references.side_effect = None
        self.client.references.return_value = CollectionResult((LspLocation(returned.as_uri(), span(0, 4, 13)),))
        result = self.run_step()
        self.assertFalse(any(r.location.file.file_id == 2 for r in result.symbol_references))
        self.assertTrue(any(d.reason is DiagnosticReason.SOURCE_FAILURE for d in self.step.report.diagnostics))

    def test_static_index_must_verify_before_documents_are_opened(self):
        self.client.workspace_symbols.side_effect = None
        self.client.workspace_symbols.return_value = CollectionResult()
        with self.assertRaisesRegex(LspSessionError, "Static index"):
            self.step.run(self.builder.build())
        self.client.open_document.assert_not_called()
        self.client.close.assert_called_once()
        self.assertIsNone(self.step.report)

    def test_transport_failure_preserves_original_and_cleans_up(self):
        self.client.references.side_effect = LspSessionError("timeout")
        original = self.builder.build()
        with self.assertRaises(LspSessionError):
            self.step.run(original)
        self.assertIsNone(original.symbol(1).definition)
        self.client.close.assert_called_once()
        self.assertIsNone(self.step.report)

    def test_invalid_imported_anchor_is_partial_without_aborting_other_symbols(self):
        self.builder.add_location(location_id=100, file_id=1, start=m.Position(100, 0), end=m.Position(100, 1))
        self.builder.add_symbol(symbol_id=5, name="bad", language_id=1, scope_id=1, kind_id=1,
                                declaration_location_id=100)
        result = self.run_step()
        self.assertEqual(len(result.class_inheritance), 1)
        self.assertEqual(result.collection_status(1, m.CollectionStage.SYMBOLS).status, m.CollectionState.PARTIAL)
        self.assertTrue(any(d.reason is DiagnosticReason.INVALID_RANGE and d.symbol_id == 5
                            for d in self.step.report.diagnostics))

    def test_fatal_nested_document_open_does_not_reuse_broken_transport(self):
        target = self.root / "target.cpp"
        target.write_text("int callee();\n")
        self.builder.add_file(file_id=2, uri=target.as_uri(), content_hash=sha256(target.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=2, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        self.client.definitions.return_value = CollectionResult((LspLocation(target.as_uri(), span(0, 4, 10)),))
        self.client.open_document.side_effect = [(), LspSessionError("timeout opening identity target")]
        with self.assertRaisesRegex(LspSessionError, "timeout opening"):
            self.step.run(self.builder.build())
        self.client.close_document.assert_not_called()
        self.client.close.assert_called_once()

    def test_excluded_returned_locations_keep_prior_statuses(self):
        target = self.root / "excluded.cpp"
        target.write_text("int callee();\n")
        self.builder.add_file(file_id=2, uri=target.as_uri(), content_hash=sha256(target.read_bytes()).hexdigest())
        for stage in m.CollectionStage:
            self.builder.add_collection_status(file_id=2, stage=stage, status=m.CollectionState.NOT_REQUESTED)
        self.client.references.side_effect = None
        self.client.references.return_value = CollectionResult((LspLocation(target.as_uri(), span(0, 4, 10)),))
        self.step = ClangdLspStep(self.client, exclude_paths=["excluded.cpp"], symbol_ids_provider=lambda: self.ids,
                                 index_source_provider=lambda: StaticIndexSource(self.root / "export.yaml", "0" * 64))
        result = self.run_step()
        self.assertFalse(any(ref.location.file.file_id == 2 for ref in result.symbol_references))
        self.assertTrue(all(result.collection_status(2, stage).status is m.CollectionState.NOT_REQUESTED
                            for stage in m.CollectionStage))
        self.assertTrue(any(d.reason is DiagnosticReason.OUT_OF_SCOPE for d in self.step.report.diagnostics))


if __name__ == "__main__":
    unittest.main()
