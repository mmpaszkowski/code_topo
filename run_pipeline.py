"""Run configured index enrichment steps, optionally save to SQLite, and print a summary."""

import argparse
from collections.abc import Sequence
import logging
from pathlib import Path
import sys

from application_factory import create_application
from index_model import IndexModel
from index_pipeline import Step
from pipeline_config import ConfigurationError, load_config


logger = logging.getLogger(__name__)


def print_summary(model: IndexModel, steps: Sequence[Step]) -> None:
    """Print model counts and the summary values supplied by each step."""
    print("Pipeline completed")
    print(f"Project: {model.metadata.project_root}")
    print(f"Snapshot status: {model.metadata.status.value}")
    print(f"Files: {len(model.files)}")
    print(f"Symbols: {len(model.symbols)}")
    print(f"Locations: {len(model.locations)}")
    print(f"References: {len(model.symbol_references)}")
    print(f"Inheritance relations: {len(model.class_inheritance)}")
    print(f"Overrides: {len(model.method_overrides)}")

    for step in steps:
        for item in step.summary():
            print(f"{item.label}: {item.value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"),
                        help="configuration file (default: config.yaml in the current directory)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        config = load_config(args.config)
    except ConfigurationError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    try:
        application = create_application(config)
        model = application.run()
        print_summary(model, application.steps)
        if config.database is not None and config.database.save_after_each_step:
            print(f"Database: {config.database.path}")
    except Exception as error:
        # Pipeline already logs the failed step and its traceback.
        logger.error("Pipeline failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
