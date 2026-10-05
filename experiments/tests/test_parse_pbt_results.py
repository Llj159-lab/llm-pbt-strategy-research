import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from parse_pbt_results import parse_instance, parse_run_dir  # noqa: E402


def make_record(**overrides):
    test_result = {
        "bug_results": [
            {"bug_id": "bug_1", "found": True, "detection_method": "conservative", "function_results": [{"function": "test_strict", "buggy_passed": False, "fixed_passed": True}]},
            {"bug_id": "bug_2", "found": True, "detection_method": "liberal", "function_results": [{"function": "test_liberal", "buggy_passed": False, "fixed_passed": False}]},
            {"bug_id": "bug_3", "found": False, "detection_method": "not_found", "function_results": []},
        ],
        "bugs_found": 2, "bugs_total": 3, "recall": 2 / 3, "perfect_solve": False,
        "false_positive": False, "f2p_any": True, "eval_phase_aborted": False,
        "eval_test_runs_timeout_count": 0, "function_collection_status": "success",
        "function_collection_error": None, "test_file_found": True, "uses_hypothesis": True,
    }
    record = {
        "instance_id": "TEST-001", "agent_terminal_status": "normal_finished",
        "conversation_error": None, "error": None,
        "agent_test": "from hypothesis import given\n\n@given()\ndef test_ok():\n    pass\n",
        "test_result": test_result,
        "timeout_stats": {"agent_timed_out": False, "agent_wall_clock_s": 2.0, "agent_wall_clock_limit_s": 2400},
    }
    record.update(overrides)
    return record


class ParsePbtResultsTests(unittest.TestCase):
    def test_account_error_is_read_from_conversation_event(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            events = run / "_workspaces" / "TEST-001" / "conversations" / "c1" / "events"
            events.mkdir(parents=True)
            (events / "event-00001.json").write_text(json.dumps({
                "kind": "ConversationErrorEvent",
                "code": "LLMAuthenticationError",
                "detail": "Access denied: overdue-payment",
            }), encoding="utf-8")
            parsed = parse_instance(make_record(), run)
            self.assertEqual(parsed["execution_health"]["status"], "error")
            self.assertTrue(parsed["execution_health"]["account_error"])
            self.assertTrue(parsed["failure_categories"]["account_error"])

    def test_strict_liberal_and_missed_bug_ids(self):
        result = parse_instance(make_record(), Path("run"))["result"]
        self.assertEqual(result["strict_f2p_bug_ids"], ["bug_1"])
        self.assertEqual(result["liberal_bug_ids"], ["bug_2"])
        self.assertEqual(result["missed_bug_ids"], ["bug_3"])

    def test_fixed_version_failures_are_counted_separately(self):
        parsed = parse_instance(make_record(), Path("run"))
        self.assertEqual(parsed["result"]["fixed_version_failure_functions"], ["bug_2:test_liberal"])
        self.assertEqual(parsed["failure_categories"]["fixed_version_failure_count"], 1)

    def test_missed_bug_is_only_a_no_trigger_candidate(self):
        categories = parse_instance(make_record(), Path("run"))["failure_categories"]
        self.assertEqual(categories["no_trigger_candidate_bug_ids"], ["bug_3"])
        self.assertIn("not proof", categories["interpretation_note"])

    def test_timeout_and_abort_are_execution_health_signals(self):
        record = make_record(timeout_stats={"agent_timed_out": True}, test_result={**make_record()["test_result"], "eval_phase_aborted": True})
        parsed = parse_instance(record, Path("run"))
        self.assertEqual(parsed["execution_health"]["status"], "error")
        self.assertTrue(parsed["execution_health"]["agent_timed_out"])

    def test_syntax_error_is_detected_by_ast(self):
        parsed = parse_instance(make_record(agent_test="def broken(:\n    pass\n"), Path("run"))
        self.assertEqual(parsed["failure_categories"]["syntax_error"], "observed")

    def test_import_error_is_detected_from_persisted_error_text(self):
        result = {**make_record()["test_result"], "function_collection_error": "ModuleNotFoundError: No module named x"}
        parsed = parse_instance(make_record(test_result=result), Path("run"))
        self.assertEqual(parsed["failure_categories"]["import_error"], "observed")

    def test_health_check_without_structured_field_is_unassessed(self):
        parsed = parse_instance(make_record(), Path("run"))
        self.assertEqual(parsed["failure_categories"]["health_check"], "unassessed")

    def test_unreliable_telemetry_is_not_in_aggregate(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "metadata.json").write_text(json.dumps({"llm_model": "test", "agent": "test", "prompt_template": "p", "max_iterations": 1}), encoding="utf-8")
            (run / "output.jsonl").write_text(json.dumps(make_record()) + "\n", encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({"total_functions": 999, "usage": {"total_prompt_tokens": 123}}), encoding="utf-8")
            parsed = parse_run_dir(run)
            self.assertNotIn("total_functions", parsed["aggregate"])
            self.assertIn("total_functions", parsed["excluded_telemetry"])

    def test_invalid_account_task_is_excluded_from_recall_denominator(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "metadata.json").write_text(json.dumps({"llm_model": "test"}), encoding="utf-8")
            invalid = make_record(instance_id="TEST-002", test_result={
                **make_record()["test_result"], "test_file_found": False,
                "bugs_found": 0, "bugs_total": 4,
                "function_collection_error": "agent did not create pbt_test.py",
            })
            (run / "output.jsonl").write_text(
                json.dumps(make_record()) + "\n" + json.dumps(invalid) + "\n", encoding="utf-8"
            )
            parsed = parse_run_dir(run)
            self.assertEqual(parsed["aggregate"]["task_count"], 2)
            self.assertEqual(parsed["aggregate"]["valid_task_count"], 1)
            self.assertEqual(parsed["aggregate"]["invalid_task_count"], 1)
            self.assertEqual(parsed["aggregate"]["bugs_total"], 3)
            self.assertEqual(parsed["aggregate"]["bugs_found_any_criterion"], 2)

    def test_explicit_eval_override_replaces_scoring_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "metadata.json").write_text(json.dumps({"llm_model": "test"}), encoding="utf-8")
            original = make_record(test_result={
                **make_record()["test_result"],
                "bugs_found": 0,
                "function_collection_status": "failed",
                "function_collection_error": "collection timeout",
            })
            (run / "output.jsonl").write_text(json.dumps(original) + "\n", encoding="utf-8")
            workspace = run / "_workspaces" / "TEST-001"
            workspace.mkdir(parents=True)
            override = make_record()["test_result"]
            (workspace / "eval_result.collection120.json").write_text(
                json.dumps(override), encoding="utf-8"
            )

            parsed = parse_run_dir(run, eval_override_suffix="collection120")
            task = parsed["tasks"][0]
            self.assertEqual(task["execution_health"]["status"], "complete")
            self.assertEqual(task["result"]["bugs_found"], 2)
            self.assertEqual(task["result"]["strict_f2p_bug_ids"], ["bug_1"])
            self.assertEqual(
                task["provenance"]["evaluation_override"],
                "_workspaces/TEST-001/eval_result.collection120.json",
            )


if __name__ == "__main__":
    unittest.main()
