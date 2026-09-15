"""Canonical review schema, strict JSON parsing and a dependency-free validator.

The same schema object is sent to the provider (strict structured output) and used
locally to validate the response, so a provider that silently ignores
``response_format`` cannot get its output accepted.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from . import config as _config

VALID_CATEGORIES = list(_config.VALID_CATEGORIES)
VALID_SEVERITIES = list(_config.VALID_SEVERITIES)
VALID_TEST_TYPES = list(_config.VALID_TEST_TYPES)
REVIEW_STATES = list(_config.REVIEW_STATES)

_STRING = {"type": "string"}
_FENCE_OPEN = re.compile(r"^\s*```[A-Za-z0-9_.+-]*\s*$")
_FENCE_CLOSE = re.compile(r"^\s*```\s*$")
_ISSUE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


ISSUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "category",
        "severity",
        "title",
        "file",
        "lines",
        "problem",
        "why_it_matters",
        "exact_fix",
        "suggested_patch",
    ],
    "properties": {
        "id": {"type": "string", "pattern": _ISSUE_ID.pattern},
        "category": {"type": "string", "enum": VALID_CATEGORIES},
        "severity": {"type": "string", "enum": VALID_SEVERITIES},
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "file": {"type": "string", "minLength": 1, "maxLength": 1000},
        "lines": {"type": "string", "minLength": 1, "maxLength": 200},
        "problem": {"type": "string", "minLength": 1, "maxLength": 4000},
        "why_it_matters": {"type": "string", "minLength": 1, "maxLength": 4000},
        "exact_fix": {"type": "string", "minLength": 1, "maxLength": 4000},
        "suggested_patch": _nullable({"type": "string", "maxLength": 20000}),
    },
}

TEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "type", "name", "file", "covers_error_cases", "assertions", "why"],
    "properties": {
        "id": {"type": "string", "pattern": _ISSUE_ID.pattern},
        "type": {"type": "string", "enum": VALID_TEST_TYPES},
        "name": {"type": "string", "minLength": 1, "maxLength": 500},
        "file": {"type": "string", "minLength": 1, "maxLength": 1000},
        "covers_error_cases": {"type": "array", "items": _STRING, "maxItems": 20},
        "assertions": {"type": "array", "items": _STRING, "maxItems": 20},
        "why": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}

GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "why", "action"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 500},
        "why": {"type": "string", "minLength": 1, "maxLength": 2000},
        "action": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
}

# Model-provided fields only. Identity, coverage, verdict and blocking flags are
# computed by trusted Python code and are NOT part of the model contract.
MODEL_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "issues",
        "tests_to_add",
        "human_gates",
        "definition_of_done",
        "diff_risks",
        "confidence",
    ],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "issues": {"type": "array", "items": ISSUE_SCHEMA, "maxItems": 200},
        "tests_to_add": {"type": "array", "items": TEST_SCHEMA, "maxItems": 100},
        "human_gates": {"type": "array", "items": GATE_SCHEMA, "maxItems": 50},
        "definition_of_done": {"type": "array", "items": _STRING, "maxItems": 50},
        "diff_risks": {"type": "array", "items": _STRING, "maxItems": 50},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


COVERAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "required": ["complete", "total_files", "included", "excluded", "missing", "failed"],
    "properties": {
        "complete": {"type": "boolean"},
        "total_files": {"type": "integer", "minimum": 0},
        "included": {"type": "array", "items": _STRING},
        "excluded": {"type": "array", "items": {"type": "object"}},
        "missing": {"type": "array", "items": {"type": "object"}},
        "failed": {"type": "array", "items": {"type": "object"}},
    },
}

# Versioned trusted envelope written to the run artifact and (when it fits) the
# comment. Consumers validate against this before trusting any field.
RESULT_ENVELOPE_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "required": [
        "schema_version",
        "repository",
        "pr_number",
        "head_sha",
        "base_sha",
        "inputs_hash",
        "model",
        "config_hash",
        "run_id",
        "run_attempt",
        "timestamp",
        "review_state",
        "verdict",
        "blocking_count",
        "issues",
        "summary",
        "coverage",
    ],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "repository": {"type": "string", "minLength": 3},
        "pr_number": {"type": "integer", "minimum": 1},
        "head_sha": {"type": "string", "pattern": "^[0-9a-f]{7,64}$"},
        "base_sha": {"type": "string", "pattern": "^[0-9a-f]{7,64}$"},
        "inputs_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "model": {"type": "string", "minLength": 1},
        "config_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "run_id": {"type": "string", "minLength": 1},
        "run_attempt": {"type": "integer", "minimum": 1},
        "timestamp": {"type": "string", "minLength": 10},
        "review_state": {"type": "string", "enum": REVIEW_STATES},
        "verdict": {"anyOf": [{"type": "string", "enum": ["green", "red"]}, {"type": "null"}]},
        "blocking_count": {"type": "integer", "minimum": 0},
        "issues": {"type": "array"},
        "summary": {"type": "string"},
        "coverage": COVERAGE_SCHEMA,
    },
}


def _type_ok(value, name: str) -> bool:
    if name == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return True


def validate(instance, schema: dict, path: str = "$") -> list:
    """Validate ``instance`` against the JSON-Schema subset used in this repository."""
    errors: list = []
    if not isinstance(schema, dict):
        return errors

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not any(not validate(instance, option, path) for option in any_of):
            errors.append(f"{path}: does not match any allowed variant")
        return errors

    enum = schema.get("enum")
    if enum is not None and instance not in enum:
        errors.append(f"{path}: {instance!r} is not one of {enum}")

    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, name) for name in names):
            errors.append(f"{path}: expected {'/'.join(names)}, got {type(instance).__name__}")
            return errors

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected key {key!r}")
    elif isinstance(instance, list):
        if schema.get("minItems") is not None and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if schema.get("maxItems") is not None and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: has more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))
    elif isinstance(instance, str):
        if schema.get("minLength") is not None and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if schema.get("maxLength") is not None and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, instance):
            errors.append(f"{path}: does not match {pattern}")
    elif isinstance(instance, int | float) and not isinstance(instance, bool):
        if schema.get("minimum") is not None and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if schema.get("maximum") is not None and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    return errors


class JsonRejection(ValueError):
    """Raised for JSON that must never be silently repaired."""


def _reject_constant(name: str):
    raise JsonRejection(f"non-finite JSON number {name!r} is not allowed")


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise JsonRejection(f"duplicate JSON key {key!r} is not allowed")
        seen[key] = value
    return seen


def load_json_strict(text: str):
    """Parse JSON rejecting duplicate keys and NaN/Infinity (no heuristic repair)."""
    return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)


def strip_outer_fence(text: str) -> str:
    """Strip one optional surrounding ``` fence, preserving internal fences.

    Only the opening line and the final closing line are removed, and only when the
    first non-blank line opens a fence and the last non-blank line closes it. This is
    the audited bug: ``text.split("```")`` destroys valid JSON that embeds a fenced
    code block (for example a ``suggested_patch``).
    """
    lines = text.splitlines()
    start, end = 0, len(lines) - 1
    while start <= end and not lines[start].strip():
        start += 1
    while end >= start and not lines[end].strip():
        end -= 1
    if start >= end:
        return text
    if _FENCE_OPEN.match(lines[start]) and _FENCE_CLOSE.match(lines[end]):
        return "\n".join(lines[start + 1 : end])
    return text


@dataclass
class ParseOutcome:
    data: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_model_json(text) -> ParseOutcome:
    """Strip one outer fence, parse strictly and validate against the model schema."""
    if text is None:
        return ParseOutcome(errors=["model response content is missing (null)"])
    if not isinstance(text, str):
        return ParseOutcome(
            errors=[f"model response content is not a string: {type(text).__name__}"]
        )
    if not text.strip():
        return ParseOutcome(errors=["model response content is empty"])
    try:
        data = load_json_strict(strip_outer_fence(text))
    except JsonRejection as exc:
        return ParseOutcome(errors=[f"rejected JSON: {exc}"])
    except json.JSONDecodeError as exc:
        return ParseOutcome(errors=[f"invalid JSON: {exc}"])
    if not isinstance(data, dict):
        return ParseOutcome(errors=[f"expected a JSON object, got {type(data).__name__}"])
    errors = validate(data, MODEL_RESPONSE_SCHEMA)
    return ParseOutcome(data=data, errors=errors)


def parse_result_json(text) -> ParseOutcome:
    """Parse and validate a v2 result envelope (used by the extractor and re-reads)."""
    if not isinstance(text, str) or not text.strip():
        return ParseOutcome(errors=["result payload is empty"])
    try:
        data = load_json_strict(text)
    except (JsonRejection, json.JSONDecodeError) as exc:
        return ParseOutcome(errors=[f"invalid result JSON: {exc}"])
    if not isinstance(data, dict):
        return ParseOutcome(errors=["result payload is not a JSON object"])
    if data.get("schema_version") != _config.SCHEMA_VERSION:
        return ParseOutcome(
            errors=[
                f"unsupported schema_version {data.get('schema_version')!r} "
                f"(expected {_config.SCHEMA_VERSION})"
            ]
        )
    return ParseOutcome(data=data, errors=validate(data, RESULT_ENVELOPE_SCHEMA))
