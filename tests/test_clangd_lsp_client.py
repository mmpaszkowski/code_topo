from pathlib import Path
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from clangd_lsp import ClangdClient, LspRequestError, LspSessionError
from clangd_lsp.records import CollectionResult, CompilerDiagnostic, LspPosition, LspRange, SymbolInfo
from clangd_lsp.static_index import StaticIndexSource


RANGE = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


class ClientTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "compile_commands.json").write_text("[]")
        self.patch = patch("clangd_lsp.client.JsonRpcProcess")
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.rpc = self.factory.return_value
        self.rpc.request.return_value = {"capabilities": {"documentSymbolProvider": True,
                                                         "documentLinkProvider": {}}}
        self.client = ClangdClient(self.root)
        self.addCleanup(self.client.close)

    def test_initialization_defaults_to_utf16_and_disables_background_index(self):
        self.client.start(self.root)
        self.assertEqual(self.client.position_encoding, "utf-16")
        self.assertIn("--background-index=false", self.factory.call_args.args[0])
        self.rpc.notify.assert_called_once_with("initialized", {})
        self.client.close()
        self.rpc.close.assert_called_once()

    def test_negotiated_encoding_and_invalid_initialization_cleanup(self):
        for encoding in ("utf-8", "utf-16", "unsupported"):
            self.rpc.request.return_value = {"capabilities": {"positionEncoding": encoding}}
            if encoding == "unsupported":
                with self.assertRaises(LspSessionError):
                    self.client.start(self.root)
                self.assertIsNone(self.client._rpc)
            else:
                self.client.start(self.root)
                self.assertEqual(self.client.position_encoding, encoding)
                self.client.close()

    def test_document_lifecycle_waits_for_parse_diagnostics(self):
        self.client.start(self.root)
        uri = (self.root / "main.cpp").as_uri()
        notification = self.factory.call_args.args[3]

        def publish(predicate):
            self.assertFalse(predicate())
            notification("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": [
                {"severity": 1, "message": "header missing", "range": RANGE},
                {"severity": 2, "message": "unused", "range": RANGE},
            ]})
            self.assertTrue(predicate())

        self.rpc.wait_until.side_effect = publish
        self.assertEqual(self.client.open_document(uri, "int x;"),
                         (CompilerDiagnostic("header missing", LspRange(LspPosition(0, 0), LspPosition(0, 1))),))
        self.client.close_document(uri)
        self.assertNotIn(uri, self.client._diagnostics)

    def test_decodes_nested_symbols_and_links_and_reports_flat_symbols(self):
        self.client.start(self.root)
        child = {"name": "f", "selectionRange": RANGE, "range": RANGE}
        self.rpc.request.return_value = [dict(child, name="scope", children=[child])]
        self.assertEqual(self.client.document_symbols("file:///a").items[0].children[0].name, "f")
        self.rpc.request.return_value = [{"range": RANGE, "target": "file:///header"}, {"range": RANGE}]
        links = self.client.document_links("file:///a").items
        self.assertEqual(links[0].target, "file:///header")
        self.assertIsNone(links[1].target)
        self.rpc.request.return_value = [{"name": "flat", "location": {"uri": "file:///a", "range": RANGE}}]
        result = self.client.document_symbols("file:///a")
        self.assertEqual(result.items, ())
        self.assertEqual(len(result.issues), 1)
        self.rpc.request.return_value = None
        self.assertEqual(self.client.document_symbols("file:///a"), CollectionResult())
        self.assertEqual(self.client.document_links("file:///a"), CollectionResult())

    def test_capability_failure_is_a_recoverable_request_error(self):
        self.rpc.request.return_value = {"capabilities": {}}
        self.client.start(self.root)
        with self.assertRaises(LspRequestError):
            self.client.document_symbols("file:///a")
        with self.assertRaises(LspRequestError):
            self.client.document_links("file:///a")

    def test_malformed_symbols_preserve_siblings_children_and_valid_parents(self):
        self.client.start(self.root)
        good = {"name": "f", "selectionRange": RANGE, "range": RANGE}
        self.rpc.request.return_value = [
            dict(good, name="first", children=[None, dict(good, name="child")]),
            {"name": "broken parent", "children": [dict(good, name="orphan")]},
            dict(good, name="last", children="malformed"),
        ]
        result = self.client.document_symbols("file:///a")
        self.assertEqual([s.name for s in result.items], ["first", "orphan", "last"])
        self.assertEqual([s.name for s in result.items[0].children], ["child"])
        self.assertEqual({i.path for i in result.issues},
                         {"result[0].children[0]", "result[1]", "result[2].children"})
        self.assertTrue(all(i.message for i in result.issues))

    def test_malformed_links_preserve_valid_entries_and_top_level_errors_fail(self):
        self.client.start(self.root)
        self.rpc.request.return_value = [None, {"range": RANGE, "target": "file:///header"},
                                         {"range": RANGE, "target": 42}, {"target": "file:///bad"}]
        result = self.client.document_links("file:///a")
        self.assertEqual([link.target for link in result.items], ["file:///header"])
        self.assertEqual([i.path for i in result.issues], ["result[0]", "result[2]", "result[3]"])
        for method in (self.client.document_links, self.client.document_symbols):
            for malformed in ({}, "bad", 42):
                self.rpc.request.return_value = malformed
                with self.subTest(method=method, value=malformed), self.assertRaises(ValueError):
                    method("file:///a")

    def test_symbol_info_preserves_optional_identity_and_null_container(self):
        self.client.start(self.root)
        self.rpc.request.return_value = [
            {"name": "f", "containerName": None, "usr": "c:@F@f#", "id": "0123456789abcdef"},
            {"name": "local", "containerName": "f", "usr": "", "id": None},
        ]
        self.assertEqual(self.client.symbol_info("file:///a", LspPosition(2, 7)), (
            SymbolInfo("f", None, "c:@F@f#", "0123456789ABCDEF"), SymbolInfo("local", "f", ""),
        ))
        self.rpc.request.assert_called_with("textDocument/symbolInfo", {
            "textDocument": {"uri": "file:///a"}, "position": {"line": 2, "character": 7},
        })
        for malformed in (None, {}, [{"name": "f"}],
                          [{"name": "f", "containerName": "", "usr": "u", "id": 42}]):
            self.rpc.request.return_value = malformed
            with self.subTest(value=malformed), self.assertRaises(ValueError):
                self.client.symbol_info("file:///a", LspPosition(0, 0))
        self.rpc.request.return_value = []
        self.assertEqual(self.client.symbol_info("file:///a", LspPosition(0, 0)), ())
        self.rpc.request.side_effect = LspRequestError("textDocument/symbolInfo", {"code": -32601, "message": "unsupported"})
        with self.assertRaises(LspRequestError):
            self.client.symbol_info("file:///a", LspPosition(0, 0))

    def test_diagnostics_ignore_closed_and_stale_versions_on_reopen(self):
        self.client.start(self.root)
        uri = (self.root / "main.cpp").as_uri()
        notification = self.factory.call_args.args[3]
        notification("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})
        self.assertNotIn(uri, self.client._diagnostics)
        versions = []

        def publish(predicate):
            version = self.rpc.notify.call_args.args[1]["textDocument"]["version"]
            versions.append(version)
            for stale in (version - 1, version + 1):
                notification("textDocument/publishDiagnostics", {"uri": uri, "version": stale, "diagnostics": []})
                self.assertFalse(predicate())
            notification("textDocument/publishDiagnostics", {"uri": uri, "version": version, "diagnostics": []})
            self.assertTrue(predicate())

        self.rpc.wait_until.side_effect = publish
        for _ in range(2):
            self.assertEqual(self.client.open_document(uri, "int x;"), ())
            self.client.close_document(uri)
            notification("textDocument/publishDiagnostics", {"uri": uri, "version": versions[-1], "diagnostics": []})
            notification("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})
            self.assertNotIn(uri, self.client._diagnostics)
        self.assertLess(versions[0], versions[1])

    def test_missing_database_does_not_spawn_process(self):
        (self.root / "compile_commands.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.client.start(self.root)
        self.factory.assert_not_called()

    def test_structural_locations_accept_links_and_preserve_good_neighbors(self):
        self.rpc.request.return_value = {"capabilities": {
            "declarationProvider": True, "definitionProvider": True, "referencesProvider": True,
        }}
        self.client.start(self.root)
        link = {"targetUri": "file:///target", "targetRange": RANGE, "targetSelectionRange": RANGE}
        for method in (self.client.declarations, self.client.definitions):
            self.rpc.request.return_value = [link, {"uri": "file:///bad"}]
            result = method("file:///a", LspPosition(0, 0))
            self.assertEqual(result.items[0].uri, "file:///target")
            self.assertEqual(len(result.items), 1)
            self.assertEqual(len(result.issues), 1)
        self.rpc.request.return_value = [{"uri": "file:///target", "range": RANGE}]
        self.assertEqual(len(self.client.references("file:///a", LspPosition(0, 0)).items), 1)
        self.assertEqual(self.rpc.request.call_args.args[1]["context"], {"includeDeclaration": False})

    def test_hierarchy_round_trips_opaque_data_and_preserves_valid_call_ranges(self):
        self.rpc.request.return_value = {"capabilities": {"callHierarchyProvider": True, "typeHierarchyProvider": True}}
        self.client.start(self.root)
        raw = {"name": "f", "kind": 12, "uri": "file:///a", "range": RANGE, "selectionRange": RANGE,
               "data": {"symbolID": "0123456789abcdef", "parents": [{"opaque": [1, 2]}]}}
        self.rpc.request.return_value = [raw]
        item, = self.client.prepare_types("file:///a", LspPosition(0, 0)).items
        self.assertEqual(item.symbol_id, "0123456789ABCDEF")
        self.client.supertypes(item)
        self.assertEqual(self.rpc.request.call_args.args[1]["item"]["data"], raw["data"])
        self.rpc.request.return_value = [{"to": raw, "fromRanges": [RANGE, {"bad": True}]}]
        result = self.client.outgoing_calls(item)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.items[0].ranges), 1)
        self.assertEqual(len(result.issues), 1)
        self.rpc.request.return_value = [{"from": raw}]
        result = self.client.incoming_calls(item)
        self.assertEqual(result.items, ())
        self.assertEqual(len(result.issues), 1)

    def test_static_index_flags_and_cleanup_on_normal_and_failed_start(self):
        path = self.root / "index.yaml"
        path.write_bytes(b"--- !Symbol\n")
        source = StaticIndexSource(path, sha256(path.read_bytes()).hexdigest())
        for failure in (None, FileNotFoundError("clangd")):
            client = ClangdClient(self.root)
            self.addCleanup(client.close)
            self.factory.side_effect = failure
            if failure:
                with self.assertRaises(FileNotFoundError):
                    client.start(self.root, index_source=source)
            else:
                client.start(self.root, index_source=source)
            command = self.factory.call_args.args[0]
            temporary = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--index-file=")))
            for flag in ("--sync", "--background-index=false", "--limit-references=0", "--limit-results=0"):
                self.assertIn(flag, command)
            if not failure:
                self.assertTrue(temporary.exists())
            client.close()
            self.assertFalse(temporary.exists())
        self.assertEqual(path.read_bytes(), b"--- !Symbol\n")

    def test_filtered_database_lives_until_close_and_is_removed_on_startup_failure(self):
        for failure in (None, FileNotFoundError("executable missing")):
            with self.subTest(failure=failure):
                client = ClangdClient(self.root, remove_compile_flags=["-fprofile-abs-path"])
                self.addCleanup(client.close)
                prepared = []
                prepare = client._database.prepare

                def remember():
                    directory = prepare()
                    prepared.append(directory)
                    return directory

                self.factory.side_effect = failure
                with patch.object(client._database, "prepare", side_effect=remember):
                    if failure:
                        with self.assertRaises(FileNotFoundError):
                            client.start(self.root)
                    else:
                        client.start(self.root)
                        self.assertTrue(prepared[0].is_dir())
                        client.close()
                self.assertFalse(prepared[0].exists())


if __name__ == "__main__":
    unittest.main()
