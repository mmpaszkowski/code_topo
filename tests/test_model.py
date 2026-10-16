from contextlib import closing
from dataclasses import FrozenInstanceError, fields, replace
import hashlib
from pathlib import Path
import sqlite3
import unittest

import index_model as m


STAMP = "2026-10-15T12:00:00Z"
HASH = hashlib.sha256(b"input").hexdigest()
SCHEMA = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()


def metrics(**changes):
    values = {f.name: (1 if f.name == "cyclomatic_complexity" else 0)
              for f in fields(m.BodyMetrics)}
    values.update(changes)
    return m.BodyMetrics(**values)


def builder(*, file=False, **metadata):
    b = m.IndexBuilder()
    b.set_metadata(m.IndexMetadata(project_root="/example", created_at=STAMP, **metadata))
    b.add_language(language_id=m.LanguageId(1), name="C++")
    if file:
        add_file(b, 1)
        b.add_location(location_id=m.LocationId(1), file_id=m.FileId(1),
                       start=m.Position(0, 0), end=m.Position(9, 1))
    return b


def add_file(b, key, **changes):
    b.add_file(file_id=m.FileId(key), uri=f"file:///example/{key}.cpp", **changes)
    for stage in m.CollectionStage:
        b.add_collection_status(file_id=m.FileId(key), stage=stage,
                                status=m.CollectionState.NOT_REQUESTED)


def symbol(b, key, *, scope=1, **changes):
    b.add_symbol(symbol_id=m.SymbolId(key), name=f"symbol{key}", scope_id=m.ScopeId(scope),
                 language_id=m.LanguageId(1), **changes)


