"""Build two functions and a call edge without a database or indexing tool.

Run from the repository root: python3 -m examples.two_functions
"""

from hashlib import sha256

from index_model import (
    CollectionStage, CollectionState, FileId, GLOBAL_SCOPE_ID, IndexBuilder,
    IndexMetadata, IndexModel, LanguageId, LocationId, Position, ReferenceId,
    ReferenceKind, SymbolId,
)


def make_example() -> IndexModel:
    source = b"int leaf() { return 1; }\nint root() { return leaf(); }\n"
    builder = IndexBuilder()
    builder.set_metadata(IndexMetadata(project_root="/example"))
    builder.add_language(language_id=LanguageId(1), name="C++")
    builder.add_file(file_id=FileId(1), uri="file:///example/main.cpp",
                     content_hash=sha256(source).hexdigest())
    for stage in CollectionStage:
        state = (CollectionState.COMPLETE if stage in (CollectionStage.SYMBOLS, CollectionStage.REFERENCES)
                 else CollectionState.NOT_REQUESTED)
        builder.add_collection_status(file_id=FileId(1), stage=stage, status=state)

    # A relationship can arrive before either of its symbols or its location.
    builder.add_symbol_reference(reference_id=ReferenceId(1), target_symbol_id=SymbolId(1),
        container_symbol_id=SymbolId(2), location_id=LocationId(3),
        kind=ReferenceKind.REFERENCE | ReferenceKind.SPELLED | ReferenceKind.CALL)
    builder.add_symbol(symbol_id=SymbolId(1))
    builder.resolve_symbol(symbol_id=SymbolId(1), name="leaf", scope_id=GLOBAL_SCOPE_ID,
        language_id=LanguageId(1), definition_location_id=LocationId(1), signature="()", return_type="int")
    builder.add_symbol(symbol_id=SymbolId(2), name="root", scope_id=GLOBAL_SCOPE_ID,
        language_id=LanguageId(1), definition_location_id=LocationId(2), signature="()", return_type="int")

    lines = source.splitlines()
    for location_id, line, word in ((1, 0, b"leaf"), (2, 1, b"root"), (3, 1, b"leaf")):
        start = lines[line].index(word)
        builder.add_location(location_id=LocationId(location_id), file_id=FileId(1),
            start=Position(line, start), end=Position(line, start + len(word)))
    return builder.build()


if __name__ == "__main__":
    index = make_example()
    for call in index.symbol_references:
        print(f"{call.container.name} -> {call.target.name} at "
              f"{call.location.file.uri}:{call.location.start.line + 1}")
