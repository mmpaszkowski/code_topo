"""Normalize commands deterministically without executing shell text."""
from hashlib import sha256
import json
from pathlib import Path
import shlex


def commands(directory: Path, remove_flags=()):
    directory = Path(directory).resolve()
    records = json.loads((directory / 'compile_commands.json').read_text())
    if not isinstance(records, list):
        raise ValueError('compile_commands.json must contain an array')
    unique = {}
    for entry in records:
        if not isinstance(entry, dict):
            raise ValueError('invalid compilation command')
        for key in ('directory', 'file'):
            if not isinstance(entry.get(key), str) or not entry[key] or '\0' in entry[key]:
                raise ValueError(f'invalid compilation command {key}')
        working = (directory / entry['directory']).resolve()
        source = (working / entry['file']).resolve()
        args = entry.get('arguments')
        if args is None:
            args = shlex.split(entry['command'])
        if (not isinstance(args, list) or not args or not args[0]
                or any(not isinstance(a, str) or '\0' in a for a in args)):
            raise ValueError('invalid compilation arguments')
        args = [args[0], *(a for a in args[1:] if a not in remove_flags)]
        # Output options do not define a semantic context. ClangTool also strips
        # these, including options introduced through response files.
        filtered, skip = [], False
        for arg in args:
            if skip:
                skip = False
            elif arg in ('-o', '-MF', '-MT', '-MQ'):
                skip = True
            elif arg in ('-c', '-MD', '-MMD', '-MP', '-MG'):
                continue
            elif any(arg.startswith(flag) and arg != flag for flag in ('-o', '-MF', '-MT', '-MQ')):
                continue
            else:
                filtered.append(arg)
        command = {'directory': str(working), 'file': str(source), 'arguments': filtered}
        key = sha256(json.dumps(command, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        unique[key] = command
    return tuple(sorted(unique.items()))
