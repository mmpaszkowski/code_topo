from dataclasses import FrozenInstanceError
from io import BytesIO, StringIO
from unittest.mock import patch
import unittest

import yaml

from clangd_import.records import ExportLocation, ExportPosition, ClangdReferences, ClangdRelation, ClangdSymbol
from clangd_import.reader import IndexParseError, iter_index_records


ID = "0000000000000001"
OTHER_ID = "FFFFFFFFFFFFFFFF"
LOCATION = (
    "{FileURI: 'file:///usr/include/c%2B%2B/example.h', "
    "Start: {Line: 0, Column: 1}, End: {Line: 0, Column: 4}}"
)


def symbol_yaml(extra="", info="  Kind: Function\n  Lang: Cpp\n"):
    return (
        f"--- !Symbol\nID: {ID}\nName: true\nScope: ''\nSymInfo:\n{info}"
        f"CanonicalDeclaration: {LOCATION}\n"
        "Flags: 1073741824\nSignature: '(int x)'\n"
        "TemplateSpecializationArgs: '<T>'\n"
        "CompletionSnippetSuffix: '(${1:int x})'\n"
        'Documentation: "first\\nsecond – 文"\nReturnType: null\nType: c:I\n'
        f"{extra}...\n"
    ).encode()


def refs_yaml(container=ID, extra="", duplicate=False):
    entry = (
        f"  - Kind: 25\n    Location: {LOCATION}\n"
        f"    Container: {{ID: '{container}'}}\n"
    )
    return (
        f"--- !Refs\nID: {OTHER_ID}\nReferences:\n"
        f"{entry * (2 if duplicate else 1)}{extra}...\n"
    ).encode()


def relation_yaml(predicate=1):
    return (
        f"--- !Relations\nSubject: {{ID: '{ID}'}}\nPredicate: {predicate}\n"
        f"Object: {{ID: '{OTHER_ID}'}}\n...\n"
    ).encode()


def read(raw, **kwargs):
    with BytesIO(raw) as stream:
        return list(iter_index_records(stream, **kwargs))


