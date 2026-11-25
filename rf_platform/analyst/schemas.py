"""JSON Schemas for investigation output, plus a small dependency-free validator.

A report separates four kinds of statement:
  observed_facts   - values read from captured evidence (snapshot/entity/event/sensor refs)
  computed_results - values produced by a deterministic read-only tool (computed:* refs)
  hypotheses       - candidate explanations with supporting/weakening refs, never facts
  recommendations  - proposed next steps; Phase 1 never executes them
"""
from __future__ import annotations

from typing import Any

SCALAR = {"type": ["string", "number", "integer", "boolean", "null"]}
REF = {"type": "string", "minLength": 3}

FACT = {
    "type": "object", "additionalProperties": False,
    "required": ["statement", "ref", "field", "value"],
    "properties": {"statement": {"type": "string", "minLength": 1}, "ref": REF,
                   "field": {"type": "string", "minLength": 1}, "value": SCALAR},
}

HYPOTHESIS = {
    "type": "object", "additionalProperties": False,
    "required": ["hypothesis_id", "statement", "kind", "status", "supporting_refs", "weakening_refs"],
    "properties": {
        "hypothesis_id": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
        "kind": {"enum": ["benign", "sensor_or_collection", "environmental", "configuration_change", "needs_review"]},
        "status": {"enum": ["supported", "weakened", "open"]},
        "supporting_refs": {"type": "array", "items": REF},
        "weakening_refs": {"type": "array", "items": REF},
    },
}

FINDING = {
    "type": "object", "additionalProperties": False,
    "required": ["finding_id", "category", "title", "observed_facts", "computed_results", "hypotheses",
                 "assessment", "confidence"],
    "properties": {
        "finding_id": {"type": "string", "minLength": 1},
        "category": {"enum": ["new_entity", "relationship_change", "signal_change", "not_reobserved",
                              "collection_quality"]},
        "title": {"type": "string", "minLength": 1},
        "observed_facts": {"type": "array", "minItems": 1, "items": FACT},
        "computed_results": {"type": "array", "items": FACT},
        "hypotheses": {"type": "array", "items": HYPOTHESIS},
        "assessment": {"type": "string", "minLength": 1},
        "confidence": {"enum": ["low", "medium", "high"]},
    },
}

RECOMMENDATION = {
    "type": "object", "additionalProperties": False,
    "required": ["recommendation_id", "action", "rationale", "permission", "executed"],
    "properties": {
        "recommendation_id": {"type": "string", "minLength": 1},
        "action": {"type": "string", "minLength": 1},
        "rationale": {"type": "string", "minLength": 1},
        "tool": {"type": ["string", "null"]},
        "permission": {"enum": ["read_only", "requires_approval"]},
        "executed": {"const": False},
    },
}

# What a provider submits. The orchestrator adds identity, scope, audit, and grounding.
DRAFT = {
    "type": "object", "additionalProperties": False,
    "required": ["findings", "recommendations", "limitations"],
    "properties": {
        "findings": {"type": "array", "items": FINDING},
        "recommendations": {"type": "array", "items": RECOMMENDATION},
        "limitations": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
    },
}

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
          "object": dict, "array": list}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return human-readable violations; empty means valid. Supports the subset used above."""
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        return [f"{path}: must equal {schema['const']!r}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {schema['enum']}"]
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        def matches(kind):
            if isinstance(value, bool) and kind in ("number", "integer"):
                return False
            return isinstance(value, _TYPES[kind])
        if not any(matches(kind) for kind in types):
            return [f"{path}: expected {'/'.join(types)}"]
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: too short")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors += validate(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: not allowed")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        for index, item in enumerate(value):
            if "items" in schema:
                errors += validate(item, schema["items"], f"{path}[{index}]")
    return errors
