#!/usr/bin/env python3
"""Minimal, dependency-free JSON-Schema validation for enrichment output.

The `LLMClient.parse` contract returns a schema-valid object or raises (ADR-0026). Adapters
ask the provider for native schema-constrained decoding, but the client re-validates the
returned object here as defence in depth — output that does not validate is dropped, never
surfaced. This covers the small subset of JSON Schema the enrichment schemas use:
object/array/string/integer/number/boolean/null, `properties`, `required`,
`additionalProperties: false`, `items`, and `enum`.
"""
from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    """Raised when a value does not conform to its schema."""


def validate(value: Any, schema: dict[str, Any], *, path: str = "$") -> Any:
    """Validate ``value`` against ``schema``; return it unchanged or raise SchemaError."""
    expected = schema.get("type")

    if expected == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected object, got {type(value).__name__}")
        props: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required property '{key}'")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path}: unexpected propertie(s): {sorted(extra)}")
        for key, subschema in props.items():
            if key in value:
                validate(value[key], subschema, path=f"{path}.{key}")
    elif expected == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path}: expected array, got {type(value).__name__}")
        items = schema.get("items")
        if items:
            for i, element in enumerate(value):
                validate(element, items, path=f"{path}[{i}]")
    elif expected == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: expected string, got {type(value).__name__}")
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise SchemaError(f"{path}: expected integer, got {type(value).__name__}")
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SchemaError(f"{path}: expected number, got {type(value).__name__}")
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: expected boolean, got {type(value).__name__}")
    elif expected == "null":
        if value is not None:
            raise SchemaError(f"{path}: expected null")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in enum {schema['enum']}")
    return value
