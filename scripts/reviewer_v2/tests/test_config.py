"""T007/T010 regression tests: defaults, validation and the documented table."""

import os
import re
import unittest

from reviewer_v2 import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class DefaultTests(unittest.TestCase):
    def setUp(self):
        self.config = C.Config.from_env({})

    def test_should_keep_the_reviewed_model_and_budgets(self):
        self.assertEqual("z-ai/glm-5.3-flash", self.config.model)
        self.assertEqual("medium", self.config.reasoning_effort)
        self.assertEqual(500000, self.config.budgets.max_diff_chars)
        self.assertEqual(65536, self.config.budgets.max_completion_tokens)
        self.assertFalse(self.config.gate)
        self.assertFalse(self.config.allow_draft)
        self.assertEqual(
            ("security", "data-loss", "breaking"), self.config.policy.blocking_categories
        )
        self.assertEqual(("critical", "high"), self.config.policy.blocking_bug_severities)
        self.assertEqual(("github-actions[bot]",), self.config.trusted_actors)

    def test_should_hash_only_review_relevant_configuration(self):
        base = self.config.config_hash()
        other = C.Config.from_env({"MODEL": "other/model"}).config_hash()
        self.assertNotEqual(base, other)
        stricter = C.Config.from_env({"BLOCKING_BUG_SEVERITIES": "critical"}).config_hash()
        self.assertNotEqual(base, stricter)
        unrelevant = C.Config.from_env({"GATE": "true"}).config_hash()
        self.assertEqual(base, unrelevant)

    def test_should_document_the_defaults_in_the_readme_table(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        expected = {
            "model": "`z-ai/glm-5.3-flash`",
            "max_diff_chars": "`500000`",
            "max_completion_tokens": "`65536`",
            "reasoning_effort": "`medium`",
            "gate": "`false`",
        }
        for name, value in expected.items():
            row = re.search(rf"\|\s*`{name}`\s*\|([^\n]*)", readme)
            self.assertIsNotNone(row, f"README has no input row for {name}")
            self.assertIn(value, row.group(1), f"README row for {name} is out of date")

    def test_should_expose_every_budget_as_a_typed_workflow_input(self):
        import yaml

        path = os.path.join(ROOT, ".github", "workflows", "llm-pr-review-v2.yml")
        with open(path, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
            handle.seek(0)
            text = handle.read()
        call = doc[True]["workflow_call"] if True in doc else doc["on"]["workflow_call"]
        inputs = call["inputs"]
        expected = {
            "model": C.DEFAULT_MODEL,
            "max_diff_chars": C.DEFAULT_MAX_DIFF_CHARS,
            "max_completion_tokens": C.DEFAULT_MAX_COMPLETION_TOKENS,
            "reasoning_effort": C.DEFAULT_REASONING_EFFORT,
            "max_chunks": C.DEFAULT_MAX_CHUNKS,
            "request_timeout_seconds": C.DEFAULT_REQUEST_TIMEOUT_SECONDS,
            "max_attempts": C.DEFAULT_MAX_ATTEMPTS,
            "total_budget_seconds": C.DEFAULT_TOTAL_BUDGET_SECONDS,
            "completion_reserve_tokens": C.DEFAULT_COMPLETION_RESERVE_TOKENS,
            "model_context_tokens": C.DEFAULT_MODEL_CONTEXT_TOKENS,
            "max_comment_chars": C.DEFAULT_MAX_COMMENT_CHARS,
        }
        for name, default in expected.items():
            self.assertIn(name, inputs, f"{name} is not a reusable-workflow input")
            self.assertEqual(default, inputs[name]["default"], f"{name} default drifted")
        # Every tunable must also be wired to the tooling, or it stays unreachable.
        for env_name in (
            "MAX_DIFF_CHARS",
            "MAX_CHUNKS",
            "REQUEST_TIMEOUT_SECONDS",
            "MAX_ATTEMPTS",
            "TOTAL_BUDGET_SECONDS",
            "COMPLETION_RESERVE_TOKENS",
            "MODEL_CONTEXT_TOKENS",
            "MAX_COMMENT_CHARS",
        ):
            self.assertIn(env_name, text, f"{env_name} is not passed to the tooling")
        # Worst-case retries plus publication must fit inside the job timeout.
        timeout = doc["jobs"]["review"]["timeout-minutes"]
        self.assertLess(C.DEFAULT_TOTAL_BUDGET_SECONDS, timeout * 60)
        # The bot-identity allowlist is a security boundary, not a caller knob.
        self.assertNotIn("trusted_actors", inputs)
        self.assertNotIn("TRUSTED_ACTORS", text)

    def test_should_document_pr_number_as_required(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        row = re.search(r"\|\s*`pr_number`\s*\|([^\n]*)", readme)
        self.assertIsNotNone(row)
        self.assertIn("yes", row.group(1).lower())


class ValidationTests(unittest.TestCase):
    def test_should_reject_invalid_values_before_paid_inference(self):
        cases = {
            "MAX_DIFF_CHARS": "zero",
            "GATE": "yes",
            "REASONING_EFFORT": "turbo",
            "BLOCKING_CATEGORIES": "security,typo",
            "BLOCKING_BUG_SEVERITIES": "blocker",
            "OPENROUTER_BASE_URL": "http://insecure.example",
            "MAX_COMPLETION_TOKENS": "99999999",
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(C.ConfigError):
                    C.Config.from_env({name: value})

    def test_should_reject_an_empty_trusted_actor_list(self):
        with self.assertRaises(C.ConfigError):
            C.Config.from_env({"TRUSTED_ACTORS": " , "})

    def test_should_report_missing_required_environment_variables(self):
        with self.assertRaises(C.ConfigError) as caught:
            C.require_env({"GITHUB_TOKEN": "x"})
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
