from dataclasses import replace
from contextlib import closing, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from application import Application
from application_factory import create_application
from index_model import IndexBuilder, SnapshotStatus
from index_pipeline import Step, SummaryItem
from pipeline_config import ClangdYamlStepConfig, DatabaseConfig, PipelineConfig
from run_pipeline import print_summary
from sqlite_model_store import SQLiteModelStore


SCRIPT = Path(__file__).resolve().parents[1] / "run_pipeline.py"


class RecordStep(Step):
    def __init__(self, name, calls, failure=None):
        self.name = name
        self.calls = calls
        self.failure = failure

    def run(self, model):
        self.calls.append((self.name, model))
        if self.failure is not None:
            raise self.failure
        builder = IndexBuilder.from_model(model)
        builder.update_metadata(replace(model.metadata, source_revision=self.name))
        return builder.build()


class ApplicationFactoryTests(unittest.TestCase):
    def test_existing_destination_is_checked_before_constructing_steps(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "unrelated.sqlite3"
            database.write_text("unrelated", encoding="utf-8")
            config = PipelineConfig(Path(directory), (ClangdYamlStepConfig(Path("input.yaml")),),
                                    DatabaseConfig(database, True))
            with patch("application_factory.create_step") as factory:
                with self.assertRaises(sqlite3.DatabaseError):
                    create_application(config)
            factory.assert_not_called()
            self.assertEqual(database.read_text(encoding="utf-8"), "unrelated")

    def test_factory_preserves_configuration_order(self):
        configs = (ClangdYamlStepConfig(Path("first.yaml")), ClangdYamlStepConfig(Path("second.yaml")))
        config = PipelineConfig(Path("/example"), configs)
        steps = (RecordStep("first", []), RecordStep("second", []))
        with patch("application_factory.create_step", side_effect=steps) as factory:
            app = create_application(config)
        self.assertEqual([call.args[0] for call in factory.call_args_list], list(configs))
        self.assertEqual(app.steps, steps)


class ApplicationTests(unittest.TestCase):
    def test_last_saved_step_survives_a_later_step_failure(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            calls = []
            steps = [RecordStep("first", calls), RecordStep("second", calls, RuntimeError("failed"))]
            app = Application(Path(directory), steps, store=SQLiteModelStore(database))
            with self.assertLogs("index_pipeline", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    app.run()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT source_revision, status FROM index_metadata").fetchall(),
                                 [("first", "building")])

    def test_steps_receive_previous_snapshot_in_supplied_order(self):
        calls = []
        steps = (RecordStep("first", calls), RecordStep("second", calls))
        supplied = list(steps)
        app = Application(Path("/example"), supplied)
        supplied.reverse()

        with self.assertLogs("index_pipeline", level="INFO"):
            result = app.run()

        self.assertEqual([name for name, _ in calls], ["first", "second"])
        self.assertEqual(calls[0][1].metadata.project_root, "/example")
        self.assertIsNone(calls[0][1].metadata.source_revision)
        self.assertEqual(calls[0][1].symbols, ())
        self.assertEqual(calls[1][1].metadata.source_revision, "first")
        self.assertEqual(result.metadata.source_revision, "second")
        self.assertEqual(result.metadata.status, SnapshotStatus.BUILDING)
        self.assertEqual(app.steps, steps)

    def test_failure_propagates_and_prevents_later_step_execution(self):
        calls = []
        failure = RuntimeError("tool failed")
        steps = [RecordStep("first", calls, failure), RecordStep("later", calls)]
        app = Application(Path("/example"), steps)
        with self.assertLogs("index_pipeline", level="ERROR"):
            with self.assertRaises(RuntimeError) as caught:
                app.run()
        self.assertIs(caught.exception, failure)
        self.assertEqual([name for name, _ in calls], ["first"])


class SummaryTests(unittest.TestCase):
    def test_any_step_can_supply_summary_values_without_an_import_report(self):
        class AnalysisStep(RecordStep):
            def summary(self):
                return (SummaryItem("Analyzed files", 3), SummaryItem("Analysis mode", "quick"))

        ordinary = RecordStep("ordinary", [])
        step = AnalysisStep("analysis", [])
        app = Application(Path("/example"), [ordinary, step])
        model = app.run()
        output = StringIO()
        with redirect_stdout(output):
            print_summary(model, app.steps)
        self.assertEqual(ordinary.summary(), ())
        self.assertIn("Analyzed files: 3\nAnalysis mode: quick\n", output.getvalue())
        self.assertNotIn("clangd", output.getvalue())


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.settings = self.directory / "settings"
        self.settings.mkdir()
        self.source = self.directory / "source"
        self.source.mkdir()
        self.caller = self.directory / "caller"
        self.caller.mkdir()
        source_file = self.source / "main.cpp"
        source_file.write_text("f\n", encoding="utf-8")
        self.export = self.settings / "input.yaml"
        self.export.write_text(f"""\
--- !Symbol
ID: '0000000000000001'
Name: f
Scope: ''
SymInfo:
  Kind: Function
  Lang: Cpp
CanonicalDeclaration:
  FileURI: '{source_file.as_uri()}'
  Start: {{Line: 0, Column: 0}}
  End: {{Line: 0, Column: 1}}
Flags: 9
Signature: ''
TemplateSpecializationArgs: ''
CompletionSnippetSuffix: ''
Documentation: ''
ReturnType: int
Type: c:I
...
""", encoding="utf-8")
        self.config = self.settings / "config.yaml"
        self.config.write_text("project_root: ../source\nsteps:\n"
                               "  - type: clangd_yaml\n    path: input.yaml\n", encoding="utf-8")

    def run_cli(self, *arguments, cwd=None):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-W", "error::ResourceWarning", str(SCRIPT), *arguments],
                              cwd=cwd or self.settings, env=environment, capture_output=True,
                              text=True, timeout=10)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Project: {self.source}", result.stdout)
        self.assertIn("Snapshot status: building", result.stdout)
        self.assertIn("Files: 1", result.stdout)
        self.assertIn("Symbols: 1", result.stdout)
        self.assertIn("Locations: 1", result.stdout)
        self.assertIn("References: 0", result.stdout)
        self.assertIn("Source assumption:", result.stdout)
        self.assertIn("Starting step 1/1: ClangdYamlStep", result.stderr)
        self.assertIn("Completed step 1/1: ClangdYamlStep", result.stderr)
        self.assertNotIn("Starting step", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_default_config_runs_real_import_and_prints_summary(self):
        before = set(self.directory.rglob("*"))
        result = self.run_cli()
        self.assert_success(result)
        self.assertIn("clangd_yaml diagnostics: 0", result.stdout)
        self.assertEqual(set(self.directory.rglob("*")), before)

    def test_explicit_config_resolves_paths_from_its_directory(self):
        result = self.run_cli("--config", "../settings/config.yaml", cwd=self.caller)
        self.assert_success(result)

    def test_database_enabled_creates_and_updates_one_file(self):
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("database:\n  path: 'index #1.sqlite3'\n  save_after_each_step: true\n")
        database = self.settings / "index #1.sqlite3"
        for name in ("f", "new_name"):
            raw = self.export.read_text(encoding="utf-8")
            self.export.write_text(raw.replace("Name: f\n", f"Name: {name}\n"), encoding="utf-8")
            result = self.run_cli("--config", "../settings/config.yaml", cwd=self.caller)
            self.assert_success(result)
            self.assertIn(f"Database: {database}", result.stdout)
            self.assertIn("Saved model to", result.stderr)
            self.assertLess(result.stderr.index("Saved model to"), result.stderr.index("Completed step"))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT name FROM symbols").fetchall(), [(name,)])
                self.assertEqual(connection.execute("SELECT status FROM index_metadata").fetchall(), [("building",)])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_disabled_saving_does_not_touch_destination(self):
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("database:\n  path: input.yaml\n  save_after_each_step: false\n")
        before = self.export.read_bytes()
        self.assert_success(self.run_cli())
        self.assertEqual(self.export.read_bytes(), before)

    def test_incompatible_database_fails_before_import(self):
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("database:\n  path: input.yaml\n  save_after_each_step: true\n")
        before = self.export.read_bytes()
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Starting step", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.export.read_bytes(), before)

    def test_recovery_prints_diagnostics_and_can_be_disabled(self):
        raw = self.export.read_text(encoding="utf-8")
        self.export.write_text(raw.replace("Kind: Function\n  Lang: Cpp",
                                           "Kind:            Lang:            C"), encoding="utf-8")
        result = self.run_cli()
        self.assert_success(result)
        self.assertIn("clangd_yaml diagnostics: 1", result.stdout)
        self.assertIn("Recovered symbols: 1", result.stdout)
        self.assertIn(f"{self.export}:", result.stderr)
        self.assertIn("SymInfo.Kind", result.stderr)
        self.assertEqual(result.stderr.count("SymInfo.Kind"), 1)
        self.assertLess(result.stderr.index("SymInfo.Kind"), result.stderr.index("Completed step"))

        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("    recover_known_errors: false\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Failed step 1/1", result.stderr)

    def test_config_errors_fail_before_import(self):
        self.export.unlink()
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write("    unexpected: true\n")
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("steps[0]: unknown settings: unexpected", result.stderr)
        self.assertNotIn("Starting step", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_config_and_invalid_arguments_exit_two(self):
        for args in (("--config", "absent.yaml"), ("--unknown",)):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_missing_export_is_an_execution_failure(self):
        self.export.unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Failed step 1/1", result.stderr)
        self.assertIn(str(self.export), result.stderr)

    def test_help_does_not_require_configuration(self):
        result = self.run_cli("--help", cwd=self.caller)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
