import unittest
from types import SimpleNamespace

from eval.run_pbt import collect_test_functions, run_test_in_workspace


class FakeWorkspace:
    def __init__(self):
        self.timeout = None

    def execute_command(self, command, timeout=30.0):
        self.command = command
        self.timeout = timeout
        return SimpleNamespace(
            stdout="pbt_test.py::test_one\npbt_test.py::TestGroup::test_two\n",
            stderr="",
            exit_code=0,
        )


class CollectTestFunctionsTests(unittest.TestCase):
    def test_collection_timeout_is_forwarded(self):
        workspace = FakeWorkspace()
        functions, status, error = collect_test_functions(
            workspace, collection_timeout_s=120.0
        )
        self.assertEqual(workspace.timeout, 120.0)
        self.assertEqual(functions, ["test_one", "TestGroup::test_two"])
        self.assertEqual(status, "success")
        self.assertIsNone(error)

    def test_hypothesis_seed_is_forwarded_to_pytest(self):
        workspace = FakeWorkspace()
        result = run_test_in_workspace(
            workspace,
            "pbt_test.py::test_one",
            "/workspace/lib",
            hypothesis_seed=20260815,
        )
        self.assertIn("--hypothesis-seed=20260815", workspace.command)
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
