from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import unittest
from unittest.mock import Mock

from test_clang_ast import Fixture, EXE
from clang_ast import Extractor
from clang_ast.records import Issue
from clang_ast.step import symbol_key
from index_model import IndexBuilder, IndexMetadata, CollectionStage, CollectionState
from snapshot_steps import PrepareSnapshotStep, FinalizeSnapshotStep, RunInputs
from pipeline_config import PipelineConfig, ClangdYamlStepConfig
from sqlite_model_store import SQLiteModelStore


class LifecycleTests(Fixture):
    def inputs(self):
        export = self.root / 'index.yaml'
        export.write_text('test export\n')
        return RunInputs(PipelineConfig(self.root, (ClangdYamlStepConfig(export),)))

    def test_vocabulary_and_completion_preserve_partial_coverage(self):
        model, _ = self.model()
        b = IndexBuilder.from_model(model)
        b.update_collection_status(file_id=1, stage=CollectionStage.METRICS,
                                   status=CollectionState.PARTIAL, error='unresolved body')
        inputs = self.inputs()
        prepared = PrepareSnapshotStep(inputs).run(b.build())
        self.assertEqual(len(prepared.reference_kind_bits), 5)
        self.assertEqual(len(prepared.access_specifiers), 3)
        final = FinalizeSnapshotStep(inputs).run(prepared)
        self.assertEqual(final.metadata.status.value, 'complete')
        self.assertTrue(final.metadata.completed_at)
        self.assertEqual(len(final.metadata.build_input_hash), 64)
        self.assertEqual(final.collection_status(1, CollectionStage.METRICS).status, CollectionState.PARTIAL)
        self.assertEqual(model.metadata.status.value, 'building')
        db = self.root / 'test.sqlite3'
        SQLiteModelStore(db).save(final)
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute('select count(*) from reference_kind_bits').fetchone(), (5,))
            self.assertEqual(connection.execute('select count(*) from access_specifiers').fetchone(), (3,))

    def test_input_fingerprint_is_stable_and_changed_export_blocks_finalization(self):
        model, _ = self.model()
        inputs = self.inputs()
        prepared = PrepareSnapshotStep(inputs).run(model)
        self.assertEqual(inputs.fingerprint(), inputs.initial)
        (self.root / 'index.yaml').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            FinalizeSnapshotStep(inputs).run(prepared)

    def test_conflicting_vocabulary_is_rejected(self):
        model, _ = self.model()
        b = IndexBuilder.from_model(model)
        b.add_access_specifier(access_id=1, name='unexpected')
        with self.assertRaisesRegex(ValueError, 'vocabulary'):
            PrepareSnapshotStep(self.inputs()).run(b.build())

    def test_preparation_seed_does_not_replace_previous_database(self):
        from snapshot_steps import CollectorCheckpointStore
        model,_=self.model()
        db=self.root/'previous.sqlite3'
        store=SQLiteModelStore(db)
        store.save(model)
        original=db.read_bytes()
        preparation=PrepareSnapshotStep(self.inputs())
        builder=IndexBuilder()
        builder.set_metadata(IndexMetadata(project_root=str(self.root)))
        seed=preparation.run(builder.build())
        filtered=CollectorCheckpointStore(store,preparation)
        filtered.save(seed)
        self.assertEqual(db.read_bytes(),original)
        final=FinalizeSnapshotStep(preparation.inputs).run(model)
        filtered.save(final)
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute('select status from index_metadata').fetchone(),('complete',))

    def test_response_file_content_changes_fingerprint(self):
        from pipeline_config import ClangAstStepConfig
        if not EXE:
            self.skipTest('native extractor required')
        response = self.root / 'flags.rsp'
        response.write_text('-DONE=1')
        self.write_commands([{**self.command, 'arguments': self.command['arguments'] + ['@flags.rsp']}])
        inputs = RunInputs(PipelineConfig(self.root, (ClangAstStepConfig(self.root, EXE),)))
        before = inputs.fingerprint()
        response.write_text('-DONE=2')
        self.assertNotEqual(inputs.fingerprint(), before)


