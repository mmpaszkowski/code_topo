"""One isolated, syntax-only compiler invocation per compilation context."""
from dataclasses import fields
import json
from pathlib import Path
import re
import subprocess

from index_model import BodyMetrics
from .records import Body, Call, Extent, Issue, Result, Declaration, Inheritance, Override


class ProtocolError(ValueError):
    pass


def _text(value):
    if not isinstance(value, str) or '\0' in value:
        raise ProtocolError('expected a string without NUL')
    return value


def _extent(value):
    if not isinstance(value, dict):
        raise ProtocolError('expected a source extent')
    path = _text(value.get('path'))
    start, end = value.get('start'), value.get('end')
    if (not Path(path).is_absolute() or type(start) is not int or type(end) is not int
            or not 0 <= start < end):
        raise ProtocolError('invalid source extent')
    return Extent(str(Path(path).resolve()), start, end)


def _version(value):
    if (not isinstance(value, dict) or type(value.get('protocol')) is not int
            or type(value.get('llvm_major')) is not int
            or value != {'type': 'version', 'protocol': 2, 'llvm_major': 21}):
        raise ProtocolError('extractor must implement protocol 2 using LLVM 21')


def decode(stdout: str, context: str) -> Result:
    try:
        records = [json.loads(line) for line in stdout.splitlines()]
        if len(records) < 2 or any(not isinstance(r, dict) for r in records):
            raise ProtocolError('incomplete extractor output')
        _version(records[0])
        end = records[-1]
        if end.get('type') != 'complete' or type(end.get('ok')) is not bool:
            raise ProtocolError('missing completion record')
        files, bodies, calls, issues = {}, [], [], []
        declarations, inheritance, overrides = [], [], []
        compiler_error = False
        for r in records[1:-1]:
            kind = r.get('type')
            if kind == 'file':
                path, digest = _text(r.get('path')), _text(r.get('sha256'))
                if not Path(path).is_absolute() or not re.fullmatch('[0-9a-f]{64}', digest):
                    raise ProtocolError('invalid file hash record')
                path = str(Path(path).resolve())
                if path in files and files[path] != digest:
                    raise ProtocolError('conflicting source hashes')
                files[path] = digest
            elif kind == 'body':
                values = r.get('metrics')
                if not isinstance(values, dict) or set(values) != {f.name for f in fields(BodyMetrics)}:
                    raise ProtocolError('invalid metrics fields')
                bodies.append(Body(_text(r.get('usr')), _extent(r.get('body')),
                                   _extent(r['definition']) if r.get('definition') is not None else None,
                                   BodyMetrics(**values)))
            elif kind == 'call':
                calls.append(Call(_text(r.get('target_usr')), _text(r.get('container_usr')),
                                  _extent(r.get('location'))))
            elif kind == 'declaration':
                values = {name: _text(r.get(name)) for name in
                          ('usr', 'name', 'kind', 'language', 'parent_usr', 'scope', 'path',
                           'signature', 'return_type', 'template_arguments', 'documentation', 'eligibility')}
                if values['kind'] not in ('constructor', 'destructor', 'conversion_function', 'static_method',
                        'instance_method', 'function', 'class', 'struct', 'union', 'namespace', 'enum',
                        'enum_constant', 'field', 'type_alias', 'static_property', 'variable'):
                    raise ProtocolError('invalid declaration kind')
                if not Path(values['path']).is_absolute() or values['language'] not in ('C', 'Cpp'):
                    raise ProtocolError('invalid declaration source or language')
                if values['eligibility'] not in ('not_applicable', 'written', 'declaration_only', 'defaulted', 'deleted', 'implicit'):
                    raise ProtocolError('invalid body eligibility')
                values['path'] = str(Path(values['path']).resolve())
                declarations.append(Declaration(**values,
                    declaration=_extent(r['declaration']) if r.get('declaration') is not None else None,
                    definition=_extent(r['definition']) if r.get('definition') is not None else None))
            elif kind in ('inheritance', 'override'):
                args = {name: _text(r.get(name)) for name in ('base_usr', 'derived_usr', 'path')}
                if not Path(args['path']).is_absolute():
                    raise ProtocolError('invalid relation source')
                args['path'] = str(Path(args['path']).resolve())
                if kind == 'inheritance':
                    if r.get('access') not in ('public', 'protected', 'private') or type(r.get('virtual')) is not bool:
                        raise ProtocolError('invalid inheritance attributes')
                    existing_edge_only = r.get('existing_edge_only', False)
                    if type(existing_edge_only) is not bool:
                        raise ProtocolError('invalid inheritance existing_edge_only flag')
                    inheritance.append(Inheritance(**args, access=r['access'], virtual=r['virtual'],
                                                   existing_edge_only=existing_edge_only))
                else:
                    overrides.append(Override(**args))
            elif kind in ('diagnostic', 'issue'):
                if kind == 'diagnostic':
                    if type(r.get('error')) is not bool:
                        raise ProtocolError('invalid diagnostic severity')
                    compiler_error |= r['error']
                # Warnings are retained without poisoning collection coverage.
                stage = _text(r.get('stage', 'symbols' if kind == 'diagnostic' else 'references'))
                if stage not in ('symbols', 'references', 'source_ranges', 'metrics'):
                    raise ProtocolError('invalid diagnostic stage')
                issues.append(Issue(context, _text(r.get('path')), _text(r.get('message')), stage,
                                    'warning' if kind == 'diagnostic' and not r['error'] else 'gap'))
            else:
                raise ProtocolError(f'unknown record type: {kind}')
        if any(d.path not in files for d in declarations):
            raise ProtocolError('declaration has no evidence source hash')
        for extent in ([b.body for b in bodies] + [b.definition for b in bodies if b.definition]
                       + [c.location for c in calls]
                       + [e for d in declarations for e in (d.declaration, d.definition) if e]):
            if extent.path not in files:
                raise ProtocolError('extent has no source-buffer hash')
        return Result(tuple(sorted(files.items())), tuple(bodies), tuple(calls), tuple(issues),
                      end['ok'] and not compiler_error, tuple(declarations), tuple(inheritance), tuple(overrides))
    except (KeyError, TypeError, ValueError) as error:
        raise ProtocolError(str(error)) from error


class Extractor:
    def __init__(self, executable: str, timeout_seconds: float = 120):
        self.executable = executable
        self.timeout = timeout_seconds

    def check(self):
        result = subprocess.run([self.executable, '--version'], capture_output=True, text=True,
                                encoding='utf-8', timeout=self.timeout, check=True)
        _version(json.loads(result.stdout))

    def run(self, command: dict, files: tuple[str, ...], context: str, *, metadata_files=(), ids=()) -> Result:
        result = subprocess.run([self.executable], input=json.dumps({
            'protocol': 2, 'command': command, 'files': files, 'metadata_files': metadata_files, 'ids': ids}), cwd=command['directory'],
            capture_output=True, text=True, encoding='utf-8', timeout=self.timeout, check=True)
        return decode(result.stdout, context)
