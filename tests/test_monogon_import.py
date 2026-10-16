"""Optional acceptance test against the supplied export and its current sources."""

from dataclasses import asdict
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from index_model import (
    CollectionStage, CollectionState, IndexBuilder, IndexMetadata,
    ReferenceKind, SnapshotStatus, validate,
)
from sqlite_model_store import SQLiteModelStore


EXPORT = Path(__file__).resolve().parents[1] / "monogon-index.yaml"


@unittest.skipUnless(os.environ.get("RUN_MONOGON_IMPORT_TEST") == "1" and EXPORT.is_file(),
                     "set RUN_MONOGON_IMPORT_TEST=1 with the Monogon export and source files present")
class MonogonImportTests(unittest.TestCase):
    def test_complete_export(self):
        builder = IndexBuilder()
        builder.set_metadata(IndexMetadata(project_root="/home/noname/Workspace/monogon"))
        initial = builder.build()
        step = ClangdYamlStep(ClangdYamlReader(EXPORT), LocalSourceReader())
        model = step.run(initial)
        report = step.report
        self.assertEqual(asdict(report.counts), {
            "documents": 49285, "symbol_records": 20135, "reference_groups": 28087,
            "reference_occurrences": 195945, "relation_records": 1063, "recovered_symbols": 71,
            "symbols": 28551, "placeholders": 8416, "files": 647, "locations": 112106,
            "references": 195945, "skipped_references": 0, "duplicate_references": 0,
            "inheritance": 682, "overrides": 380, "deferred_relations": 1, "duplicate_relations": 0,
        })
        self.assertEqual(report.yaml_sha256,
                         "274f6a50cb1fc7bbcd7c454781019368b8f78737080f916ed7fa1bdc9564ed2b")
        self.assertEqual(len(report.diagnostics), 72)
        self.assertEqual(sum(d.field_path == "SymInfo.Kind" for d in report.diagnostics), 71)
        self.assertTrue(all(not ref.kind & ReferenceKind.CALL for ref in model.symbol_references))
        self.assertEqual(sum(bool(ref.original_kind & 16) for ref in report.references), 113424)
        self.assertTrue(all(ref.reference_id is not None for ref in report.references))
        self.assertEqual(sum(s.symbol.definition is not None for s in report.exported_symbols.values()), 16487)
        self.assertEqual(sum(len(s.symbol.include_headers) for s in report.exported_symbols.values()), 8337)
        self.assertEqual(len(model.scopes), 16759)
        self.assertTrue(all(scope.owner is None for scope in model.scopes))
        self.assertTrue(all(file.content_hash == report.source_hashes[file.uri] and file.content_hash
                            for file in model.files))
        self.assertEqual(model.file_includes, ())
        self.assertEqual(model.symbol_source_ranges, ())
        self.assertEqual(model.symbol_bodies, ())
        self.assertEqual(model.metadata.status, SnapshotStatus.BUILDING)
        for status in model.collection_statuses:
            if status.stage in (CollectionStage.INCLUDES, CollectionStage.SOURCE_RANGES, CollectionStage.METRICS):
                self.assertEqual(status.status, CollectionState.NOT_REQUESTED)
            if status.stage is CollectionStage.REFERENCES:
                self.assertEqual(status.status, CollectionState.COMPLETE)
        self.assertEqual(report.deferred_relations[0].relation.subject_symbol_id, "44EDD91B88949A8D")
        self.assertEqual(report.deferred_relations[0].source_line, 2893131)
        self.assertEqual(initial.symbols, ())
        self.assertEqual(validate(model), ())

        with TemporaryDirectory() as directory:
            database = Path(directory) / "monogon.sqlite3"
            SQLiteModelStore(database).save(model)
            with closing(sqlite3.connect(database)) as connection:
                for table, expected in {
                    "files": 647, "symbols": 28551, "locations": 112106,
                    "symbol_references": 195945, "class_inheritance": 682,
                    "method_overrides": 380, "scopes": 16759, "collection_status": 647 * 5,
                }.items():
                    self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], expected)
                self.assertEqual(connection.execute("SELECT count(*) FROM symbols WHERE name IS NULL").fetchone()[0], 8416)
                self.assertEqual(connection.execute("SELECT status FROM index_metadata").fetchone()[0], "building")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


if __name__ == "__main__":
    unittest.main()
