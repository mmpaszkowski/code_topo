from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pipeline_config import ConfigurationError, load_config


VALID_CONFIG = """\
project_root: ../source
steps:
  - type: clangd_yaml
    path: export.yaml
"""


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "config.yaml"

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_paths_defaults_and_immutable_settings(self):
        config = self.load(VALID_CONFIG)
        self.assertIsNone(config.database)
        self.assertEqual(config.project_root, self.directory.parent / "source")
        self.assertEqual(config.steps[0].path, self.directory / "export.yaml")
        self.assertTrue(config.steps[0].recover_known_errors)
        self.assertIsInstance(config.steps, tuple)
        with self.assertRaises(FrozenInstanceError):
            config.project_root = self.directory
        with self.assertRaises(FrozenInstanceError):
            config.steps[0].recover_known_errors = False

    def test_absolute_paths_and_disabled_recovery(self):
        config = self.load(f"""\
project_root: '{self.directory}'
steps:
  - type: clangd_yaml
    path: '{self.directory / 'input.yaml'}'
    recover_known_errors: false
""")
        self.assertEqual(config.project_root, self.directory)
        self.assertEqual(config.steps[0].path, self.directory / "input.yaml")
        self.assertFalse(config.steps[0].recover_known_errors)

    def test_database_path_default_and_explicit_saving(self):
        text = VALID_CONFIG + "database:\n  path: output.sqlite3\n"
        config = self.load(text)
        self.assertEqual(config.database.path, self.directory / "output.sqlite3")
        self.assertFalse(config.database.save_after_each_step)
        for save in ("true", "false"):
            config = self.load(text + f"  save_after_each_step: {save}\n")
            self.assertEqual(config.database.save_after_each_step, save == "true")
        with self.assertRaises(FrozenInstanceError):
            config.database.save_after_each_step = True

    def test_invalid_database_settings(self):
        cases = [
            ("database: null", "database: expected a mapping"),
            ("database: []", "database: expected a mapping"),
            ("database: {}", "database.path"),
            ("database: {path: 123}", "database.path"),
            ("database: {path: ''}", "database.path"),
            ("database: {path: a, unexpected: true}", "database: unknown settings"),
            ("database: {path: a, save_after_each_step: 'true'}", "database.save_after_each_step"),
            ("database: {path: a, save_after_each_step: 1}", "database.save_after_each_step"),
            ("database: {path: a, save_after_each_step: null}", "database.save_after_each_step"),
            ("database: {path: a, path: b}", "duplicate setting: path"),
        ]
        for text, expected in cases:
            with self.subTest(text=text), self.assertRaises(ConfigurationError) as caught:
                self.load(VALID_CONFIG + text)
            self.assertIn(expected, str(caught.exception))

    def test_invalid_settings_identify_the_affected_field(self):
        cases = [
            ("", "config: expected a mapping"),
            ("[]", "config: expected a mapping"),
            (VALID_CONFIG.replace("project_root: ../source\n", ""), "project_root"),
            (VALID_CONFIG.replace("../source", "42"), "project_root"),
            (VALID_CONFIG.replace("../source", "' '"), "project_root"),
            ("project_root: .", "steps: expected a nonempty list"),
            ("project_root: .\nsteps: []", "steps: expected a nonempty list"),
            ("project_root: .\nsteps: {}", "steps: expected a nonempty list"),
            ("project_root: .\nsteps: [clangd_yaml]", "steps[0]: expected a mapping"),
            (VALID_CONFIG.replace("type: clangd_yaml", "type: future_step"), "steps[0].type"),
            (VALID_CONFIG.replace("type: clangd_yaml", "type: null"), "steps[0].type"),
            (VALID_CONFIG.replace("    path: export.yaml\n", ""), "steps[0].path"),
            (VALID_CONFIG.replace("path: export.yaml", "path: []"), "steps[0].path"),
            (VALID_CONFIG + "    recover_known_errors: 'false'\n", "steps[0].recover_known_errors"),
            (VALID_CONFIG + "    recover_known_errors: 1\n", "steps[0].recover_known_errors"),
            (VALID_CONFIG + "    recover_known_errors: null\n", "steps[0].recover_known_errors"),
            (VALID_CONFIG + "unexpected: true\n", "config: unknown settings: unexpected"),
            (VALID_CONFIG + "    typo: true\n", "steps[0]: unknown settings: typo"),
            (VALID_CONFIG + "  - type: clangd_yaml\n    path: second.yaml\n", "cannot be repeated"),
            (VALID_CONFIG + "project_root: /second\n", "duplicate setting: project_root"),
            (VALID_CONFIG + "    path: second.yaml\n", "duplicate setting: path"),
            (VALID_CONFIG + "42: invalid\n", "setting names must be strings"),
            (VALID_CONFIG.replace("export.yaml", '"bad\\0path"'), "paths cannot contain a null"),
            ("project_root: .\nsteps: &steps [*steps]", "steps[0]: expected a mapping"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                with self.assertRaises(ConfigurationError) as caught:
                    self.load(text)
                self.assertIn(expected, str(caught.exception))
                self.assertIn(str(self.path), str(caught.exception))

    def test_unreadable_or_invalid_yaml_is_a_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            load_config(self.path)
        with self.assertRaises(ConfigurationError):
            load_config(self.directory)
        self.path.write_bytes(b"\xff")
        with self.assertRaises(ConfigurationError):
            load_config(self.path)
        for text in ("steps: [", "!unsupported {}", "---\n{}\n---\n{}"):
            with self.subTest(text=text), self.assertRaises(ConfigurationError):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
