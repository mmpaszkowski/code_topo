"""Sequential enrichment using two fixture tools with normalized application IDs.

These tools supply example data, not real source analysis.
Run from the repository root: python3 -m examples.sequential_steps
"""

import logging

from index_model import (
    GLOBAL_SCOPE_ID, IndexBuilder, IndexMetadata, IndexModel, LanguageId, SymbolId,
)
from index_pipeline import Pipeline, Step


class FixtureSymbolTool:
    def collect(self) -> tuple[tuple[SymbolId, str], ...]:
        return ((SymbolId(1), "leaf"), (SymbolId(2), "root"))


class FixtureSignatureTool:
    def signature(self, symbol_id: SymbolId) -> str | None:
        return {SymbolId(1): "()", SymbolId(2): "()"}.get(symbol_id)


class CollectSymbolsStep(Step):
    def __init__(self, tool: FixtureSymbolTool, language_id: LanguageId) -> None:
        self._tool = tool
        self._language_id = language_id

    def run(self, model: IndexModel) -> IndexModel:
        model.language(self._language_id)  # Required lookup must already exist.
        builder = IndexBuilder.from_model(model)
        for symbol_id, name in self._tool.collect():
            builder.add_symbol(symbol_id=symbol_id, name=name,
                               scope_id=GLOBAL_SCOPE_ID, language_id=self._language_id)
        return builder.build()


class CollectSignaturesStep(Step):
    def __init__(self, tool: FixtureSignatureTool) -> None:
        self._tool = tool

    def run(self, model: IndexModel) -> IndexModel:
        builder = IndexBuilder.from_model(model)
        for symbol in model.symbols:
            if symbol.name is not None:
                builder.enrich_symbol(symbol_id=symbol.symbol_id,
                                      signature=self._tool.signature(symbol.symbol_id))
        return builder.build()


def make_example() -> IndexModel:
    builder = IndexBuilder()
    builder.set_metadata(IndexMetadata(project_root="/example"))
    language_id = LanguageId(1)
    builder.add_language(language_id=language_id, name="C++")
    pipeline = Pipeline([
        CollectSymbolsStep(FixtureSymbolTool(), language_id),
        CollectSignaturesStep(FixtureSignatureTool()),
    ])
    return pipeline.run(builder.build())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for symbol in make_example().symbols:
        print(f"{symbol.name}{symbol.signature}")
