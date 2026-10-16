"""Read-only structural validation of a completed code-index snapshot."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 3
COLLECTION_STAGES = ("symbols", "references", "includes", "source_ranges", "metrics")


def validate_snapshot(connection: sqlite3.Connection) -> list[str]:
    """Return publication-blocking issues; do not modify the database or sources.

    This verifies stored structure and declared collection status, not whether
    a collector found every semantic relationship or used the correct source.
    """
    issues = []
    for (result,) in connection.execute("PRAGMA integrity_check"):
        if result != "ok":
            issues.append(f"Integrity check: {result}")
    for table, rowid, parent, _ in connection.execute("PRAGMA foreign_key_check"):
        issues.append(f"Foreign key: {table} row {rowid} references missing {parent}")

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        issues.append(f"Unsupported schema version {version}; expected {SCHEMA_VERSION}")
        return issues

    metadata = connection.execute(
        "SELECT schema_version, status, metrics_version FROM index_metadata"
    ).fetchall()
    if len(metadata) != 1:
        issues.append("Exactly one index_metadata row is required")
    else:
        schema_version, status, metrics_version = metadata[0]
        if schema_version != SCHEMA_VERSION:
            issues.append("Metadata schema version does not match the database")
        if status != "complete":
            issues.append(f"Snapshot is {status}, not complete")
        if metrics_version is None and connection.execute(
            "SELECT 1 FROM symbol_bodies LIMIT 1"
        ).fetchone():
            issues.append("Body metrics require index_metadata.metrics_version")

    for file_id, count in connection.execute(
        "SELECT f.file_id, count(c.stage) FROM files f "
        "LEFT JOIN collection_status c ON c.file_id = f.file_id "
        "GROUP BY f.file_id HAVING count(c.stage) <> ?",
        (len(COLLECTION_STAGES),),
    ):
        issues.append(
            f"File {file_id} has {count}/{len(COLLECTION_STAGES)} collection statuses"
        )
    for file_id, stage, status, error in connection.execute(
        "SELECT file_id, stage, status, error FROM collection_status "
        "WHERE status IN ('partial', 'failed') ORDER BY file_id, stage"
    ):
        issues.append(f"File {file_id} {stage}: {status}: {error}")

    if not connection.execute(
        "SELECT 1 FROM scopes WHERE scope_id = 1 AND qualified_name = '' "
        "AND owner_symbol_id IS NULL"
    ).fetchone():
        issues.append("The global scope (scope_id 1) is required")

    # Parentage is derived from semantic scope ownership, never stored twice.
    # Walk each parent chain once. Unlike a recursive ancestor query, this is
    # linear in the number of symbols and handles deep trees without recursion.
    parents = dict(connection.execute("SELECT symbol_id, parent_symbol_id FROM symbol_details"))
    visited = set()
    for start in parents:
        trail = []
        positions = {}
        current = start
        while current in parents and current not in visited and current not in positions:
            positions[current] = len(trail)
            trail.append(current)
            current = parents[current]
        if current in positions:
            cycle = trail[positions[current]:] + [current]
            issues.append("Symbol parent cycle: " + " -> ".join(map(str, cycle)))
        visited.update(trail)

    for (symbol_id,) in connection.execute(
        "SELECT b.symbol_id FROM symbol_bodies b WHERE NOT EXISTS ("
        "SELECT 1 FROM symbol_source_ranges r WHERE r.symbol_id = b.symbol_id "
        "AND r.location_id = b.body_location_id AND r.role = 'body')"
    ):
        issues.append(f"Symbol {symbol_id} metrics have no matching body source range")

    for symbol_id, location_id in connection.execute(
        "SELECT r.symbol_id, r.location_id FROM symbol_source_ranges r "
        "JOIN locations l ON l.location_id = r.location_id "
        "WHERE r.role = 'body' AND l.start_line = l.end_line "
        "AND l.start_column = l.end_column"
    ):
        issues.append(f"Symbol {symbol_id} has an empty body source range at {location_id}")

    for (symbol_id,) in connection.execute(
        "SELECT d.symbol_id FROM generated_documentation d "
        "JOIN symbols s ON s.symbol_id = d.symbol_id "
        "WHERE d.status = 'complete' AND s.name IS NULL"
    ):
        issues.append(f"Symbol {symbol_id} has completed documentation but is unresolved")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        uri = args.database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            # Keep all checks on the same read snapshot if another process writes.
            connection.execute("BEGIN")
            issues = validate_snapshot(connection)
    except (OSError, sqlite3.Error) as error:
        print(f"Validation failed: {error}")
        return 1
    if issues:
        for issue in issues:
            print(f"ERROR: {issue}")
        return 1
    print("Snapshot validation passed (structure and declared collection status).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
