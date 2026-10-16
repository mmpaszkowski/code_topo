"""Read a small YAML configuration into immutable application settings."""

from dataclasses import dataclass
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
class DatabaseConfig:
    path: Path
    save_after_each_step: bool = False


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    project_root: Path
    steps: tuple[ClangdYamlStepConfig, ...]
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
            step = _settings(raw_step, {"type", "path", "recover_known_errors"}, field)
            step_type = _text(step.get("type"), f"{field}.type")
            if step_type != "clangd_yaml":
                raise ConfigurationError(f"{field}.type: unknown step type: {step_type}")
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
