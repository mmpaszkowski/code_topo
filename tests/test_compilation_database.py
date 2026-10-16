import json
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from clangd_lsp.compilation_database import CompilationDatabase


class CompilationDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "compile_commands.json"

    def write(self, entries):
        self.path.write_text(json.dumps(entries), encoding="utf-8")

    def test_filters_exact_tokens_preserving_order_contexts_and_original_bytes(self):
        arguments = ["clang++", "-fprofile-abs-path", "-DFLAG=-fprofile-abs-path", "-I", "include dir",
                     "-DVALUE=a b", "-c", "source file.cpp", "-fprofile-abs-path"]
        self.write([
            {"directory": "../source", "file": "source file.cpp", "arguments": arguments, "output": "out.o"},
            {"directory": str(self.root), "file": "source file.cpp", "command": shlex.join(arguments)},
        ])
        before = self.path.read_bytes()
        database = CompilationDatabase(self.root, ["-fprofile-abs-path"])
        self.addCleanup(database.close)
        directory = database.prepare()
        filtered = json.loads((directory / "compile_commands.json").read_text())
        self.assertEqual(filtered[0]["arguments"], [arg for arg in arguments if arg != "-fprofile-abs-path"])
        self.assertEqual(filtered[1]["arguments"], filtered[0]["arguments"])
        self.assertEqual(filtered[0]["file"], "source file.cpp")
        self.assertEqual(filtered[0]["directory"], str(self.root.parent / "source"))
        self.assertEqual(filtered[0]["output"], "out.o")
        self.assertNotIn("command", filtered[1])
        self.assertEqual(self.path.read_bytes(), before)
        database.close()
        self.assertFalse(directory.exists())
        database.close()
        self.assertTrue(database.prepare().is_dir())

    def test_arguments_take_precedence_and_command_text_is_never_executed(self):
        marker = self.root / "must-not-exist"
        literal = f"$(touch {marker})"
        self.write([{"directory": str(self.root), "file": "a.cpp",
                     "command": f"clang++ '-DVALUE={literal}' -fprofile-abs-path a.cpp"},
                    {"directory": str(self.root), "file": "b.cpp", "command": "invalid '",
                     "arguments": ["clang++", "@flags.rsp", "b.cpp"]}])
        database = CompilationDatabase(self.root, ["-fprofile-abs-path"])
        self.addCleanup(database.close)
        filtered = json.loads((database.prepare() / "compile_commands.json").read_text())
        self.assertEqual(filtered[0]["arguments"], ["clang++", f"-DVALUE={literal}", "a.cpp"])
        self.assertEqual(filtered[1]["arguments"], ["clang++", "@flags.rsp", "b.cpp"])
        self.assertFalse(marker.exists())

    def test_no_removals_uses_original_directory(self):
        self.write([])
        database = CompilationDatabase(self.root)
        self.assertEqual(database.prepare(), self.root)
        database.close()
        self.assertTrue(self.path.exists())

    def test_invalid_entries_fail_before_creating_temporary_database(self):
        base = {"directory": str(self.root), "file": "a.cpp", "arguments": ["clang++", "a.cpp"]}
        for entry in (None, {}, dict(base, arguments=[]), dict(base, arguments=[1]),
                      dict(base, directory=42), dict(base, file=""),
                      {"directory": str(self.root), "file": "a.cpp", "command": "clang++ '"}):
            self.write([entry])
            with self.subTest(entry=entry), patch("clangd_lsp.compilation_database.TemporaryDirectory") as temporary:
                with self.assertRaisesRegex(ValueError, "entry 0"):
                    CompilationDatabase(self.root, ["-flag"]).prepare()
                temporary.assert_not_called()

    def test_write_failure_cleans_temporary_directory(self):
        self.write([])
        database = CompilationDatabase(self.root, ["-flag"])
        with patch("clangd_lsp.compilation_database.TemporaryDirectory", wraps=TemporaryDirectory) as temporary:
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    database.prepare()
            temporary.assert_called_once()
        self.assertIsNone(database._temporary)


if __name__ == "__main__":
    unittest.main()
