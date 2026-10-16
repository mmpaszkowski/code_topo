from contextlib import closing
from dataclasses import fields, replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import index_model as m
from sqlite_model_store import SQLiteModelStore
from validate_snapshot import validate_snapshot


STAMP = "2026-10-16T12:00:00Z"


def populated_model():
    """Exercise every collection, nullable links, and non-default lookup IDs."""
    b = m.IndexBuilder()
    b.set_metadata(m.IndexMetadata(project_root="/example", source_revision="revision-one",
        build_input_hash="a" * 64, metrics_version="metrics-v1", created_at=STAMP))
    b.add_library(library_id=11, name="core")
    b.add_language(language_id=41, name="C++")
    b.add_symbol_kind(kind_id=51, name="function")
    b.add_access_specifier(access_id=61, name="public")
    for flag in m.ReferenceKind:
        b.add_reference_kind_bit(bit=int(flag), name=flag.name.lower())
    b.add_file(file_id=21, uri="file:///example/main.cpp", library_id=11, content_hash="b" * 64)
    b.add_file(file_id=22, uri="file:///example/header.h")
    for file_id in (21, 22):
        for stage in m.CollectionStage:
            state, error = m.CollectionState.NOT_REQUESTED, None
            if stage is m.CollectionStage.SYMBOLS:
                state, error = m.CollectionState.PARTIAL, "unknown kind"
            b.add_collection_status(file_id=file_id, stage=stage, status=state, error=error)
    b.add_location(location_id=31, file_id=21, start=m.Position(0, 3), end=m.Position(3, 4))
    b.add_symbol(symbol_id=81, name="Base", scope_id=1, language_id=41)
    b.add_symbol(symbol_id=82, name="Derived", scope_id=1, language_id=41)
    b.add_scope(scope_id=71, qualified_name="Base::", owner_symbol_id=81)
    b.add_scope(scope_id=72, qualified_name="Derived::", owner_symbol_id=82)
    b.add_symbol(symbol_id=83, name="method", scope_id=71, language_id=41, kind_id=51,
        declaration_location_id=31, definition_location_id=31, signature="(int)",
        template_specialization_args="<int>", documentation="Source comment", return_type="int")
    b.add_symbol(symbol_id=84, name="method", scope_id=72, language_id=41)
    b.add_symbol(symbol_id=85)
    b.add_file_include(include_id=111, includer_file_id=21, included_file_id=22)
    b.add_symbol_reference(reference_id=101, target_symbol_id=83, container_symbol_id=84,
        location_id=31, kind=m.ReferenceKind.REFERENCE | m.ReferenceKind.CALL)
    b.add_symbol_reference(reference_id=102, target_symbol_id=85, location_id=31, kind=m.ReferenceKind.REFERENCE)
    b.add_class_inheritance(inheritance_id=91, base_symbol_id=81, derived_symbol_id=82,
        access_id=61, is_virtual=True)
    b.add_class_inheritance(inheritance_id=92, base_symbol_id=82, derived_symbol_id=85)
    b.add_class_inheritance(inheritance_id=93, base_symbol_id=81, derived_symbol_id=85, is_virtual=False)
    b.add_method_override(override_id=121, overridden_symbol_id=83, overriding_symbol_id=84)
    b.add_symbol_source_range(symbol_id=84, location_id=31, role=m.SourceRangeRole.BODY)
    metrics = m.BodyMetrics(**{field.name: number for number, field in enumerate(fields(m.BodyMetrics), 1)})
    b.add_symbol_body(symbol_id=84, body_location_id=31, metrics=metrics)
    b.add_generated_documentation(symbol_id=84, comment="Generated comment", status=m.DocumentationStatus.COMPLETE,
        model="test-model", prompt_version="v2", source_input_hash="c" * 64,
        dependency_context_hash="d" * 64, created_at=STAMP, completed_at=STAMP)
    b.add_generated_documentation(symbol_id=85, status=m.DocumentationStatus.FAILED,
        error="generation failed", created_at=STAMP)
    return b.build()


class SQLiteModelStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "snapshot #1.sqlite3"
        self.store = SQLiteModelStore(self.path)
        self.model = populated_model()

    def query(self, sql):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql).fetchall()

    def test_every_collection_and_field_is_saved_without_changing_status(self):
        self.store.check_destination("/example")
        self.assertFalse(self.path.exists())
        self.store.save(self.model)
        with closing(sqlite3.connect(self.path)) as connection:
            for field in fields(m.IndexModel):
                if field.name.startswith("_"):
                    continue
                table = {"metadata": "index_metadata", "collection_statuses": "collection_status"}.get(field.name, field.name)
                count = 1 if field.name == "metadata" else len(getattr(self.model, field.name))
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], count, table)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], m.SCHEMA_VERSION)
            issues = validate_snapshot(connection)
            self.assertEqual(len(issues), 3)  # Building metadata and two partial coverage rows.
            self.assertIn("Snapshot is building, not complete", issues)

        expected = {
            "index_metadata": [(1, 3, "/example", "revision-one", "a" * 64, "metrics-v1", STAMP, None, "building", None)],
            "libraries": [(11, "core")],
            "files": [(21, "file:///example/main.cpp", 11, "b" * 64), (22, "file:///example/header.h", None, None)],
            "locations": [(31, 21, 0, 3, 3, 4)],
            "languages": [(41, "C++")],
            "symbol_kinds": [(51, "function")],
            "access_specifiers": [(61, "public")],
            "reference_kind_bits": [(1, "declaration"), (2, "definition"), (4, "reference"), (8, "spelled"), (16, "call")],
            "scopes": [(1, "", None), (71, "Base::", 81), (72, "Derived::", 82)],
            "symbols": [
                (81, "Base", 1, None, 41, None, None, None, None, None, None),
                (82, "Derived", 1, None, 41, None, None, None, None, None, None),
                (83, "method", 71, 51, 41, 31, 31, "(int)", "<int>", "Source comment", "int"),
                (84, "method", 72, None, 41, None, None, None, None, None, None),
                (85, None, None, None, None, None, None, None, None, None, None),
            ],
            "file_includes": [(111, 21, 22)],
            "symbol_references": [(101, 83, 20, 31, 84), (102, 85, 4, 31, None)],
            "class_inheritance": [(91, 81, 82, 61, 1), (92, 82, 85, None, None), (93, 81, 85, None, 0)],
            "method_overrides": [(121, 83, 84)],
            "symbol_source_ranges": [(84, 31, "body")],
            "symbol_bodies": [(84, 31, *range(1, 18))],
            "generated_documentation": [
                (84, "Generated comment", "complete", "test-model", "v2", "c" * 64, "d" * 64, STAMP, STAMP, None),
                (85, None, "failed", None, None, None, None, STAMP, None, "generation failed"),
            ],
        }
        for table, rows in expected.items():
            with self.subTest(table=table):
                self.assertEqual(self.query(f"SELECT * FROM {table} ORDER BY 1"), rows)
        self.assertEqual(self.query("SELECT * FROM collection_status ORDER BY file_id, stage"), [
            (file_id, stage.value, "partial" if stage is m.CollectionStage.SYMBOLS else "not_requested",
             "unknown kind" if stage is m.CollectionStage.SYMBOLS else None)
            for file_id in (21, 22) for stage in sorted(m.CollectionStage, key=lambda item: item.value)
        ])
        self.assertEqual(self.query("SELECT parent_symbol_id, resolution_state FROM symbol_details WHERE symbol_id=83"), [(81, "resolved")])
        self.assertEqual(self.query("SELECT line_count FROM symbol_body_metrics"), [(4,)])

    def test_updates_replace_all_rows_and_readers_see_previous_state_until_commit(self):
        self.store.save(self.model)
        builder = m.IndexBuilder()
        builder.set_metadata(replace(self.model.metadata, source_revision="revision-two"))
        newer = builder.build()
        write = self.store._write_model

        def write_and_read(connection, model):
            write(connection, model)
            self.assertEqual(self.query("SELECT source_revision FROM index_metadata"), [("revision-one",)])
            self.assertEqual(self.query("SELECT count(*) FROM symbols"), [(5,)])

        with patch.object(self.store, "_write_model", side_effect=write_and_read):
            self.store.save(newer)
        self.assertEqual(self.query("SELECT source_revision FROM index_metadata"), [("revision-two",)])
        self.assertEqual(self.query("SELECT count(*) FROM symbols"), [(0,)])
        self.assertEqual(self.query("SELECT * FROM scopes"), [(1, "", None)])
        self.assertEqual(self.query("SELECT * FROM access_specifiers"), [])

    def test_owned_scopes_can_be_replaced_without_foreign_key_failures(self):
        self.store.save(self.model)
        self.store.save(self.model)
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.query("SELECT count(*) FROM scopes"), [(3,)])

    def test_write_failure_rolls_back_previous_database(self):
        self.store.save(self.model)
        before = self.path.read_bytes()

        def partial_write(connection, model):
            connection.execute("INSERT INTO libraries VALUES (999, 'uncommitted')")
            raise RuntimeError("write failed")

        with patch.object(self.store, "_write_model", side_effect=partial_write):
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    self.store.save(self.model)
        self.assertEqual(self.path.read_bytes(), before)

    def test_foreign_key_failure_rolls_back_previous_database(self):
        self.store.save(self.model)
        before = self.path.read_bytes()
        write = self.store._write_model

        def broken_link(connection, model):
            write(connection, model)
            connection.execute("UPDATE scopes SET owner_symbol_id = 999 WHERE scope_id = 71")

        with patch.object(self.store, "_write_model", side_effect=broken_link):
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "Foreign key check failed"):
                    self.store.save(self.model)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_first_save_and_interruption_remove_only_new_output(self):
        unrelated = self.directory / "keep.txt"
        unrelated.write_text("keep", encoding="utf-8")
        for failure in (RuntimeError("write failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure)), patch.object(self.store, "_write_model", side_effect=failure):
                with self.assertLogs("sqlite_model_store", level="ERROR"):
                    with self.assertRaises(type(failure)):
                        self.store.save(self.model)
                self.assertEqual(list(self.directory.iterdir()), [unrelated])

    def test_invalid_model_is_rejected_before_touching_database(self):
        # Simulate a corrupted graph; normal model construction rejects this state.
        object.__setattr__(self.model, "metadata", replace(self.model.metadata, metrics_version=None))
        with self.assertLogs("sqlite_model_store", level="ERROR"):
            with self.assertRaises(m.ModelValidationError):
                self.store.save(self.model)
        self.assertFalse(self.path.exists())

    def test_incompatible_schema_or_metadata_is_never_overwritten(self):
        mutations = [
            "PRAGMA user_version = 2",
            "CREATE TABLE unrelated (value TEXT)",
            "DROP INDEX symbols_by_name",
            "DROP VIEW symbol_details",
            "UPDATE index_metadata SET project_root = '/other'",
            "DELETE FROM index_metadata",
        ]
        for number, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                store = SQLiteModelStore(self.directory / f"incompatible-{number}.sqlite3")
                store.save(self.model)
                with closing(sqlite3.connect(store.path)) as connection:
                    connection.execute(mutation)
                    connection.commit()
                before = store.path.read_bytes()
                with self.assertRaises(ValueError):
                    store.check_destination("/example")
                with self.assertLogs("sqlite_model_store", level="ERROR"):
                    with self.assertRaises(ValueError):
                        store.save(self.model)
                self.assertEqual(store.path.read_bytes(), before)

    def test_existing_empty_or_non_sqlite_file_and_directory_are_preserved(self):
        for contents in (b"", b"unrelated text"):
            self.path.write_bytes(contents)
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaises((ValueError, sqlite3.DatabaseError)):
                    self.store.save(self.model)
            self.assertEqual(self.path.read_bytes(), contents)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(ValueError):
            self.store.check_destination("/example")
        self.assertTrue(self.path.is_dir())

    def test_missing_parent_is_rejected_without_creating_directories(self):
        store = SQLiteModelStore(self.directory / "missing" / "index.sqlite3")
        with self.assertRaises(FileNotFoundError):
            store.check_destination("/example")
        self.assertFalse(store.path.parent.exists())

    def test_commit_failure_rolls_back_previous_database(self):
        self.store.save(self.model)
        before = self.path.read_bytes()
        connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("commit failed")

        with patch("sqlite_model_store.sqlite3.connect", side_effect=lambda *args, **kwargs:
                   connect(*args, **kwargs, factory=FailedCommit)):
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"):
                    self.store.save(self.model)
        self.assertEqual(self.path.read_bytes(), before)

    def test_file_appearing_after_preflight_is_not_overwritten_or_removed(self):
        def create_unrelated(*args):
            self.path.write_bytes(b"another writer's file")
            raise FileExistsError(self.path)

        with patch("sqlite_model_store.os.open", side_effect=create_unrelated):
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaises(sqlite3.DatabaseError):
                    self.store.save(self.model)
        self.assertEqual(self.path.read_bytes(), b"another writer's file")

    def test_compatibility_is_rechecked_after_preflight(self):
        self.store.save(self.model)
        check = self.store.check_destination

        def change_after_check(project_root):
            check(project_root)
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute("UPDATE index_metadata SET project_root = '/other'")
                connection.commit()

        with patch.object(self.store, "check_destination", side_effect=change_after_check):
            with self.assertLogs("sqlite_model_store", level="ERROR"):
                with self.assertRaisesRegex(ValueError, "same project"):
                    self.store.save(self.model)
        self.assertEqual(self.query("SELECT project_root FROM index_metadata"), [("/other",)])
        self.assertEqual(self.query("SELECT count(*) FROM symbols"), [(5,)])


if __name__ == "__main__":
    unittest.main()
