#!/usr/bin/env python3
"""Generate a non-oracular diagnosis and a compatible structured PBT repair."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.constrained_repair.model_diagnosis import (
    validate_diagnosis_dsl_consistency,
    validate_model_diagnosis,
)
from experiments.constrained_repair.model_dsl import (
    ModelDSLError,
    extract_json_object,
    given_selector_inventory,
    scrub_prompt_hints,
    validate_model_specification,
)
from experiments.constrained_repair.run_model_structured_repair import (
    _find_problem_dir,
    _public_llm_config,
    _read_context_files,
    _response_text,
    _sha256,
)
from experiments.constrained_repair.structured_edit import (
    StructuredEditError,
    apply_structured_repair,
)


PROTOCOL_VERSION = 5
SYSTEM_PROMPT = """You are part of a two-stage, structure-preserving Hypothesis test-development protocol. You have no tools. Do not output Python files, Markdown, patches, assertions, shell commands, or evaluator instructions. Return exactly one JSON object requested by the current stage. Use only the supplied frozen baseline and public documentation. Do not claim that any bug is present, absent, fixed, found, or evaluated."""


def _baseline_test_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _shared_context(
    task_id: str, baseline_source: str, public_context: str, selector_inventory: str
) -> str:
    return f"""Task ID: {task_id}

The frozen baseline, selector inventory, and public supporting files below are the only task materials. They exclude ground truth, patches, evaluator output, and solution files.

Legal @given selectors derived mechanically from the frozen baseline:
{selector_inventory}

--- BEGIN FROZEN BASELINE pbt_test.py ---
{baseline_source}
--- END FROZEN BASELINE pbt_test.py ---

{public_context}
"""


def _diagnosis_prompt(shared_context: str, task_id: str) -> str:
    return f"""Return exactly one JSON object, optionally in one otherwise-empty json fence.

Choose one observable coverage gap in one frozen baseline test. The rationale must describe only missing input, state, or operation coverage from public materials. It must not mention outcomes, failures, defects, bug IDs, assertions, ground truth, patches, or evaluation.

Required schema:
{{
  "version": 1,
  "study_role": "model-generated-development-only",
  "task_id": "{task_id}",
  "target_test": "an existing test name",
  "coverage_gap": "state_transition | operation_sequence | configuration_combination | boundary_value",
  "rationale": "brief coverage rationale",
  "required_operations": ["construct", "update", "observe"]
}}

For state_transition or operation_sequence, specify at least two required operations. Select only operation names from: construct, configure, update, remove, clear, copy, lookup, observe, serialize, deserialize.

{shared_context}"""


def _dsl_prompt(
    shared_context: str, diagnosis: dict[str, Any], task_id: str
) -> str:
    diagnosis_json = json.dumps(diagnosis, indent=2, ensure_ascii=False)
    return f"""Return exactly one JSON object, optionally in one otherwise-empty json fence.

The following diagnosis was accepted by a schema validator. Produce a structure-preserving edit compatible with it. Do not restate or reinterpret the diagnosis.

--- BEGIN ACCEPTED DIAGNOSIS ---
{diagnosis_json}
--- END ACCEPTED DIAGNOSIS ---

Required typed-operation DSL schema:
{{
  "version": 3,
  "study_role": "model-generated-development-only",
  "task_id": "{task_id}",
  "edits": [one to four edit objects]
}}

Allowed edit: one typed clone_test. It may contain `steps`, `insert_before_steps`, or `replace_assignments`; it must not contain Python source strings such as `insert_at_start`, `insert_before`, `assignment_replacements`, strategy expressions, or examples.

Step kinds:
1. {{"kind":"method_call","receiver":"ob","method":"move_to_end","args":[{{"ref":"key_to_move"}}],"kwargs":{{"last":false}}}}
2. {{"kind":"function_call","function":"list","args":[{{"ref":"ob"}}],"kwargs":{{}},"assign_to":"observed_keys"}}
3. {{"kind":"set_item","target":"mapping","key":{{"ref":"key"}},"value":{{"ref":"value"}}}}
4. {{"kind":"delete_item","target":"mapping","key":{{"literal":"old-key"}}}}

