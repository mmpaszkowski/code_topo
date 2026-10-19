from contextlib import closing
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from clang_ast import ClangAstStep, Extractor
from clang_ast.client import ProtocolError, decode
from clang_ast.commands import commands
from clang_ast.records import Body, Call, Extent, Issue, Result
from clang_ast.step import METRICS_VERSION, symbol_key
from index_model import (BodyMetrics, CollectionStage, CollectionState, IndexBuilder,
                         IndexMetadata, Position, ReferenceKind)
from pipeline_config import load_config, ConfigurationError, ClangAstStepConfig
from sqlite_model_store import SQLiteModelStore
from progress import ProgressReporter

EXE = os.environ.get('INDEXER_AST_EXECUTABLE', shutil.which('indexer-clang-ast') or '')


def metrics(**overrides):
    values = {name: 0 for name in BodyMetrics.__dataclass_fields__}
    values['cyclomatic_complexity'] = 1
    values.update(overrides)
    return BodyMetrics(**values)


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'main.cpp'
        self.path.write_text('int target(int x) { return x; }\nint caller() { return target(1); }\n')
        self.command = {'directory': str(self.root), 'file': str(self.path),
                        'arguments': ['clang++-21', '-std=c++20', str(self.path)]}
        self.write_commands([self.command])

    def write_commands(self, values):
        (self.root / 'compile_commands.json').write_text(json.dumps(values))

    def model(self, paths=None, symbols=None):
        paths = paths or [self.path]
        symbols = symbols or [('target', 'c:@F@target#I#'), ('caller', 'c:@F@caller#')]
        b = IndexBuilder()
        b.set_metadata(IndexMetadata(project_root=str(self.root)))
        b.add_language(language_id=1, name='Cpp')
        b.add_symbol_kind(kind_id=1, name='function')
        for bit in ReferenceKind:
            b.add_reference_kind_bit(bit=int(bit), name=bit.name.lower())
        for i, path in enumerate(paths, 1):
            b.add_file(file_id=i, uri=path.as_uri(), content_hash=sha256(path.read_bytes()).hexdigest())
            for stage in CollectionStage:
                b.add_collection_status(file_id=i, stage=stage, status=CollectionState.NOT_REQUESTED)
        mapping = {}
        for i, (name, usr) in enumerate(symbols, 1):
            b.add_symbol(symbol_id=i, name=name, scope_id=1, language_id=1, kind_id=1)
            mapping[symbol_key(usr)] = i
        return b.build(), mapping

    def step(self, mapping, extractor=None, **kwargs):
        return ClangAstStep(extractor or Extractor(EXE, 30), self.root,
                            symbol_ids_provider=lambda: mapping, **kwargs)

    def result(self, bodies=(), calls=(), **kwargs):
        return Result(((str(self.path), sha256(self.path.read_bytes()).hexdigest()),),
                      tuple(bodies), tuple(calls), (), True, **kwargs)

    def fake(self, result):
        return Mock(check=Mock(), run=Mock(return_value=result))

    def extent(self, text):
        data = self.path.read_bytes()
        start = data.index(text.encode())
        return Extent(str(self.path), start, start + len(text.encode()))


