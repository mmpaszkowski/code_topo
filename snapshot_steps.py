"""Application-owned vocabulary, input provenance, and successful-run finalization."""
from collections import Counter
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import shlex
import subprocess

from index_model import IndexBuilder, SnapshotStatus
from index_model.records import utc_now
from index_pipeline import Step, SummaryItem
from model_store import ModelStore
from progress import NO_PROGRESS, ProgressReporter
from pipeline_config import ClangdYamlStepConfig
from clang_ast.commands import commands

REFERENCE_BITS = {1: 'declaration', 2: 'definition', 4: 'reference', 8: 'spelled', 16: 'call'}
ACCESS = ('public', 'protected', 'private')


def _digest(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _responses(arguments, directory, seen=None):
    seen = set() if seen is None else seen
    found = {}
    for arg in arguments:
        if not arg.startswith('@'):
            continue
        path = (directory / arg[1:]).resolve()
        if path in seen:
            raise ValueError(f'Recursive response file: {path}')
        data = path.read_bytes()
        found[str(path)] = sha256(data).hexdigest()
        found.update(_responses(shlex.split(data.decode()), directory, seen | {path}))
    return found


class RunInputs:
    def __init__(self, config, *, progress: ProgressReporter = NO_PROGRESS):
        self.config = config
        self.progress = progress
        self.initial = None
        self.revision = None

    def fingerprint(self):
        inputs = []
        for step in self.config.steps:
            record = asdict(step)
            if isinstance(step, ClangdYamlStepConfig):
                self.progress.update(detail=f"Hashing {step.path}")
                record['yaml_sha256'] = _digest(step.path)
            else:
                self.progress.update(detail=f"Compilation commands: {step.compile_commands_dir}")
                effective = commands(step.compile_commands_dir, step.remove_compile_flags)
                record['effective_commands'] = effective
                record['response_files'] = {key: value for _, command in effective for key, value in
                    _responses(command['arguments'], Path(command['directory'])).items()}
                self.progress.update(detail=f"Tool version: {step.executable}")
                version = subprocess.run([step.executable, '--version'], capture_output=True,
                    text=True, check=True, timeout=step.timeout_seconds)
                record['tool_version'] = version.stdout.strip()
                executable = Path(step.executable)
                if executable.is_file():
                    self.progress.update(detail=f"Hashing {executable}")
                    record['executable_sha256'] = _digest(executable)
            inputs.append(record)
        payload = {'project_root': str(self.config.project_root), 'steps': inputs}
        return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()

    def source_revision(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.config.project_root), *args],
                                  capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        try:
            revision = git('rev-parse', 'HEAD')
            return revision + ('-dirty' if git('status', '--porcelain', '--untracked-files=normal') else '')
        except (OSError, subprocess.SubprocessError):
            return None


class PrepareSnapshotStep(Step):
    def __init__(self, inputs, *, progress: ProgressReporter = NO_PROGRESS):
        self.inputs = inputs
        self.progress = progress
        self.prepared_model = None

    def run(self, model):
        self.progress.phase("initializing vocabulary")
        b = IndexBuilder.from_model(model)
        bits = {r.bit: r.name for r in model.reference_kind_bits}
        if any(REFERENCE_BITS.get(bit) != name for bit, name in bits.items()):
            raise ValueError('Conflicting reference-kind vocabulary')
        for bit, name in REFERENCE_BITS.items():
            if bit not in bits:
                b.add_reference_kind_bit(bit=bit, name=name)
        access = {r.name: r.access_id for r in model.access_specifiers}
        if set(access) - set(ACCESS):
            raise ValueError('Conflicting access-specifier vocabulary')
        next_id = max(access.values(), default=0) + 1
        for name in ACCESS:
            if name not in access:
                b.add_access_specifier(access_id=next_id, name=name)
                next_id += 1
        self.progress.phase("fingerprinting inputs")
        self.inputs.initial = self.inputs.fingerprint()
        self.progress.phase("reading source revision")
        self.inputs.revision = self.inputs.source_revision()
        b.update_metadata(replace(model.metadata, build_input_hash=self.inputs.initial,
                                  source_revision=self.inputs.revision))
        self.progress.phase("validating model")
        self.prepared_model = b.build()
        return self.prepared_model


class FinalizeSnapshotStep(Step):
    def __init__(self, inputs, *, progress: ProgressReporter = NO_PROGRESS):
        self.inputs = inputs
        self.progress = progress
        self.counts = Counter()

    def run(self, model):
        self.progress.phase("rechecking inputs")
        if self.inputs.initial is None or self.inputs.fingerprint() != self.inputs.initial:
            raise ValueError('Build inputs or source revision changed during indexing')
        self.progress.phase("rechecking source revision")
        if self.inputs.source_revision() != self.inputs.revision:
            raise ValueError('Build inputs or source revision changed during indexing')
        self.progress.phase("finalizing metadata")
        self.counts = Counter(row.status.value for row in model.collection_statuses)
        return replace(model, metadata=replace(model.metadata, status=SnapshotStatus.COMPLETE,
                                               completed_at=utc_now(), error=None))

    def summary(self):
        return tuple(SummaryItem(f'Collection outcomes ({key})', self.counts[key])
                     for key in ('complete', 'partial', 'failed', 'not_requested'))


class CollectorCheckpointStore(ModelStore):
    """Do not replace a prior index with the preparation-only empty seed."""
    def __init__(self, store, preparation):
        self.store = store
        self.preparation = preparation

    def save(self, model):
        if model is not self.preparation.prepared_model:
            self.store.save(model)
