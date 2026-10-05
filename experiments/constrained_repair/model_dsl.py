"""Validation helpers for model-generated structured-repair specifications."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


class ModelDSLError(ValueError):
    """Raised when a model response is not one valid DSL object."""


_FENCED_JSON = re.compile(r"\A```(?:json)?\s*\n(?P<body>[\s\S]*?)\n```\s*\Z")
_TOP_LEVEL_KEYS = {"version", "study_role", "task_id", "edits"}
_EDIT_KEYS = {
    "extend_given": {"op", "test", "position", "keyword", "strategy"},
    "add_examples": {"op", "test", "examples"},
    "clone_test": {
        "op",
        "source_test",
        "new_name",
        "insert_at_start",
        "insert_before",
        "assignment_replacements",
        "steps",
        "insert_before_steps",
        "replace_assignments",
    },
}
_LEGACY_CLONE_KEYS = {
    "insert_at_start",
    "insert_before",
    "assignment_replacements",
}
_TYPED_CLONE_KEYS = {"steps", "insert_before_steps", "replace_assignments"}
_TYPED_STEP_KEYS = {
    "method_call": {
        "kind",
        "receiver",
        "method",
        "args",
        "kwargs",
        "assign_to",
    },
    "function_call": {"kind", "function", "args", "kwargs", "assign_to"},
    "set_item": {"kind", "target", "key", "value"},
    "delete_item": {"kind", "target", "key"},
}
_HINT_WORDS = re.compile(
    r"(?i)\b(bug(_\d+)?|trigger|avoid|deliberately|giveaway|uncovered|"
    r"blind.?spot|skip.*bug|broken|injected?|injection|off.?by.?one|"
    r"or-based|wrong|not\s+affected|does not trigger|doesn'?t trigger)\b"
)


def scrub_prompt_hints(source: str) -> str:
    """Remove hint-bearing comments/docstrings before source enters a prompt."""

    def scrub_double(match: re.Match[str]) -> str:
        return '\"\"\"\"\"\"' if _HINT_WORDS.search(match.group(0)) else match.group(0)

    def scrub_single(match: re.Match[str]) -> str:
        return "''''''" if _HINT_WORDS.search(match.group(0)) else match.group(0)

    source = re.sub(r'\"\"\"[\s\S]*?\"\"\"', scrub_double, source)
    source = re.sub(r"'''[\s\S]*?'''", scrub_single, source)
    cleaned_lines: list[str] = []
    for line in source.splitlines(keepends=True):
        comment = re.search(r"(\s*)#(?![!]).*$", line.rstrip("\r\n"))
        if comment and _HINT_WORDS.search(comment.group(0)):
            code = line[: comment.start()].rstrip()
            cleaned_lines.append(code + "\n" if code.strip() else "\n")
        else:
            cleaned_lines.append(line)
    return "".join(cleaned_lines)


def given_selector_inventory(source: str) -> str:
    """Describe legal @given selectors without exposing evaluator information."""
    tree = ast.parse(source)
    rows: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        calls = [
            decorator
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and (
                isinstance(decorator.func, ast.Name)
                and decorator.func.id == "given"
                or isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "given"
            )
        ]
        if len(calls) != 1:
            continue
        call = calls[0]
        positions = list(range(len(call.args)))
        keywords = [item.arg for item in call.keywords if item.arg is not None]
        rows.append(
            f"- {node.name}: position={positions or 'none'}; "
            f"keyword={keywords or 'none'}"
        )
    return "\n".join(rows) if rows else "- No unambiguous @given selectors found."


def extract_json_object(response: str) -> dict[str, Any]:
    """Accept one raw JSON object or one otherwise-empty JSON code fence."""
    candidate = response.strip()
    match = _FENCED_JSON.fullmatch(candidate)
    if match:
        candidate = match.group("body").strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ModelDSLError(f"response is not one JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ModelDSLError("response JSON must be an object")
    return value


def _require_nonempty_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelDSLError(f"{field} must be a non-empty string")


def _validate_json_value(value: Any, field: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ModelDSLError(f"{field} must contain only JSON values") from exc


def _validate_typed_value(value: Any, field: str) -> None:
    """Validate a JSON value or a typed variable reference."""
    _validate_json_value(value, field)
    if isinstance(value, dict) and set(value) == {"ref"}:
        _require_nonempty_string(value["ref"], f"{field}.ref")
        if not value["ref"].isidentifier():
            raise ModelDSLError(f"{field}.ref must be an identifier")
    elif isinstance(value, dict) and set(value) == {"literal"}:
        _validate_json_value(value["literal"], f"{field}.literal")


def _validate_typed_steps(steps: Any, field: str) -> None:
    if not isinstance(steps, list) or not steps:
        raise ModelDSLError(f"{field} must be a non-empty step list")
    for step_index, step in enumerate(steps):
        prefix = f"{field}[{step_index}]"
        if not isinstance(step, dict):
            raise ModelDSLError(f"{prefix} must be an object")
        kind = step.get("kind")
        if kind not in _TYPED_STEP_KEYS:
            raise ModelDSLError(f"{prefix}.kind is unsupported: {kind!r}")
        extra = set(step) - _TYPED_STEP_KEYS[kind]
        if extra:
            raise ModelDSLError(f"{prefix} contains unknown keys: {sorted(extra)}")
        if kind == "method_call":
            _require_nonempty_string(step.get("receiver"), f"{prefix}.receiver")
            _require_nonempty_string(step.get("method"), f"{prefix}.method")
            if not step["receiver"].isidentifier() or not step["method"].isidentifier():
                raise ModelDSLError(f"{prefix} receiver and method must be identifiers")
            if step["method"].startswith("__"):
                raise ModelDSLError(f"{prefix}.method cannot be a dunder method")
        elif kind == "function_call":
            _require_nonempty_string(step.get("function"), f"{prefix}.function")
            if not step["function"].isidentifier():
                raise ModelDSLError(f"{prefix}.function must be an identifier")
        else:
            _require_nonempty_string(step.get("target"), f"{prefix}.target")
            if not step["target"].isidentifier():
                raise ModelDSLError(f"{prefix}.target must be an identifier")
        if "assign_to" in step:
            _require_nonempty_string(step["assign_to"], f"{prefix}.assign_to")
            if not step["assign_to"].isidentifier():
                raise ModelDSLError(f"{prefix}.assign_to must be an identifier")
        args = step.get("args", [])
        if not isinstance(args, list):
            raise ModelDSLError(f"{prefix}.args must be a list")
        for arg_index, value in enumerate(args):
            _validate_typed_value(value, f"{prefix}.args[{arg_index}]")
        kwargs = step.get("kwargs", {})
        if not isinstance(kwargs, dict) or any(
            not isinstance(key, str) or not key.isidentifier() for key in kwargs
        ):
            raise ModelDSLError(f"{prefix}.kwargs must map identifier names to values")
        for key, value in kwargs.items():
            _validate_typed_value(value, f"{prefix}.kwargs.{key}")
        if kind in {"set_item", "delete_item"}:
            _validate_typed_value(step["key"], f"{prefix}.key")
        if kind == "set_item":
            _validate_typed_value(step["value"], f"{prefix}.value")


def _validate_edit(edit: Any, index: int) -> None:
    prefix = f"edits[{index}]"
    if not isinstance(edit, dict):
        raise ModelDSLError(f"{prefix} must be an object")
    operation = edit.get("op")
    if operation not in _EDIT_KEYS:
        raise ModelDSLError(f"{prefix}.op is unsupported: {operation!r}")
    extra = set(edit) - _EDIT_KEYS[operation]
    if extra:
        raise ModelDSLError(f"{prefix} contains unknown keys: {sorted(extra)}")

    if operation == "extend_given":
        _require_nonempty_string(edit.get("test"), f"{prefix}.test")
        _require_nonempty_string(edit.get("strategy"), f"{prefix}.strategy")
        selectors = int("position" in edit) + int("keyword" in edit)
        if selectors != 1:
            raise ModelDSLError(
                f"{prefix} requires exactly one of position or keyword"
            )
        if "position" in edit:
            position = edit["position"]
            if (
                not isinstance(position, int)
                or isinstance(position, bool)
                or position < 0
            ):
                raise ModelDSLError(f"{prefix}.position must be a non-negative integer")
        if "keyword" in edit:
            _require_nonempty_string(edit["keyword"], f"{prefix}.keyword")
        return

    if operation == "add_examples":
        _require_nonempty_string(edit.get("test"), f"{prefix}.test")
        examples = edit.get("examples")
        if not isinstance(examples, list) or not examples:
            raise ModelDSLError(f"{prefix}.examples must be a non-empty list")
        for example_index, example in enumerate(examples):
            if not isinstance(example, dict) or set(example) - {"args", "kwargs"}:
                raise ModelDSLError(
                    f"{prefix}.examples[{example_index}] must contain only args/kwargs"
                )
            if not isinstance(example.get("args", []), list):
                raise ModelDSLError(
                    f"{prefix}.examples[{example_index}].args must be a list"
                )
            kwargs = example.get("kwargs", {})
            if not isinstance(kwargs, dict) or any(
                not isinstance(key, str) or not key for key in kwargs
            ):
                raise ModelDSLError(
                    f"{prefix}.examples[{example_index}].kwargs must be an object"
                )
            _validate_json_value(example, f"{prefix}.examples[{example_index}]")
        return

    _require_nonempty_string(edit.get("source_test"), f"{prefix}.source_test")
    _require_nonempty_string(edit.get("new_name"), f"{prefix}.new_name")
    typed_fields = set(edit) & _TYPED_CLONE_KEYS
    legacy_fields = set(edit) & _LEGACY_CLONE_KEYS
    if typed_fields:
        if legacy_fields:
            raise ModelDSLError(
                f"{prefix} cannot mix typed steps with legacy Python string edits"
            )
        if not any(field in edit for field in _TYPED_CLONE_KEYS):
            raise ModelDSLError(f"{prefix} typed clone does not contain an edit")
        if "steps" in edit:
            _validate_typed_steps(edit["steps"], f"{prefix}.steps")
        if "insert_before_steps" in edit:
            entries = edit["insert_before_steps"]
            if not isinstance(entries, list) or not entries:
                raise ModelDSLError(
                    f"{prefix}.insert_before_steps must be a non-empty list"
                )
            for entry_index, entry in enumerate(entries):
                entry_prefix = f"{prefix}.insert_before_steps[{entry_index}]"
                if not isinstance(entry, dict) or set(entry) != {"anchor", "steps"}:
                    raise ModelDSLError(f"{entry_prefix} requires anchor/steps")
                _require_nonempty_string(entry["anchor"], f"{entry_prefix}.anchor")
                _validate_typed_steps(entry["steps"], f"{entry_prefix}.steps")
        if "replace_assignments" in edit:
            replacements = edit["replace_assignments"]
            if not isinstance(replacements, list) or not replacements:
                raise ModelDSLError(
                    f"{prefix}.replace_assignments must be a non-empty list"
                )
            for replacement_index, replacement in enumerate(replacements):
                replacement_prefix = (
                    f"{prefix}.replace_assignments[{replacement_index}]"
                )
                if not isinstance(replacement, dict) or set(replacement) != {
                    "target",
                    "value",
                }:
                    raise ModelDSLError(
                        f"{replacement_prefix} requires target/value"
                    )
                _require_nonempty_string(
                    replacement["target"], f"{replacement_prefix}.target"
                )
                if not replacement["target"].isidentifier():
                    raise ModelDSLError(
                        f"{replacement_prefix}.target must be an identifier"
                    )
                _validate_typed_value(
                    replacement["value"], f"{replacement_prefix}.value"
                )
        return
    optional_fields = {
        "insert_at_start",
        "insert_before",
        "assignment_replacements",
    }
    if not any(field in edit for field in optional_fields):
        raise ModelDSLError(f"{prefix} clone_test does not contain an edit")
    if "insert_at_start" in edit:
        values = edit["insert_at_start"]
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ModelDSLError(
                f"{prefix}.insert_at_start must be a non-empty string list"
            )
    if "insert_before" in edit:
        entries = edit["insert_before"]
        if not isinstance(entries, list) or not entries:
            raise ModelDSLError(f"{prefix}.insert_before must be a non-empty list")
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"anchor", "statements"}:
                raise ModelDSLError(
                    f"{prefix}.insert_before[{entry_index}] requires anchor/statements"
                )
            _require_nonempty_string(
                entry["anchor"], f"{prefix}.insert_before[{entry_index}].anchor"
            )
            statements = entry["statements"]
            if not isinstance(statements, list) or not statements or not all(
                isinstance(value, str) and value.strip() for value in statements
            ):
                raise ModelDSLError(
                    f"{prefix}.insert_before[{entry_index}].statements must be a "
                    "non-empty string list"
                )
    if "assignment_replacements" in edit:
        replacements = edit["assignment_replacements"]
        if not isinstance(replacements, list) or not replacements:
            raise ModelDSLError(
                f"{prefix}.assignment_replacements must be a non-empty list"
            )
        for replacement_index, replacement in enumerate(replacements):
            if not isinstance(replacement, dict) or set(replacement) != {
                "target",
                "expression",
            }:
                raise ModelDSLError(
                    f"{prefix}.assignment_replacements[{replacement_index}] requires "
                    "target/expression"
                )
            _require_nonempty_string(
                replacement["target"],
                f"{prefix}.assignment_replacements[{replacement_index}].target",
            )
            _require_nonempty_string(
                replacement["expression"],
                f"{prefix}.assignment_replacements[{replacement_index}].expression",
            )


def validate_model_specification(
    specification: dict[str, Any], task_id: str, max_edits: int = 4
) -> dict[str, Any]:
    """Validate the model-facing schema before invoking the AST transformer."""
    extra = set(specification) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(specification)
    if extra:
        raise ModelDSLError(f"specification contains unknown keys: {sorted(extra)}")
    if missing:
        raise ModelDSLError(f"specification is missing keys: {sorted(missing)}")
    if specification["version"] not in {1, 2, 3}:
        raise ModelDSLError("version must be 1, 2, or 3")
    if specification["study_role"] != "model-generated-development-only":
        raise ModelDSLError(
            "study_role must be 'model-generated-development-only'"
        )
    if specification["task_id"] != task_id:
        raise ModelDSLError(f"task_id must be {task_id!r}")
    edits = specification["edits"]
    if not isinstance(edits, list) or not edits:
        raise ModelDSLError("edits must be a non-empty list")
    if len(edits) > max_edits:
        raise ModelDSLError(f"edits exceeds the maximum of {max_edits}")
    for index, edit in enumerate(edits):
        _validate_edit(edit, index)
        if specification["version"] in {2, 3}:
            if edit.get("op") != "clone_test":
                raise ModelDSLError(
                    "versions 2 and 3 permit only typed clone_test edits"
                )
            if not set(edit) & _TYPED_CLONE_KEYS:
                raise ModelDSLError(
                    "versions 2 and 3 clone_test require typed operation fields"
                )
    return specification