class AdapterTests(Fixture):
    def test_progress_counts_failed_contexts_and_excludes_commands(self):
        excluded = self.root / 'dep.cpp'
        excluded.write_text('int dep;')
        self.write_commands([self.command,
            {**self.command, 'arguments': self.command['arguments'] + ['-DALT']},
            {'directory': str(self.root), 'file': str(excluded), 'arguments': ['clang++-21', str(excluded)]}])
        model, mapping = self.model()
        progress = Mock(spec=ProgressReporter)
        extractor = self.fake(self.result())
        extractor.run.side_effect = [ValueError('bad output'), replace(self.result(), ok=False)]
        step = self.step(mapping, extractor, exclude_paths=('dep.cpp',), progress=progress)
        step.run(model)
        progress.phase.assert_any_call('compiling', total=2, unit='contexts')
        self.assertEqual([c.args[0] for c in progress.update.call_args_list if c.args], [1, 2])
        self.assertEqual(step.report.contexts_failed, 2)
        progress.phase.assert_any_call('merging and validating')

    def test_inheritance_protocol_optional_existing_edge_flag(self):
        edge = {'type': 'inheritance', 'base_usr': 'base', 'derived_usr': 'derived',
                'path': str(self.path), 'access': 'public', 'virtual': False}
        def read(record):
            return decode('\n'.join(json.dumps(r) for r in (
                {'type': 'version', 'protocol': 2, 'llvm_major': 21}, record,
                {'type': 'complete', 'ok': True})), 'test').inheritance[0]
        self.assertFalse(read(edge).existing_edge_only)
        self.assertTrue(read({**edge, 'existing_edge_only': True}).existing_edge_only)
        for invalid in (None, 0, 1, 'true', []):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                read({**edge, 'existing_edge_only': invalid})

    def test_merge_references_preserves_ids_bits_and_canonical_objects(self):
        model, mapping = self.model()
        extent = self.extent('target(1)')
        extent = replace(extent, end=extent.start + 6)
        # Source offset checked independently below; use exact observed coordinates.
        from clang_ast.step import _Merge
        merge = _Merge(model, mapping, ())
        merge.sources[str(self.path)] = self.path.read_bytes()
        start, end = merge.coordinates(extent)
        b = IndexBuilder.from_model(model)
        b.add_location(location_id=1, file_id=1, start=start, end=end)
        b.add_symbol_reference(reference_id=7, target_symbol_id=1, location_id=1,
                               kind=ReferenceKind.REFERENCE | ReferenceKind.SPELLED)
        model = b.build()
        call = Call('c:@F@target#I#', 'c:@F@caller#', extent)
        step = self.step(mapping, self.fake(self.result(calls=[call])))
        result = step.run(model)
        ref, = result.symbol_references
        self.assertEqual(ref.reference_id, 7)
        self.assertEqual(ref.kind, ReferenceKind.REFERENCE | ReferenceKind.SPELLED | ReferenceKind.CALL)
        self.assertIs(ref.target, result.symbols[0])
        self.assertIs(ref.container, result.symbols[1])
        self.assertFalse(model.symbol_references[0].kind & ReferenceKind.CALL)
        again = step.run(result)
        self.assertEqual(len(again.symbol_references), 1)
        self.assertEqual(again.symbol_references[0].reference_id, 7)

    def test_conflicting_contexts_leave_metrics_and_calls_unfilled(self):
        model, mapping = self.model()
        body = Body('c:@F@target#I#', self.extent('{ return x; }'), None, metrics(return_count=1))
        call = Call('c:@F@target#I#', 'c:@F@caller#', self.extent('target(1)'))
        first = self.result(bodies=[body], calls=[call])
        second = self.result(bodies=[replace(body, metrics=metrics(return_count=2))],
                             calls=[replace(call, target_usr='c:@F@unknown#')])
        self.write_commands([self.command, {**self.command, 'arguments': self.command['arguments'] + ['-DALT']}])
        extractor = self.fake(first)
        extractor.run.side_effect = [first, second]
        step = self.step(mapping, extractor)
        result = step.run(model)
        self.assertFalse(result.symbol_bodies)
        self.assertFalse(result.symbol_references)
        self.assertEqual(step.report.conflicts, 2)
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.PARTIAL)

    def test_failed_compilation_discards_facts_and_continues(self):
        model, mapping = self.model()
        body = Body('c:@F@target#I#', self.extent('{ return x; }'), None, metrics(return_count=1))
        extractor = self.fake(replace(self.result(bodies=[body]), ok=False))
        step = self.step(mapping, extractor)
        result = step.run(model)
        self.assertFalse(result.symbol_bodies)
        self.assertEqual(step.report.contexts_failed, 1)
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.PARTIAL)

    def test_timeout_and_protocol_failure_are_partial(self):
        model, mapping = self.model()
        for error in (subprocess.TimeoutExpired('extractor', 1), ProtocolError('truncated')):
            with self.subTest(error=error):
                extractor = self.fake(None)
                extractor.run.side_effect = error
                step = self.step(mapping, extractor)
                result = step.run(model)
                self.assertEqual(step.report.contexts_failed, 1)
                self.assertFalse(result.symbol_bodies)

    def test_stale_source_and_unknown_identity_are_not_published(self):
        model, mapping = self.model()
        body = Body('c:@F@target#I#', self.extent('{ return x; }'), None, metrics(return_count=1))
        response = self.result(bodies=[body])
        self.path.write_text(self.path.read_text() + '// changed\n')
        step = self.step(mapping, self.fake(response))
        self.assertFalse(step.run(model).symbol_bodies)
        self.assertTrue(any('hash' in d.message for d in step.report.diagnostics))
        model, mapping = self.model()
        response = self.result(bodies=[replace(body, usr='unknown')])
        step = self.step(mapping, self.fake(response))
        self.assertFalse(step.run(model).symbol_bodies)
        self.assertEqual(step.report.symbols_unmatched, 1)

    def test_failed_context_does_not_prevent_successful_context(self):
        model, mapping = self.model()
        body = Body('c:@F@target#I#', self.extent('{ return x; }'), None, metrics(return_count=1))
        self.write_commands([self.command, {**self.command, 'arguments': self.command['arguments'] + ['-DALT']}])
        extractor = self.fake(None)
        extractor.run.side_effect = [subprocess.TimeoutExpired('extractor', 1), self.result(bodies=[body])]
        step = self.step(mapping, extractor)
        result = step.run(model)
        self.assertEqual(len(result.symbol_bodies), 1)
        self.assertEqual(step.report.contexts_failed, 1)
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.PARTIAL)

    def test_excluded_file_has_no_commands_or_new_coverage(self):
        model, mapping = self.model()
        extractor = self.fake(None)
        step = self.step(mapping, extractor, exclude_paths=('main.cpp',))
        result = step.run(model)
        extractor.run.assert_not_called()
        self.assertEqual(result.collection_status(1, CollectionStage.METRICS).status, CollectionState.NOT_REQUESTED)

    def test_ambiguous_identity_mapping_is_not_used(self):
        model, mapping = self.model()
        mapping['0000000000000000'] = mapping[symbol_key('c:@F@target#I#')]
        body = Body('c:@F@target#I#', self.extent('{ return x; }'), None, metrics(return_count=1))
        step = self.step(mapping, self.fake(self.result(bodies=[body])))
        self.assertFalse(step.run(model).symbol_bodies)

    def test_out_of_bounds_and_utf8_split_offsets_rejected(self):
        self.path.write_text('int target(int x) { /* é */ return x; }')
        model, mapping = self.model()
        encoded = self.path.read_bytes()
        bad = [Extent(str(self.path), 0, len(encoded)+1),
               Extent(str(self.path), encoded.index('é'.encode())+1, len(encoded))]
        for extent in bad:
            body = Body('c:@F@target#I#', extent, None, metrics())
            step = self.step(mapping, self.fake(self.result(bodies=[body])))
            self.assertFalse(step.run(model).symbol_bodies)
            self.assertTrue(step.report.diagnostics)

    def test_existing_metrics_version_rejected_before_execution(self):
        model, mapping = self.model()
        extractor = self.fake(None)
        with self.assertRaisesRegex(ValueError, 'metrics versions'):
            self.step(mapping, extractor).run(replace(model, metadata=replace(model.metadata, metrics_version='other')))
        extractor.check.assert_not_called()

    def test_config_order_and_defaults(self):
        path = self.root / 'config.yaml'
        base = 'project_root: .\nsteps:\n  - type: clangd_yaml\n    path: export.yaml\n'
        ast = '  - type: clang_ast\n    compile_commands_dir: .\n'
        path.write_text(base + ast)
        config = load_config(path)
        self.assertIsInstance(config.steps[-1], ClangAstStepConfig)
        self.assertEqual(config.steps[-1].timeout_seconds, 120)
        for text in ('project_root: .\nsteps:\n' + ast, base + ast + ast,
                     base + ast + '  - type: clangd_lsp\n    compile_commands_dir: .\n'):
            path.write_text(text)
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_commands_deduplicate_outputs_and_preserve_semantic_flags(self):
        self.write_commands([{**self.command, 'arguments': self.command['arguments'] + ['-o', 'a.o', '-DVALUE=1']},
                             {**self.command, 'arguments': self.command['arguments'] + ['-o', 'b.o', '-DVALUE=1']},
                             {**self.command, 'arguments': self.command['arguments'] + ['-DVALUE=2']}])
        values = commands(self.root)
        self.assertEqual(len(values), 2)
        self.assertTrue(all('-o' not in c['arguments'] for _, c in values))

    def test_decoder_rejects_truncation_unknown_protocol_and_invalid_metrics(self):
        header = {'type': 'version', 'protocol': 2, 'llvm_major': 21}
        tail = {'type': 'complete', 'ok': True}
        for records in ([header], [dict(header, protocol=1), tail], [dict(header, protocol=True), tail], [header, {'type': 'mystery'}, tail],
                        [header, {'type': 'body', 'usr': 'x', 'body': {}, 'metrics': {}}, tail]):
            with self.subTest(records=records), self.assertRaises(ProtocolError):
                decode('\n'.join(json.dumps(r) for r in records), 'test')


