from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from clangd_lsp import ClangdClient, LspRequestError, LspSessionError
from clangd_lsp.records import CompilerDiagnostic, LspPosition, LspRange


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

    def test_decodes_nested_symbols_and_links_and_rejects_flat_symbols(self):
        self.client.start(self.root)
        child = {"name": "f", "selectionRange": RANGE, "range": RANGE}
        self.rpc.request.return_value = [dict(child, name="scope", children=[child])]
        self.assertEqual(self.client.document_symbols("file:///a")[0].children[0].name, "f")
        self.rpc.request.return_value = [{"range": RANGE, "target": "file:///header"}, {"range": RANGE}]
        links = self.client.document_links("file:///a")
        self.assertEqual(links[0].target, "file:///header")
        self.assertIsNone(links[1].target)
        self.rpc.request.return_value = [{"name": "flat", "location": {"uri": "file:///a", "range": RANGE}}]
        with self.assertRaises(ValueError):
            self.client.document_symbols("file:///a")
        self.rpc.request.return_value = None
        self.assertEqual(self.client.document_symbols("file:///a"), ())
        self.assertEqual(self.client.document_links("file:///a"), ())

    def test_capability_failure_is_a_recoverable_request_error(self):
        self.rpc.request.return_value = {"capabilities": {}}
        self.client.start(self.root)
        with self.assertRaises(LspRequestError):
            self.client.document_symbols("file:///a")
        with self.assertRaises(LspRequestError):
            self.client.document_links("file:///a")

    def test_missing_database_does_not_spawn_process(self):
        (self.root / "compile_commands.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.client.start(self.root)
        self.factory.assert_not_called()

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