Use `{{"ref":"name"}}` for a variable reference and `{{"literal":"text"}}` for an intentional string literal. Never write a generated input variable such as `key` as the bare string `"key"`. `receiver`, `function`, `target`, and `assign_to` are identifiers. A receiver or reference must already be defined before the insertion point. `assign_to` must not duplicate any local variable already defined in the source test, and every assigned value must be consumed by a later operation or inherited assertion. `method` must be a non-dunder identifier. Only imported/baseline functions and safe builtins may be called. The anchor in `insert_before_steps` must exactly match one existing top-level statement; it is a location selector, not a new statement.

Valid shape example (replace names only with symbols from the frozen source): {{"op":"clone_test","source_test":"{diagnosis['target_test']}","new_name":"test_variant","insert_before_steps":[{{"anchor":"observed = list(ob)","steps":[{{"kind":"method_call","receiver":"ob","method":"move_to_end","args":[{{"ref":"other_key"}}],"kwargs":{{"last":false}}}}]}}]}}

For every diagnosis category, use clone_test with typed operations. Each inserted operation must differ from every complete source-test statement. Implement the diagnosed operation sequence rather than repeating the anchor or recording an unused observation. When the rationale names a documented API category, use the corresponding public API rather than a weaker neighboring API. Do not output imports, assertions, control flow, return, raise, exception handlers, nested definitions, lambda expressions, arbitrary Python expressions, or evaluator instructions. The deterministic transformer creates the Python AST and rejects undefined names, duplicate source variables, ambiguous item keys, unused assigned values, redundant operations, unsafe calls, dunder methods, unknown tests, and invalid anchors.

