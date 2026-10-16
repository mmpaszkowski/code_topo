-- Create a new snapshot database. Existing databases must be rebuilt.
-- Foreign keys must also be enabled on every subsequent writer connection,
-- before starting a transaction.
PRAGMA foreign_keys = ON;
PRAGMA user_version = 3;

-- Exactly one row is required by snapshot validation. IDs are snapshot-local.
-- Fingerprints are lowercase SHA-256 hex digests; timestamps are UTC ISO 8601.
CREATE TABLE index_metadata (
    metadata_id INTEGER PRIMARY KEY CHECK (metadata_id = 1),
    schema_version INTEGER NOT NULL DEFAULT 3 CHECK (schema_version = 3),
    project_root TEXT NOT NULL CHECK (project_root <> ''),
    source_revision TEXT CHECK (source_revision <> ''),
    build_input_hash TEXT CHECK (length(build_input_hash) = 64
        AND build_input_hash NOT GLOB '*[^0-9a-f]*'),
    metrics_version TEXT CHECK (metrics_version <> ''),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT CHECK (completed_at <> ''),
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building', 'complete', 'failed')),
    error TEXT CHECK (error <> ''),
    CHECK (status <> 'complete' OR (completed_at IS NOT NULL AND error IS NULL)),
    CHECK (status <> 'failed' OR error IS NOT NULL)
) STRICT;

-- Logical ownership, not build-target membership.
CREATE TABLE libraries (
    library_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (name <> '')
) STRICT;

CREATE TABLE files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    uri TEXT NOT NULL UNIQUE, -- percent-encoded file URI
    library_id INTEGER REFERENCES libraries(library_id), -- NULL when not assigned
    -- Digest of indexed file bytes; NULL when source was unavailable.
    content_hash TEXT CHECK (length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*')
) STRICT;

-- One row per file and stage, including explicitly unrequested stages.
-- Complete means the configured collector finished, not semantic exhaustiveness.
CREATE TABLE collection_status (
    file_id INTEGER NOT NULL REFERENCES files(file_id),
    stage TEXT NOT NULL CHECK (stage IN (
        'symbols', 'references', 'includes', 'source_ranges', 'metrics')),
    status TEXT NOT NULL CHECK (status IN (
        'not_requested', 'complete', 'partial', 'failed')),
    error TEXT CHECK (error <> ''),
    PRIMARY KEY (file_id, stage),
    CHECK (status NOT IN ('partial', 'failed') OR error IS NOT NULL),
    CHECK (status NOT IN ('not_requested', 'complete') OR error IS NULL)
) STRICT;

-- Direct resolved #include edges; individual directives are not stored here.
CREATE TABLE file_includes (
    include_id INTEGER PRIMARY KEY AUTOINCREMENT,
    includer_file_id INTEGER NOT NULL REFERENCES files(file_id),
    included_file_id INTEGER NOT NULL REFERENCES files(file_id),
    UNIQUE (includer_file_id, included_file_id)
) STRICT;

-- Zero-based lines and UTF-8 byte columns; end positions are exclusive.
-- Importers convert coordinates against the exact indexed source bytes.
CREATE TABLE locations (
    location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(file_id),
    start_line INTEGER NOT NULL CHECK (start_line >= 0),
    start_column INTEGER NOT NULL CHECK (start_column >= 0),
    end_line INTEGER NOT NULL CHECK (end_line >= 0),
    end_column INTEGER NOT NULL CHECK (end_column >= 0),
    CHECK (end_line > start_line OR (
        end_line = start_line AND end_column >= start_column
    )),
    UNIQUE (file_id, start_line, start_column, end_line, end_column)
) STRICT;

-- Application-owned kind names (e.g. 'function'), normalized by the importer.
CREATE TABLE symbol_kinds (
    kind_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (name <> '')
) STRICT;

-- Application-owned language names (e.g. 'C++'), normalized by the importer.
CREATE TABLE languages (
    language_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK (name <> '')
) STRICT;

-- Application-owned bits of symbol_references.kind; match with (kind & bit) <> 0.
-- Importers map source semantics explicitly rather than copying producer enums.
CREATE TABLE reference_kind_bits (
    bit INTEGER PRIMARY KEY CHECK (bit > 0 AND (bit & (bit - 1)) = 0),
    name TEXT NOT NULL UNIQUE
) STRICT;

INSERT INTO reference_kind_bits (bit, name) VALUES
    (1, 'declaration'), (2, 'definition'), (4, 'reference'),
    (8, 'spelled'), (16, 'call');

CREATE TABLE access_specifiers (
    access_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
) STRICT;

INSERT INTO access_specifiers (name) VALUES ('public'), ('protected'), ('private');

