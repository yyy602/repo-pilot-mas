"""Small JSON Schema validator for model responses and tool arguments."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when a JSON-compatible value violates a supported schema rule."""


def validate_json_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate the JSON Schema subset used by RepoPilot-MAS protocols."""

    if "oneOf" in schema:
        matches = 0
        errors: list[str] = []
        for candidate in schema["oneOf"]:
            try:
                validate_json_schema(value, candidate, path=path)
                matches += 1
            except SchemaValidationError as exc:
                errors.append(str(exc))
        if matches != 1:
            raise SchemaValidationError(
                f"{path}: expected exactly one matching schema, got {matches}; "
                + "; ".join(errors[:2])
            )
        return

    for candidate in schema.get("allOf", ()):
        validate_json_schema(value, candidate, path=path)

    if "const" in schema and value != schema["const"]:
        raise SchemaValidationError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path}: expected one of {schema['enum']!r}")

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, str(expected_type)):
        raise SchemaValidationError(f"{path}: expected {expected_type}, got {type(value).__name__}")

    if expected_type == "object":
        assert isinstance(value, Mapping)
        required = schema.get("required", ())
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                validate_json_schema(item, properties[key], path=child_path)
            elif not additional_allowed:
                raise SchemaValidationError(f"{child_path}: additional property is not allowed")

    if expected_type == "array":
        assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise SchemaValidationError(f"{path}: expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaValidationError(f"{path}: expected at most {schema['maxItems']} items")
        if schema.get("uniqueItems") is True:
            serialized = [repr(item) for item in value]
            if len(serialized) != len(set(serialized)):
                raise SchemaValidationError(f"{path}: expected unique items")
        if "contains" in schema:
            matches = 0
            for index, item in enumerate(value):
                try:
                    validate_json_schema(
                        item,
                        schema["contains"],
                        path=f"{path}[{index}]",
                    )
                    matches += 1
                except SchemaValidationError:
                    pass
            minimum = int(schema.get("minContains", 1))
            maximum = schema.get("maxContains")
            if matches < minimum or (
                maximum is not None and matches > int(maximum)
            ):
                expected = (
                    f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
                )
                raise SchemaValidationError(
                    f"{path}: expected contains matches {expected}, got {matches}"
                )
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, path=f"{path}[{index}]")

    if expected_type == "string":
        assert isinstance(value, str)
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise SchemaValidationError(f"{path}: expected length >= {schema['minLength']}")
        if "pattern" in schema and re.fullmatch(str(schema["pattern"]), value) is None:
            raise SchemaValidationError(f"{path}: string does not match required pattern")

    if expected_type in {"integer", "number"}:
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(value, float) and not math.isfinite(value):
            raise SchemaValidationError(f"{path}: expected a finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"{path}: expected value >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"{path}: expected value <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise SchemaValidationError(f"{path}: expected value > {schema['exclusiveMinimum']}")


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, Mapping)
    if expected_type == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    raise SchemaValidationError(f"unsupported schema type: {expected_type}")