{shared_context}"""


def _run_stage(
    llm: Any,
    stage: str,
    prompt: str,
    output_dir: Path,
    max_attempts: int,
    validate: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    from openhands.sdk.llm import Message, TextContent

    messages = [
        Message(role="system", content=[TextContent(text=SYSTEM_PROMPT)]),
        Message(role="user", content=[TextContent(text=prompt)]),
    ]
    attempts: list[dict[str, Any]] = []
    accepted = None
    for number in range(1, max_attempts + 1):
        started = time.monotonic()
        attempt: dict[str, Any] = {
            "attempt": number,
            "accepted": False,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }
        try:
            response_text = _response_text(llm.completion(messages))
            response_path = output_dir / f"{stage}-attempt-{number:02d}-response.txt"
            response_path.write_text(response_text + "\n", encoding="utf-8")
            attempt["response_file"] = response_path.name
            attempt["response_sha256"] = _sha256(response_text)
            candidate = extract_json_object(response_text)
            validate(candidate)
            accepted = candidate
            attempt["accepted"] = True
        except (ModelDSLError, StructuredEditError) as exc:
            attempt["error"] = str(exc)[:1000]
        except Exception as exc:
            attempt["error"] = (
                f"llm_or_transport_error: {type(exc).__name__}: {exc}"
            )[:1000]
            attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
            attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
            attempts.append(attempt)
            break
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        attempts.append(attempt)
        if accepted is not None:
            break
        if number < max_attempts:
            messages.extend(
                [
                    Message(role="assistant", content=[TextContent(text=response_text)]),
                    Message(
                        role="user",
                        content=[
                            TextContent(
                                text=(
                                    "The response was rejected before evaluation: "
                                    f"{attempt['error']}. Return one complete replacement "
                                    "JSON object only. Do not discuss the error."
                                )
                            )
                        ],
                    ),
                ]
            )
    return accepted, attempts


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("llm_config_path", type=Path)
    parser.add_argument("--problem-id", required=True)
    parser.add_argument("--baseline-test", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--max-edits", type=int, default=4)
    return parser


def main() -> None:
    args = make_parser().parse_args()
    if not 1 <= args.max_attempts <= 3:
        raise SystemExit("max-attempts must be between 1 and 3")
    if not 1 <= args.max_edits <= 8:
        raise SystemExit("max-edits must be between 1 and 8")
    baseline_path = args.baseline_test.resolve()
    if not baseline_path.is_file():
        raise SystemExit(f"missing baseline test: {baseline_path}")
    baseline_source = baseline_path.read_text(encoding="utf-8")
    baseline_prompt_source = scrub_prompt_hints(baseline_source)
    test_names = _baseline_test_names(baseline_prompt_source)
    selector_inventory = given_selector_inventory(baseline_prompt_source)
    problem_dir = _find_problem_dir(args.problem_id)
    public_context, context_manifest = _read_context_files(problem_dir)
    shared_context = _shared_context(
        args.problem_id, baseline_prompt_source, public_context, selector_inventory
    )
    config = json.loads(args.llm_config_path.read_text(encoding="utf-8"))
    from openhands.sdk import LLM

    llm = LLM(**config)
    output_dir = args.output_dir.resolve() / args.problem_id
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnosis, diagnosis_attempts = _run_stage(
        llm,
        "diagnosis",
        _diagnosis_prompt(shared_context, args.problem_id),
        output_dir,
        args.max_attempts,
        lambda candidate: validate_model_diagnosis(
            candidate, args.problem_id, test_names
        ),
    )
    specification = None
    repair = None
    dsl_attempts: list[dict[str, Any]] = []
    if diagnosis is not None:
        specification, dsl_attempts = _run_stage(
            llm,
            "dsl",
            _dsl_prompt(shared_context, diagnosis, args.problem_id),
            output_dir,
            args.max_attempts,
            lambda candidate: (
                validate_model_specification(candidate, args.problem_id, args.max_edits),
                validate_diagnosis_dsl_consistency(diagnosis, candidate),
                apply_structured_repair(baseline_source, candidate),
            ),
        )
        if specification is not None:
            repair = apply_structured_repair(baseline_source, specification)
    status = "accepted" if repair is not None else "rejected_after_max_attempts"
    record: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "study_role": "model-generated-development-only",
        "task_id": args.problem_id,
        "status": status,
        "model_invoked": True,
        "ground_truth_files_available_to_model": False,
        "patches_available_to_model": False,
        "evaluator_outputs_available_to_model": False,
        "baseline_prompt_scrubbed": baseline_prompt_source != baseline_source,
        "model_config": _public_llm_config(config),
        "baseline_sha256": _sha256(baseline_source),
        "baseline_prompt_sha256": _sha256(baseline_prompt_source),
        "given_selector_inventory": selector_inventory.splitlines(),
        "diagnosis_prompt_sha256": _sha256(SYSTEM_PROMPT + "\n" + _diagnosis_prompt(shared_context, args.problem_id)),
        "context_files": context_manifest,
        "max_attempts": args.max_attempts,
        "max_edits": args.max_edits,
        "diagnosis_attempts": diagnosis_attempts,
        "dsl_attempts": dsl_attempts,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if diagnosis is not None:
        (output_dir / "accepted_diagnosis.json").write_text(
            json.dumps(diagnosis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        record["diagnosis"] = diagnosis
    if repair is not None and specification is not None:
        (output_dir / "repair_specification.json").write_text(
            json.dumps(specification, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (output_dir / "pbt_test.py").write_text(repair.source, encoding="utf-8")
        (output_dir / "structured_repair_receipt.json").write_text(
            json.dumps(repair.receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        record["repair_receipt"] = repair.receipt
    (output_dir / "diagnosis_guided_repair_record.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    if repair is None:
        raise SystemExit("diagnosis-guided structured repair was not accepted")


if __name__ == "__main__":
    main()