-- Semantic scopes have their own identity. Equal display names do not imply
-- equal scopes (e.g. anonymous namespaces in different files).
-- Each known owner introduces at most one scope; NULL means the owner is unknown.
-- The forward foreign key is resolved once symbols has been created below.
CREATE TABLE scopes (
    scope_id INTEGER PRIMARY KEY AUTOINCREMENT,
    qualified_name TEXT NOT NULL,
    owner_symbol_id INTEGER UNIQUE REFERENCES symbols(symbol_id),
    CHECK (
        (scope_id = 1 AND qualified_name = '' AND owner_symbol_id IS NULL)
        OR (scope_id <> 1 AND qualified_name <> '')
    )
) STRICT;

CREATE TABLE symbols (
    symbol_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    scope_id INTEGER REFERENCES scopes(scope_id), -- 1 is global; NULL for placeholders
    kind_id INTEGER REFERENCES symbol_kinds(kind_id), -- NULL when unknown
    language_id INTEGER REFERENCES languages(language_id),
    -- Preferred name locations for navigation, not complete source extents.
    declaration_location_id INTEGER
        REFERENCES locations(location_id),
    definition_location_id INTEGER REFERENCES locations(location_id),
    signature TEXT,
    template_specialization_args TEXT,
    documentation TEXT,
    return_type TEXT,
    CHECK (
        (name IS NOT NULL AND scope_id IS NOT NULL AND language_id IS NOT NULL)
        OR (coalesce(
            name, scope_id, kind_id, language_id,
            declaration_location_id, definition_location_id, signature,
            template_specialization_args, documentation, return_type) IS NULL)
    ),
    CHECK (signature <> '' AND template_specialization_args <> ''
        AND documentation <> '' AND return_type <> '')
) STRICT;

-- Insert after both sides of the scope/symbol foreign-key relationship exist.
INSERT INTO scopes (scope_id, qualified_name) VALUES (1, '');

-- Derived fields live in a read-only view, not in the stored symbol relation.
CREATE VIEW symbol_details AS
SELECT s.*, sc.qualified_name AS scope, sc.owner_symbol_id AS parent_symbol_id,
    CASE WHEN s.name IS NULL THEN 'placeholder' ELSE 'resolved' END AS resolution_state
FROM symbols s
LEFT JOIN scopes sc ON sc.scope_id = s.scope_id;

-- Complete source extents, independent of metric extraction. Multiple declarations
-- and definitions may be observed; name locations remain on symbols/references.
CREATE TABLE symbol_source_ranges (
    symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    location_id INTEGER NOT NULL REFERENCES locations(location_id),
    role TEXT NOT NULL CHECK (role IN ('declaration', 'definition', 'body')),
    PRIMARY KEY (symbol_id, location_id, role)
) STRICT;

-- Body metrics, 1:1 with symbols; filled by a separate analysis stage.
-- The selected body must also be registered in symbol_source_ranges.
-- The algorithm and traversal rules are identified by index_metadata.metrics_version.
-- else-if counts as an if, ?: does not; lambda statements count toward the enclosing body.
-- max_nesting_depth: nested if/switch/loop/try levels, 0 when there are none.
-- cyclomatic_complexity: 1 + ifs + loops + cases + catches + && + || + ?:.
CREATE TABLE symbol_bodies (
    symbol_id INTEGER PRIMARY KEY REFERENCES symbols(symbol_id),
    body_location_id INTEGER NOT NULL REFERENCES locations(location_id),
    statement_count INTEGER NOT NULL CHECK (statement_count >= 0),
    if_count INTEGER NOT NULL CHECK (if_count >= 0),
    switch_count INTEGER NOT NULL CHECK (switch_count >= 0),
    case_count INTEGER NOT NULL CHECK (case_count >= 0),
    loop_count INTEGER NOT NULL CHECK (loop_count >= 0),
    max_nesting_depth INTEGER NOT NULL CHECK (max_nesting_depth >= 0),
    cyclomatic_complexity INTEGER NOT NULL CHECK (cyclomatic_complexity >= 1),
    return_count INTEGER NOT NULL CHECK (return_count >= 0),
    goto_count INTEGER NOT NULL CHECK (goto_count >= 0),
    break_count INTEGER NOT NULL CHECK (break_count >= 0),
    continue_count INTEGER NOT NULL CHECK (continue_count >= 0),
    try_count INTEGER NOT NULL CHECK (try_count >= 0),
    catch_count INTEGER NOT NULL CHECK (catch_count >= 0),
    throw_count INTEGER NOT NULL CHECK (throw_count >= 0),
    parameter_count INTEGER NOT NULL CHECK (parameter_count >= 0),
    local_variable_count INTEGER NOT NULL CHECK (local_variable_count >= 0),
    lambda_count INTEGER NOT NULL CHECK (lambda_count >= 0),
    CHECK (case_count = 0 OR switch_count > 0),
    CHECK (catch_count = 0 OR try_count > 0),
    CHECK (max_nesting_depth = 0 OR if_count + switch_count + loop_count + try_count > 0)
) STRICT;

