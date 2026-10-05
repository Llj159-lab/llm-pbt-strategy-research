import ast

import pytest

from experiments.constrained_repair.structured_edit import (
    StructuredEditError,
    apply_structured_repair,
)


BASELINE = '''
from hypothesis import example, given, strategies as st

@given(st.integers())
def test_invariant(value):
    observed = value
    assert observed == value
'''


def _spec(edit):
    return {"version": 1, "edits": [edit]}


def test_extends_given_support_without_changing_test_body():
    repair = apply_structured_repair(
        BASELINE,
        _spec(
            {
                "op": "extend_given",
                "test": "test_invariant",
                "position": 0,
                "strategy": "st.just(100)",
            }
        ),
    )
    tree = ast.parse(repair.source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    assert ast.unparse(function.decorator_list[0]) == (
        "given(st.one_of(st.integers(), st.just(100)))"
    )
    assert repair.receipt["changed_existing_test_bodies"] == []
    assert repair.receipt["changed_existing_assertions"] == []


def test_adds_literal_examples_without_changing_body():
    repair = apply_structured_repair(
        BASELINE,
        _spec(
            {
                "op": "add_examples",
                "test": "test_invariant",
                "examples": [{"args": [0]}, {"kwargs": {"value": 100}}],
            }
        ),
    )
    assert "@example(0)" in repair.source
    assert "@example(value=100)" in repair.source
    assert repair.receipt["changed_existing_assertions"] == []


def test_clones_test_and_changes_only_operation_sequence():
    repair = apply_structured_repair(
        BASELINE,
        _spec(
            {
                "op": "clone_test",
                "source_test": "test_invariant",
                "new_name": "test_invariant_after_update",
                "insert_at_start": ["value += 1", "value -= 1"],
                "assignment_replacements": [
                    {"target": "observed", "expression": "int(value)"}
                ],
            }
        ),
    )
    assert repair.receipt["added_test_functions"] == [
        "test_invariant_after_update"
    ]
    assert repair.receipt["baseline_test_count"] == 1
    assert repair.receipt["repaired_test_count"] == 2
    assert "test_invariant_after_update" in repair.source
    assert repair.receipt["base_audit"]["accepted"] is True


def test_inserts_statements_before_an_ast_anchor_in_clone():
    repair = apply_structured_repair(
        BASELINE,
        _spec(
            {
                "op": "clone_test",
                "source_test": "test_invariant",
                "new_name": "test_invariant_after_anchored_update",
                "insert_before": [
                    {
                        "anchor": "observed = value",
                        "statements": ["value += 1", "value -= 1"],
                    }
                ],
            }
        ),
    )
    assert "value += 1\n    value -= 1\n    observed = value" in repair.source
    assert repair.receipt["applied_edits"][0]["inserted_statement_count"] == 2
    assert repair.receipt["changed_existing_test_bodies"] == []


TYPED_BASELINE = '''
from hypothesis import given, strategies as st

@given(st.integers())
def test_mapping(value):
    mapping = {"old": value}
    observed = mapping["old"]
    assert observed == value
'''


def _typed_spec(edit):
    return {"version": 2, "edits": [edit]}


def _semantic_typed_spec(edit):
    return {"version": 3, "edits": [edit]}


def test_typed_operations_are_translated_to_ast_without_python_strings():
    repair = apply_structured_repair(
        TYPED_BASELINE,
        _typed_spec(
            {
                "op": "clone_test",
                "source_test": "test_mapping",
                "new_name": "test_mapping_after_operations",
                "insert_before_steps": [
                    {
                        "anchor": 'observed = mapping["old"]',
                        "steps": [
                            {
                                "kind": "method_call",
                                "receiver": "mapping",
                                "method": "update",
                                "args": [{"literal": {"new": 1}}],
                                "kwargs": {},
                            },
                            {
                                "kind": "set_item",
                                "target": "mapping",
                                "key": "third",
                                "value": {"ref": "value"},
                            },
                            {
                                "kind": "function_call",
                                "function": "list",
                                "args": [{"ref": "mapping"}],
                                "kwargs": {},
                                "assign_to": "snapshot",
                            },
                        ],
                    }
                ],
            }
        ),
    )
    assert "mapping.update({'new': 1})" in repair.source
    assert "mapping['third'] = value" in repair.source
    assert "snapshot = list(mapping)" in repair.source
    assert repair.receipt["dsl_version"] == 2
    assert repair.receipt["applied_edits"][0]["dsl"] == "typed_operations"


def test_typed_operations_reject_undefined_receivers():
    with pytest.raises(StructuredEditError, match="not defined"):
        apply_structured_repair(
            TYPED_BASELINE,
            _typed_spec(
                {
                    "op": "clone_test",
                    "source_test": "test_mapping",
                    "new_name": "test_mapping_bad_receiver",
                    "steps": [
                        {
                            "kind": "method_call",
                            "receiver": "missing",
                            "method": "clear",
                        }
                    ],
                }
            ),
        )


def test_typed_operations_reject_duplicate_source_variables():
    with pytest.raises(StructuredEditError, match="duplicates"):
        apply_structured_repair(
            TYPED_BASELINE,
            _typed_spec(
                {
                    "op": "clone_test",
                    "source_test": "test_mapping",
                    "new_name": "test_mapping_bad_assignment",
                    "insert_before_steps": [
                        {
                            "anchor": 'observed = mapping["old"]',
                            "steps": [
                                {
                                    "kind": "function_call",
                                    "function": "list",
                                    "args": [{"ref": "mapping"}],
                                    "assign_to": "observed",
                                }
                            ],
                        }
                    ],
                }
            ),
        )


def test_semantic_typed_operations_reject_item_key_named_like_variable():
    with pytest.raises(StructuredEditError, match="must use.*ref"):
        apply_structured_repair(
            TYPED_BASELINE,
            _semantic_typed_spec(
                {
                    "op": "clone_test",
                    "source_test": "test_mapping",
                    "new_name": "test_mapping_ambiguous_key",
                    "insert_before_steps": [
                        {
                            "anchor": 'observed = mapping["old"]',
                            "steps": [
                                {
                                    "kind": "set_item",
                                    "target": "mapping",
                                    "key": {"literal": "value"},
                                    "value": {"ref": "value"},
                                }
                            ],
                        }
                    ],
                }
            ),
        )


def test_semantic_typed_operations_reject_redundant_source_statement():
    baseline = TYPED_BASELINE.replace(
        'observed = mapping["old"]',
        'mapping.clear()\n    observed = mapping.get("old")',
    )
    with pytest.raises(StructuredEditError, match="duplicates an existing"):
        apply_structured_repair(
            baseline,
            _semantic_typed_spec(
                {
                    "op": "clone_test",
                    "source_test": "test_mapping",
                    "new_name": "test_mapping_redundant_clear",
                    "insert_before_steps": [
                        {
                            "anchor": 'observed = mapping.get("old")',
                            "steps": [
                                {
                                    "kind": "method_call",
                                    "receiver": "mapping",
                                    "method": "clear",
                                    "args": [],
                                    "kwargs": {},
                                }
                            ],
                        }
                    ],
                }
            ),
        )


def test_semantic_typed_operations_reject_unused_assigned_value():
    with pytest.raises(StructuredEditError, match="never used"):
        apply_structured_repair(
            TYPED_BASELINE,
            _semantic_typed_spec(
                {
                    "op": "clone_test",
                    "source_test": "test_mapping",
                    "new_name": "test_mapping_unused_snapshot",
                    "insert_before_steps": [
                        {
                            "anchor": 'observed = mapping["old"]',
                            "steps": [
                                {
                                    "kind": "function_call",
                                    "function": "list",
                                    "args": [{"ref": "mapping"}],
                                    "kwargs": {},
                                    "assign_to": "snapshot",
                                }
                            ],
                        }
                    ],
                }
            ),
        )


def test_semantic_typed_operations_accept_consumed_assigned_value():
    repair = apply_structured_repair(
        TYPED_BASELINE,
        _semantic_typed_spec(
            {
                "op": "clone_test",
                "source_test": "test_mapping",
                "new_name": "test_mapping_consumed_snapshot",
                "insert_before_steps": [
                    {
                        "anchor": 'observed = mapping["old"]',
                        "steps": [
                            {
                                "kind": "function_call",
                                "function": "list",
                                "args": [{"ref": "mapping"}],
                                "kwargs": {},
                                "assign_to": "snapshot",
                            },
                            {
                                "kind": "method_call",
                                "receiver": "snapshot",
                                "method": "clear",
                                "args": [],
                                "kwargs": {},
                            },
                        ],
                    }
                ],
            }
        ),
    )
    assert repair.receipt["dsl_version"] == 3


def test_rejects_missing_ast_anchor():
    with pytest.raises(StructuredEditError, match="match exactly one"):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "clone_test",
                    "source_test": "test_invariant",
                    "new_name": "test_missing_anchor",
                    "insert_before": [
                        {
                            "anchor": "missing = value",
                            "statements": ["value += 1"],
                        }
                    ],
                }
            ),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "assert value == value",
        "return",
        "if value:\n    value += 1",
        "try:\n    value += 1\nexcept Exception:\n    pass",
        "import os",
        "open('artifact', 'w')",
        "value.__class__",
    ],
)
def test_rejects_forbidden_clone_statements(statement):
    with pytest.raises(StructuredEditError):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "clone_test",
                    "source_test": "test_invariant",
                    "new_name": "test_forbidden_edit",
                    "insert_at_start": [statement],
                }
            ),
        )


