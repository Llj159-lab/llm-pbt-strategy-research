"""Schemas and compatibility checks for diagnosis-guided structured repair."""

from __future__ import annotations

import re
from typing import Any

from experiments.constrained_repair.model_dsl import ModelDSLError


_TOP_LEVEL_KEYS = {
    "version",
    "study_role",
    "task_id",
    "target_test",
    "coverage_gap",
    "rationale",
    "required_operations",
}
_GAP_TYPES = {
    "state_transition",
    "operation_sequence",
    "configuration_combination",
    "boundary_value",
}
_OPERATIONS = {
    "construct",
    "configure",
    "update",
    "remove",
    "clear",
    "copy",
    "lookup",
    "observe",
    "serialize",
    "deserialize",
}
_DISALLOWED_RATIONALE = re.compile(
    r"(?i)\b(bug(?:[_-]?\d+)?|trigger|ground\s*truth|patch|evaluator|assertion|"
    r"fail(?:s|ed|ure)?|fix(?:ed)?|injected?)\b"
)


def _require_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelDSLError(f"{field} must be a non-empty string")


def validate_model_diagnosis(
    diagnosis: dict[str, Any], task_id: str, test_names: set[str]
) -> dict[str, Any]:
    """Validate a non-oracular coverage diagnosis from the model."""
    extra = set(diagnosis) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(diagnosis)
    if extra:
        raise ModelDSLError(f"diagnosis contains unknown keys: {sorted(extra)}")
    if missing:
        raise ModelDSLError(f"diagnosis is missing keys: {sorted(missing)}")
    if diagnosis["version"] != 1:
        raise ModelDSLError("diagnosis version must be 1")
    if diagnosis["study_role"] != "model-generated-development-only":
        raise ModelDSLError(
            "diagnosis study_role must be 'model-generated-development-only'"
        )
    if diagnosis["task_id"] != task_id:
        raise ModelDSLError(f"diagnosis task_id must be {task_id!r}")
    target_test = diagnosis["target_test"]
    _require_nonempty_string(target_test, "diagnosis target_test")
    if target_test not in test_names:
        raise ModelDSLError("diagnosis target_test is not a frozen baseline test")
    if diagnosis["coverage_gap"] not in _GAP_TYPES:
        raise ModelDSLError("diagnosis coverage_gap is unsupported")
    rationale = diagnosis["rationale"]
    _require_nonempty_string(rationale, "diagnosis rationale")
    if len(rationale) > 500:
        raise ModelDSLError("diagnosis rationale exceeds 500 characters")
    if _DISALLOWED_RATIONALE.search(rationale):
        raise ModelDSLError("diagnosis rationale contains prohibited outcome language")
    required_operations = diagnosis["required_operations"]
    if (
        not isinstance(required_operations, list)
        or not required_operations
        or not all(isinstance(value, str) for value in required_operations)
    ):
        raise ModelDSLError("diagnosis required_operations must be a non-empty string list")
    if len(set(required_operations)) != len(required_operations):
        raise ModelDSLError("diagnosis required_operations must not contain duplicates")
    unsupported = sorted(set(required_operations) - _OPERATIONS)
    if unsupported:
        raise ModelDSLError(
            f"diagnosis required_operations are unsupported: {unsupported}"
        )
    if diagnosis["coverage_gap"] in {"state_transition", "operation_sequence"}:
        if len(required_operations) < 2:
            raise ModelDSLError(
                "state_transition and operation_sequence require at least two operations"
            )
    return diagnosis


def validate_diagnosis_dsl_consistency(
    diagnosis: dict[str, Any], specification: dict[str, Any]
) -> None:
    """Require structural edits when the diagnosis claims relational coverage."""
    target_test = diagnosis["target_test"]
    edits = specification["edits"]
    clone_edits = [
        edit
        for edit in edits
        if edit["op"] == "clone_test" and edit["source_test"] == target_test
    ]
    gap = diagnosis["coverage_gap"]
    typed_clone_edits = [
        edit
        for edit in clone_edits
        if edit.get("steps")
        or edit.get("insert_before_steps")
        or edit.get("replace_assignments")
    ]
    if gap in {
        "state_transition",
        "operation_sequence",
        "configuration_combination",
        "boundary_value",
    }:
        if not typed_clone_edits:
            raise ModelDSLError(
                f"{gap} diagnosis requires a typed clone_test of the diagnosed test"
            )
        if gap in {"state_transition", "operation_sequence", "boundary_value"} and not any(
            edit.get("steps") or edit.get("insert_before_steps") for edit in typed_clone_edits
        ):
            raise ModelDSLError(f"{gap} diagnosis requires typed operation steps")
        if gap == "configuration_combination" and not any(
            edit.get("steps")
            or edit.get("insert_before_steps")
            or edit.get("replace_assignments")
            for edit in typed_clone_edits
        ):
            raise ModelDSLError(
                "configuration_combination diagnosis requires typed configuration edit"
            )