class ModelTests(unittest.TestCase):
    def test_entire_schema_uses_canonical_nested_objects(self):
        b = builder(metrics_version="body-v1")
        b.add_library(library_id=m.LibraryId(1), name="core")
        add_file(b, 1, library_id=m.LibraryId(1), content_hash=HASH)
        add_file(b, 2)
        b.add_location(location_id=m.LocationId(1), file_id=m.FileId(1),
                       start=m.Position(0, 0), end=m.Position(1, 1))
        b.add_symbol_kind(kind_id=m.KindId(1), name="class")
        b.add_access_specifier(access_id=m.AccessId(1), name="public")
        for flag in m.ReferenceKind:
            b.add_reference_kind_bit(bit=int(flag), name=flag.name.lower())
        symbol(b, 1, kind_id=m.KindId(1))
        symbol(b, 2, kind_id=m.KindId(1))
        b.add_scope(scope_id=m.ScopeId(2), qualified_name="Base::", owner_symbol_id=m.SymbolId(1))
        b.add_scope(scope_id=m.ScopeId(3), qualified_name="Derived::", owner_symbol_id=m.SymbolId(2))
        symbol(b, 3, scope=2)
        symbol(b, 4, scope=3)
        b.add_file_include(include_id=m.IncludeId(1), includer_file_id=m.FileId(1), included_file_id=m.FileId(2))
        b.add_class_inheritance(inheritance_id=m.InheritanceId(1), base_symbol_id=m.SymbolId(1),
                                derived_symbol_id=m.SymbolId(2), access_id=m.AccessId(1), is_virtual=True)
        b.add_method_override(override_id=m.OverrideId(1), overridden_symbol_id=m.SymbolId(3),
                              overriding_symbol_id=m.SymbolId(4))
        b.add_symbol_reference(reference_id=m.ReferenceId(1), target_symbol_id=m.SymbolId(3),
                               container_symbol_id=m.SymbolId(4), location_id=m.LocationId(1),
                               kind=m.ReferenceKind.REFERENCE | m.ReferenceKind.CALL)
        b.add_symbol_source_range(symbol_id=m.SymbolId(4), location_id=m.LocationId(1), role=m.SourceRangeRole.BODY)
        b.add_symbol_body(symbol_id=m.SymbolId(4), body_location_id=m.LocationId(1), metrics=metrics())
        b.add_generated_documentation(symbol_id=m.SymbolId(4), comment="Does work.",
            status=m.DocumentationStatus.COMPLETE, model="example", prompt_version="v1",
            source_input_hash=HASH, dependency_context_hash=HASH, created_at=STAMP, completed_at=STAMP)
        index = b.build()
        self.assertIs(index.location(1).file.library, index.library(1))
        self.assertIs(index.symbol(1).kind, index.kind(1))
        self.assertIs(index.symbol(1).language, index.language(1))
        self.assertIs(index.symbol(4).parent, index.symbol(2))
        self.assertIs(index.reference(1).target, index.symbol(3))
        self.assertIs(index.reference(1).container, index.symbol(4))
        self.assertIs(index.include(1).included, index.file(2))
        self.assertIs(index.inheritance(1).access, index.access_specifier(1))
        self.assertIs(index.override(1).overridden, index.symbol(3))
        self.assertIs(index.body(4).body, index.location(1))
        self.assertIs(index.source_range(4, 1, m.SourceRangeRole.BODY).symbol, index.symbol(4))
        self.assertIs(index.documentation(4).symbol, index.symbol(4))
        self.assertIs(index.collection_status(1, m.CollectionStage.SYMBOLS).file, index.file(1))
        self.assertEqual(index.reference_kind_bit(16).name, "call")
        self.assertEqual(index.body(4).line_count, 2)
        self.assertEqual(m.validate(index), ())

    def test_forward_references_resolve_to_final_symbol(self):
        b = builder(file=True)
        b.add_symbol_reference(reference_id=1, target_symbol_id=10, location_id=1, kind=m.ReferenceKind.CALL)
        b.add_symbol(symbol_id=10)
        b.resolve_symbol(symbol_id=10, name="resolved", scope_id=1, language_id=1)
        index = b.build()
        self.assertIs(index.reference(1).target, index.symbol(10))
        self.assertEqual(index.symbol(10).name, "resolved")
        self.assertEqual(index.symbol(10).resolution_state, m.ResolutionState.RESOLVED)
        with self.assertRaises(m.ModelValidationError):
            b.resolve_symbol(symbol_id=10, name="again", scope_id=1, language_id=1)

    def test_builder_reuse_cannot_change_old_snapshot(self):
        b = builder(file=True)
        b.add_symbol(symbol_id=10)
        b.add_symbol_reference(reference_id=1, target_symbol_id=10, location_id=1, kind=m.ReferenceKind.REFERENCE)
        first = b.build()
        b.resolve_symbol(symbol_id=10, name="f", scope_id=1, language_id=1)
        symbol(b, 20)
        second = b.build()
        self.assertIsNone(first.reference(1).target.name)
        self.assertEqual(second.reference(1).target.name, "f")
        self.assertIsNot(first.symbol(10), second.symbol(10))
        self.assertNotEqual(first.symbol(10), second.symbol(10))
        with self.assertRaises(KeyError):
            first.symbol(20)

    def test_immutable_collections_and_nested_records(self):
        b = builder(file=True)
        symbol(b, 1)
        index = b.build()
        with self.assertRaises(FrozenInstanceError):
            index.symbol(1).name = "changed"
        with self.assertRaises(FrozenInstanceError):
            index.location(1).start.line = 9
        with self.assertRaises(FrozenInstanceError):
            index.symbols = ()
        with self.assertRaises(TypeError):
            index._indices["symbols"][1] = None
        self.assertIsInstance(index.symbols, tuple)
        self.assertEqual(m.Position(1, 2), m.Position(1, 2))
        self.assertEqual(metrics(), metrics())

    def test_canonical_membership_checks_manually_constructed_graph(self):
        b = builder()
        symbol(b, 1)
        index = b.build()
        other_scope = m.Scope(scope_id=1, qualified_name="")
        with self.assertRaises(m.ModelValidationError) as caught:
            replace(index, symbols=(replace(index.symbol(1), scope=other_scope),))
        self.assertTrue(any("canonical" in issue.message for issue in caught.exception.issues))

    def test_direct_snapshot_copies_caller_collections(self):
        scope = m.Scope(scope_id=1, qualified_name="")
        supplied = [scope]
        index = m.IndexModel(metadata=m.IndexMetadata(project_root="/example"), scopes=supplied)
        supplied.clear()
        self.assertEqual(index.scopes, (scope,))

    def test_malformed_collection_and_resolution_id_have_structured_errors(self):
        with self.assertRaises(m.ModelValidationError) as caught:
            m.IndexModel(metadata=m.IndexMetadata(project_root="/example"), symbols=None)
        self.assertEqual(caught.exception.issues[0].field, "symbols")
        with self.assertRaises(m.ModelValidationError) as caught:
            builder().resolve_symbol(symbol_id=[], name="f", scope_id=1, language_id=1)
        self.assertEqual(caught.exception.issues[0].field, "symbol_id")

    def test_bad_record_errors_do_not_escape_as_missing_dependency_errors(self):
        b = builder()
        b.add_language(language_id=2, name="")
        b.add_symbol(symbol_id=1, name="f", scope_id=1, language_id=2)
        with self.assertRaises(m.ModelValidationError) as caught:
            b.build()
        self.assertTrue(any(issue.field == "name" for issue in caught.exception.issues))
        self.assertTrue(any("invalid record" in issue.message for issue in caught.exception.issues))

    def test_missing_ids_are_not_invented(self):
        b = builder()
        b.add_symbol_reference(reference_id=1, target_symbol_id=999, location_id=999, kind=m.ReferenceKind.CALL)
        with self.assertRaises(m.ModelValidationError) as caught:
            b.build()
        self.assertEqual({issue.field for issue in caught.exception.issues}, {"target", "location"})

    def test_metadata_required_and_cannot_be_overwritten(self):
        with self.assertRaises(m.ModelValidationError):
            m.IndexBuilder().build()
        b = builder()
        with self.assertRaises(m.ModelValidationError):
            b.set_metadata(m.IndexMetadata(project_root="/other"))

    def test_duplicate_ids_and_composite_keys_rejected(self):
        b = builder(file=True)
        symbol(b, 1)
        for operation in (
            lambda: symbol(b, 1),
            lambda: b.add_scope(scope_id=1, qualified_name=""),
            lambda: b.add_collection_status(file_id=1, stage=m.CollectionStage.SYMBOLS,
                                            status=m.CollectionState.COMPLETE),
        ):
            with self.subTest(operation=operation), self.assertRaises(m.ModelValidationError):
                operation()
        b.add_symbol_source_range(symbol_id=1, location_id=1, role=m.SourceRangeRole.DEFINITION)
        with self.assertRaises(m.ModelValidationError):
            b.add_symbol_source_range(symbol_id=1, location_id=1, role=m.SourceRangeRole.DEFINITION)

    def test_duplicate_reference_observations_rejected_including_null_container(self):
        for container in (None, 1):
            b = builder(file=True)
            symbol(b, 1)
            for key in (1, 2):
                b.add_symbol_reference(reference_id=key, target_symbol_id=1, location_id=1,
                    kind=m.ReferenceKind.CALL, container_symbol_id=container)
            with self.subTest(container=container), self.assertRaises(m.ModelValidationError):
                b.build()

    def test_duplicate_natural_keys_rejected(self):
        operations = (
            lambda b: b.add_language(language_id=2, name="C++"),
            lambda b: b.add_file(file_id=2, uri="file:///example/1.cpp"),
            lambda b: b.add_location(location_id=2, file_id=1, start=m.Position(0, 0), end=m.Position(9, 1)),
        )
        for operation in operations:
            b = builder(file=True)
            operation(b)
            with self.subTest(operation=operation), self.assertRaises(m.ModelValidationError) as caught:
                b.build()
            self.assertTrue(any("duplicate" in issue.message for issue in caught.exception.issues))

    def test_repeated_symbol_names_and_scope_names_are_allowed(self):
        b = builder()
        for key in (2, 3):
            b.add_scope(scope_id=key, qualified_name="anonymous::")
            b.add_symbol(symbol_id=key, name="f", scope_id=key, language_id=1)
        index = b.build()
        self.assertIsNot(index.symbol(2).scope, index.symbol(3).scope)

    def test_unknown_owner_is_not_global_scope(self):
        b = builder()
        b.add_scope(scope_id=2, qualified_name="unknown::")
        symbol(b, 1, scope=2)
        b.add_symbol(symbol_id=2)
        index = b.build()
        self.assertIsNone(index.symbol(1).parent)
        self.assertEqual(index.symbol(1).scope_name, "unknown::")
        self.assertIsNone(index.symbol(2).scope)
        self.assertEqual(index.scope(1).qualified_name, "")

    def test_known_owner_may_be_explicit_placeholder(self):
        b = builder()
        b.add_scope(scope_id=2, qualified_name="external::", owner_symbol_id=9)
        b.add_symbol(symbol_id=9)
        symbol(b, 1, scope=2)
        index = b.build()
        self.assertIs(index.symbol(1).parent, index.symbol(9))

    def test_duplicate_scope_owners_rejected(self):
        b = builder()
        symbol(b, 1)
        for key in (2, 3):
            b.add_scope(scope_id=key, qualified_name="ns::", owner_symbol_id=1)
        with self.assertRaises(m.ModelValidationError):
            b.build()

    def test_self_and_longer_scope_cycles_rejected(self):
        for count in (1, 3):
            b = builder()
            for key in range(1, count + 1):
                b.add_scope(scope_id=key + 1, qualified_name=f"scope{key}::", owner_symbol_id=key)
                symbol(b, key, scope=(key % count) + 2)
            with self.subTest(count=count), self.assertRaises(m.ModelValidationError) as caught:
                b.build()
            self.assertTrue(any("cycle" in issue.message for issue in caught.exception.issues))

    def test_deep_hierarchy_construction_and_repr_do_not_recurse(self):
        b = builder()
        for key in range(1500, 0, -1):
            b.add_scope(scope_id=key + 1, qualified_name=f"scope{key}::", owner_symbol_id=key)
            symbol(b, key, scope=key)
        index = b.build()
        self.assertIs(index.symbol(1500).parent, index.symbol(1499))
        self.assertIn("1500", repr(index.symbol(1500)))

    def test_recursive_and_mutually_recursive_calls_are_valid(self):
        b = builder(file=True)
        symbol(b, 1)
        symbol(b, 2)
        for key, (caller, callee) in enumerate(((1, 1), (1, 2), (2, 1)), 1):
            b.add_symbol_reference(reference_id=key, target_symbol_id=callee, container_symbol_id=caller,
                                    location_id=1, kind=m.ReferenceKind.CALL)
        self.assertEqual(len(b.build().symbol_references), 3)

    def test_self_inheritance_and_override_are_rejected(self):
        for relation in ("inheritance", "override"):
            b = builder()
            symbol(b, 1)
            if relation == "inheritance":
                b.add_class_inheritance(inheritance_id=1, base_symbol_id=1, derived_symbol_id=1)
            else:
                b.add_method_override(override_id=1, overridden_symbol_id=1, overriding_symbol_id=1)
            with self.subTest(relation=relation), self.assertRaises(m.ModelValidationError):
                b.build()

    def test_duplicate_edges_are_rejected_even_with_distinct_ids(self):
        for relation in ("include", "inheritance", "override"):
            b = builder(file=True)
            symbol(b, 1)
            symbol(b, 2)
            for key in (1, 2):
                if relation == "include":
                    b.add_file_include(include_id=key, includer_file_id=1, included_file_id=1)
                elif relation == "inheritance":
                    b.add_class_inheritance(inheritance_id=key, base_symbol_id=1, derived_symbol_id=2)
                else:
                    b.add_method_override(override_id=key, overridden_symbol_id=1, overriding_symbol_id=2)
            with self.subTest(relation=relation), self.assertRaises(m.ModelValidationError):
                b.build()

    def test_collection_errors_are_data_not_structural_errors(self):
        b = builder(status=m.SnapshotStatus.FAILED, error="analysis failed")
        b.add_file(file_id=1, uri="file:///example.cpp")
        for stage in m.CollectionStage:
            b.add_collection_status(file_id=1, stage=stage, status=m.CollectionState.PARTIAL,
                                    error="source unavailable")
        self.assertEqual(m.validate(b.build()), ())

    def test_missing_collection_statuses_are_not_filled_in(self):
        b = builder()
        b.add_file(file_id=1, uri="file:///example.cpp")
        with self.assertRaises(m.ModelValidationError) as caught:
            b.build()
        self.assertEqual(len(caught.exception.issues), len(m.CollectionStage))

    def test_body_requires_metrics_version_and_matching_range(self):
        b = builder(file=True)
        symbol(b, 1)
        b.add_symbol_body(symbol_id=1, body_location_id=1, metrics=metrics())
        with self.assertRaises(m.ModelValidationError) as caught:
            b.build()
        self.assertEqual({i.field for i in caught.exception.issues}, {"metrics_version", "body"})

    def test_empty_body_rejected_but_body_without_metrics_allowed(self):
        for end, accepted in ((m.Position(0, 0), False), (m.Position(0, 1), True)):
            b = builder(file=True)
            symbol(b, 1)
            b.add_location(location_id=2, file_id=1, start=m.Position(0, 0), end=end)
            b.add_symbol_source_range(symbol_id=1, location_id=2, role=m.SourceRangeRole.BODY)
            if accepted:
                self.assertEqual(b.build().symbol_bodies, ())
            else:
                with self.assertRaises(m.ModelValidationError):
                    b.build()

    def test_missing_document_provenance_and_unresolved_completion_rejected(self):
        for resolved in (True, False):
            b = builder()
            if resolved:
                symbol(b, 1)
            else:
                b.add_symbol(symbol_id=1)
            b.add_generated_documentation(symbol_id=1, comment="comment", status=m.DocumentationStatus.COMPLETE,
                model="example", prompt_version="v1", source_input_hash=HASH,
                dependency_context_hash=HASH if not resolved else None, completed_at=STAMP)
            with self.subTest(resolved=resolved), self.assertRaises(m.ModelValidationError):
                b.build()

    def test_local_errors_are_structured_and_bool_is_not_an_id(self):
        operations = (
            lambda: m.Position(-1, 0),
            lambda: m.Position(0, True),
            lambda: m.Library(library_id=True, name="lib"),
            lambda: m.File(file_id=1, uri="file:///a", content_hash="bad"),
            lambda: metrics(case_count=1),
            lambda: metrics(catch_count=1),
            lambda: metrics(max_nesting_depth=1),
            lambda: metrics(cyclomatic_complexity=0),
            lambda: m.Symbol(symbol_id=1, name="f"),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(m.ModelValidationError) as caught:
                operation()
            issue = caught.exception.issues[0]
            self.assertTrue(issue.entity and issue.field and issue.message)

    def test_invalid_flag_masks_rejected(self):
        for flag in (m.ReferenceKind(0), m.ReferenceKind(32), 16):
            b = builder(file=True)
            symbol(b, 1)
            b.add_symbol_reference(reference_id=1, target_symbol_id=1, location_id=1, kind=flag)
            with self.subTest(flag=flag), self.assertRaises(m.ModelValidationError):
                b.build()

    def test_insertion_order_does_not_change_facts(self):
        operations = (
            lambda b: symbol(b, 1),
            lambda b: b.add_scope(scope_id=2, qualified_name="owner::", owner_symbol_id=1),
            lambda b: symbol(b, 2, scope=2),
            lambda b: b.add_symbol_reference(reference_id=1, target_symbol_id=1, container_symbol_id=2,
                location_id=1, kind=m.ReferenceKind.CALL),
        )
        models = []
        for ordered in (operations, tuple(reversed(operations))):
            b = builder(file=True)
            for operation in ordered:
                operation(b)
            models.append(b.build())
        def facts(index):
            return ([(s.symbol_id, s.name, s.scope_name, s.parent.symbol_id if s.parent else None)
                     for s in index.symbols],
                    [(r.target.symbol_id, r.container.symbol_id, int(r.kind)) for r in index.symbol_references])
        self.assertEqual(facts(models[0]), facts(models[1]))


class SchemaAgreementTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO languages(name) VALUES ('C++')")
        self.db.execute("INSERT INTO files(uri) VALUES ('file:///example.cpp')")

    def test_position_ranges_and_hashes_agree_with_schema(self):
        file = m.File(file_id=1, uri="file:///example.cpp")
        for start_line, start_col, end_line, end_col in ((0, 0, 0, 1), (0, 3, 1, 0), (0, 2, 0, 2),
                                                        (2, 0, 1, 0), (0, 4, 0, 3), (-1, 0, 0, 1)):
            args = (start_line, start_col, end_line, end_col)
            with self.subTest(range=args):
                self.assert_agreement(
                    lambda: m.Location(location_id=1, file=file, start=m.Position(start_line, start_col),
                                       end=m.Position(end_line, end_col)),
                    "INSERT INTO locations VALUES (1,1,?,?,?,?)", args)
        for value in (None, HASH, "", "z" * 64, "A" * 64):
            with self.subTest(hash=value):
                self.assert_agreement(lambda: m.File(file_id=1, uri="file:///example.cpp", content_hash=value),
                                      "UPDATE files SET content_hash=? WHERE file_id=1", (value,))

    def assert_agreement(self, construct, sql, parameters):
        try:
            construct()
            model_accepts = True
        except m.ModelValidationError:
            model_accepts = False
        self.db.execute("SAVEPOINT test")
        try:
            self.db.execute(sql, parameters)
            sql_accepts = True
        except sqlite3.IntegrityError:
            sql_accepts = False
        finally:
            self.db.execute("ROLLBACK TO test")
            self.db.execute("RELEASE test")
        self.assertEqual(model_accepts, sql_accepts)

    def test_fixed_vocabulary_matches_schema(self):
        self.assertEqual([(int(flag), flag.name.lower()) for flag in m.ReferenceKind],
                         self.db.execute("SELECT bit,name FROM reference_kind_bits ORDER BY bit").fetchall())
        self.assertEqual(m.SCHEMA_VERSION, self.db.execute("PRAGMA user_version").fetchone()[0])

    def test_symbol_resolution_constraints_agree_with_schema(self):
        scope = m.Scope(scope_id=1, qualified_name="")
        language = m.Language(language_id=1, name="C++")
        for name, owner, lang in ((None, None, None), ("f", scope, language),
                                  ("f", None, language), (None, scope, language),
                                  ("f", scope, None)):
            with self.subTest(name=name, scope=owner, language=lang):
                self.assert_agreement(lambda: m.Symbol(symbol_id=1, name=name, scope=owner, language=lang),
                    "INSERT INTO symbols(symbol_id,name,scope_id,language_id) VALUES (1,?,?,?)",
                    (name, owner.scope_id if owner else None, lang.language_id if lang else None))

    def test_collection_status_constraints_agree_with_schema(self):
        file = m.File(file_id=1, uri="file:///example.cpp")
        for state in m.CollectionState:
            for error in (None, "", "reason"):
                with self.subTest(state=state, error=error):
                    self.assert_agreement(lambda: m.CollectionStatus(file=file, stage=m.CollectionStage.REFERENCES,
                        status=state, error=error), "INSERT INTO collection_status VALUES (1,'references',?,?)",
                        (state.value, error))

    def test_snapshot_status_constraints_agree_with_schema(self):
        for status in m.SnapshotStatus:
            for completed, error in ((None, None), (STAMP, None), (None, "reason"), (STAMP, "reason")):
                with self.subTest(status=status, completed=completed, error=error):
                    self.assert_agreement(lambda: m.IndexMetadata(project_root="/example", status=status,
                        completed_at=completed, error=error),
                        "INSERT INTO index_metadata(project_root,status,completed_at,error) VALUES ('/example',?,?,?)",
                        (status.value, completed, error))

    def test_metric_constraints_agree_with_schema(self):
        self.db.execute("INSERT INTO symbols(symbol_id) VALUES (1)")
        self.db.execute("INSERT INTO locations VALUES (1,1,0,0,0,1)")
        base = metrics()
        for changes in ({}, {"case_count": 1}, {"switch_count": 1, "case_count": 1},
                        {"catch_count": 1}, {"try_count": 1, "catch_count": 1},
                        {"max_nesting_depth": 1}, {"max_nesting_depth": 1, "if_count": 1},
                        {"cyclomatic_complexity": 0}, {"statement_count": -1}):
            values = {f.name: getattr(base, f.name) for f in fields(base)}
            values.update(changes)
            with self.subTest(changes=changes):
                self.assert_agreement(lambda: m.BodyMetrics(**values),
                    f"INSERT INTO symbol_bodies(symbol_id,body_location_id,{','.join(values)}) "
                    f"VALUES (1,1,{','.join('?' for _ in values)})", tuple(values.values()))

    def test_symbol_properties_match_sql_view(self):
        b = builder()
        symbol(b, 1)
        b.add_scope(scope_id=2, qualified_name="ns::", owner_symbol_id=1)
        symbol(b, 2, scope=2)
        b.add_symbol(symbol_id=3)
        index = b.build()
        self.db.execute("INSERT INTO symbols(symbol_id,name,scope_id,language_id) VALUES (1,'symbol1',1,1)")
        self.db.execute("INSERT INTO scopes VALUES (2,'ns::',1)")
        self.db.execute("INSERT INTO symbols(symbol_id,name,scope_id,language_id) VALUES (2,'symbol2',2,1)")
        self.db.execute("INSERT INTO symbols(symbol_id) VALUES (3)")
        expected = self.db.execute(
            "SELECT symbol_id,scope,parent_symbol_id,resolution_state FROM symbol_details ORDER BY symbol_id"
        ).fetchall()
        self.assertEqual([(s.symbol_id, s.scope_name, s.parent.symbol_id if s.parent else None,
                           s.resolution_state.value) for s in index.symbols], expected)

    def test_line_count_matches_sql_view_including_boundary_cases(self):
        file = m.File(file_id=1, uri="file:///example.cpp")
        symbol = m.Symbol(symbol_id=1)
        self.db.execute("INSERT INTO symbols(symbol_id) VALUES (1)")
        self.db.execute("INSERT INTO locations VALUES (1,1,0,6,0,9)")
        counts = metrics()
        names = [f.name for f in fields(counts)]
        self.db.execute(f"INSERT INTO symbol_bodies(symbol_id,body_location_id,{','.join(names)}) "
                        f"VALUES (1,1,{','.join('?' for _ in names)})",
                        [getattr(counts, name) for name in names])
        for end in (m.Position(0, 6), m.Position(0, 9), m.Position(1, 0), m.Position(9, 1)):
            self.db.execute("UPDATE locations SET end_line=?,end_column=? WHERE location_id=1", (end.line, end.column))
            body = m.SymbolBody(symbol=symbol, metrics=counts,
                               body=m.Location(location_id=1, file=file, start=m.Position(0, 6), end=end))
            self.assertEqual(body.line_count, self.db.execute("SELECT line_count FROM symbol_body_metrics").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