def test_rejects_arbitrary_calls_in_strategy_expression():
    with pytest.raises(StructuredEditError, match="non-whitelisted"):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "extend_given",
                    "test": "test_invariant",
                    "position": 0,
                    "strategy": "load_ground_truth()",
                }
            ),
        )


def test_rejects_strategy_extension_identical_to_original():
    with pytest.raises(StructuredEditError, match="identical"):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "extend_given",
                    "test": "test_invariant",
                    "position": 0,
                    "strategy": "st.integers()",
                }
            ),
        )


def test_rejects_missing_or_ambiguous_assignment_target():
    with pytest.raises(StructuredEditError, match="exactly once"):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "clone_test",
                    "source_test": "test_invariant",
                    "new_name": "test_missing_target",
                    "assignment_replacements": [
                        {"target": "missing", "expression": "value"}
                    ],
                }
            ),
        )


def test_rejects_duplicate_clone_name():
    with pytest.raises(StructuredEditError, match="already exists"):
        apply_structured_repair(
            BASELINE,
            _spec(
                {
                    "op": "clone_test",
                    "source_test": "test_invariant",
                    "new_name": "test_invariant",
                }
            ),
        )


def test_clone_may_inherit_source_return_but_not_add_one():
    baseline = BASELINE.replace(
        "observed = value",
        "def helper():\n        return value\n    observed = helper()",
    )
    repair = apply_structured_repair(
        baseline,
        _spec(
            {
                "op": "clone_test",
                "source_test": "test_invariant",
                "new_name": "test_invariant_with_inherited_return",
            }
        ),
    )
    assert repair.receipt["accepted"] is True


def test_rejects_examples_when_example_is_not_imported():
    baseline = BASELINE.replace("example, ", "")
    with pytest.raises(StructuredEditError, match="hypothesis.example"):
        apply_structured_repair(
            baseline,
            _spec(
                {
                    "op": "add_examples",
                    "test": "test_invariant",
                    "examples": [{"args": [0]}],
                }
            ),
        )


def test_receipt_hashes_are_deterministic():
    specification = _spec(
        {
            "op": "extend_given",
            "test": "test_invariant",
            "position": 0,
            "strategy": "st.none()",
        }
    )
    first = apply_structured_repair(BASELINE, specification)
    second = apply_structured_repair(BASELINE, specification)
    assert first.source == second.source
    assert first.receipt == second.receipt
    assert len(first.receipt["baseline_sha256"]) == 64
    assert len(first.receipt["specification_sha256"]) == 64
    assert len(first.receipt["repaired_sha256"]) == 64
