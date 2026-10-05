#!/usr/bin/env python3
"""Generate and apply a structured PBT repair without granting file tools."""

from __future__ import annotations

import argparse
import hashlib
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

from experiments.constrained_repair.model_dsl import (
    ModelDSLError,
    extract_json_object,
    given_selector_inventory,
    scrub_prompt_hints,
    validate_model_specification,
)
from experiments.constrained_repair.structured_edit import (
    StructuredEditError,
    apply_structured_repair,
)


PROTOCOL_VERSION = 2
SYSTEM_PROMPT = """You produce one JSON object for a structure-preserving repair of an existing Hypothesis test suite. You have no tools and must not output Python files, prose, Markdown, patches, assertions, imports, shell commands, or evaluator instructions. Your JSON is rejected unless it follows the supplied DSL exactly. Use only behavior supported by the supplied public documentation and tests. Do not claim that a bug is fixed or found."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_problem_dir(task_id: str) -> Path:
    matches = list(ROOT.glob(f"libraries/*/problems/{task_id}"))
    if len(matches) != 1:
        raise ValueError(f"expected one public problem directory for {task_id}")
    return matches[0]


def _read_context_files(problem_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    sections: list[str] = []
    manifest: list[dict[str, Any]] = []
    for directory_name in ("docs", "existing_tests"):
        directory = problem_dir / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if path.suffix.lower() not in {".md", ".rst", ".txt", ".py"}:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if directory_name == "existing_tests":
                source = scrub_prompt_hints(source)
            relative = path.relative_to(problem_dir).as_posix()
            sections.append(f"\n--- BEGIN PUBLIC FILE: {relative} ---\n{source}\n--- END PUBLIC FILE: {relative} ---")
            manifest.append(
                {
                    "path": relative,
                    "sha256": _sha256(source),
                    "bytes": len(source.encode("utf-8")),
                }
            )
    return "\n".join(sections), manifest


def _make_prompt(
    task_id: str,
    baseline_source: str,
    public_context: str,
    selector_inventory: str,
) -> str:
    return f"""Task ID: {task_id}