@unittest.skipUnless(EXE, 'build the native extractor and set INDEXER_AST_EXECUTABLE')
class StructuralTests(Fixture):
    def seed_inheritance(self, raw):
        model, ids = self.seed(raw)
        b = IndexBuilder.from_model(model)
        for number, edge in enumerate(raw.inheritance, 101):
            b.add_class_inheritance(inheritance_id=number,
                base_symbol_id=ids[symbol_key(edge.base_usr)],
                derived_symbol_id=ids[symbol_key(edge.derived_usr)])
        return b.build(), ids

    def test_template_inheritance_attributes_fill_existing_edges_and_repeat_safely(self):
        raw = self.extract('template<class T> struct Base {};\n'
            'template<class T> struct Public : Base<T> {};\n'
            'template<class T> class Private : Base<T> {};\n'
            'template<class T> struct Protected : protected virtual Base<T> {};\n'
            'template<class T> struct Virtual : virtual public Base<T> {};\n')
        self.assertTrue(raw.ok)
        self.assertEqual(len(raw.inheritance), 4)
        self.assertTrue(all(e.existing_edge_only for e in raw.inheritance))
        model, ids = self.seed_inheritance(raw)
        step = self.step(ids)
        result = step.run(model)
        expected = {'Public': ('public', False), 'Private': ('private', False),
                    'Protected': ('protected', True), 'Virtual': ('public', True)}
        self.assertEqual({e.derived.name: (e.access.name, e.is_virtual)
                          for e in result.class_inheritance}, expected)
        self.assertEqual([e.inheritance_id for e in result.class_inheritance],
                         [e.inheritance_id for e in model.class_inheritance])
        self.assertEqual(step.report.inheritance_enriched, 4)
        self.assertTrue(all(e.access is None and e.is_virtual is None for e in model.class_inheritance))
        again = step.run(result)
        self.assertEqual([(e.inheritance_id, e.base.symbol_id, e.derived.symbol_id, e.access.name, e.is_virtual)
                          for e in again.class_inheritance],
                         [(e.inheritance_id, e.base.symbol_id, e.derived.symbol_id, e.access.name, e.is_virtual)
                          for e in result.class_inheritance])
        self.assertEqual(step.report.inheritance_enriched, 0)
        db = self.root / 'template-inheritance.sqlite3'
        SQLiteModelStore(db).save(result)
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute('pragma integrity_check').fetchone(), ('ok',))
            self.assertEqual(c.execute('pragma foreign_key_check').fetchall(), [])
            self.assertEqual(c.execute('select count(*) from class_inheritance where access_id is not null '
                                       'and is_virtual is not null').fetchone(), (4,))

    def test_template_fallback_never_creates_edge_or_guesses_specialization(self):
        raw = self.extract('template<class T> struct Base {};\n'
            'template<> struct Base<int> {};\n'
            'template<class T> struct Derived : Base<T> {};\n'
            'struct Concrete : Base<int> {};\n')
        self.assertEqual(len(raw.inheritance), 2)
        fallback = next(e for e in raw.inheritance if e.existing_edge_only)
        concrete = next(e for e in raw.inheritance if not e.existing_edge_only)
        self.assertNotEqual(fallback.base_usr, concrete.base_usr)
        model, ids = self.seed(raw)
        step = self.step(ids)
        result = step.run(model)
        self.assertEqual(len(result.class_inheritance), 1)
        self.assertEqual(result.class_inheritance[0].derived.name, 'Concrete')
        self.assertTrue(any('no existing inheritance edge' in d.message for d in step.report.diagnostics))
        # A same-named specialization is not the template-pattern endpoint.
        b = IndexBuilder.from_model(model)
        b.add_class_inheritance(inheritance_id=77,
            base_symbol_id=ids[symbol_key(concrete.base_usr)],
            derived_symbol_id=ids[symbol_key(fallback.derived_usr)])
        result = step.run(b.build())
        self.assertEqual(len(result.class_inheritance), 2)
        untouched = next(e for e in result.class_inheritance if e.inheritance_id == 77)
        self.assertIsNone(untouched.access)
        self.assertIsNone(untouched.is_virtual)

    def test_unresolved_template_parameters_and_template_names_stay_unknown(self):
        raw = self.extract('template<class T> struct Direct : T {};\n'
            'template<template<class> class B, class T> struct Indirect : B<T> {};\n')
        self.assertTrue(raw.ok)
        self.assertFalse(raw.inheritance)
        self.assertEqual(sum(i.message == 'dependent base unresolved' for i in raw.issues), 2)

    def test_template_inheritance_missing_identity_and_conflicts_preserve_unknowns(self):
        raw = self.extract('template<class T> struct Base {};\n'
                           'template<class T> struct Derived : Base<T> {};\n')
        model, ids = self.seed_inheritance(raw)
        edge, = raw.inheritance
        missing = {key: value for key, value in ids.items() if key != symbol_key(edge.base_usr)}
        step = self.step(missing)
        result = step.run(model)
        self.assertIsNone(result.class_inheritance[0].access)
        self.assertTrue(any('Unresolved relation endpoint' in d.message for d in step.report.diagnostics))
        self.write_commands([self.command, {**self.command, 'arguments': self.command['arguments'] + ['-DALT']}])
        second = replace(raw, inheritance=(replace(edge, access='private', virtual=True),))
        step = self.step(ids, Mock(check=Mock(), run=Mock(side_effect=[raw, second])))
        result = step.run(model)
        self.assertIsNone(result.class_inheritance[0].access)
        self.assertIsNone(result.class_inheritance[0].is_virtual)
        self.assertTrue(any('Conflicting inheritance attributes' in d.message for d in step.report.diagnostics))

    def test_template_inheritance_preserves_known_values(self):
        raw = self.extract('template<class T> struct Base {};\n'
                           'template<class T> struct Derived : Base<T> {};\n')
        model, ids = self.seed_inheritance(raw)
        filled = self.step(ids).run(model)
        conflicting = replace(raw, inheritance=tuple(replace(e, access='private', virtual=True)
                                                     for e in raw.inheritance))
        step = self.step(ids, self.fake(conflicting))
        result = step.run(filled)
        edge, = result.class_inheritance
        self.assertEqual((edge.inheritance_id, edge.access.name, edge.is_virtual), (101, 'public', False))
        self.assertEqual(step.report.conflicts, 2)

    def test_template_inheritance_respects_excluded_derived_files(self):
        dep = self.root / 'dep.hpp'
        dep.write_text('template<class T> struct Base {};\n'
                       'template<class T> struct Dependency : Base<T> {};\n')
        raw = self.extract('#include "dep.hpp"\ntemplate<class T> struct Project : Base<T> {};\n',
                           [self.path, dep])
        model, ids = self.seed(raw, paths=[self.path, dep])
        b = IndexBuilder.from_model(model)
        for number, edge in enumerate(raw.inheritance, 1):
            b.add_class_inheritance(inheritance_id=number,
                base_symbol_id=ids[symbol_key(edge.base_usr)], derived_symbol_id=ids[symbol_key(edge.derived_usr)])
        result = self.step(ids, exclude_paths=('dep.hpp',)).run(b.build())
        edges = {e.derived.name: e for e in result.class_inheritance}
        self.assertEqual(edges['Project'].access.name, 'public')
        self.assertIsNone(edges['Dependency'].access)
        self.assertIsNone(edges['Dependency'].is_virtual)

    def extract(self, source, paths=None):
        self.path.write_text(source)
        paths = paths or [self.path]
        return Extractor(EXE).run(self.command, tuple(str(p) for p in paths), 'fixture')

    def seed(self, result, *, placeholders=(), exclude_usrs=(), paths=None):
        b = IndexBuilder()
        b.set_metadata(IndexMetadata(project_root=str(self.root)))
        b.add_language(language_id=1, name='C++')
        paths = paths or [self.path]
        for i, path in enumerate(paths, 1):
            b.add_file(file_id=i, uri=path.as_uri(), content_hash=sha256(path.read_bytes()).hexdigest())
            for stage in CollectionStage:
                b.add_collection_status(file_id=i, stage=stage, status=CollectionState.NOT_REQUESTED)
        kinds = {kind: i for i, kind in enumerate(sorted({d.kind for d in result.declarations}), 1)}
        for kind, i in kinds.items():
            b.add_symbol_kind(kind_id=i, name=kind)
        ids, scope_id = {}, 1
        for d in result.declarations:
            if d.usr in exclude_usrs or symbol_key(d.usr) in ids:
                continue
            i = len(ids)+1
            ids[symbol_key(d.usr)] = i
            if d.usr in placeholders:
                b.add_symbol(symbol_id=i)
                continue
            scope = 1
            if d.scope:
                scope_id += 1
                b.add_scope(scope_id=scope_id, qualified_name=d.scope)
                scope = scope_id
            b.add_symbol(symbol_id=i, name=d.name, scope_id=scope, kind_id=kinds[d.kind], language_id=1)
        return b.build(), ids

    def test_declarations_resolve_parents_bases_overrides_and_details(self):
        source = '''namespace n {
/// A documented base.
struct A { virtual int f(int x) const { return x; } };
struct B: virtual public A { int f(int x) const override; int g(); };
int B::f(int x) const { return x+1; }
int B::g() { return f(1); }
}'''
        raw = self.extract(source)
        model, ids = self.seed(raw, placeholders={'c:@N@n', 'c:@N@n@S@A', 'c:@N@n@S@A@F@f#I#1'})
        step = self.step(ids)
        result = step.run(model)
        self.assertEqual(len(result.symbols), len(model.symbols))
        self.assertTrue(all(s.name is not None for s in result.symbols))
        b = next(s for s in result.symbols if s.name == 'B')
        methods = [s for s in result.symbols if s.name in ('f', 'g') and s.scope.owner is b]
        self.assertEqual(len(methods), 2)
        self.assertIs(methods[0].scope, methods[1].scope)
        self.assertEqual({s.return_type for s in methods}, {'int'})
        self.assertIn('(int x) const', {s.signature for s in methods})
        edge, = result.class_inheritance
        self.assertEqual(edge.access.name, 'public')
        self.assertTrue(edge.is_virtual)
        self.assertEqual(len(result.method_overrides), 1)
        self.assertTrue(next(s for s in result.symbols if s.name == 'A').documentation)
        self.assertEqual(step.report.placeholders_resolved, 3)
        self.assertTrue(all(s.name is None for s in model.symbols if s.symbol_id in (ids[symbol_key(u)] for u in
            ('c:@N@n', 'c:@N@n@S@A', 'c:@N@n@S@A@F@f#I#1'))))
        again = step.run(result)
        self.assertEqual(step.report.placeholders_resolved, 0)
        self.assertEqual(len(again.scopes), len(result.scopes))
        self.assertEqual(len(again.class_inheritance), 1)
        self.assertEqual(len(again.method_overrides), 1)
        db = self.root/'relations.sqlite3'
        SQLiteModelStore(db).save(result)
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute('pragma integrity_check').fetchone(), ('ok',))
            self.assertEqual(c.execute('pragma foreign_key_check').fetchall(), [])

    def test_dependency_endpoints_resolved_without_dependency_bodies(self):
        dep = self.root/'dep.hpp'
        dep.write_text('struct A { virtual int f() { return 1; } };\n')
        raw = self.extract('#include "dep.hpp"\nstruct B:A { int f() override { return 2; } };', [self.path, dep])
        model, ids = self.seed(raw, paths=[self.path, dep], placeholders={'c:@S@A', 'c:@S@A@F@f#'})
        step = self.step(ids, exclude_paths=('dep.hpp',))
        result = step.run(model)
        self.assertEqual(step.report.placeholders_resolved, 2)
        self.assertEqual(result.class_inheritance[0].access.name, 'public')
        self.assertTrue(all(b.body.file.uri == self.path.as_uri() for b in result.symbol_bodies))
        self.assertEqual(result.collection_status(2, CollectionStage.METRICS).status, CollectionState.NOT_REQUESTED)

    def test_absent_endpoint_is_not_created(self):
        raw = self.extract('struct A {}; struct B: A {};')
        model, ids = self.seed(raw, exclude_usrs={'c:@S@A'})
        step = self.step(ids)
        result = step.run(model)
        self.assertEqual(len(result.symbols), len(model.symbols))
        self.assertFalse(result.class_inheritance)
        self.assertTrue(any('endpoint' in d.message for d in step.report.diagnostics))

    def test_expected_body_absence_and_call_gaps_do_not_poison_metrics(self):
        raw = self.extract('struct A { A()=default; ~A()=default; int f()=delete; };\n'
            'int target(int x){return x;} int caller(int(*p)(int)) { return p(1); }')
        model, ids = self.seed(raw)
        step = self.step(ids)
        result = step.run(model)
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.COMPLETE)
        self.assertEqual(result.collection_status(1, CollectionStage.REFERENCES).status, CollectionState.PARTIAL)
        self.assertIn(('defaulted', 2), step.report.eligibility)
        self.assertIn(('deleted', 1), step.report.eligibility)
        constructors = [s for s in result.symbols if s.kind.name in ('constructor', 'destructor')]
        self.assertTrue(all(s.return_type is None and s.signature for s in constructors))

    def test_warning_and_prior_error_handling(self):
        raw = self.extract('int caller() { return 1; }')
        model, ids = self.seed(raw)
        warning = Issue('fixture', str(self.path), 'unused parameter', 'symbols', 'warning')
        fake = Mock(check=Mock(), run=Mock(return_value=replace(raw, issues=(warning,))))
        result = self.step(ids, fake).run(model)
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.COMPLETE)
        b = IndexBuilder.from_model(model)
        b.update_collection_status(file_id=1, stage=CollectionStage.SOURCE_RANGES,
                                   status=CollectionState.PARTIAL, error='prior LSP gap')
        result = self.step(ids, fake).run(b.build())
        self.assertIn('prior LSP gap', result.collection_status(1, CollectionStage.SOURCE_RANGES).error)

    def test_inheritance_conflicts_do_not_discard_symbol_details(self):
        raw = self.extract('struct A {}; struct B: public A { int f() const {return 1;} };')
        model, ids = self.seed(raw)
        self.write_commands([self.command, {**self.command,'arguments':self.command['arguments']+['-DALT']}])
        second = replace(raw, inheritance=tuple(replace(e, access='private') for e in raw.inheritance))
        fake = Mock(check=Mock(), run=Mock(side_effect=[raw, second]))
        step = self.step(ids, fake)
        result = step.run(model)
        self.assertFalse(result.class_inheritance)
        self.assertEqual(next(s for s in result.symbols if s.name=='f').return_type,'int')
        self.assertGreater(step.report.conflicts, 0)

    def test_reopened_namespace_and_overload_local_scopes_use_identity(self):
        raw = self.extract('namespace n { int a(){return 1;} } namespace n { int b(){return 2;} }\n'
            'void f(int) { struct Local { int x; }; }\nvoid f(double) { struct Local { int x; }; }\n'
            'namespace { struct Private {}; }')
        model, ids = self.seed(raw)
        result = self.step(ids).run(model)
        functions = [s for s in result.symbols if s.name in ('a','b')]
        self.assertIs(functions[0].scope, functions[1].scope)
        locals = [s for s in result.symbols if s.name=='Local']
        self.assertEqual(len(locals),2)
        self.assertIsNot(locals[0].scope,locals[1].scope)
        self.assertNotEqual(locals[0].parent.symbol_id,locals[1].parent.symbol_id)
        private = next(s for s in result.symbols if s.name=='Private')
        self.assertIsNotNone(private.parent)

    def test_cyclic_parent_observations_remain_unowned(self):
        raw = self.extract('struct A {}; struct B {};')
        model, ids = self.seed(raw)
        records = tuple(replace(d, parent_usr='c:@S@B' if d.name=='A' else 'c:@S@A', scope='cycle::')
                        for d in raw.declarations)
        fake = Mock(check=Mock(), run=Mock(return_value=replace(raw,declarations=records)))
        step=self.step(ids,fake)
        result=step.run(model)
        self.assertTrue(all(s.parent is None for s in result.symbols))
        self.assertTrue(any('cyclic' in d.message for d in step.report.diagnostics))

    def test_ref_qualifier_alias_and_template_fields(self):
        raw = self.extract('using Number=int; struct A { Number f(Number x) const & noexcept {return x;} };\n'
            'template<class T> struct Box {}; template<> struct Box<int> {};')
        model,ids=self.seed(raw)
        result=self.step(ids).run(model)
        method=next(s for s in result.symbols if s.name=='f')
        self.assertEqual(method.return_type,'Number')
        self.assertEqual(method.signature,'(Number x) const & noexcept')
        self.assertTrue(any(s.template_specialization_args=='<int>' for s in result.symbols))

    def test_out_of_line_defaulted_redeclarations_have_no_missing_body_gap(self):
        raw=self.extract('struct A { A(); A(const A&); ~A(); A& operator=(const A&); };\n'
            'A::A()=default; A::A(const A&)=default; A::~A()=default; A& A::operator=(const A&)=default;\n'
            'template<class T> struct B { B(); ~B(); };\n'
            'template<class T> B<T>::B()=default; template<class T> B<T>::~B()=default;')
        functions=[d for d in raw.declarations if d.kind in ('constructor','destructor','instance_method')]
        self.assertTrue(functions)
        self.assertTrue(all(d.eligibility=='defaulted' for d in functions))
        model,ids=self.seed(raw)
        step=self.step(ids)
        result=step.run(model)
        self.assertFalse(result.symbol_bodies)
        self.assertEqual(result.collection_status(1,CollectionStage.METRICS).status,CollectionState.COMPLETE)
        self.assertFalse(any('No analyzed body' in d.message for d in step.report.diagnostics))
