"""Read a small YAML configuration into immutable application settings."""

from dataclasses import dataclass
import math
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError


class ConfigurationError(ValueError):
    """The configuration cannot be read or contains invalid settings."""


@dataclass(frozen=True, slots=True)
class ClangdYamlStepConfig:
    path: Path
    recover_known_errors: bool = True


@dataclass(frozen=True, slots=True)
class ClangdLspStepConfig:
    compile_commands_dir: Path
    executable: str = "clangd"
    timeout_seconds: float = 60
    remove_compile_flags: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClangAstStepConfig:
    compile_commands_dir: Path
    executable: str = "indexer-clang-ast"
    timeout_seconds: float = 120
    remove_compile_flags: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()


StepConfig = ClangdYamlStepConfig | ClangdLspStepConfig | ClangAstStepConfig


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: Path
    save_after_each_step: bool = False


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    project_root: Path
    steps: tuple[StepConfig, ...]
    database: DatabaseConfig | None = None


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader with explicit string keys and no silent duplicate overrides."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConstructorError(None, None, "setting names must be strings", key_node.start_mark)
            if key in mapping:
                raise ConstructorError(None, None, f"duplicate setting: {key}", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _settings(value: object, allowed: set[str], field: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field}: expected a mapping")
    unknown = value.keys() - allowed
    if unknown:
        raise ConfigurationError(f"{field}: unknown settings: {', '.join(sorted(unknown))}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field}: expected a nonempty string")
    return value


def _path(value: object, field: str, directory: Path) -> Path:
    text = _text(value, field)
    if "\0" in text:
        raise ConfigurationError(f"{field}: paths cannot contain a null character")
    # Joining an absolute path leaves it absolute; relative paths use the config directory.
    return (directory / text).resolve()


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{field}: expected a list of strings")
    result = tuple(_text(item, field) for item in value)
    if any("\0" in item for item in result):
        raise ConfigurationError(f"{field}: cannot contain a null character")
    return result


def load_config(path: Path) -> PipelineConfig:
    """Validate the whole configuration before creating or running any steps."""
    path = Path(path).absolute()
    try:
        with path.open(encoding="utf-8") as stream:
            raw = yaml.load(stream, Loader=_ConfigLoader)
        settings = _settings(raw, {"project_root", "steps", "database"}, "config")
        project_root = _path(settings.get("project_root"), "project_root", path.parent)
        database = None
        if "database" in settings:
            db = _settings(settings["database"], {"path", "save_after_each_step"}, "database")
            database_path = _path(db.get("path"), "database.path", path.parent)
            save = db.get("save_after_each_step", False)
            if not isinstance(save, bool):
                raise ConfigurationError("database.save_after_each_step: expected a boolean")
            database = DatabaseConfig(database_path, save)
        raw_steps = settings.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ConfigurationError("steps: expected a nonempty list")

        steps = []
        for index, raw_step in enumerate(raw_steps):
            field = f"steps[{index}]"
            step = _settings(raw_step, {"type", "path", "recover_known_errors",
                                        "compile_commands_dir", "executable", "timeout_seconds",
                                        "remove_compile_flags", "exclude_paths"}, field)
            step_type = _text(step.get("type"), f"{field}.type")
            if step_type in ("clangd_lsp", "clang_ast"):
                _settings(step, {"type", "compile_commands_dir", "executable", "timeout_seconds",
                                 "remove_compile_flags", "exclude_paths"}, field)
                if step_type == "clangd_lsp" and index != 1:
                    raise ConfigurationError(f"{field}: clangd_lsp must follow clangd_yaml and cannot be repeated")
                if step_type == "clang_ast" and (not steps or
                        any(isinstance(s, ClangAstStepConfig) for s in steps)):
                    raise ConfigurationError(f"{field}: clang_ast must follow clangd_yaml and cannot be repeated")
                directory = _path(step.get("compile_commands_dir"), f"{field}.compile_commands_dir", path.parent)
                executable = _text(step.get("executable", "clangd" if step_type == "clangd_lsp" else "indexer-clang-ast"), f"{field}.executable")
                if "\0" in executable:
                    raise ConfigurationError(f"{field}.executable: cannot contain a null character")
                if "/" in executable:
                    executable = str(_path(executable, f"{field}.executable", path.parent))
                timeout = step.get("timeout_seconds", 60 if step_type == "clangd_lsp" else 120)
                if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                        or not math.isfinite(timeout) or timeout <= 0):
                    raise ConfigurationError(f"{field}.timeout_seconds: expected a positive finite number")
                remove = _strings(step.get("remove_compile_flags", []), f"{field}.remove_compile_flags")
                if any(not flag.startswith("-") or any(char.isspace() for char in flag) for flag in remove):
                    raise ConfigurationError(f"{field}.remove_compile_flags: expected standalone flag tokens")
                exclude = _strings(step.get("exclude_paths", []), f"{field}.exclude_paths")
                if any(Path(pattern).is_absolute() for pattern in exclude):
                    raise ConfigurationError(f"{field}.exclude_paths: expected project-relative patterns")
                config_type = ClangdLspStepConfig if step_type == "clangd_lsp" else ClangAstStepConfig
                steps.append(config_type(directory, executable, timeout, remove, exclude))
                continue
            if step_type != "clangd_yaml":
                raise ConfigurationError(f"{field}.type: unknown step type: {step_type}")
            _settings(step, {"type", "path", "recover_known_errors"}, field)
            if index != 0:
                raise ConfigurationError(f"{field}: clangd_yaml must be first and cannot be repeated")
            export_path = _path(step.get("path"), f"{field}.path", path.parent)
            recover = step.get("recover_known_errors", True)
            if not isinstance(recover, bool):
                raise ConfigurationError(f"{field}.recover_known_errors: expected a boolean")
            steps.append(ClangdYamlStepConfig(export_path, recover))

        return PipelineConfig(project_root, tuple(steps), database)
    except (ConfigurationError, OSError, UnicodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{path}: {error}") from error
