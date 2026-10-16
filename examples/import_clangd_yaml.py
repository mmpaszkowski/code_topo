"""Build an in-memory snapshot from a clangd YAML export and print its summary."""

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path

from clangd_import import ClangdYamlReader, ClangdYamlStep, LocalSourceReader
from index_model import IndexBuilder, IndexMetadata
from index_pipeline import Pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("yaml_path", type=Path)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    builder = IndexBuilder()
    builder.set_metadata(IndexMetadata(project_root=str(args.project_root.absolute())))
    step = ClangdYamlStep(ClangdYamlReader(args.yaml_path), LocalSourceReader())
    model = Pipeline([step]).run(builder.build())
    report = step.report
    assert report is not None
    print(json.dumps({
        "counts": asdict(report.counts),
        "yaml_sha256": report.yaml_sha256,
        "diagnostics": len(report.diagnostics),
        "source_assumption": report.source_assumption,
        "snapshot_status": model.metadata.status.value,
    }, indent=2))


if __name__ == "__main__":
    main()
