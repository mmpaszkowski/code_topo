from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clangd_lsp.static_index import StaticIndex, StaticIndexSource


class StaticIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "export.yaml"
        self.index = StaticIndex()
        self.addCleanup(self.index.close)

    def prepare(self, data):
        self.path.write_bytes(data)
        return self.index.prepare(StaticIndexSource(self.path, sha256(data).hexdigest()))

    def test_copy_preserves_input_and_outlives_original_replacement(self):
        data = b"--- !Symbol\nID: 0123456789ABCDEF\n...\n"
        prepared = self.prepare(data)
        self.assertEqual(self.path.read_bytes(), data)
        self.path.write_bytes(b"changed")
        self.assertEqual(prepared.read_bytes(), data)
        self.assertEqual(self.index.normalized_records, 0)
        self.index.close()
        self.assertFalse(prepared.exists())

    def test_only_known_broken_kind_is_normalized_with_original_newlines(self):
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                self.index.close()
                data = b"SymInfo:" + newline + b"  Kind:            Lang:            C" + newline
                data += b"other: 'Kind: Lang: C'" + newline
                prepared = self.prepare(data)
                self.assertEqual(self.index.normalized_records, 1)
                self.assertEqual(prepared.read_bytes(), b"SymInfo:" + newline + b"  Kind: Unknown" + newline
                                 + b"  Lang: C" + newline + b"other: 'Kind: Lang: C'" + newline)
                self.assertEqual(self.path.read_bytes(), data)

    def test_unrelated_malformed_data_is_not_repaired(self):
        data = b"SymInfo:\n  Kind: Lang: Cpp\n"
        self.assertEqual(self.prepare(data).read_bytes(), data)
        self.assertEqual(self.index.normalized_records, 0)

    def test_digest_mismatch_fails_without_creating_temporary_index(self):
        self.path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.index.prepare(StaticIndexSource(self.path, sha256(b"original").hexdigest()))
        self.assertIsNone(self.index._temporary)


if __name__ == "__main__":
    unittest.main()
