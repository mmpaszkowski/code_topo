from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from application_factory import create_application
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
        export = (yaml_symbol("0000000000000001", "add", yaml_location(self.header, 0, 4, 7),
                              yaml_location(self.main, 1, 4, 7))
                  + yaml_symbol("0000000000000002", "main", main_location, main_location))
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
        self.assertEqual(model.symbol_references, ())
        for file in model.files:
            for stage in (CollectionStage.SOURCE_RANGES, CollectionStage.INCLUDES):
                self.assertEqual(model.collection_status(file.file_id, stage).status, CollectionState.COMPLETE)
        self.assertEqual(app.steps[1].report.diagnostics, ())
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
        self.assertEqual(app.steps[1].report.files_excluded, 1)
        self.assertEqual(app.steps[1].report.files_processed, 1)
        self.assertEqual(app.steps[1].report.diagnostics, ())

    def test_cli_runs_both_steps_and_reports_generic_summary(self):
        result = subprocess.run([sys.executable, "-W", "error::ResourceWarning", str(ROOT / "run_pipeline.py"),
                                 "--config", str(self.config)], cwd=ROOT, capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LSP source ranges added: 4", result.stdout)
        self.assertIn("LSP includes added: 1", result.stdout)
        self.assertIn("Completed step 2/2", result.stderr)

    def test_failed_lsp_start_leaves_yaml_checkpoint_intact(self):
        with self.config.open("a") as stream:
            stream.write("    executable: ./does-not-exist\n")
        app = create_application(load_config(self.config))
        with self.assertLogs("index_pipeline", level="ERROR"), self.assertRaises(FileNotFoundError):
            app.run()
        with closing(sqlite3.connect(self.root / "index.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM symbols").fetchone(), (2,))
            self.assertEqual(connection.execute("SELECT count(*) FROM symbol_source_ranges").fetchone(), (0,))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))


if __name__ == "__main__":
    unittest.main()
