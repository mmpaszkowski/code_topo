from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from validate_snapshot import COLLECTION_STAGES, validate_snapshot


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "schema.sql").read_text()
STAMP = "2026-10-15T12:00:00Z"
DIGEST = hashlib.sha256(b"source input").hexdigest()
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO index_metadata(project_root, status, completed_at) "
            "VALUES ('/example', 'complete', ?)", (STAMP,)
        )
        self.db.execute("INSERT INTO languages(name) VALUES ('C++')")
        self.db.execute("INSERT INTO files(uri) VALUES ('file:///example/main.cpp')")
        self.db.executemany(
            "INSERT INTO collection_status(file_id, stage, status) "
            "VALUES (1, ?, 'not_requested')",
            [(stage,) for stage in COLLECTION_STAGES],
        )
        self.db.execute(
            "INSERT INTO locations(file_id, start_line, start_column, end_line, end_column) "
            "VALUES (1, 0, 6, 0, 9)"
        )

    def owned_scope(self, owner):
        existing = self.db.execute(
            "SELECT scope_id FROM scopes WHERE owner_symbol_id=?", (owner,)
        ).fetchone()
        if existing:
            return existing[0]
        name, scope = self.db.execute(
            "SELECT name, scope FROM symbol_details WHERE symbol_id=?", (owner,)
        ).fetchone()
        return self.db.execute(
            "INSERT INTO scopes(qualified_name, owner_symbol_id) VALUES (?, ?)",
            (scope + name + "::", owner),
        ).lastrowid

    def symbol(self, name="f", parent=None, scope_id=None):
        if scope_id is None:
            scope_id = 1 if parent is None else self.owned_scope(parent)
        return self.db.execute(
            "INSERT INTO symbols(name, scope_id, language_id) VALUES (?, ?, 1)",
            (name, scope_id),
        ).lastrowid

    def reference(self, target, container=None, kind=4):
        return self.db.execute(
            "INSERT INTO symbol_references(target_symbol_id, location_id, kind, "
            "container_symbol_id) VALUES (?, 1, ?, ?)", (target, kind, container)
        )

    def metrics(self, symbol):
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(symbol_bodies)")]
        values = dict.fromkeys(columns, 0)
        values.update(symbol_id=symbol, body_location_id=1, cyclomatic_complexity=1)
        self.db.execute(
            f"INSERT INTO symbol_bodies({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})", list(values.values())
        )

    def document(self, symbol, **changes):
        values = dict(symbol_id=symbol, comment="Returns the value.", status="complete",
                      model="example-model", prompt_version="brief-v1",
                      source_input_hash=DIGEST, dependency_context_hash=EMPTY_DIGEST,
                      completed_at=STAMP)
        values.update(changes)
        self.db.execute(
            f"INSERT INTO generated_documentation({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})", list(values.values())
        )

    def test_valid_snapshot_and_foreign_key_enforcement(self):
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone(), (1,))
        self.assertEqual(validate_snapshot(self.db), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.reference(999)

    def test_placeholder_resolves_after_references_without_changing_identity(self):
        placeholder = self.db.execute("INSERT INTO symbols DEFAULT VALUES").lastrowid
        self.reference(placeholder)
        self.db.execute(
            "UPDATE symbols SET name='f', scope_id=1, "
            "language_id=1, definition_location_id=1 WHERE symbol_id=?", (placeholder,)
        )
        self.assertEqual(self.db.execute(
            "SELECT target_symbol_id FROM symbol_references"
        ).fetchone(), (placeholder,))
        self.assertEqual(self.db.execute(
            "SELECT resolution_state FROM symbol_details WHERE symbol_id=?", (placeholder,)
        ).fetchone(), ("resolved",))
        self.assertEqual(validate_snapshot(self.db), [])

    def test_resolved_symbol_without_locations_and_unresolved_target_are_valid(self):
        self.symbol()
        target = self.db.execute("INSERT INTO symbols DEFAULT VALUES").lastrowid
        self.reference(target)
        self.assertEqual(self.db.execute(
            "SELECT resolution_state, scope, parent_symbol_id FROM symbol_details "
            "WHERE symbol_id=?", (target,)
        ).fetchone(), ("placeholder", None, None))
        self.assertEqual(validate_snapshot(self.db), [])

    def test_placeholder_cannot_carry_partial_attributes(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO symbols(name) VALUES ('f')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO symbols(scope_id, language_id) VALUES (1, 1)")

    def test_symbols_with_same_display_name_remain_distinct(self):
        self.assertNotEqual(self.symbol("overload"), self.symbol("overload"))

    def test_duplicate_references_rejected_with_and_without_container(self):
        target, container = self.symbol("callee"), self.symbol("caller")
        for owner in (None, container):
            with self.subTest(container=owner):
                self.reference(target, owner)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.reference(target, owner)
        self.reference(target, container, kind=20)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM symbol_references"
        ).fetchone(), (3,))

    def test_repeated_observations_can_be_ignored_explicitly(self):
        target = self.symbol()
        for _ in range(2):
            self.db.execute(
                "INSERT INTO symbol_references(target_symbol_id, location_id, kind) "
                "VALUES (?, 1, 4) ON CONFLICT DO NOTHING", (target,)
            )
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM symbol_references"
        ).fetchone(), (1,))

    def test_self_relationships_rejected_but_recursive_calls_allowed(self):
        symbol = self.symbol()
        for table, left, right in (
            ("class_inheritance", "base_symbol_id", "derived_symbol_id"),
            ("method_overrides", "overridden_symbol_id", "overriding_symbol_id"),
        ):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(f"INSERT INTO {table}({left}, {right}) VALUES (?, ?)",
                                (symbol, symbol))
        self.reference(symbol, symbol, kind=20)
        self.assertEqual(validate_snapshot(self.db), [])

    def test_parent_cycles_rejected_at_validation(self):
        first = self.symbol("outer")
        second = self.symbol("inner", parent=first)
        self.db.execute("UPDATE symbols SET scope_id=? WHERE symbol_id=?",
                        (self.owned_scope(second), first))
        issues = validate_snapshot(self.db)
        self.assertEqual(len(issues), 1)
        self.assertIn("Symbol parent cycle", issues[0])

    def test_self_owned_scope_is_rejected_at_validation(self):
        symbol = self.symbol()
        self.db.execute("UPDATE symbols SET scope_id=? WHERE symbol_id=?",
                        (self.owned_scope(symbol), symbol))
        self.assertTrue(any("Symbol parent cycle" in issue for issue in validate_snapshot(self.db)))

    def test_scope_label_and_owner_are_shared_by_all_members(self):
        owner = self.symbol("ns")
        first = self.symbol("f", parent=owner)
        second = self.symbol("g", parent=owner)
        scope_id = self.owned_scope(owner)
        self.db.execute("UPDATE scopes SET qualified_name='renamed::' WHERE scope_id=?",
                        (scope_id,))
        self.assertEqual(self.db.execute(
            "SELECT scope, parent_symbol_id FROM symbol_details WHERE symbol_id IN (?, ?) "
            "ORDER BY symbol_id", (first, second)
        ).fetchall(), [("renamed::", owner), ("renamed::", owner)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO scopes(qualified_name, owner_symbol_id) VALUES ('other::', ?)",
                            (owner,))

    def test_unknown_scope_owner_can_be_resolved_without_rewriting_members(self):
        scope_id = self.db.execute(
            "INSERT INTO scopes(qualified_name) VALUES ('external::')"
        ).lastrowid
        member = self.symbol("f", scope_id=scope_id)
        self.assertEqual(self.db.execute(
            "SELECT scope, parent_symbol_id FROM symbol_details WHERE symbol_id=?", (member,)
        ).fetchone(), ("external::", None))
        owner = self.symbol("external")
        self.db.execute("UPDATE scopes SET owner_symbol_id=? WHERE scope_id=?", (owner, scope_id))
        self.assertEqual(self.db.execute(
            "SELECT scope_id, parent_symbol_id FROM symbol_details WHERE symbol_id=?", (member,)
        ).fetchone(), (scope_id, owner))
        self.assertEqual(validate_snapshot(self.db), [])

    def test_equal_scope_labels_do_not_merge_distinct_owners(self):
        first, second = self.symbol("local"), self.symbol("local")
        self.assertNotEqual(self.owned_scope(first), self.owned_scope(second))

    def test_global_scope_is_distinct_from_unknown_owner(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO scopes(qualified_name) VALUES ('')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE scopes SET qualified_name='not_global' WHERE scope_id=1")
        self.db.execute("DELETE FROM scopes WHERE scope_id=1")
        self.assertTrue(any("global scope" in issue for issue in validate_snapshot(self.db)))

    def test_deep_parent_tree_validates_without_recursion(self):
        parent = None
        for i in range(1500):
            parent = self.symbol(str(i), parent)
        self.assertEqual(validate_snapshot(self.db), [])

    def test_utf8_byte_range_round_trip(self):
        source = "é😀foo()".encode("utf-8")
        start, end = self.db.execute(
            "SELECT start_column, end_column FROM locations WHERE location_id=1"
        ).fetchone()
        self.assertEqual(source[start:end].decode("utf-8"), "foo")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE locations SET end_column=5 WHERE location_id=1")

    def test_full_source_ranges_do_not_require_metrics(self):
        symbol = self.symbol()
        for role in ("declaration", "definition", "body"):
            self.db.execute(
                "INSERT INTO symbol_source_ranges VALUES (?, 1, ?)", (symbol, role)
            )
        self.assertEqual(validate_snapshot(self.db), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO symbol_source_ranges VALUES (?, 1, 'body')",
                            (symbol,))

    def test_metrics_require_algorithm_version_and_registered_body(self):
        symbol = self.symbol()
        self.metrics(symbol)
        issues = validate_snapshot(self.db)
        self.assertTrue(any("metrics_version" in issue for issue in issues))
        self.assertTrue(any("body source range" in issue for issue in issues))
        self.db.execute("UPDATE index_metadata SET metrics_version='body-v1'")
        self.db.execute("INSERT INTO symbol_source_ranges VALUES (?, 1, 'body')", (symbol,))
        self.assertEqual(validate_snapshot(self.db), [])

    def test_shared_body_range_has_one_derived_line_count(self):
        self.db.execute("UPDATE locations SET end_line=9, end_column=1 WHERE location_id=1")
        for name in ("f", "g"):
            symbol = self.symbol(name)
            self.metrics(symbol)
            self.db.execute("INSERT INTO symbol_source_ranges VALUES (?, 1, 'body')", (symbol,))
        self.db.execute("UPDATE index_metadata SET metrics_version='body-v1'")
        self.assertEqual(self.db.execute(
            "SELECT line_count FROM symbol_body_metrics ORDER BY symbol_id"
        ).fetchall(), [(10,), (10,)])
        with self.assertRaises(sqlite3.OperationalError):
            self.db.execute("UPDATE symbol_bodies SET line_count=99 WHERE symbol_id=2")
        self.db.execute("UPDATE locations SET end_line=19 WHERE location_id=1")
        self.assertEqual(self.db.execute(
            "SELECT line_count FROM symbol_body_metrics ORDER BY symbol_id"
        ).fetchall(), [(20,), (20,)])
        self.assertEqual(validate_snapshot(self.db), [])

    def test_derived_line_count_obeys_exclusive_end_and_empty_ranges(self):
        symbol = self.symbol()
        self.metrics(symbol)
        self.db.execute("INSERT INTO symbol_source_ranges VALUES (?, 1, 'body')", (symbol,))
        self.db.execute("UPDATE index_metadata SET metrics_version='body-v1'")
        for end_line, end_column, expected in ((0, 9, 1), (1, 0, 1), (1, 1, 2), (0, 6, 0)):
            with self.subTest(end=(end_line, end_column)):
                self.db.execute("UPDATE locations SET end_line=?, end_column=? WHERE location_id=1",
                                (end_line, end_column))
                self.assertEqual(self.db.execute(
                    "SELECT line_count FROM symbol_body_metrics"
                ).fetchone(), (expected,))
                issues = validate_snapshot(self.db)
                if expected == 0:
                    self.assertTrue(any("empty body" in issue for issue in issues))
                else:
                    self.assertEqual(issues, [])

    def test_symbol_derived_fields_are_not_writable(self):
        self.symbol()
        for field, value in (("scope", "'other::'"), ("parent_symbol_id", "1"),
                             ("resolution_state", "'placeholder'")):
            with self.subTest(field=field), self.assertRaises(sqlite3.OperationalError):
                self.db.execute(f"UPDATE symbols SET {field}={value}")

    def test_source_hash_can_be_unknown_but_not_malformed(self):
        for value in (None, DIGEST):
            self.db.execute("UPDATE files SET content_hash=?", (value,))
        for value in ("", "abc", "z" * 64, "A" * 64):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("UPDATE files SET content_hash=?", (value,))

    def test_metadata_singleton_and_completion_requirements(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO index_metadata(project_root) VALUES ('/other')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE index_metadata SET completed_at=NULL")
        self.db.execute("DELETE FROM index_metadata")
        self.assertIn("Exactly one index_metadata row is required", validate_snapshot(self.db))

    def test_incomplete_snapshot_cannot_be_published(self):
        self.db.execute("UPDATE index_metadata SET status='building', completed_at=NULL")
        self.assertTrue(any("not complete" in issue for issue in validate_snapshot(self.db)))

    def test_missing_collection_status_cannot_be_published(self):
        self.db.execute("DELETE FROM collection_status WHERE stage='references'")
        self.assertTrue(any("4/5" in issue for issue in validate_snapshot(self.db)))

    def test_failed_and_partial_collection_remain_visible(self):
        for status in ("partial", "failed"):
            with self.subTest(status=status):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute("UPDATE collection_status SET status=? "
                                    "WHERE stage='references'", (status,))
                self.db.execute("UPDATE collection_status SET status=?, error='missing source' "
                                "WHERE stage='references'", (status,))
                self.assertTrue(any("missing source" in issue
                                    for issue in validate_snapshot(self.db)))
                self.db.execute("UPDATE collection_status SET status='complete', error=NULL "
                                "WHERE stage='references'")
        self.assertEqual(validate_snapshot(self.db), [])

    def test_documentation_keeps_source_comment_and_provenance_separate(self):
        symbol = self.symbol()
        self.db.execute("UPDATE symbols SET documentation='Original comment'")
        self.document(symbol)
        self.assertEqual(self.db.execute("SELECT documentation FROM symbols").fetchone(),
                         ("Original comment",))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE generated_documentation SET source_input_hash=NULL")
        self.db.execute("UPDATE generated_documentation SET status='stale'")
        self.assertEqual(validate_snapshot(self.db), [])

    def test_complete_documentation_needs_a_resolved_symbol(self):
        symbol = self.db.execute("INSERT INTO symbols DEFAULT VALUES").lastrowid
        self.document(symbol)
        self.assertTrue(any("unresolved" in issue for issue in validate_snapshot(self.db)))

    def test_pending_and_failed_documentation_do_not_block_code_index(self):
        symbol = self.symbol()
        self.db.execute("INSERT INTO generated_documentation(symbol_id) VALUES (?)", (symbol,))
        self.assertEqual(validate_snapshot(self.db), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE generated_documentation SET status='failed'")
        self.db.execute("UPDATE generated_documentation SET status='failed', error='timeout'")
        self.assertEqual(validate_snapshot(self.db), [])

    def test_validator_detects_broken_foreign_keys_from_unchecked_writer(self):
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.reference(999)
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=ON")
        self.assertTrue(any("Foreign key" in issue for issue in validate_snapshot(self.db)))

    def test_unsupported_schema_version(self):
        self.db.execute("PRAGMA user_version=2")
        self.assertTrue(any("Unsupported schema" in issue for issue in validate_snapshot(self.db)))

    def test_cli_validates_read_only_and_never_creates_missing_database(self):
        self.db.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot #1.sqlite3"
            with closing(sqlite3.connect(path)) as destination:
                self.db.backup(destination)
            before = path.read_bytes()
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate_snapshot.py"), str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(path.read_bytes(), before)
            missing = Path(directory) / "missing.sqlite3"
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate_snapshot.py"), str(missing)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
