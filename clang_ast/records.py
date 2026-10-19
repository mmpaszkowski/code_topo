"""Adapter-owned protocol records; none are persisted in the domain model."""
from dataclasses import dataclass
from index_model import BodyMetrics


@dataclass(frozen=True)
class Extent:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Body:
    usr: str
    body: Extent
    definition: Extent | None
    metrics: BodyMetrics


@dataclass(frozen=True)
class Call:
    target_usr: str
    container_usr: str
    location: Extent


@dataclass(frozen=True)
class Issue:
    context: str
    path: str
    message: str
    stage: str = "references"
    severity: str = "gap"


@dataclass(frozen=True)
class Result:
    files: tuple[tuple[str, str], ...]
    bodies: tuple[Body, ...]
    calls: tuple[Call, ...]
    issues: tuple[Issue, ...]
    ok: bool
    declarations: tuple["Declaration", ...] = ()
    inheritance: tuple["Inheritance", ...] = ()
    overrides: tuple["Override", ...] = ()


@dataclass(frozen=True)
class Report:
    contexts_processed: int
    contexts_failed: int
    symbols_matched: int
    symbols_unmatched: int
    bodies_added: int
    calls_confirmed: int
    conflicts: int
    elapsed_seconds: float
    diagnostics: tuple[Issue, ...]
    placeholders_resolved: int = 0
    scope_owners_filled: int = 0
    symbol_fields_filled: int = 0
    inheritance_enriched: int = 0
    overrides_added: int = 0
    eligibility: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Declaration:
    usr: str
    name: str
    kind: str
    language: str
    parent_usr: str
    scope: str
    path: str
    declaration: Extent | None
    definition: Extent | None
    signature: str
    return_type: str
    template_arguments: str
    documentation: str
    eligibility: str


@dataclass(frozen=True)
class Inheritance:
    base_usr: str
    derived_usr: str
    path: str
    access: str
    virtual: bool
    existing_edge_only: bool = False


@dataclass(frozen=True)
class Override:
    base_usr: str
    derived_usr: str
    path: str
