import pytest

from experiments.constrained_repair.model_diagnosis import (
    validate_diagnosis_dsl_consistency,
    validate_model_diagnosis,
)
from experiments.constrained_repair.model_dsl import ModelDSLError


def valid_diagnosis(**overrides):
    diagnosis = {
        "version": 1,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "target_test": "test_roundtrip",
        "coverage_gap": "operation_sequence",
        "rationale": "The public API documents construction followed by update and observation.",
        "required_operations": ["construct", "update", "observe"],
    }
    diagnosis.update(overrides)
    return diagnosis


def valid_spec(edit=None):
    return {
        "version": 2,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            edit
            or {
                "op": "clone_test",
                "source_test": "test_roundtrip",
                "new_name": "test_roundtrip_after_update",
                "steps": [
                    {
                        "kind": "method_call",
                        "receiver": "value",
                        "method": "update",
                        "args": [{"literal": {}}],
                        "kwargs": {},
                    }
                ],
            }
        ],
    }


def test_accepts_a_non_oracular_operation_sequence_diagnosis():
    assert validate_model_diagnosis(
        valid_diagnosis(), "TEST-001", {"test_roundtrip"}
    ) == valid_diagnosis()


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_test": "test_missing"},
        {"coverage_gap": "unknown"},
        {"rationale": "This exposes bug_1."},
        {"required_operations": ["construct"]},
        {"required_operations": ["construct", "construct"]},
        {"extra": "not permitted"},
    ],
)
def test_rejects_invalid_or_oracular_diagnoses(overrides):
    with pytest.raises(ModelDSLError):
        validate_model_diagnosis(
            valid_diagnosis(**overrides), "TEST-001", {"test_roundtrip"}
        )


def test_sequence_diagnosis_requires_clone_of_its_target():
    specification = valid_spec(
        {
            "op": "extend_given",
            "test": "test_roundtrip",
            "position": 0,
            "strategy": "st.none()",
        }
    )
    with pytest.raises(ModelDSLError):
        validate_diagnosis_dsl_consistency(valid_diagnosis(), specification)


def test_configuration_diagnosis_requires_typed_configuration_edit():
    diagnosis = valid_diagnosis(
        coverage_gap="configuration_combination", required_operations=["construct", "configure"]
    )
    validate_diagnosis_dsl_consistency(diagnosis, valid_spec())


def test_boundary_diagnosis_rejects_legacy_strategy_extension():
    diagnosis = valid_diagnosis(
        coverage_gap="boundary_value", required_operations=["construct"]
    )
    specification = {
        "version": 1,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            {
                "op": "extend_given",
                "test": "test_roundtrip",
                "position": 0,
                "strategy": "st.none()",
            }
        ],
    }
    with pytest.raises(ModelDSLError):
        validate_diagnosis_dsl_consistency(diagnosis, specification)