-- Physical line counts are derived from the selected half-open body range.
-- An end at column zero excludes that line; an empty range occupies zero lines.
CREATE VIEW symbol_body_metrics AS
SELECT b.*,
    CASE WHEN l.start_line = l.end_line AND l.start_column = l.end_column THEN 0
         ELSE l.end_line - l.start_line + CASE WHEN l.end_column > 0 THEN 1 ELSE 0 END
    END AS line_count
FROM symbol_bodies b
JOIN locations l ON l.location_id = b.body_location_id;

CREATE TABLE symbol_references (
    reference_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    -- Bitmask of reference_kind_bits.
    kind INTEGER NOT NULL CHECK (kind BETWEEN 1 AND 31),
    location_id INTEGER NOT NULL REFERENCES locations(location_id),
    container_symbol_id INTEGER REFERENCES symbols(symbol_id) -- NULL outside any symbol
) STRICT;

-- Direct base classes. access_id and is_virtual are NULL when the source does not provide them.
CREATE TABLE class_inheritance (
    inheritance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    derived_symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    access_id INTEGER REFERENCES access_specifiers(access_id),
    is_virtual INTEGER CHECK (is_virtual IN (0, 1)),
    CHECK (base_symbol_id <> derived_symbol_id),
    UNIQUE (base_symbol_id, derived_symbol_id)
) STRICT;

CREATE TABLE method_overrides (
    override_id INTEGER PRIMARY KEY AUTOINCREMENT,
    overridden_symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    overriding_symbol_id INTEGER NOT NULL REFERENCES symbols(symbol_id),
    CHECK (overridden_symbol_id <> overriding_symbol_id),
    UNIQUE (overridden_symbol_id, overriding_symbol_id)
) STRICT;

-- One current generated document per symbol; source comments stay on symbols.
-- Hashes cover the exact source input and dependency context supplied to generation.
-- Rows belong to this snapshot and are not automatically reused after a rebuild.
CREATE TABLE generated_documentation (
    symbol_id INTEGER PRIMARY KEY REFERENCES symbols(symbol_id),
    comment TEXT CHECK (trim(comment) <> ''),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'complete', 'failed', 'stale')),
    model TEXT CHECK (model <> ''),
    prompt_version TEXT CHECK (prompt_version <> ''),
    source_input_hash TEXT CHECK (length(source_input_hash) = 64
        AND source_input_hash NOT GLOB '*[^0-9a-f]*'),
    dependency_context_hash TEXT CHECK (length(dependency_context_hash) = 64
        AND dependency_context_hash NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at TEXT CHECK (completed_at <> ''),
    error TEXT CHECK (error <> ''),
    CHECK (status <> 'complete' OR (
        comment IS NOT NULL AND model IS NOT NULL AND prompt_version IS NOT NULL
        AND source_input_hash IS NOT NULL AND dependency_context_hash IS NOT NULL
        AND completed_at IS NOT NULL AND error IS NULL)),
    CHECK (status <> 'failed' OR error IS NOT NULL)
) STRICT;

CREATE INDEX files_by_library ON files(library_id);
CREATE INDEX file_includes_by_included ON file_includes(included_file_id);
CREATE INDEX scopes_by_name ON scopes(qualified_name);
CREATE INDEX symbols_by_name ON symbols(name, scope_id);
CREATE INDEX symbols_by_scope ON symbols(scope_id, name);
CREATE INDEX symbols_by_kind ON symbols(kind_id);
CREATE INDEX symbols_by_declaration ON symbols(declaration_location_id);
CREATE INDEX symbols_by_definition ON symbols(definition_location_id);
CREATE INDEX source_ranges_by_location ON symbol_source_ranges(location_id);
-- NULL containers need their own uniqueness rule: SQLite treats NULLs as distinct.
CREATE UNIQUE INDEX references_unique_with_container
    ON symbol_references(target_symbol_id, location_id, kind, container_symbol_id)
    WHERE container_symbol_id IS NOT NULL;
CREATE UNIQUE INDEX references_unique_without_container
    ON symbol_references(target_symbol_id, location_id, kind)
    WHERE container_symbol_id IS NULL;
CREATE INDEX references_by_target ON symbol_references(target_symbol_id);
CREATE INDEX references_by_container ON symbol_references(container_symbol_id);
CREATE INDEX references_by_location ON symbol_references(location_id);
CREATE INDEX inheritance_by_derived ON class_inheritance(derived_symbol_id);
CREATE INDEX overrides_by_overriding ON method_overrides(overriding_symbol_id);
