import unittest
from unittest.mock import Mock

from examples.sequential_steps import make_example
from index_model import (
    GLOBAL_SCOPE_ID, IndexBuilder, IndexMetadata, IndexModel, LanguageId,
    ModelValidationError, SnapshotStatus, SymbolId,
)
from index_pipeline import Pipeline, Step
from model_store import ModelStore


def initial_model():
    builder = IndexBuilder()
    builder.set_metadata(IndexMetadata(project_root="/example"))
    builder.add_language(language_id=LanguageId(1), name="C++")
    return builder.build()


class AddSymbol(Step):
    def run(self, model: IndexModel) -> IndexModel:
        builder = IndexBuilder.from_model(model)
        builder.add_symbol(symbol_id=SymbolId(1), name="f", scope_id=GLOBAL_SCOPE_ID,
                           language_id=LanguageId(1))
        return builder.build()


class EnrichSymbol(Step):
    def __init__(self):
        self.received = None

    def run(self, model: IndexModel) -> IndexModel:
        self.received = model
        builder = IndexBuilder.from_model(model)
        builder.enrich_symbol(symbol_id=SymbolId(1), signature="()")
        return builder.build()


class PipelineTests(unittest.TestCase):
    def test_save_finishes_before_next_step_starts(self):
        store = Mock(spec=ModelStore)

        class CheckSaved(EnrichSymbol):
            def run(self, model):
                store.save.assert_called_once_with(model)
                return super().run(model)

        enrichment = CheckSaved()
        result = Pipeline([AddSymbol(), enrichment], store=store).run(initial_model())
        self.assertEqual(store.save.call_count, 2)
        self.assertIs(store.save.call_args.args[0], result)
        self.assertIs(store.save.call_args_list[0].args[0], enrichment.received)

    def test_save_failure_stops_pipeline_without_reporting_step_completion(self):
        failure = OSError("disk full")
        store = Mock(spec=ModelStore)
        store.save.side_effect = failure
        later = EnrichSymbol()
        with self.assertLogs("index_pipeline", level="INFO") as logs:
            with self.assertRaises(OSError) as caught:
                Pipeline([AddSymbol(), later], store=store).run(initial_model())
        self.assertIs(caught.exception, failure)
        self.assertIsNone(later.received)
        store.save.assert_called_once()
        self.assertFalse(any("Completed step" in record.getMessage() for record in logs.records))

    def test_failed_step_wrong_result_and_empty_pipeline_do_not_save(self):
        store = Mock(spec=ModelStore)
        for outcome in (None, RuntimeError("tool failed")):
            step = Mock(spec=Step)
            if isinstance(outcome, Exception):
                step.run.side_effect = outcome
            else:
                step.run.return_value = outcome
            with self.assertLogs("index_pipeline", level="ERROR"):
                with self.assertRaises((TypeError, RuntimeError)):
                    Pipeline([step], store=store).run(initial_model())
        Pipeline([], store=store).run(initial_model())
        store.save.assert_not_called()

    def test_order_snapshot_isolation_and_progress_logs(self):
        original = initial_model()
        enrichment = EnrichSymbol()
        steps = [AddSymbol(), enrichment]
        pipeline = Pipeline(steps)
        steps.reverse()  # Caller-owned list changes must not change configured order.
        with self.assertLogs("index_pipeline", level="INFO") as logs:
            result = pipeline.run(original)

        self.assertEqual(original.symbols, ())
        self.assertIsNone(enrichment.received.symbol(SymbolId(1)).signature)
        self.assertEqual(result.symbol(SymbolId(1)).signature, "()")
        self.assertIsNot(enrichment.received.symbol(SymbolId(1)), result.symbol(SymbolId(1)))
        self.assertEqual(result.metadata.status, SnapshotStatus.BUILDING)
        self.assertEqual([record.getMessage() for record in logs.records], [
            "Starting step 1/2: AddSymbol", "Completed step 1/2: AddSymbol",
            "Starting step 2/2: EnrichSymbol", "Completed step 2/2: EnrichSymbol",
        ])

    def test_failure_propagates_and_later_step_does_not_run(self):
        failure = RuntimeError("tool failed")
        later = EnrichSymbol()

        class FailingToolStep(Step):
            def run(self, model):
                self.received = model
                builder = IndexBuilder.from_model(model)
                builder.enrich_symbol(symbol_id=SymbolId(1), documentation="unpublished")
                raise failure

        failing = FailingToolStep()
        original = initial_model()
        with self.assertLogs("index_pipeline", level="INFO") as logs:
            with self.assertRaises(RuntimeError) as caught:
                Pipeline([AddSymbol(), failing, later]).run(original)
        self.assertIs(caught.exception, failure)
        self.assertIsNone(later.received)
        self.assertIsNone(failing.received.symbol(SymbolId(1)).documentation)
        self.assertEqual(original.symbols, ())
        self.assertEqual(logs.records[-1].getMessage(), "Failed step 2/3: FailingToolStep")
        self.assertIs(logs.records[-1].exc_info[1], failure)

    def test_failed_build_stops_pipeline_without_publishing_invalid_data(self):
        class InvalidStep(Step):
            def run(self, model):
                builder = IndexBuilder.from_model(model)
                builder.enrich_symbol(symbol_id=SymbolId(1), definition_location_id=999)
                return builder.build()

        later = EnrichSymbol()
        with self.assertLogs("index_pipeline", level="ERROR"):
            with self.assertRaises(ModelValidationError):
                Pipeline([AddSymbol(), InvalidStep(), later]).run(initial_model())
        self.assertIsNone(later.received)

    def test_wrong_return_type_stops_pipeline(self):
        class MissingReturnStep(Step):
            def run(self, model):
                pass

        later = EnrichSymbol()
        with self.assertLogs("index_pipeline", level="ERROR"):
            with self.assertRaisesRegex(TypeError, "MissingReturnStep.run must return an IndexModel"):
                Pipeline([MissingReturnStep(), later]).run(initial_model())
        self.assertIsNone(later.received)

    def test_empty_pipeline_returns_input_and_rejects_wrong_input_type(self):
        original = initial_model()
        self.assertIs(Pipeline([]).run(original), original)
        with self.assertRaises(TypeError):
            Pipeline([]).run(None)

    def test_step_requires_an_implementation(self):
        with self.assertRaises(TypeError):
            Step()

    def test_example_uses_two_tools_to_enrich_the_same_symbols(self):
        model = make_example()
        self.assertEqual([(symbol.symbol_id, symbol.name, symbol.signature) for symbol in model.symbols],
                         [(1, "leaf", "()"), (2, "root", "()")])


if __name__ == "__main__":
    unittest.main()
