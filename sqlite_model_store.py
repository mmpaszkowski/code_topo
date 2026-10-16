"""Save one complete model at a time using the existing SQLite schema."""

from collections.abc import Iterable
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import stat

from index_model import IndexModel, ModelValidationError, SCHEMA_VERSION, validate
from model_store import ModelStore


logger = logging.getLogger(__name__)


def _schema_objects(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY type, name"
    ).fetchall()


def _insert(connection: sqlite3.Connection, table: str, columns: str, rows: Iterable[tuple]) -> None:
    # Table and column names below are code constants; only values come from the model.
    parameters = ", ".join("?" for _ in columns.split(","))
    connection.executemany(f"INSERT INTO {table} ({columns}) VALUES ({parameters})", rows)


class SQLiteModelStore(ModelStore):
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(":memory:")) as reference:
            reference.executescript(self._schema)
            self._expected_schema = _schema_objects(reference)
        self._tables = tuple(name for kind, name, _, _ in self._expected_schema if kind == "table")

    def check_destination(self, project_root: str) -> None:
        """Check an existing database without creating or changing any files."""
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"Database directory does not exist: {self.path.parent}")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Database must be a regular file: {self.path}")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            self._check_compatible(connection, project_root)

    def _check_compatible(self, connection: sqlite3.Connection, project_root: str) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION or _schema_objects(connection) != self._expected_schema:
            raise ValueError(f"Database schema does not match schema.sql: {self.path}")
        metadata = connection.execute(
            "SELECT metadata_id, schema_version, project_root FROM index_metadata"
        ).fetchall()
        if metadata != [(1, SCHEMA_VERSION, project_root)]:
            raise ValueError(f"Database metadata must identify the same project ({project_root}): {self.path}")

    def save(self, model: IndexModel) -> None:
        logger.info("Saving model to %s", self.path)
        created_file = None
        try:
            if not isinstance(model, IndexModel):
                raise TypeError("SQLiteModelStore.save requires an IndexModel")
            issues = validate(model)
            if issues:
                raise ModelValidationError(issues)
            self.check_destination(model.metadata.project_root)
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass  # Open without creation, and recheck compatibility under the write lock.
            else:
                try:
                    created_file = os.fstat(descriptor)
                finally:
                    os.close(descriptor)

            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                         isolation_level=None)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                if created_file is not None:
                    connection.executescript(self._schema)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if created_file is None:
                        self._check_compatible(connection, model.metadata.project_root)
                    connection.execute("PRAGMA defer_foreign_keys = ON")
                    for table in self._tables:
                        connection.execute(f"DELETE FROM {table}")
                    connection.execute("DELETE FROM sqlite_sequence")
                    self._write_model(connection, model)
                    self._check_integrity(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except BaseException as error:
            if created_file is not None:
                # Never remove a pre-existing database, or a different file at this path.
                try:
                    current = self.path.lstat()
                    if (current.st_dev, current.st_ino) == (created_file.st_dev, created_file.st_ino):
                        self.path.unlink()
                except FileNotFoundError:
                    pass
            logger.error("Failed to save model to %s: %s", self.path, error)
            raise
        logger.info("Saved model to %s", self.path)

    @staticmethod
    def _check_integrity(connection: sqlite3.Connection) -> None:
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise sqlite3.IntegrityError(f"Foreign key check failed: {foreign_keys}")
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise sqlite3.IntegrityError(f"Database integrity check failed: {integrity}")

    @staticmethod
    def _write_model(connection: sqlite3.Connection, model: IndexModel) -> None:
        """Flatten nested model links into explicit SQL columns and local IDs."""
        metadata = model.metadata
        _insert(connection, "index_metadata",
                "metadata_id, schema_version, project_root, source_revision, build_input_hash, "
                "metrics_version, created_at, completed_at, status, error", [(
                    metadata.metadata_id, metadata.schema_version, metadata.project_root,
                    metadata.source_revision, metadata.build_input_hash, metadata.metrics_version,
                    metadata.created_at, metadata.completed_at, metadata.status.value, metadata.error,
                )])
        _insert(connection, "libraries", "library_id, name",
                ((row.library_id, row.name) for row in model.libraries))
        _insert(connection, "files", "file_id, uri, library_id, content_hash", (
            (row.file_id, row.uri, row.library.library_id if row.library else None, row.content_hash)
            for row in model.files
        ))
        _insert(connection, "locations", "location_id, file_id, start_line, start_column, end_line, end_column", (
            (row.location_id, row.file.file_id, row.start.line, row.start.column, row.end.line, row.end.column)
            for row in model.locations
        ))
        _insert(connection, "symbol_kinds", "kind_id, name",
                ((row.kind_id, row.name) for row in model.symbol_kinds))
        _insert(connection, "languages", "language_id, name",
                ((row.language_id, row.name) for row in model.languages))
        _insert(connection, "access_specifiers", "access_id, name",
                ((row.access_id, row.name) for row in model.access_specifiers))
        _insert(connection, "reference_kind_bits", "bit, name",
                ((row.bit, row.name) for row in model.reference_kind_bits))
        _insert(connection, "scopes", "scope_id, qualified_name, owner_symbol_id", (
            (row.scope_id, row.qualified_name, row.owner.symbol_id if row.owner else None)
            for row in model.scopes
        ))
        _insert(connection, "symbols", "symbol_id, name, scope_id, kind_id, language_id, "
                "declaration_location_id, definition_location_id, signature, template_specialization_args, "
                "documentation, return_type", (
            (row.symbol_id, row.name, row.scope.scope_id if row.scope else None,
             row.kind.kind_id if row.kind else None, row.language.language_id if row.language else None,
             row.declaration.location_id if row.declaration else None,
             row.definition.location_id if row.definition else None,
             row.signature, row.template_specialization_args, row.documentation, row.return_type)
            for row in model.symbols
        ))
        _insert(connection, "file_includes", "include_id, includer_file_id, included_file_id", (
            (row.include_id, row.includer.file_id, row.included.file_id) for row in model.file_includes
        ))
        _insert(connection, "symbol_references", "reference_id, target_symbol_id, kind, location_id, container_symbol_id", (
            (row.reference_id, row.target.symbol_id, int(row.kind), row.location.location_id,
             row.container.symbol_id if row.container else None) for row in model.symbol_references
        ))
        _insert(connection, "class_inheritance", "inheritance_id, base_symbol_id, derived_symbol_id, access_id, is_virtual", (
            (row.inheritance_id, row.base.symbol_id, row.derived.symbol_id,
             row.access.access_id if row.access else None, row.is_virtual) for row in model.class_inheritance
        ))
        _insert(connection, "method_overrides", "override_id, overridden_symbol_id, overriding_symbol_id", (
            (row.override_id, row.overridden.symbol_id, row.overriding.symbol_id) for row in model.method_overrides
        ))
        _insert(connection, "symbol_source_ranges", "symbol_id, location_id, role", (
            (row.symbol.symbol_id, row.location.location_id, row.role.value) for row in model.symbol_source_ranges
        ))
        _insert(connection, "symbol_bodies", "symbol_id, body_location_id, statement_count, if_count, "
                "switch_count, case_count, loop_count, max_nesting_depth, cyclomatic_complexity, "
                "return_count, goto_count, break_count, continue_count, try_count, catch_count, "
                "throw_count, parameter_count, local_variable_count, lambda_count", (
            (row.symbol.symbol_id, row.body.location_id, row.metrics.statement_count, row.metrics.if_count,
             row.metrics.switch_count, row.metrics.case_count, row.metrics.loop_count, row.metrics.max_nesting_depth,
             row.metrics.cyclomatic_complexity, row.metrics.return_count, row.metrics.goto_count,
             row.metrics.break_count, row.metrics.continue_count, row.metrics.try_count, row.metrics.catch_count,
             row.metrics.throw_count, row.metrics.parameter_count, row.metrics.local_variable_count,
             row.metrics.lambda_count) for row in model.symbol_bodies
        ))
        _insert(connection, "collection_status", "file_id, stage, status, error", (
            (row.file.file_id, row.stage.value, row.status.value, row.error) for row in model.collection_statuses
        ))
        _insert(connection, "generated_documentation", "symbol_id, comment, status, model, prompt_version, "
                "source_input_hash, dependency_context_hash, created_at, completed_at, error", (
            (row.symbol.symbol_id, row.comment, row.status.value, row.model, row.prompt_version,
             row.source_input_hash, row.dependency_context_hash, row.created_at, row.completed_at, row.error)
            for row in model.generated_documentation
        ))
