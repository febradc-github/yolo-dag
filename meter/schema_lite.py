"""A minimal JSON Schema (draft 2020-12 subset) validator, stdlib only.

No third-party `jsonschema` package is used anywhere in this plugin (see
meter-handoff.md Section 5.3: "Python 3.11+, standard library only"). This
covers exactly the keywords the schemas under `meter/schemas/` actually use:
`type` (single or list), `const`, `enum`, `required`, `properties`,
`additionalProperties: false`, `items`, `minItems`, `maxItems`. It is not a
general-purpose validator and should not be extended past what a schema in
this package actually needs — anything more is scope the stdlib-only
constraint was meant to avoid.
"""

from __future__ import annotations

from typing import Any

_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
}


def validate(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Returns a list of human-readable error strings; empty means valid."""
    errors: list[str] = []

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}, got {instance!r}")
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']!r}")
        return errors

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        py_types = tuple(_TYPE_MAP[t] for t in types if t in _TYPE_MAP)
        # bool is a subclass of int in Python; only accept bool for "boolean".
        if isinstance(instance, bool) and "boolean" not in types:
            errors.append(f"{path}: expected {types}, got boolean")
            return errors
        if py_types and not isinstance(instance, py_types):
            errors.append(f"{path}: expected {types}, got {type(instance).__name__}")
            return errors

    if isinstance(instance, dict) and schema.get("type") in ("object", None) and "properties" in schema:
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: missing required field {req!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {key!r}")

    if isinstance(instance, list) and "items" in schema:
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(instance) < min_items:
            errors.append(f"{path}: expected at least {min_items} item(s), got {len(instance)}")
        if max_items is not None and len(instance) > max_items:
            errors.append(f"{path}: expected at most {max_items} item(s), got {len(instance)}")
        item_schema = schema["items"]
        for i, item in enumerate(instance):
            errors.extend(validate(item, item_schema, f"{path}[{i}]"))

    return errors
