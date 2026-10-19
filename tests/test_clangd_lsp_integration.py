from contextlib import closing
import json
from hashlib import sha1, sha256
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from application_factory import create_application
from clangd_lsp import ClangdClient
from clangd_lsp.records import DiagnosticReason, LspPosition
from clangd_lsp.static_index import StaticIndexSource
from index_model import CollectionStage, CollectionState, SourceRangeRole
from pipeline_config import load_config


ROOT = Path(__file__).resolve().parents[1]


def yaml_location(path, line, start, end):
    return (f"{{FileURI: '{path.as_uri()}', Start: {{Line: {line}, Column: {start}}}, "
            f"End: {{Line: {line}, Column: {end}}}}}")


def yaml_symbol(identifier, name, declaration, definition):
    return (f"--- !Symbol\nID: '{identifier}'\nName: {name}\nScope: ''\n"
            "SymInfo: {Kind: Function, Lang: Cpp}\n"
            f"CanonicalDeclaration: {declaration}\nDefinition: {definition}\nFlags: 9\n"
            "Signature: ''\nTemplateSpecializationArgs: ''\nCompletionSnippetSuffix: ''\n"
            "Documentation: ''\nReturnType: int\nType: c:I\n...\n")


@unittest.skipUnless(shutil.which("clangd"), "clangd executable is not installed")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.header = self.root / "functions.hpp"
        self.main = self.root / "main.cpp"
        self.header.write_text("int add(int value);\n")
        self.main.write_text('#include "functions.hpp"\n'
                             'int add(int value) { return value + 1; }\n'
                             'int main() { return add(2); }\n')
        (self.root / "compile_commands.json").write_text(json.dumps([{
            "directory": str(self.root), "file": str(self.main),
            "arguments": ["clang++", "-std=c++17", "-fprofile-abs-path", "-c", str(self.main)],
        }]))
        main_location = yaml_location(self.main, 2, 4, 8)
        export = (yaml_symbol("0E86DEBF47BE685C", "add", yaml_location(self.header, 0, 4, 7),
                              yaml_location(self.main, 1, 4, 7))
                  + yaml_symbol("3017D418E3EE7259", "main", main_location, main_location))
        (self.root / "index.yaml").write_text(export)
        self.config = self.root / "config.yaml"
        self.config.write_text("project_root: .\ndatabase:\n  path: index.sqlite3\n"
                               "  save_after_each_step: true\nsteps:\n"
                               "  - type: clangd_yaml\n    path: index.yaml\n"
                               "  - type: clangd_lsp\n    compile_commands_dir: .\n    timeout_seconds: 10\n"
                               "    remove_compile_flags: [-fprofile-abs-path]\n")

    def test_real_clangd_enriches_imported_symbols_and_sqlite(self):
        original_commands = (self.root / "compile_commands.json").read_bytes()
        app = create_application(load_config(self.config))
        model = app.run()
        self.assertEqual(len(model.symbols), 2)
        self.assertEqual(len(model.symbol_source_ranges), 4)
        self.assertEqual(len(model.file_includes), 1)
        edge = model.file_includes[0]
        self.assertEqual(edge.includer.uri, self.main.as_uri())
        self.assertEqual(edge.included.uri, self.header.as_uri())
        definition = next(r for r in model.symbol_source_ranges
                          if r.symbol.name == "add" and r.role is SourceRangeRole.DEFINITION)
        self.assertEqual(definition.location.start.column, 0)
        self.assertEqual(definition.location.end.column, len(self.main.read_text().splitlines()[1]))
        self.assertEqual(model.symbol_bodies, ())
        call, = model.symbol_references
        self.assertEqual(call.target.name, "add")
        self.assertEqual(call.container.name, "main")
        self.assertEqual(int(call.kind), 4)
        for file in model.files:
            for stage in (CollectionStage.SOURCE_RANGES, CollectionStage.INCLUDES):
                self.assertEqual(model.collection_status(file.file_id, stage).status, CollectionState.COMPLETE)
        self.assertEqual(app.steps[2].report.diagnostics, ())
        self.assertEqual((self.root / "compile_commands.json").read_bytes(), original_commands)
        with closing(sqlite3.connect(self.root / "index.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM symbol_source_ranges").fetchone(), (4,))
            self.assertEqual(connection.execute("SELECT count(*) FROM file_includes").fetchone(), (1,))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_excluded_header_keeps_imported_symbols_and_resolved_include(self):
        with self.config.open("a") as stream:
            stream.write("    exclude_paths: ['functions.hpp']\n")
        app = create_application(load_config(self.config))
        model = app.run()
        self.assertEqual(len(model.symbols), 2)
        self.assertEqual(len(model.symbol_source_ranges), 3)
        self.assertEqual(len(model.file_includes), 1)
        header = model.file_includes[0].included
        self.assertEqual(model.collection_status(header.file_id, CollectionStage.SOURCE_RANGES).status,
                         CollectionState.NOT_REQUESTED)
        self.assertEqual(app.steps[2].report.files_excluded, 1)
        self.assertEqual(app.steps[2].report.files_processed, 1)
        self.assertEqual(app.steps[2].report.diagnostics, ())

    def test_real_identity_recovers_ambiguous_ranges_and_refreshes_mapping_on_rerun(self):
        # Obtain the actual clangd identity, then use it as the exported identity.
        # A second exported symbol deliberately shares the same source locations.
        with closing(ClangdClient(self.root, timeout_seconds=10,
                                  remove_compile_flags=["-fprofile-abs-path"])) as client:
            client.start(self.root)
            client.open_document(self.main.as_uri(), self.main.read_text())
            info, = client.symbol_info(self.main.as_uri(), LspPosition(1, 4))
            self.assertEqual(info.name, "add")
            self.assertTrue(info.usr)
            self.assertRegex(info.id, r"^[0-9A-F]{16}$")
        decoy_id = "FFFFFFFFFFFFFFFF"
        self.assertNotEqual(info.id, decoy_id)
        declaration = yaml_location(self.header, 0, 4, 7)
        definition = yaml_location(self.main, 1, 4, 7)
        main_location = yaml_location(self.main, 2, 4, 8)
        records = [yaml_symbol(info.id, "add", declaration, definition),
                   yaml_symbol("3017D418E3EE7259", "main", main_location, main_location),
                   yaml_symbol(decoy_id, "add", declaration, definition)]
        app = create_application(load_config(self.config))
        local_ids = []
        for export in (records, list(reversed(records))):
            (self.root / "index.yaml").write_text("".join(export))
            with self.assertLogs("clangd_lsp.step", level="WARNING"):
                model = app.run()
            imported = app.steps[1].report.symbol_ids
            actual, decoy = imported[info.id], imported[decoy_id]
            local_ids.append(actual)
            self.assertEqual(len(model.symbol_source_ranges), 4)
            self.assertEqual(sum(r.symbol.symbol_id == actual for r in model.symbol_source_ranges), 2)
            self.assertFalse(any(r.symbol.symbol_id == decoy for r in model.symbol_source_ranges))
            report = app.steps[2].report
            self.assertEqual(report.occurrences_identity_resolved, 2)
            self.assertEqual(report.occurrences_matched, 4)
            self.assertEqual(report.occurrences_total, 6)
            self.assertEqual({d.reason for d in report.diagnostics},
                             {DiagnosticReason.AMBIGUOUS_MATCH, DiagnosticReason.UNRESOLVED_IDENTITY})
            self.assertEqual({d.symbol_id for d in report.diagnostics}, {decoy})
            with closing(sqlite3.connect(self.root / "index.sqlite3")) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM symbol_source_ranges WHERE symbol_id = ?", (actual,)).fetchone(), (2,))
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM symbol_source_ranges WHERE symbol_id = ?", (decoy,)).fetchone(), (0,))
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        self.assertNotEqual(*local_ids)

    def test_cli_runs_both_steps_and_reports_generic_summary(self):
        result = subprocess.run([sys.executable, "-W", "error::ResourceWarning", str(ROOT / "run_pipeline.py"),
                                 "--config", str(self.config)], cwd=ROOT, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LSP source ranges added: 4", result.stdout)
        self.assertIn("LSP includes added: 1", result.stdout)
        self.assertIn("Completed step 4/4", result.stderr)

    def test_recovers_definition_and_direct_inheritance_from_real_clangd(self):
        self.main.write_text("int callee();\nint callee() { return 1; }\nstruct Base {};\nstruct Derived : Base {};\n")
        def identifier(usr):
            return sha1(usr.encode()).hexdigest()[:16].upper()
        loc = yaml_location(self.main, 0, 4, 10)
        callee = yaml_symbol(identifier("c:@F@callee#"), "callee", loc, loc)
        callee = "\n".join(line for line in callee.splitlines() if not line.startswith("Definition:")) + "\n"
        records = [callee]
        for name, line, end in (("Base", 2, 11), ("Derived", 3, 14)):
            location = yaml_location(self.main, line, 7, end)
            records.append(yaml_symbol(identifier("c:@S@" + name), name, location, location)
                           .replace("Kind: Function", "Kind: Struct"))
        (self.root / "index.yaml").write_text("".join(records))
        app = create_application(load_config(self.config))
        model = app.run()
        callee = next(s for s in model.symbols if s.name == "callee")
        self.assertEqual(callee.definition.start.line, 1)
        base, = model.class_inheritance
        self.assertEqual((base.base.name, base.derived.name), ("Base", "Derived"))
        self.assertIsNone(base.access)
        self.assertIsNone(base.is_virtual)
        self.assertEqual(app.steps[2].report.structural.locations_filled, 1)
        self.assertEqual(app.steps[2].report.structural.inheritance_added, 1)
        again = app.steps[2].run(model)
        self.assertEqual(len(again.symbol_references), len(model.symbol_references))
        self.assertEqual(app.steps[2].report.structural.locations_filled, 0)
        self.assertEqual(app.steps[2].report.structural.inheritance_added, 0)

    def test_static_index_supplies_call_from_unopened_translation_unit(self):
        remote = self.root / "remote.cpp"
        remote.write_text("int add(int);\nint remote() { return add(1); }\n")
        remote_id = sha1(b"c:@F@remote#").hexdigest()[:16].upper()
        export = self.root / "index.yaml"
        location = yaml_location(remote, 1, 4, 10)
        with export.open("a") as stream:
            stream.write(yaml_symbol(remote_id, "remote", location, location))
            stream.write("--- !Refs\nID: '0E86DEBF47BE685C'\nReferences:\n"
                         f"  - Kind: 28\n    Location: {yaml_location(remote, 1, 22, 25)}\n"
                         f"    Container: {{ID: '{remote_id}'}}\n...\n")
        with closing(ClangdClient(self.root, timeout_seconds=10,
                                  remove_compile_flags=["-fprofile-abs-path"])) as client:
            client.start(self.root, index_source=StaticIndexSource(export, sha256(export.read_bytes()).hexdigest()))
            self.assertTrue(client.workspace_symbols("remote").items)
            client.open_document(self.main.as_uri(), self.main.read_text())
            item, = client.prepare_calls(self.main.as_uri(), LspPosition(1, 4)).items
            calls = client.incoming_calls(item).items
            self.assertTrue(any(call.other.name == "remote" and call.other.location.uri == remote.as_uri() for call in calls))
            self.assertNotIn(remote.as_uri(), client._open_versions)
        app = create_application(load_config(self.config))
        model = app.run()
        calls = [r for r in model.symbol_references if r.container is not None and r.target.name == "add"]
        self.assertEqual({r.container.name for r in calls}, {"main", "remote"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(app.steps[2].report.structural.call_candidates, 2)
        self.assertFalse(any(int(r.kind) & 16 for r in model.symbol_references))

    def test_invalid_static_index_does_not_silently_continue_with_dynamic_index(self):
        export = self.root / "index.yaml"
        app = create_application(load_config(self.config))
        from index_model import IndexBuilder, IndexMetadata
        builder = IndexBuilder()
        builder.set_metadata(IndexMetadata(project_root=str(self.root)))
        model = app.steps[1].run(builder.build())
        bad = self.root / "invalid.yaml"
        bad.write_text("not an index: [\n")
        step = app.steps[2]
        step._index_source_provider = lambda: StaticIndexSource(bad, sha256(bad.read_bytes()).hexdigest())
        from clangd_lsp import LspSessionError
        with self.assertRaisesRegex(LspSessionError, "Static index"):
            step.run(model)
        self.assertIsNone(step.report)
        self.assertEqual(model.symbol_source_ranges, ())
        self.assertIsNone(step._client._index._temporary)
        self.assertTrue(export.exists())

    def test_call_hierarchy_address_taking_is_preserved_without_false_call_flags(self):
        self.main.write_text("void target() {}\nvoid caller() { auto p = &target; target(); }\n")
        records = []
        for name, line in (("target", 0), ("caller", 1)):
            identifier = sha1(f"c:@F@{name}#".encode()).hexdigest()[:16].upper()
            location = yaml_location(self.main, line, 5, 5 + len(name))
            records.append(yaml_symbol(identifier, name, location, location))
        (self.root / "index.yaml").write_text("".join(records))
        app = create_application(load_config(self.config))
        model = app.run()
        references = [r for r in model.symbol_references if r.target.name == "target"]
        self.assertEqual({r.location.start.column for r in references}, {26, 34})
        self.assertTrue(all(r.container.name == "caller" for r in references))
        self.assertFalse(any(int(r.kind) & 16 for r in references))
        self.assertEqual(app.steps[2].report.structural.call_candidates, 2)

    def test_missing_lsp_executable_fails_preflight_without_creating_database(self):
        with self.config.open("a") as stream:
            stream.write("    executable: ./does-not-exist\n")
        app = create_application(load_config(self.config))
        with self.assertLogs("index_pipeline", level="ERROR"), self.assertRaises(FileNotFoundError):
            app.run()
        self.assertFalse((self.root / "index.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