@unittest.skipUnless(EXE, 'build the native extractor and set INDEXER_AST_EXECUTABLE')
class NativeTests(Fixture):
    def extract(self, text, args=()):
        self.path.write_bytes(text.encode())
        command = {**self.command, 'arguments': self.command['arguments'] + list(args)}
        return Extractor(EXE).run(command, (str(self.path),), 'test')

    @unittest.skipUnless(shutil.which('clangd'), 'clangd is needed for independent identity verification')
    def test_usr_identity_agrees_with_real_clangd_symbol_info(self):
        from clangd_lsp import ClangdClient
        from clangd_lsp.records import LspPosition
        response = Extractor(EXE).run(self.command, (str(self.path),), 'identity')
        client = ClangdClient(self.root, timeout_seconds=10)
        self.addCleanup(client.close)
        client.start(self.root)
        client.open_document(self.path.as_uri(), self.path.read_text())
        identity, = client.symbol_info(self.path.as_uri(), LspPosition(0, 4))
        body = next(b for b in response.bodies if b.usr == 'c:@F@target#I#')
        self.assertEqual(symbol_key(body.usr), identity.id.upper())
        client.close_document(self.path.as_uri())

    def test_identity_metrics_calls_idempotence_and_sqlite(self):
        model, mapping = self.model()
        step = self.step(mapping)
        result = step.run(model)
        self.assertEqual(len(result.symbol_bodies), 2)
        self.assertEqual(result.metadata.metrics_version, METRICS_VERSION)
        self.assertEqual(result.symbol_bodies[0].metrics, metrics(statement_count=1, return_count=1, parameter_count=1))
        call, = result.symbol_references
        self.assertEqual(call.target.name, 'target')
        self.assertEqual(call.container.name, 'caller')
        self.assertTrue(call.kind & ReferenceKind.CALL)
        again = step.run(result)
        self.assertEqual(step.report.bodies_added, 0)
        self.assertEqual(len(again.locations), len(result.locations))
        self.assertEqual(len(again.symbol_references), 1)
        self.assertFalse(model.symbol_bodies)
        db = self.root / 'out.sqlite3'
        SQLiteModelStore(db).save(result)
        with closing(sqlite3.connect(db)) as connection:
            self.assertEqual(connection.execute('pragma integrity_check').fetchone(), ('ok',))
            self.assertFalse(connection.execute('pragma foreign_key_check').fetchall())
            self.assertEqual(connection.execute('select count(*) from symbol_bodies').fetchone(), (2,))

    def test_calls_exclude_address_unevaluated_indirect_and_discarded_branches(self):
        result = self.extract('int target(int x) { return x; }\nint other(int x) { return x; }\n'
            'int caller() { auto p=&target; (void)sizeof(target(1)); (void)noexcept(target(1)); '
            'if constexpr (false) other(1); p(1); return target(2); }\n')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.calls[0].target_usr, 'c:@F@target#I#')
        self.assertTrue(any('indirect' in i.message for i in result.issues))

    def test_exact_branch_loop_lambda_and_exception_metrics(self):
        result = self.extract('int target(int x) {\n'
            'auto f=[] { if (true) return 1; return 0; };\n'
            'for(int i=0; i<x; ++i) { if(i && x) continue; else break; }\n'
            'switch(x) { case 1: break; default: break; }\n'
            'try { if(x) throw 1; } catch(int e) { return e; }\n'
            'return x ? 1 : 0; }\n')
        self.assertTrue(result.ok)
        m = result.bodies[0].metrics
        self.assertEqual(m.if_count, 3)
        self.assertEqual(m.loop_count, 1)
        self.assertEqual(m.case_count, 1)
        self.assertEqual(m.catch_count, 1)
        self.assertEqual(m.cyclomatic_complexity, 9)
        self.assertEqual(m.max_nesting_depth, 2)
        self.assertEqual(m.return_count, 4)
        self.assertEqual(m.local_variable_count, 3)
        self.assertEqual(m.lambda_count, 1)

    def test_lambda_container_and_local_class_metrics(self):
        result = self.extract('int target(int x) { return x; }\n'
            'int caller() { struct Local { int f() { if(true) return 1; return 2; } }; '
            'auto l=[] { return target(1); }; return target(2); }\n')
        self.assertTrue(result.ok)
        caller = next(b for b in result.bodies if b.usr == 'c:@F@caller#')
        self.assertEqual(caller.metrics.if_count, 0)
        self.assertEqual(caller.metrics.return_count, 2)
        self.assertEqual(len(result.calls), 2)
        self.assertNotEqual(result.calls[0].container_usr, 'c:@F@caller#')
        self.assertEqual(result.calls[1].container_usr, 'c:@F@caller#')

    def test_defaulted_bodies_and_calls_are_not_written_facts(self):
        result = self.extract('struct Field { Field& operator=(const Field&); };\n'
            'struct A { Field f; A& operator=(const A&); };\n'
            'A& A::operator=(const A&) = default;\n')
        self.assertTrue(result.ok)
        self.assertFalse(result.bodies)
        self.assertFalse(result.calls)

    def test_macro_boundaries_are_reported_not_guessed(self):
        result = self.extract('#define BODY { return 1; }\n#define CALL target(1)\n'
            'int target(int x) { return x; }\nint hidden() BODY\nint caller() { return CALL; }\n')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.bodies), 2)
        self.assertFalse(result.calls)
        self.assertTrue(any('macro-generated body' in i.message for i in result.issues))
        self.assertTrue(any('macro-generated call' in i.message for i in result.issues))

    def test_overloads_have_distinct_compiler_identities(self):
        result = self.extract('int target(int x) { return x; }\n'
            'int target(double x) { return 1; }\nint caller() { return target(1)+target(1.0); }\n')
        self.assertTrue(result.ok)
        self.assertEqual({c.target_usr for c in result.calls}, {'c:@F@target#I#', 'c:@F@target#d#'})

    def test_member_operator_virtual_and_recursive_calls(self):
        result = self.extract('struct A { virtual int f() { return 1; } '
            'int operator+(int x) { return x; } };\n'
            'int caller(A& a, int x) { if(x) return caller(a,x-1); return a.f() + (a + 1); }\n')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.calls), 3)
        self.assertTrue(any('@F@f#' in c.target_usr for c in result.calls))
        self.assertTrue(any('operator+' in c.target_usr for c in result.calls))

    def test_range_for_ignores_lowered_nodes_and_calls(self):
        result = self.extract('int target(int x) { int a[2]={1,2}; for(int v:a) x+=v; return x; }\n')
        self.assertTrue(result.ok)
        self.assertEqual(result.bodies[0].metrics.local_variable_count, 2)
        self.assertEqual(result.bodies[0].metrics.loop_count, 1)
        self.assertEqual(result.bodies[0].metrics.statement_count, 4)
        self.assertFalse(result.calls)

    def test_unicode_crlf_macros_and_templates(self):
        result = self.extract('#define RET(x) return x\r\n'
            'template<class T> T identity(T x) { RET(x); }\r\n'
            'int target(int x) { /* é😀 */ return identity(x); }\r\n')
        self.assertTrue(result.ok)
        self.assertEqual(len(result.bodies), 2)
        self.assertEqual(result.bodies[0].metrics.return_count, 1)
        data = self.path.read_bytes()
        self.assertTrue(all(data[b.body.start:b.body.end].startswith(b'{') for b in result.bodies))

    def test_header_and_conflicting_defines(self):
        header = self.root / 'header.hpp'
        header.write_text('inline int target(int x) {\n#ifdef ALT\nif(x) return 1;\n#endif\nreturn x; }\n')
        self.path.write_text('#include "header.hpp"\nint caller() { return target(1); }\n')
        model, mapping = self.model(paths=[self.path, header])
        self.write_commands([self.command, {**self.command, 'arguments': self.command['arguments'] + ['-DALT']}])
        step = self.step(mapping)
        result = step.run(model)
        self.assertEqual([b.symbol.name for b in result.symbol_bodies], ['caller'])
        self.assertEqual(step.report.conflicts, 1)

    def test_factory_pipeline_uses_yaml_identity_callback(self):
        from application_factory import create_application
        export = self.root / 'export.yaml'
        documents = []
        for name, key, line, start in [('target', 'c:@F@target#I#', 0, 4), ('caller', 'c:@F@caller#', 1, 4)]:
            loc = f"{{FileURI: '{self.path.as_uri()}', Start: {{Line: {line}, Column: {start}}}, End: {{Line: {line}, Column: {start + len(name)}}}}}"
            documents.append(f"--- !Symbol\nID: '{symbol_key(key)}'\nName: {name}\nScope: ''\n"
                f"SymInfo: {{Kind: Function, Lang: Cpp}}\nCanonicalDeclaration: {loc}\nDefinition: {loc}\n"
                "Flags: 9\nSignature: ''\nTemplateSpecializationArgs: ''\nCompletionSnippetSuffix: ''\n"
                "Documentation: ''\nReturnType: int\nType: c:I\n...\n")
        export.write_text(''.join(documents))
        config = self.root / 'config.yaml'
        config.write_text('project_root: .\nsteps:\n  - type: clangd_yaml\n    path: export.yaml\n'
                          f'  - type: clang_ast\n    compile_commands_dir: .\n    executable: {EXE}\n')
        app = create_application(load_config(config))
        result = app.run()
        self.assertEqual(len(result.symbol_bodies), 2)
        self.assertEqual(app.steps[-2].report.calls_confirmed, 1)
        self.assertEqual(len(app.run().symbol_bodies), 2)

    def test_compile_error_discards_valid_body(self):
        self.path.write_text('int target(int x) { return x; }\nint caller() { return missing; }\n')
        model, mapping = self.model()
        step = self.step(mapping)
        result = step.run(model)
        self.assertEqual(step.report.contexts_failed, 1)
        self.assertFalse(result.symbol_bodies)
        self.assertTrue(any('missing' in i.message for i in step.report.diagnostics))
