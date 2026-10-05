"""AST-level guardrails for constrained PBT repair experiments."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from typing import Any


def _dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _test_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _assertions(node: ast.AST) -> list[str]:
    return [_dump(item) for item in ast.walk(node) if isinstance(item, ast.Assert)]


def _is_subsequence(expected: Iterable[str], observed: Iterable[str]) -> bool:
    iterator = iter(observed)
    return all(any(item == candidate for candidate in iterator) for item in expected)


def _imports(tree: ast.Module) -> set[tuple[str, int, str, str | None]]:
    imports: set[tuple[str, int, str, str | None]] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(("", 0, alias.name, alias.asname) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(
                (node.module or "", node.level, alias.name, alias.asname)
                for alias in node.names
            )
    return imports


def _duplicate_test_names(tree: ast.Module) -> list[str]:
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    return sorted({name for name in names if names.count(name) > 1})


def _forbidden_constructs(node: ast.AST) -> list[str]:
    violations: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            name = _dump(item.func)
            if any(token in name for token in ("skip", "xfail")):
                violations.append(f"forbidden_call:{name}")
        elif isinstance(item, ast.Try):
            for handler in item.handlers:
                if handler.type is None:
                    violations.append("bare_except")
                elif isinstance(handler.type, ast.Name) and handler.type.id in {
                    "Exception",
                    "BaseException",
                }:
                    violations.append(f"broad_except:{handler.type.id}")
    return violations


def _has_early_return_before_assert(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    first_assert_line = min(
        (item.lineno for item in ast.walk(node) if isinstance(item, ast.Assert)),
        default=None,
    )
    if first_assert_line is None:
        return False
    return any(
        isinstance(item, ast.Return) and item.lineno < first_assert_line
        for item in ast.walk(node)
    )


def _early_return_functions(
    tests: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> set[str]:
    return {
        name for name, node in tests.items() if _has_early_return_before_assert(node)
    }


def audit_repair(
    baseline_source: str,
    repaired_source: str,
    inherited_functions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Reject repairs that remove tests, assertions, or imports.

    ``inherited_functions`` is used only by the structured editor. It maps an
    added cloned test to its baseline source test so that constructs already
    present in the source test are not misclassified as newly introduced.
    """
    result: dict[str, Any] = {
        "accepted": False,
        "baseline_parse_error": None,
        "repaired_parse_error": None,
        "missing_test_functions": [],
        "functions_with_missing_assertions": {},
        "missing_imports": [],
        "duplicate_test_functions": [],
        "forbidden_constructs": [],
        "functions_with_early_return": [],
        "baseline_test_count": 0,
        "repaired_test_count": 0,
    }
    try:
        baseline_tree = ast.parse(baseline_source)
    except SyntaxError as exc:
        result["baseline_parse_error"] = str(exc)
        return result
    try:
        repaired_tree = ast.parse(repaired_source)
    except SyntaxError as exc:
        result["repaired_parse_error"] = str(exc)
        return result

    baseline_tests = _test_functions(baseline_tree)
    repaired_tests = _test_functions(repaired_tree)
    result["baseline_test_count"] = len(baseline_tests)
    result["repaired_test_count"] = len(repaired_tests)
    result["missing_test_functions"] = sorted(
        set(baseline_tests) - set(repaired_tests)
    )

    missing_assertions: dict[str, list[str]] = {}
    for name, baseline_test in baseline_tests.items():
        repaired_test = repaired_tests.get(name)
        if repaired_test is None:
            continue
        expected = _assertions(baseline_test)
        observed = _assertions(repaired_test)
        if not _is_subsequence(expected, observed):
            missing_assertions[name] = expected
    result["functions_with_missing_assertions"] = missing_assertions

    repaired_imports = _imports(repaired_tree)
    result["missing_imports"] = sorted(_imports(baseline_tree) - repaired_imports)
    result["duplicate_test_functions"] = _duplicate_test_names(repaired_tree)
    # Existing generated baselines can already contain a broad handler. The
    # repair arm must preserve it, but must not introduce another one.
    baseline_forbidden = Counter(_forbidden_constructs(baseline_tree))
    inherited_functions = inherited_functions or {}
    for clone_name, source_name in inherited_functions.items():
        source_test = baseline_tests.get(source_name)
        if source_test is not None:
            baseline_forbidden.update(_forbidden_constructs(source_test))
    repaired_forbidden = Counter(_forbidden_constructs(repaired_tree))
    result["forbidden_constructs"] = sorted(
        (repaired_forbidden - baseline_forbidden).elements()
    )
    baseline_early_returns = _early_return_functions(baseline_tests)
    for clone_name, source_name in inherited_functions.items():
        source_test = baseline_tests.get(source_name)
        if source_test is not None and _has_early_return_before_assert(source_test):
            baseline_early_returns.add(clone_name)
    repaired_early_returns = _early_return_functions(repaired_tests)
    result["functions_with_early_return"] = sorted(
        repaired_early_returns - baseline_early_returns
    )
    result["accepted"] = not (
        result["missing_test_functions"]
        or result["functions_with_missing_assertions"]
        or result["missing_imports"]
        or result["duplicate_test_functions"]
        or result["forbidden_constructs"]
        or result["functions_with_early_return"]
    )
    return result
