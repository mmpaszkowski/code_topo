"""Structured model validation errors."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    entity: str
    field: str
    message: str


class ModelValidationError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]):
        self.issues = tuple(issues)
        super().__init__("; ".join(
            f"{issue.entity}.{issue.field}: {issue.message}" for issue in self.issues
        ))


def require(condition: bool, entity: object, field: str, message: str) -> None:
    if not condition:
        raise ModelValidationError((ValidationIssue(type(entity).__name__, field, message),))


def integer(entity: object, field: str, value: object, minimum: int = -(2**63)) -> None:
    require(type(value) is int and minimum <= value < 2**63, entity, field,
            f"expected an integer between {minimum} and {2**63 - 1} (not bool)")


def text(entity: object, field: str, value: object, *, optional: bool = False,
         nonempty: bool = False) -> None:
    if value is None and optional:
        return
    require(isinstance(value, str), entity, field, "expected text")
    require(not nonempty or value != "", entity, field, "must not be empty")


def digest(entity: object, field: str, value: object) -> None:
    if value is None:
        return
    require(isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value),
            entity, field, "expected a lowercase SHA-256 hex digest or None")


def instance(entity: object, field: str, value: object, cls: type,
             *, optional: bool = False) -> None:
    require((optional and value is None) or type(value) is cls, entity, field,
            f"expected {cls.__name__}" + (" or None" if optional else ""))
