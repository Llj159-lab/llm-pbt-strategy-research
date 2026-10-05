"""Deterministic, structure-preserving edits for PBT repair experiments."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from experiments.constrained_repair.audit import audit_repair


class StructuredEditError(ValueError):
    """Raised when a repair specification violates the edit grammar."""


@dataclass(frozen=True)
class StructuredRepair:
    source: str
    receipt: dict[str, Any]


def _dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tests(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _assertions(node: ast.AST) -> list[str]:
    return [_dump(item) for item in ast.walk(node) if isinstance(item, ast.Assert)]


def _strategy_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "hypothesis":
            for alias in node.names:
                if alias.name == "strategies":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "hypothesis.strategies":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _hypothesis_decorator_aliases(tree: ast.Module, name: str) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "hypothesis":
            for alias in node.names:
                if alias.name == name:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


_SAFE_BUILTIN_CALLS = {
    "abs",
    "all",
    "any",
    "dict",
    "frozenset",
    "len",
    "list",
    "max",
    "min",
    "range",
    "set",
    "sorted",
    "sum",
    "tuple",
}
_DANGEROUS_CALLS = {"__import__", "compile", "eval", "exec", "open"}
_STRATEGY_METHODS = {"filter", "flatmap", "map"}


def _validate_strategy_expression(node: ast.AST, aliases: set[str]) -> None:
    if not aliases:
        raise StructuredEditError("baseline does not import hypothesis.strategies")
    for item in ast.walk(node):
        if isinstance(item, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
            raise StructuredEditError(
                f"strategy expression contains forbidden node: {type(item).__name__}"
            )
        if not isinstance(item, ast.Call):
            continue
        if isinstance(item.func, ast.Name):
            if item.func.id not in _SAFE_BUILTIN_CALLS:
                raise StructuredEditError(
                    f"strategy expression calls non-whitelisted name: {item.func.id}"
                )
            continue
        if isinstance(item.func, ast.Attribute):
            root = _root_name(item.func)
            chained_strategy_call = (
                isinstance(item.func.value, ast.Call)
                and item.func.attr in _STRATEGY_METHODS
            )
            if root not in aliases and not chained_strategy_call:
                raise StructuredEditError(
                    f"strategy expression calls outside strategy namespace: "
                    f"{ast.unparse(item.func)}"
                )
            continue
        raise StructuredEditError("strategy expression contains an unsupported call")


def _parse_strategy_expression(source: str, aliases: set[str]) -> ast.expr:
    try:
        node = ast.parse(source, mode="eval").body
    except SyntaxError as exc:
        raise StructuredEditError(f"invalid strategy expression: {exc}") from exc
    _validate_strategy_expression(node, aliases)
    return node


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _find_decorator_call(
    function: ast.FunctionDef | ast.AsyncFunctionDef, aliases: set[str]
) -> ast.Call:
    matches = [
        decorator
        for decorator in function.decorator_list
        if isinstance(decorator, ast.Call) and _call_name(decorator.func) in aliases
    ]
    if len(matches) != 1:
        raise StructuredEditError(
            f"{function.name} must have exactly one matching Hypothesis decorator"
        )
    return matches[0]


def _select_given_argument(call: ast.Call, edit: dict[str, Any]) -> tuple[Any, str]:
    has_position = "position" in edit
    has_keyword = "keyword" in edit
    if has_position == has_keyword:
        raise StructuredEditError("given edit requires exactly one of position or keyword")
    if has_position:
        position = edit["position"]
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise StructuredEditError("given position must be a non-negative integer")
        if position >= len(call.args):
            raise StructuredEditError("given position is out of range")
        return position, "position"
    keyword = edit["keyword"]
    if not isinstance(keyword, str) or not keyword:
        raise StructuredEditError("given keyword must be a non-empty string")
    matches = [item for item in call.keywords if item.arg == keyword]
    if len(matches) != 1:
        raise StructuredEditError(f"given keyword not found exactly once: {keyword}")
    return matches[0], "keyword"


def _extend_given(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    edit: dict[str, Any],
    strategy_aliases: set[str],
    given_aliases: set[str],
) -> dict[str, Any]:
    call = _find_decorator_call(function, given_aliases)
    extension_source = edit.get("strategy")
    if not isinstance(extension_source, str) or not extension_source.strip():
        raise StructuredEditError("extend_given requires a strategy string")
    extension = _parse_strategy_expression(extension_source, strategy_aliases)
    target, kind = _select_given_argument(call, edit)
    original = call.args[target] if kind == "position" else target.value
    if _dump(original) == _dump(extension):
        raise StructuredEditError("extended strategy is identical to the original")
    alias = sorted(strategy_aliases)[0]
    combined = ast.Call(
        func=ast.Attribute(value=ast.Name(id=alias, ctx=ast.Load()), attr="one_of", ctx=ast.Load()),
        args=[copy.deepcopy(original), extension],
        keywords=[],
    )
    if kind == "position":
        call.args[target] = combined
        selector: int | str = target
    else:
        target.value = combined
        selector = target.arg or ""
    return {"op": "extend_given", "test": function.name, kind: selector}


def _json_expression(value: Any) -> ast.expr:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
        return ast.parse(encoded, mode="eval").body
    except (TypeError, SyntaxError) as exc:
        raise StructuredEditError(f"example value is not JSON-compatible: {value!r}") from exc


def _add_examples(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    edit: dict[str, Any],
    example_aliases: set[str],
) -> dict[str, Any]:
    if len(example_aliases) != 1:
        raise StructuredEditError(
            "add_examples requires exactly one imported hypothesis.example alias"
        )
    examples = edit.get("examples")
    if not isinstance(examples, list) or not examples:
        raise StructuredEditError("add_examples requires a non-empty examples list")
    alias = next(iter(example_aliases))
    added: list[ast.Call] = []
    for example in examples:
        if not isinstance(example, dict):
            raise StructuredEditError("each example must be an object")
        args = example.get("args", [])
        kwargs = example.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise StructuredEditError("example args/kwargs must be a list/object")
        if any(not isinstance(key, str) or not key for key in kwargs):
            raise StructuredEditError("example keyword names must be non-empty strings")
        added.append(
            ast.Call(
                func=ast.Name(id=alias, ctx=ast.Load()),
                args=[_json_expression(value) for value in args],
                keywords=[
                    ast.keyword(arg=key, value=_json_expression(value))
                    for key, value in kwargs.items()
                ],
            )
        )
    function.decorator_list = added + function.decorator_list
    return {"op": "add_examples", "test": function.name, "count": len(added)}


_FORBIDDEN_INSERT_NODES = (
    ast.Assert,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.Break,
    ast.ClassDef,
    ast.Continue,
    ast.Delete,
    ast.For,
    ast.FunctionDef,
    ast.Global,
    ast.If,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Match,
    ast.NamedExpr,
    ast.Nonlocal,
    ast.Raise,
    ast.Return,
    ast.Try,
    ast.While,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)
_ALLOWED_INSERT_STATEMENTS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)


def _validate_inserted_statements(statements: list[ast.stmt]) -> None:
    for statement in statements:
        if not isinstance(statement, _ALLOWED_INSERT_STATEMENTS):
            raise StructuredEditError(
                f"inserted statement type is not allowed: {type(statement).__name__}"
            )
        for item in ast.walk(statement):
            if isinstance(item, _FORBIDDEN_INSERT_NODES):
                raise StructuredEditError(
                    f"inserted statement contains forbidden node: {type(item).__name__}"
                )
            if isinstance(item, ast.Call):
                name = _call_name(item.func)
                if name and name.split(".")[-1] in _DANGEROUS_CALLS:
                    raise StructuredEditError(f"inserted statement calls {name}")
            if isinstance(item, ast.Attribute) and item.attr.startswith("__"):
                raise StructuredEditError("dunder attribute access is not allowed")


def _parse_insertions(values: Any) -> list[ast.stmt]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise StructuredEditError("insert_at_start must be a list of statement strings")
    statements: list[ast.stmt] = []
    for value in values:
        try:
            parsed = ast.parse(value).body
        except SyntaxError as exc:
            raise StructuredEditError(f"invalid inserted statement: {exc}") from exc
        statements.extend(parsed)
    _validate_inserted_statements(statements)
    return statements


def _insert_before_anchors(
    function: ast.FunctionDef | ast.AsyncFunctionDef, entries: Any
) -> int:
    if entries is None:
        return 0
    if not isinstance(entries, list):
        raise StructuredEditError("insert_before must be a list")
    inserted_count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise StructuredEditError("insert_before entry must be an object")
        anchor_source = entry.get("anchor")
        if not isinstance(anchor_source, str) or not anchor_source.strip():
            raise StructuredEditError("insert_before requires an anchor statement")
        try:
            anchor_body = ast.parse(anchor_source).body
        except SyntaxError as exc:
            raise StructuredEditError(f"invalid anchor statement: {exc}") from exc
        if len(anchor_body) != 1:
            raise StructuredEditError("insert_before anchor must be one statement")
        anchor_dump = _dump(anchor_body[0])
        matches = [
            index
            for index, statement in enumerate(function.body)
            if _dump(statement) == anchor_dump
        ]
        if len(matches) != 1:
            raise StructuredEditError(
                "insert_before anchor must match exactly one top-level statement"
            )
        statements = _parse_insertions(entry.get("statements"))
        if not statements:
            raise StructuredEditError("insert_before requires inserted statements")
        index = matches[0]
        function.body[index:index] = statements
        inserted_count += len(statements)
    return inserted_count


def _module_symbols(tree: ast.Module) -> set[str]:
    symbols = set(_SAFE_BUILTIN_CALLS)
    for node in tree.body:
        if isinstance(node, ast.Import):
            symbols.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            symbols.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            symbols.update(
                item.id
                for item in ast.walk(node)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
            )
    return symbols


def _function_local_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    names = {argument.arg for argument in function.args.args}
    names.update(argument.arg for argument in function.args.kwonlyargs)
    if function.args.vararg:
        names.add(function.args.vararg.arg)
    if function.args.kwarg:
        names.add(function.args.kwarg.arg)
    for item in ast.walk(function):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.add(item.id)
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(item.name)
    return names


def _names_defined_by_statement(statement: ast.stmt) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(statement):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.add(item.id)
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(item.name)
    return names


def _available_names_at(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    statement_index: int,
    module_symbols: set[str],
) -> set[str]:
    names = set(module_symbols)
    names.update(argument.arg for argument in function.args.args)
    names.update(argument.arg for argument in function.args.kwonlyargs)
    if function.args.vararg:
        names.add(function.args.vararg.arg)
    if function.args.kwarg:
        names.add(function.args.kwarg.arg)
    for statement in function.body[:statement_index]:
        names.update(_names_defined_by_statement(statement))
    return names


def _typed_value_to_ast(
    value: Any,
    available: set[str],
    path: str,
    *,
    semantic_checks: bool = False,
) -> ast.expr:
    if isinstance(value, dict) and set(value) == {"ref"}:
        reference = value["ref"]
        if not isinstance(reference, str) or not reference.isidentifier():
            raise StructuredEditError(f"{path}.ref must be an identifier")
        if reference not in available:
            raise StructuredEditError(f"{path}.ref is not defined at the insertion point: {reference}")
        return ast.Name(id=reference, ctx=ast.Load())
    if isinstance(value, dict) and set(value) == {"literal"}:
        return _typed_literal_to_ast(value["literal"], path)
    if value is None or isinstance(value, (bool, int, float, str)):
        if semantic_checks and isinstance(value, str) and value in available:
            raise StructuredEditError(
                f"{path} is an ambiguous bare string matching an in-scope variable; "
                "use {'ref': name} or {'literal': value}"
            )
        return ast.Constant(value=value)
    if isinstance(value, list):
        return ast.List(
            elts=[
                _typed_value_to_ast(
                    item,
                    available,
                    f"{path}[{index}]",
                    semantic_checks=semantic_checks,
                )
                for index, item in enumerate(value)
            ],
            ctx=ast.Load(),
        )
    if isinstance(value, dict):
        keys: list[ast.expr] = []
        values: list[ast.expr] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise StructuredEditError(f"{path} dictionary keys must be strings")
            keys.append(ast.Constant(value=key))
            values.append(
                _typed_value_to_ast(
                    item,
                    available,
                    f"{path}.{key}",
                    semantic_checks=semantic_checks,
                )
            )
        return ast.Dict(keys=keys, values=values)
    raise StructuredEditError(f"{path} is not a supported JSON value")


def _typed_literal_to_ast(value: Any, path: str) -> ast.expr:
    if value is None or isinstance(value, (bool, int, float, str)):
        return ast.Constant(value=value)
    if isinstance(value, list):
        return ast.List(
            elts=[_typed_literal_to_ast(item, f"{path}[{index}]") for index, item in enumerate(value)],
            ctx=ast.Load(),
        )
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise StructuredEditError(f"{path} dictionary keys must be strings")
        return ast.Dict(
            keys=[ast.Constant(value=key) for key in value],
            values=[_typed_literal_to_ast(item, f"{path}.{key}") for key, item in value.items()],
        )
    raise StructuredEditError(f"{path} is not a supported literal")


def _typed_call_keywords(
    kwargs: Any,
    available: set[str],
    path: str,
    *,
    semantic_checks: bool = False,
) -> list[ast.keyword]:
    if kwargs is None:
        return []
    if not isinstance(kwargs, dict):
        raise StructuredEditError(f"{path} must be an object")
    result: list[ast.keyword] = []
    for name, value in kwargs.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise StructuredEditError(f"{path} contains an invalid keyword: {name!r}")
        result.append(
            ast.keyword(
                arg=name,
                value=_typed_value_to_ast(
                    value,
                    available,
                    f"{path}.{name}",
                    semantic_checks=semantic_checks,
                ),
            )
        )
    return result


def _ensure_new_typed_name(
    name: Any,
    source_local_names: set[str],
    introduced_names: set[str],
    path: str,
) -> str:
    if not isinstance(name, str) or not name.isidentifier():
        raise StructuredEditError(f"{path} must be an identifier")
    if name in source_local_names:
        raise StructuredEditError(
            f"{path} duplicates a variable already defined by the source test: {name}"
        )
    if name in introduced_names:
        raise StructuredEditError(f"{path} is assigned more than once: {name}")
    introduced_names.add(name)
    return name


def _typed_step_statements(
    steps: Any,
    available: set[str],
    source_local_names: set[str],
    introduced_names: set[str],
    path: str,
    *,
    semantic_checks: bool = False,
) -> tuple[list[ast.stmt], int]:
    if not isinstance(steps, list) or not steps:
        raise StructuredEditError(f"{path} must be a non-empty step list")
    statements: list[ast.stmt] = []
    for index, step in enumerate(steps):
        step_path = f"{path}[{index}]"
        if not isinstance(step, dict):
            raise StructuredEditError(f"{step_path} must be an object")
        kind = step.get("kind")
        if kind == "method_call":
            receiver = step.get("receiver")
            method = step.get("method")
            if (
                not isinstance(receiver, str)
                or not receiver.isidentifier()
                or receiver not in available
            ):
                raise StructuredEditError(
                    f"{step_path}.receiver is not defined at the insertion point: {receiver!r}"
                )
            if not isinstance(method, str) or not method.isidentifier() or method.startswith("__"):
                raise StructuredEditError(f"{step_path}.method is not a safe identifier")
            args = [
                _typed_value_to_ast(
                    value,
                    available,
                    f"{step_path}.args[{arg_index}]",
                    semantic_checks=semantic_checks,
                )
                for arg_index, value in enumerate(step.get("args", []))
            ]
            call = ast.Call(
                func=ast.Attribute(value=ast.Name(id=receiver, ctx=ast.Load()), attr=method, ctx=ast.Load()),
                args=args,
                keywords=_typed_call_keywords(
                    step.get("kwargs", {}),
                    available,
                    f"{step_path}.kwargs",
                    semantic_checks=semantic_checks,
                ),
            )
        elif kind == "function_call":
            function_name = step.get("function")
            if not isinstance(function_name, str) or not function_name.isidentifier():
                raise StructuredEditError(f"{step_path}.function must be an identifier")
            if function_name not in available:
                raise StructuredEditError(
                    f"{step_path}.function is not defined at the insertion point: {function_name}"
                )
            call = ast.Call(
                func=ast.Name(id=function_name, ctx=ast.Load()),
                args=[
                    _typed_value_to_ast(
                        value,
                        available,
                        f"{step_path}.args[{arg_index}]",
                        semantic_checks=semantic_checks,
                    )
                    for arg_index, value in enumerate(step.get("args", []))
                ],
                keywords=_typed_call_keywords(
                    step.get("kwargs", {}),
                    available,
                    f"{step_path}.kwargs",
                    semantic_checks=semantic_checks,
                ),
            )
        elif kind == "set_item":
            target = step.get("target")
            if not isinstance(target, str) or target not in available:
                raise StructuredEditError(f"{step_path}.target is not defined: {target!r}")
            statements.append(
                ast.Assign(
                    targets=[
                        ast.Subscript(
                            value=ast.Name(id=target, ctx=ast.Load()),
                            slice=_typed_item_key_to_ast(
                                step.get("key"),
                                available,
                                f"{step_path}.key",
                                semantic_checks=semantic_checks,
                            ),
                            ctx=ast.Store(),
                        )
                    ],
                    value=_typed_value_to_ast(
                        step.get("value"),
                        available,
                        f"{step_path}.value",
                        semantic_checks=semantic_checks,
                    ),
                )
            )
            continue
        elif kind == "delete_item":
            target = step.get("target")
            if not isinstance(target, str) or target not in available:
                raise StructuredEditError(f"{step_path}.target is not defined: {target!r}")
            statements.append(
                ast.Delete(
                    targets=[
                        ast.Subscript(
                            value=ast.Name(id=target, ctx=ast.Load()),
                            slice=_typed_item_key_to_ast(
                                step.get("key"),
                                available,
                                f"{step_path}.key",
                                semantic_checks=semantic_checks,
                            ),
                            ctx=ast.Del(),
                        )
                    ]
                )
            )
            continue
        else:
            raise StructuredEditError(f"{step_path}.kind is unsupported: {kind!r}")

        assign_to = step.get("assign_to")
        if assign_to is None:
            statements.append(ast.Expr(value=call))
        else:
            name = _ensure_new_typed_name(
                assign_to, source_local_names, introduced_names, f"{step_path}.assign_to"
            )
            statements.append(
                ast.Assign(targets=[ast.Name(id=name, ctx=ast.Store())], value=call)
            )
            available.add(name)
    return statements, len(statements)


def _typed_item_key_to_ast(
    value: Any, available: set[str], path: str, *, semantic_checks: bool
) -> ast.expr:
    """Reject a literal item key that shadows an in-scope variable name."""
    literal = (
        value.get("literal")
        if isinstance(value, dict) and set(value) == {"literal"}
        else value
    )
    if semantic_checks and isinstance(literal, str) and literal in available:
        raise StructuredEditError(
            f"{path} names an in-scope variable and must use {{'ref': {literal!r}}}"
        )
    return _typed_value_to_ast(
        value, available, path, semantic_checks=semantic_checks
    )


def _typed_anchor_insertions(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    entries: Any,
    module_symbols: set[str],
    source_local_names: set[str],
    introduced_names: set[str],
    *,
    semantic_checks: bool = False,
    original_statement_dumps: set[str] | None = None,
) -> int:
    if entries is None:
        return 0
    if not isinstance(entries, list):
        raise StructuredEditError("insert_before_steps must be a list")
    inserted_count = 0
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise StructuredEditError("insert_before_steps entry must be an object")
        anchor_source = entry.get("anchor")
        if not isinstance(anchor_source, str) or not anchor_source.strip():
            raise StructuredEditError("insert_before_steps requires an anchor")
        try:
            anchor_body = ast.parse(anchor_source).body
        except SyntaxError as exc:
            raise StructuredEditError(f"invalid typed anchor: {exc}") from exc
        if len(anchor_body) != 1:
            raise StructuredEditError("typed anchor must be one statement")
        anchor_dump = _dump(anchor_body[0])
        matches = [
            index
            for index, statement in enumerate(function.body)
            if _dump(statement) == anchor_dump
        ]
        if len(matches) != 1:
            raise StructuredEditError("typed anchor must match exactly one top-level statement")
        index = matches[0]
        available = _available_names_at(function, index, module_symbols)
        available.update(introduced_names)
        statements, count = _typed_step_statements(
            entry.get("steps"),
            available,
            source_local_names,
            introduced_names,
            f"insert_before_steps[{entry_index}].steps",
            semantic_checks=semantic_checks,
        )
        if semantic_checks and original_statement_dumps is not None:
            if any(_dump(statement) in original_statement_dumps for statement in statements):
                raise StructuredEditError(
                    "typed operation duplicates an existing source-test statement"
                )
        function.body[index:index] = statements
        inserted_count += count
    return inserted_count


def _replace_typed_assignments(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    replacements: Any,
    available: set[str],
    *,
    semantic_checks: bool = False,
) -> list[str]:
    if replacements is None:
        return []
    if not isinstance(replacements, list):
        raise StructuredEditError("replace_assignments must be a list")
    changed: list[str] = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise StructuredEditError("typed assignment replacement must be an object")
        target = replacement.get("target")
        if not isinstance(target, str) or not target.isidentifier():
            raise StructuredEditError("typed assignment target must be an identifier")
        matches: list[ast.Assign | ast.AnnAssign] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                if _assignment_target_name(node.targets[0]) == target:
                    matches.append(node)
            elif isinstance(node, ast.AnnAssign) and _assignment_target_name(node.target) == target:
                matches.append(node)
        if len(matches) != 1:
            raise StructuredEditError(
                f"typed assignment target must occur exactly once: {target}"
            )
        matches[0].value = _typed_value_to_ast(
            replacement.get("value"),
            available,
            f"replace_assignments.{target}.value",
            semantic_checks=semantic_checks,
        )
        changed.append(target)
    return changed


def _clone_test_typed(
    tree: ast.Module,
    tests: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    edit: dict[str, Any],
    *,
    semantic_checks: bool = False,
) -> dict[str, Any]:
    source_name = edit.get("source_test")
    new_name = edit.get("new_name")
    if not isinstance(source_name, str) or source_name not in tests:
        raise StructuredEditError(f"unknown source test: {source_name!r}")
    if not isinstance(new_name, str) or not new_name.startswith("test_") or not new_name.isidentifier():
        raise StructuredEditError("new typed clone name must be a valid test_ identifier")
    if new_name in tests:
        raise StructuredEditError(f"test already exists: {new_name}")
    source = tests[source_name]
    clone = copy.deepcopy(source)
    clone.name = new_name
    original_statement_dumps = {_dump(statement) for statement in clone.body}
    module_symbols = _module_symbols(tree)
    source_local_names = _function_local_names(source)
    introduced_names: set[str] = set()
    insertion_index = (
        1
        if clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and isinstance(clone.body[0].value.value, str)
        else 0
    )
    available = _available_names_at(clone, insertion_index, module_symbols)
    steps, inserted_count = _typed_step_statements(
        edit.get("steps"),
        available,
        source_local_names,
        introduced_names,
        "steps",
        semantic_checks=semantic_checks,
    ) if "steps" in edit else ([], 0)
    if semantic_checks and any(
        _dump(statement) in original_statement_dumps for statement in steps
    ):
        raise StructuredEditError(
            "typed operation duplicates an existing source-test statement"
        )
    clone.body[insertion_index:insertion_index] = steps
    inserted_count += _typed_anchor_insertions(
        clone,
        edit.get("insert_before_steps"),
        module_symbols,
        source_local_names,
        introduced_names,
        semantic_checks=semantic_checks,
        original_statement_dumps=original_statement_dumps,
    )
    changed_assignments = _replace_typed_assignments(
        clone,
        edit.get("replace_assignments"),
        module_symbols | source_local_names | introduced_names,
        semantic_checks=semantic_checks,
    )
    if inserted_count == 0 and not changed_assignments:
        raise StructuredEditError("typed clone does not contain an operation")
    if _assertions(source) != _assertions(clone):
        raise StructuredEditError("typed clone operation changed inherited assertions")
    if semantic_checks:
        unused = sorted(
            name
            for name in introduced_names
            if not any(
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == name
                for node in ast.walk(clone)
            )
        )
        if unused:
            raise StructuredEditError(
                f"typed assigned values are never used after insertion: {unused}"
            )
    tree.body.append(clone)
    tests[new_name] = clone
    return {
        "op": "clone_test",
        "dsl": "typed_operations",
        "source_test": source_name,
        "new_name": new_name,
        "typed_operation_count": inserted_count,
        "assignment_targets_replaced": changed_assignments,
        "assertion_fingerprint": _sha256("\n".join(_assertions(source))),
    }


def _assignment_target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _replace_assignment_values(
    function: ast.FunctionDef | ast.AsyncFunctionDef, replacements: Any
) -> list[str]:
    if replacements is None:
        return []
    if not isinstance(replacements, list):
        raise StructuredEditError("assignment_replacements must be a list")
    changed: list[str] = []
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise StructuredEditError("assignment replacement must be an object")
        target = replacement.get("target")
        expression = replacement.get("expression")
        if not isinstance(target, str) or not target:
            raise StructuredEditError("assignment replacement requires a target name")
        if not isinstance(expression, str) or not expression.strip():
            raise StructuredEditError("assignment replacement requires an expression")
        try:
            value = ast.parse(expression, mode="eval").body
        except SyntaxError as exc:
            raise StructuredEditError(f"invalid replacement expression: {exc}") from exc
        _validate_inserted_statements(
            [ast.Assign(targets=[ast.Name(id=target, ctx=ast.Store())], value=value)]
        )
        matches: list[ast.Assign | ast.AnnAssign] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                if _assignment_target_name(node.targets[0]) == target:
                    matches.append(node)
            elif isinstance(node, ast.AnnAssign):
                if _assignment_target_name(node.target) == target:
                    matches.append(node)
        if len(matches) != 1:
            raise StructuredEditError(
                f"assignment target must occur exactly once in cloned test: {target}"
            )
        matches[0].value = value
        changed.append(target)
    return changed


def _clone_test(
    tree: ast.Module,
    tests: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    edit: dict[str, Any],
    *,
    semantic_checks: bool = False,
) -> dict[str, Any]:
    if any(
        field in edit for field in ("steps", "insert_before_steps", "replace_assignments")
    ):
        if any(
            field in edit
            for field in ("insert_at_start", "insert_before", "assignment_replacements")
        ):
            raise StructuredEditError(
                "typed clone cannot mix typed operations with legacy Python string edits"
            )
        return _clone_test_typed(
            tree, tests, edit, semantic_checks=semantic_checks
        )
    source_name = edit.get("source_test")
    new_name = edit.get("new_name")
    if not isinstance(source_name, str) or source_name not in tests:
        raise StructuredEditError(f"unknown source test: {source_name!r}")
    if (
        not isinstance(new_name, str)
        or not new_name.startswith("test_")
        or not new_name.isidentifier()
    ):
        raise StructuredEditError("new clone name must be a valid test_ identifier")
    if new_name in tests:
        raise StructuredEditError(f"test already exists: {new_name}")

    source = tests[source_name]
    clone = copy.deepcopy(source)
    clone.name = new_name
    replacements = _replace_assignment_values(
        clone, edit.get("assignment_replacements")
    )
    insertions = _parse_insertions(edit.get("insert_at_start"))
    insertion_index = (
        1
        if clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and isinstance(clone.body[0].value.value, str)
        else 0
    )
    clone.body[insertion_index:insertion_index] = insertions
    anchored_insertions = _insert_before_anchors(clone, edit.get("insert_before"))
    if _assertions(source) != _assertions(clone):
        raise StructuredEditError("clone operation changed inherited assertions")
    tree.body.append(clone)
    tests[new_name] = clone
    return {
        "op": "clone_test",
        "source_test": source_name,
        "new_name": new_name,
        "inserted_statement_count": len(insertions) + anchored_insertions,
        "assignment_targets_replaced": replacements,
        "assertion_fingerprint": _sha256("\n".join(_assertions(source))),
    }


def apply_structured_repair(
    baseline_source: str, specification: dict[str, Any]
) -> StructuredRepair:
    """Apply a versioned repair DSL and return source plus an audit receipt."""
    if not isinstance(specification, dict) or specification.get("version") not in {
        1,
        2,
        3,
    }:
        raise StructuredEditError("repair specification version must be 1, 2, or 3")
    edits = specification.get("edits")
    if not isinstance(edits, list) or not edits:
        raise StructuredEditError("repair specification requires a non-empty edits list")
    try:
        baseline_tree = ast.parse(baseline_source)
    except SyntaxError as exc:
        raise StructuredEditError(f"baseline is not valid Python: {exc}") from exc
    tree = copy.deepcopy(baseline_tree)
    tests = _tests(tree)
    strategy_aliases = _strategy_aliases(tree)
    given_aliases = _hypothesis_decorator_aliases(tree, "given")
    example_aliases = _hypothesis_decorator_aliases(tree, "example")
    applied: list[dict[str, Any]] = []
    inherited_functions: dict[str, str] = {}

    for edit in edits:
        if not isinstance(edit, dict):
            raise StructuredEditError("each edit must be an object")
        operation = edit.get("op")
        if operation == "clone_test":
            if specification["version"] in {2, 3} and not any(
                field in edit
                for field in ("steps", "insert_before_steps", "replace_assignments")
            ):
                raise StructuredEditError(
                    "versions 2 and 3 clone_test require typed operation fields"
                )
            applied.append(
                _clone_test(
                    tree,
                    tests,
                    edit,
                    semantic_checks=specification["version"] == 3,
                )
            )
            inherited_functions[applied[-1]["new_name"]] = applied[-1]["source_test"]
            continue
        test_name = edit.get("test")
        if not isinstance(test_name, str) or test_name not in tests:
            raise StructuredEditError(f"unknown target test: {test_name!r}")
        if operation == "extend_given":
            applied.append(
                _extend_given(
                    tests[test_name], edit, strategy_aliases, given_aliases
                )
            )
        elif operation == "add_examples":
            applied.append(_add_examples(tests[test_name], edit, example_aliases))
        else:
            raise StructuredEditError(f"unsupported edit operation: {operation!r}")

    ast.fix_missing_locations(tree)
    repaired_source = ast.unparse(tree) + "\n"
    audit = audit_repair(
        baseline_source,
        repaired_source,
        inherited_functions=inherited_functions,
    )
    if not audit["accepted"]:
        raise StructuredEditError(f"generated repair failed base audit: {audit}")

    repaired_tree = ast.parse(repaired_source)
    baseline_tests = _tests(baseline_tree)
    repaired_tests = _tests(repaired_tree)
    changed_existing_bodies = sorted(
        name
        for name, baseline_test in baseline_tests.items()
        if _dump(ast.Module(body=baseline_test.body, type_ignores=[]))
        != _dump(ast.Module(body=repaired_tests[name].body, type_ignores=[]))
    )
    changed_existing_assertions = sorted(
        name
        for name, baseline_test in baseline_tests.items()
        if _assertions(baseline_test) != _assertions(repaired_tests[name])
    )
    if changed_existing_bodies or changed_existing_assertions:
        raise StructuredEditError("structured repair changed an existing test body")

    normalized_spec = json.dumps(
        specification, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    receipt = {
        "dsl_version": specification["version"],
        "baseline_sha256": _sha256(baseline_source),
        "specification_sha256": _sha256(normalized_spec),
        "repaired_sha256": _sha256(repaired_source),
        "baseline_test_count": len(baseline_tests),
        "repaired_test_count": len(repaired_tests),
        "added_test_functions": sorted(set(repaired_tests) - set(baseline_tests)),
        "changed_existing_test_bodies": changed_existing_bodies,
        "changed_existing_assertions": changed_existing_assertions,
        "applied_edits": applied,
        "base_audit": audit,
        "accepted": True,
    }
    return StructuredRepair(source=repaired_source, receipt=receipt)