class YamlReaderTests(unittest.TestCase):
    def test_symbol_all_scalar_fields_and_optional_absence(self):
        record, = read(symbol_yaml())
        symbol = record.payload
        self.assertIsInstance(symbol, ClangdSymbol)
        self.assertEqual((symbol.symbol_id, symbol.name, symbol.scope), (ID, "true", ""))
        self.assertEqual((symbol.sym_info.kind, symbol.sym_info.language), ("Function", "Cpp"))
        self.assertEqual(symbol.canonical_declaration, ExportLocation(
            "file:///usr/include/c%2B%2B/example.h", ExportPosition(0, 1), ExportPosition(0, 4)
        ))
        self.assertEqual(symbol.flags, 1073741824)
        self.assertEqual(symbol.signature, "(int x)")
        self.assertEqual(symbol.template_specialization_args, "<T>")
        self.assertEqual(symbol.completion_snippet_suffix, "(${1:int x})")
        self.assertEqual(symbol.documentation, "first\nsecond – 文")
        self.assertEqual((symbol.return_type, symbol.type), ("null", "c:I"))
        self.assertIsNone(symbol.definition)
        self.assertIsNone(symbol.exported_reference_count)
        self.assertEqual(symbol.include_headers, ())
        self.assertEqual(record.diagnostics, ())
        self.assertEqual(record.source.parse_status, "parsed")

    def test_optional_definition_counts_and_repeated_headers(self):
        symbol = read(symbol_yaml(
            f"Definition: {LOCATION}\nReferences: 0\nIncludeHeaders:\n"
            "  - {Header: '<one.h>', References: 12}\n"
            "  - {Header: '<two.h>', References: 0}\n"
            "  - {Header: '<one.h>', References: 12}\n"
        ))[0].payload
        self.assertEqual(symbol.definition, symbol.canonical_declaration)
        self.assertEqual(symbol.exported_reference_count, 0)
        self.assertEqual([h.header for h in symbol.include_headers], ["<one.h>", "<two.h>", "<one.h>"])
        self.assertEqual([h.exported_reference_count for h in symbol.include_headers], [12, 0, 12])

    def test_models_are_frozen_and_nested_collections_are_tuples(self):
        record = read(symbol_yaml("IncludeHeaders: []\n"))[0]
        self.assertFalse(hasattr(record.payload, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            record.payload.name = "changed"
        with self.assertRaises(FrozenInstanceError):
            record.payload.canonical_declaration.start.line = 99
        self.assertIsInstance(record.payload.include_headers, tuple)
        self.assertIsInstance(record.diagnostics, tuple)

    def test_references_preserve_unknown_ids_duplicates_and_bitmasks(self):
        record = read(refs_yaml(duplicate=True))[0]
        self.assertIsInstance(record.payload, ClangdReferences)
        self.assertEqual(record.payload.target_symbol_id, OTHER_ID)
        self.assertIsInstance(record.payload.references, tuple)
        first, second = record.payload.references
        self.assertEqual(first, second)
        self.assertEqual(first.kind, 25)
        self.assertEqual(first.container_symbol_id, ID)

    def test_zero_container_only_becomes_none(self):
        group = read(refs_yaml(container="0" * 16))[0].payload
        self.assertIsNone(group.references[0].container_symbol_id)
        with self.assertRaises(IndexParseError):
            read(relation_yaml().replace(ID.encode(), b"0" * 16))

    def test_empty_and_repeated_groups_and_relations(self):
        group = f"--- !Refs\nID: {ID}\nReferences: []\n...\n".encode()
        raw = group * 2 + relation_yaml(1024) * 2
        records = read(raw)
        self.assertEqual([r.source.document_ordinal for r in records], [0, 1, 2, 3])
        self.assertEqual(records[0].payload.references, ())
        self.assertEqual(records[0].payload, records[1].payload)
        self.assertEqual(records[2].payload, ClangdRelation(ID, 1024, OTHER_ID))
        self.assertEqual(records[2].payload, records[3].payload)

    def test_hex_normalization_and_unknown_enums(self):
        raw = symbol_yaml(info="  Kind: FutureKind\n  Lang: FutureLanguage\n")
        raw = raw.replace(ID.encode(), b"0abcdef012345678")
        symbol = read(raw)[0].payload
        self.assertEqual(symbol.symbol_id, "0ABCDEF012345678")
        self.assertEqual(symbol.sym_info.kind, "FutureKind")
        self.assertEqual(symbol.sym_info.language, "FutureLanguage")

    def test_byte_reconstruction_prefix_whitespace_bom_and_crlf(self):
        for prefix in (b"", b"# comment\n\n", b"\xef\xbb\xbf"):
            with self.subTest(prefix=prefix):
                first = prefix + symbol_yaml() + b"\n# between documents\n"
                raw = (first + refs_yaml()).replace(b"\n", b"\r\n")
                records = read(raw)
                self.assertEqual(b"".join(r.source.raw_yaml for r in records), raw)
                self.assertEqual(records[0].source.start_line, 1)
                self.assertEqual(records[1].source.start_line, first.count(b"\n") + 1)

    def test_known_recovery_retains_language_raw_and_absolute_line(self):
        prefix = relation_yaml()
        broken = symbol_yaml(info="  Kind:            Lang:            C\n")
        for ending in (b"\n", b"\r\n"):
            raw = (prefix + broken).replace(b"\n", ending)
            record = read(raw)[1]
            self.assertIsNone(record.payload.sym_info.kind)
            self.assertEqual(record.payload.sym_info.language, "C")
            self.assertEqual(record.source.parse_status, "recovered")
            self.assertEqual(record.source.raw_yaml, broken.replace(b"\n", ending))
            diagnostic, = record.diagnostics
            expected_line = raw[:raw.index(b"  Kind:")].count(b"\n") + 1
            self.assertEqual(diagnostic.source_line, expected_line)
            self.assertEqual(diagnostic.field_path, "SymInfo.Kind")
            self.assertEqual(diagnostic.severity, "warning")

    def test_strict_mode_rejects_known_corruption(self):
        with self.assertRaises(IndexParseError):
            read(symbol_yaml(info="  Kind:            Lang:            C\n"), recover_known_errors=False)

    def test_recovery_does_not_hide_other_errors(self):
        broken = symbol_yaml(info="  Kind:            Lang:            C\n")
        for invalid in (broken.replace(b"Flags: 1073741824", b"Flags: -1"),
                        broken.replace(b"Lang:            C", b"Lang:            C extra"),
                        broken.replace(b"Lang:            C", b"Lang:            Cpp"),
                        broken.replace(b"!Symbol", b"!Future")):
            with self.subTest(raw=invalid), self.assertRaises(IndexParseError):
                read(invalid)

    def test_unknown_fields_are_preserved_and_unknown_tags_rejected_without_constructors(self):
        raw = symbol_yaml("FutureField: {value: true}\n")
        symbol = read(raw)[0]
        self.assertEqual(symbol.source.raw_yaml, raw)
        unknown = b"--- !Future\nValue: !python/object:Thing {new: field}\n...\n"
        with patch.object(yaml, "load", side_effect=AssertionError("constructors forbidden")):
            with self.assertRaisesRegex(IndexParseError, "Unsupported document tag"):
                read(unknown)

    def test_duplicate_keys_rejected_in_known_and_unknown_fields(self):
        for raw, field in (
            (symbol_yaml("Name: duplicate\n"), "Name"),
            (symbol_yaml("Extra: {x: first, x: second}\n"), "Extra.x"),
            (b"--- !Future\nExtra: {x: first, x: second}\n...\n", "Extra.x"),
        ):
            with self.subTest(field=field), self.assertRaises(IndexParseError) as caught:
                read(raw)
            self.assertEqual(caught.exception.field_path, field)
            self.assertIn("duplicate", caught.exception.message)

    def test_errors_identify_document_field_and_absolute_line(self):
        prefix = refs_yaml()
        invalid = symbol_yaml().replace(b"Flags: 1073741824", b"Flags: false")
        with self.assertRaises(IndexParseError) as caught:
            read(prefix + invalid)
        error = caught.exception
        self.assertEqual(error.document_ordinal, 1)
        self.assertEqual(error.field_path, "Flags")
        self.assertEqual(error.source_line, (prefix + invalid).split(b"Flags:")[0].count(b"\n") + 1)

    def test_invalid_ids_shapes_ranges_and_missing_fields(self):
        original = symbol_yaml()
        cases = [
            (original.replace(ID.encode(), b"123"), "ID"),
            (original.replace(b"Name: true\n", b""), "Name"),
            (original.replace(b"Name: true", b"Name: []"), "Name"),
            (original.replace(b"  Kind: Function\n", b""), "SymInfo.Kind"),
            (symbol_yaml("IncludeHeaders: {}\n"), "IncludeHeaders"),
            (symbol_yaml("IncludeHeaders: [0]\n"), "IncludeHeaders[0]"),
            (original.replace(b"Line: 0, Column: 1", b"Line: -1, Column: 1"), "CanonicalDeclaration.Start.Line"),
            (original.replace(b"Line: 0, Column: 4", b"Line: 0, Column: 0"), "CanonicalDeclaration.End"),
            (refs_yaml().replace(b"Kind: 25", b"Kind: 1.5"), "References[0].Kind"),
        ]
        for raw, field in cases:
            with self.subTest(field=field), self.assertRaises(IndexParseError) as caught:
                read(raw)
            self.assertEqual(caught.exception.field_path, field)

    def test_syntax_and_encoding_failures(self):
        for raw in (b"--- !Symbol\nName: [\n", b"--- !Future\nName: \xff\n",
                    b"Name: untagged\n", b"# comments without a document\n"):
            with self.subTest(raw=raw), self.assertRaises(IndexParseError):
                read(raw)

    def test_invalid_character_line_with_unicode_and_both_loaders(self):
        raw = relation_yaml() + ("--- !Future\nName: " + "文" * 100 + "\nBad: \x00\n").encode()
        expected_line = raw[:raw.index(b"\x00")].count(b"\n") + 1
        for loader in (getattr(yaml, "CBaseLoader", yaml.BaseLoader), yaml.BaseLoader):
            with self.subTest(loader=loader.__name__):
                with patch("clangd_import.reader._LOADER", loader):
                    with self.assertRaises(IndexParseError) as caught:
                        read(raw)
                self.assertEqual(caught.exception.source_line, expected_line)
                self.assertEqual(caught.exception.document_ordinal, 1)

    def test_recursive_alias_rejected_and_shared_alias_accepted(self):
        with self.assertRaises(IndexParseError):
            read(b"--- !Future\nCycle: &self [*self]\n...\n")
        raw = symbol_yaml("One: &value {x: 1}\nTwo: *value\n")
        self.assertEqual(read(raw)[0].source.raw_yaml, raw)

    def test_stream_is_incremental_and_caller_owned(self):
        first = relation_yaml()
        second = refs_yaml()

        class GuardedStream(BytesIO):
            def read(self, *args):
                raise AssertionError("reader must iterate lines, not read the entire file")

        with GuardedStream(first + second) as stream:
            records = iter_index_records(stream)
            record = next(records)
            self.assertEqual(record.source.raw_yaml, first)
            self.assertEqual(stream.tell(), len(first) + len(second.splitlines(keepends=True)[0]))
            records.close()
            self.assertFalse(stream.closed)
        with BytesIO(symbol_yaml().replace(b"Flags: 1073741824", b"Flags: bad")) as stream:
            with self.assertRaises(IndexParseError):
                list(iter_index_records(stream))
            self.assertFalse(stream.closed)

    def test_empty_input_and_binary_stream_requirement(self):
        with self.assertRaises(IndexParseError):
            read(b"")
        with StringIO("--- !Future\n") as stream:
            with self.assertRaises(TypeError):
                list(iter_index_records(stream))

    def test_pure_python_loader_fallback(self):
        with patch("clangd_import.reader._LOADER", yaml.BaseLoader):
            self.assertEqual(read(symbol_yaml())[0].payload.symbol_id, ID)


if __name__ == "__main__":
    unittest.main()
