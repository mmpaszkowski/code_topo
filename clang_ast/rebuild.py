"""Replay domain rows through public builder methods after adapter-local changes.

This is deliberately adapter-owned: no producer state or mutation API is added
into the immutable model or its builder. Forward references are resolved by build().
"""
from dataclasses import fields
from index_model import IndexBuilder

# Explicit nested-link translations match the existing public builder methods.
LINKS = {
    'files': {'library': ('library_id', 'library_id')},
    'locations': {'file': ('file_id', 'file_id')},
    'scopes': {'owner': ('owner_symbol_id', 'symbol_id')},
    'symbols': {'scope': ('scope_id', 'scope_id'), 'kind': ('kind_id', 'kind_id'),
                'language': ('language_id', 'language_id'),
                'declaration': ('declaration_location_id', 'location_id'),
                'definition': ('definition_location_id', 'location_id')},
    'file_includes': {'includer': ('includer_file_id', 'file_id'), 'included': ('included_file_id', 'file_id')},
    'symbol_references': {'target': ('target_symbol_id', 'symbol_id'), 'location': ('location_id', 'location_id'),
                          'container': ('container_symbol_id', 'symbol_id')},
    'class_inheritance': {'base': ('base_symbol_id', 'symbol_id'), 'derived': ('derived_symbol_id', 'symbol_id'),
                          'access': ('access_id', 'access_id')},
    'method_overrides': {'overridden': ('overridden_symbol_id', 'symbol_id'), 'overriding': ('overriding_symbol_id', 'symbol_id')},
    'symbol_source_ranges': {'symbol': ('symbol_id', 'symbol_id'), 'location': ('location_id', 'location_id')},
    'symbol_bodies': {'symbol': ('symbol_id', 'symbol_id'), 'body': ('body_location_id', 'location_id')},
    'collection_statuses': {'file': ('file_id', 'file_id')},
    'generated_documentation': {'symbol': ('symbol_id', 'symbol_id')},
}
METHODS = dict(zip(
    ('libraries', 'files', 'locations', 'symbol_kinds', 'languages', 'access_specifiers',
     'reference_kind_bits', 'scopes', 'symbols', 'file_includes', 'symbol_references',
     'class_inheritance', 'method_overrides', 'symbol_source_ranges', 'symbol_bodies',
     'collection_statuses', 'generated_documentation'),
    ('library', 'file', 'location', 'symbol_kind', 'language', 'access_specifier',
     'reference_kind_bit', 'scope', 'symbol', 'file_include', 'symbol_reference',
     'class_inheritance', 'method_override', 'symbol_source_range', 'symbol_body',
     'collection_status', 'generated_documentation')))


def rows(model):
    result = {}
    for collection in METHODS:
        translated = []
        for record in getattr(model, collection):
            row = {}
            for field in fields(record):
                value = getattr(record, field.name)
                link = LINKS.get(collection, {}).get(field.name)
                if link:
                    row[link[0]] = getattr(value, link[1]) if value is not None else None
                else:
                    row[field.name] = value
            translated.append(row)
        result[collection] = translated
    return result


def build(metadata, data):
    builder = IndexBuilder()
    builder.set_metadata(metadata)
    for collection, method in METHODS.items():
        for row in data[collection]:
            if collection == 'scopes' and row['scope_id'] == 1:
                continue
            getattr(builder, 'add_' + method)(**row)
    return builder.build()
