"""JSON Schema validation for the request documents shipped with the spec."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from cas.domain.errors import Unprocessable

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "docs" / "schemas"
FILES = {"goal": "goal-contract.schema.json", "contract": "coordination-contract.schema.json"}


@lru_cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / FILES[name]).read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate(name: str, doc: object) -> None:
    errors = sorted(_validator(name).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        raise Unprocessable(
            f"{name} document does not match its schema: {errors[0].message}",
            code="SCHEMA_VIOLATION",
            details={"errors": [{"path": "/".join(str(p) for p in e.absolute_path), "message": e.message}
                                for e in errors[:20]]},
        )
