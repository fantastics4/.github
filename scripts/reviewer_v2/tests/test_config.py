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
        self.assertEqual("high", self.config.reasoning_effort)
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
            "reasoning_effort": "`high`",
            "gate": "`false`",
        }
        for name, value in expected.items():
            row = re.search(rf"\|\s*`{name}`\s*\|([^\n]*)", readme)
            self.assertIsNotNone(row, f"README has no input row for {name}")
            self.assertIn(value, row.group(1), f"README row for {name} is out of date")

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
