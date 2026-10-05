import pytest

from experiments.constrained_repair.model_dsl import (
    ModelDSLError,
    extract_json_object,
    given_selector_inventory,
    scrub_prompt_hints,
    validate_model_specification,
)


def valid_spec(edit=None):
    return {
        "version": 1,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            edit
            or {
                "op": "extend_given",
                "test": "test_value",
                "position": 0,
                "strategy": "st.none()",
            }
        ],
    }


def test_extracts_raw_json_object():
    assert extract_json_object('{"version": 1}') == {"version": 1}


def test_extracts_single_json_fence():
    assert extract_json_object('```json\n{"version": 1}\n```') == {"version": 1}


def test_scrubs_hint_bearing_docstrings_and_comments():
    source = '''
def test_case():
    """This currently fails due to bug_1 at line 10."""
    value = 1  # Trigger the injected defect.
    assert value == 1
'''
    scrubbed = scrub_prompt_hints(source)
    assert "bug_1" not in scrubbed
    assert "Trigger" not in scrubbed
    assert "assert value == 1" in scrubbed


def test_lists_positional_and_keyword_given_selectors():
    source = '''
from hypothesis import given, strategies as st

@given(st.integers(), name=st.text())
def test_case(value, name):
    assert value == value
'''
    assert given_selector_inventory(source) == (
        "- test_case: position=[0]; keyword=['name']"
    )


@pytest.mark.parametrize(
    "response",
    [
        'Here is the JSON: {"version": 1}',
        '```json\n{"version": 1}\n```\nextra',
        '[{"version": 1}]',
    ],
)
def test_rejects_non_object_or_surrounding_prose(response):
    with pytest.raises(ModelDSLError):
        extract_json_object(response)


def test_validates_extend_given():
    assert validate_model_specification(valid_spec(), "TEST-001") == valid_spec()


def test_validates_add_examples():
    spec = valid_spec(
        {
            "op": "add_examples",
            "test": "test_value",
            "examples": [{"args": [1], "kwargs": {"name": "x"}}],
        }
    )
    validate_model_specification(spec, "TEST-001")


def test_validates_clone_test():
    spec = valid_spec(
        {
            "op": "clone_test",
            "source_test": "test_value",
            "new_name": "test_value_after_update",
            "insert_at_start": ["value.update()"],
        }
    )
    validate_model_specification(spec, "TEST-001")


def test_validates_typed_clone_test():
    spec = {
        "version": 2,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            {
                "op": "clone_test",
                "source_test": "test_value",
                "new_name": "test_value_typed",
                "steps": [
                    {
                        "kind": "method_call",
                        "receiver": "value",
                        "method": "bit_length",
                        "args": [],
                        "kwargs": {},
                    }
                ],
            }
        ],
    }
    validate_model_specification(spec, "TEST-001")


def test_validates_semantically_checked_typed_clone_test():
    spec = {
        "version": 3,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            {
                "op": "clone_test",
                "source_test": "test_value",
                "new_name": "test_value_semantic",
                "steps": [
                    {
                        "kind": "method_call",
                        "receiver": "value",
                        "method": "bit_length",
                        "args": [],
                        "kwargs": {},
                    }
                ],
            }
        ],
    }
    validate_model_specification(spec, "TEST-001")


def test_semantic_version_rejects_non_typed_edits():
    spec = valid_spec()
    spec["version"] = 3
    with pytest.raises(ModelDSLError, match="typed clone_test"):
        validate_model_specification(spec, "TEST-001")


def test_rejects_python_string_fields_in_typed_clone_test():
    spec = {
        "version": 2,
        "study_role": "model-generated-development-only",
        "task_id": "TEST-001",
        "edits": [
            {
                "op": "clone_test",
                "source_test": "test_value",
                "new_name": "test_value_typed",
                "insert_at_start": ["value += 1"],
            }
        ],
    }
    with pytest.raises(ModelDSLError):
        validate_model_specification(spec, "TEST-001")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda spec: spec.update(task_id="OTHER-001"),
        lambda spec: spec.update(study_role="final-evaluation"),
        lambda spec: spec.update(extra="not allowed"),
        lambda spec: spec["edits"][0].update(assertion="assert True"),
        lambda spec: spec.update(edits=[]),
    ],
)
def test_rejects_schema_violations(mutation):
    spec = valid_spec()
    mutation(spec)
    with pytest.raises(ModelDSLError):
        validate_model_specification(spec, "TEST-001")


def test_rejects_clone_without_an_edit():
    spec = valid_spec(
        {
            "op": "clone_test",
            "source_test": "test_value",
            "new_name": "test_clone",
        }
    )
    with pytest.raises(ModelDSLError):
        validate_model_specification(spec, "TEST-001")


def test_rejects_blank_clone_insertion_statements():
    spec = valid_spec(
        {
            "op": "clone_test",
            "source_test": "test_value",
            "new_name": "test_clone",
            "insert_before": [
                {"anchor": "value = 1", "statements": ["", "other = 2"]}
            ],
        }
    )
    with pytest.raises(ModelDSLError):
        validate_model_specification(spec, "TEST-001")


def test_rejects_more_than_maximum_edits():
    spec = valid_spec()
    spec["edits"] *= 5
    with pytest.raises(ModelDSLError):
        validate_model_specification(spec, "TEST-001", max_edits=4)