Return exactly one JSON object, optionally enclosed in one otherwise-empty ```json fence.

Required top-level object:
{{
  "version": 1,
  "study_role": "model-generated-development-only",
  "task_id": "{task_id}",
  "edits": [one to four edit objects]
}}

Allowed edits:
1. Extend one existing @given argument:
{{"op":"extend_given","test":"test_name","position":0,"strategy":"st.valid_strategy(...)"}}
Use exactly one of position or keyword. The original strategy is retained by the transformer.
For a named @given argument, use its exact keyword; position is legal only when the inventory lists that integer. An extension must add values outside the original strategy's support. Do not duplicate the original strategy, add only a known subtype/subset, or merely reweight existing values.

2. Add JSON-valued examples to an existing test:
{{"op":"add_examples","test":"test_name","examples":[{{"args":[],"kwargs":{{"name":1}}}}]}}

3. Clone an existing test while inheriting its assertions:
{{"op":"clone_test","source_test":"test_name","new_name":"test_new_name","insert_at_start":["statement"],"insert_before":[{{"anchor":"existing top-level statement","statements":["statement"]}}],"assignment_replacements":[{{"target":"local_name","expression":"documented_expression"}}]}}
At least one clone edit field is required. Omit unused fields.

The transformer rejects assertion changes, imports, control flow, return, raise, exception handling, nested definitions, dunder access, dangerous calls, unknown tests, unmatched anchors, and unsupported strategy namespaces. Prefer the smallest edit that adds a documented state transition, operation sequence, boundary value, or configuration combination. If an existing strategy already covers broad primitive values, prefer a clone_test operation sequence over strategy reweighting. Do not reproduce any example above unless it matches the supplied code.

Legal @given selectors derived mechanically from the frozen baseline:
{selector_inventory}

--- BEGIN FROZEN BASELINE pbt_test.py ---
{baseline_source}
--- END FROZEN BASELINE pbt_test.py ---

The following are the only public supporting files available to you. They exclude ground truth, patches, bug descriptions, evaluator output, and solution files.
{public_context}
"""


def _response_text(response: Any) -> str:
    parts = [
        content.text
        for content in response.message.content
        if hasattr(content, "text") and isinstance(content.text, str)
    ]
    return "\n".join(parts).strip()


def _public_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    sensitive_fragments = ("key", "token", "secret", "password", "credential")
    return {
        key: value
        for key, value in config.items()
        if not any(fragment in key.lower() for fragment in sensitive_fragments)
    }


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
    selector_inventory = given_selector_inventory(baseline_prompt_source)
    problem_dir = _find_problem_dir(args.problem_id)
    public_context, context_manifest = _read_context_files(problem_dir)
    prompt = _make_prompt(
        args.problem_id,
        baseline_prompt_source,
        public_context,
        selector_inventory,
    )

    config = json.loads(args.llm_config_path.read_text(encoding="utf-8"))
    from openhands.sdk import LLM
    from openhands.sdk.llm import Message, TextContent

    llm = LLM(**config)
    output_dir = args.output_dir.resolve() / args.problem_id
    output_dir.mkdir(parents=True, exist_ok=True)
    messages = [
        Message(role="system", content=[TextContent(text=SYSTEM_PROMPT)]),
        Message(role="user", content=[TextContent(text=prompt)]),
    ]
    attempts: list[dict[str, Any]] = []
    repair = None
    accepted_specification = None

    for attempt_number in range(1, args.max_attempts + 1):
        attempt_started = time.monotonic()
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "accepted": False,
            "error": None,
        }
        try:
            response = llm.completion(messages)
            response_text = _response_text(response)
            response_path = output_dir / f"attempt-{attempt_number:02d}-response.txt"
            response_path.write_text(response_text + "\n", encoding="utf-8")
            attempt["response_sha256"] = _sha256(response_text)
            attempt["response_file"] = response_path.name
            specification = extract_json_object(response_text)
            validate_model_specification(
                specification, args.problem_id, max_edits=args.max_edits
            )
            repair = apply_structured_repair(baseline_source, specification)
            accepted_specification = specification
            attempt["accepted"] = True
            attempt["specification_sha256"] = repair.receipt[
                "specification_sha256"
            ]
        except (ModelDSLError, StructuredEditError) as exc:
            attempt["error"] = str(exc)[:1000]
        except Exception as exc:
            attempt["error"] = f"llm_or_transport_error: {type(exc).__name__}: {exc}"[:1000]
            attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
            attempt["elapsed_seconds"] = round(time.monotonic() - attempt_started, 3)
            attempts.append(attempt)
            break
        attempt["finished_at"] = datetime.now(timezone.utc).isoformat()
        attempt["elapsed_seconds"] = round(time.monotonic() - attempt_started, 3)
        attempts.append(attempt)
        if repair is not None:
            break
        if attempt_number < args.max_attempts:
            messages.extend(
                [
                    Message(
                        role="assistant", content=[TextContent(text=response_text)]
                    ),
                    Message(
                        role="user",
                        content=[
                            TextContent(
                                text=(
                                    "The response was rejected before evaluation: "
                                    f"{attempt['error']}. Return a complete replacement "
                                    "JSON object only. Do not discuss the error."
                                )
                            )
                        ],
                    ),
                ]
            )

    status = "accepted" if repair is not None else "rejected_after_max_attempts"
    record = {
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
        "prompt_sha256": _sha256(SYSTEM_PROMPT + "\n" + prompt),
        "context_files": context_manifest,
        "max_attempts": args.max_attempts,
        "max_edits": args.max_edits,
        "attempts": attempts,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if repair is not None and accepted_specification is not None:
        (output_dir / "repair_specification.json").write_text(
            json.dumps(accepted_specification, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (output_dir / "pbt_test.py").write_text(repair.source, encoding="utf-8")
        (output_dir / "structured_repair_receipt.json").write_text(
            json.dumps(repair.receipt, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        record["repair_receipt"] = repair.receipt
    (output_dir / "model_structured_repair_record.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    if repair is None:
        raise SystemExit("model structured repair was not accepted")


if __name__ == "__main__":
    main()
