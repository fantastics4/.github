"""T007/T009 regression tests: workflow triggers, pinning and preserved legacy paths."""

import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CALLER = os.path.join(ROOT, "callers", "pr-llm-review-v2.yml")
REFRESH = os.path.join(ROOT, "callers", "pr-llm-review-refresh-v2.yml")
REUSABLE = os.path.join(ROOT, ".github", "workflows", "llm-pr-review-v2.yml")
LEGACY_WORKFLOW = os.path.join(ROOT, ".github", "workflows", "llm-pr-review.yml")
LEGACY_SCRIPT = os.path.join(ROOT, "scripts", "llm_review.py")
LEGACY_CALLER = os.path.join(ROOT, "callers", "pr-llm-review.yml")
PIN = re.compile(r"uses:\s*\S+@([0-9a-f]{40})")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def raw(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class CallerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = raw(CALLER)
        self.doc = load(CALLER)

    def test_should_trigger_on_pull_request_events_including_edited(self):
        triggers = self.doc[True] if True in self.doc else self.doc["on"]
        self.assertEqual(
            ["opened", "synchronize", "reopened", "ready_for_review", "edited"],
            triggers["pull_request"]["types"],
        )

    def test_should_not_depend_on_a_ci_workflow_name(self):
        self.assertNotIn("workflow_run", self.text)
        self.assertNotIn('"CI"', self.text)

    def test_should_keep_manual_dispatch_with_a_pr_number_input(self):
        triggers = self.doc[True] if True in self.doc else self.doc["on"]
        self.assertTrue(triggers["workflow_dispatch"]["inputs"]["pr_number"]["required"])

    def test_should_admit_before_taking_the_concurrency_slot(self):
        review = self.doc["jobs"]["review"]
        self.assertEqual("admission", list(self.doc["jobs"])[0])
        self.assertIn("needs: admission", self.text)
        self.assertIn("if: needs.admission.outputs.should_review == 'true'", self.text)
        self.assertIn("needs.admission.outputs.pr_number", review["concurrency"]["group"])
        self.assertNotIn("concurrency", self.doc)

    def test_should_pin_the_release_and_never_use_a_branch(self):
        self.assertIn("__RELEASE_SHA__", self.text)
        self.assertNotIn("@main", self.text)
        self.assertNotIn("@develop", self.text)

    def test_should_pin_every_third_party_action_to_a_sha(self):
        for line in self.text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:"):
                continue
            if "__RELEASE_SHA__" in stripped:  # the reusable workflow, pinned by release
                continue
            self.assertIsNotNone(PIN.search(line), line)

    def test_should_keep_minimal_permissions_and_the_model_secret(self):
        self.assertEqual("read", self.doc["permissions"]["contents"])
        self.assertEqual("write", self.doc["permissions"]["statuses"])
        self.assertEqual("read", self.doc["permissions"]["actions"])
        self.assertNotIn("actions", self.doc["jobs"]["admission"]["permissions"])
        self.assertIn("OPENROUTER_API_KEY", raw(CALLER))


class RefreshWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.doc = load(REFRESH)
        self.text = raw(REFRESH)

    def test_should_run_on_integration_branch_pushes_and_manual_dispatch(self):
        triggers = self.doc[True] if True in self.doc else self.doc["on"]
        self.assertIn("push", triggers)
        self.assertIn("__BRANCHES__", self.text)
        self.assertIn("workflow_dispatch", triggers)

    def test_should_scope_actions_write_to_the_dispatcher_only(self):
        self.assertEqual("write", self.doc["permissions"]["actions"])
        self.assertEqual("read", self.doc["permissions"]["contents"])

    def test_should_not_hold_the_model_secret(self):
        self.assertNotIn("OPENROUTER", self.text)

    def test_should_pin_the_release_and_the_actions(self):
        self.assertIn("__RELEASE_SHA__", self.text)
        self.assertNotIn("@main", self.text)
        for line in self.text.splitlines():
            if line.strip().startswith("uses:"):
                self.assertIsNotNone(PIN.search(line), line)


class ReusableWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.doc = load(REUSABLE)
        self.text = raw(REUSABLE)

    def test_should_declare_the_review_inputs_and_outputs(self):
        call = (
            self.doc[True]["workflow_call"] if True in self.doc else self.doc["on"]["workflow_call"]
        )
        for name in (
            "pr_number",
            "model",
            "max_diff_chars",
            "max_completion_tokens",
            "reasoning_effort",
            "gate",
            "blocking_categories",
            "blocking_bug_severities",
            "allow_draft",
        ):
            self.assertIn(name, call["inputs"])
        self.assertEqual("z-ai/glm-5.3-flash", call["inputs"]["model"]["default"])
        self.assertEqual(500000, call["inputs"]["max_diff_chars"]["default"])
        self.assertEqual(65536, call["inputs"]["max_completion_tokens"]["default"])
        self.assertFalse(call["inputs"]["gate"]["default"])
        self.assertTrue(call["secrets"]["OPENROUTER_API_KEY"]["required"])
        self.assertIn("review_state", call["outputs"])

    def test_should_checkout_the_tooling_at_the_same_revision_as_the_workflow(self):
        self.assertIn("repository: ${{ job.workflow_repository }}", self.text)
        self.assertIn("ref: ${{ job.workflow_sha }}", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertNotIn("github.sha", self.text)

    def test_should_never_check_out_the_pull_request(self):
        self.assertNotIn("pull_request.head", self.text)
        self.assertNotIn("refs/pull", self.text)

    def test_should_pin_the_actions(self):
        for line in self.text.splitlines():
            if line.strip().startswith("uses:"):
                self.assertIsNotNone(PIN.search(line), line)

    def test_should_run_the_tools_own_entry_point(self):
        self.assertIn("scripts/reviewer_v2/review.py", self.text)
        self.assertNotIn("scripts/llm_review.py", self.text)


class LegacyPreservationTests(unittest.TestCase):
    """While consumers remain on the legacy caller, its behaviour must not change."""

    def test_should_keep_the_legacy_workflow_script_and_caller(self):
        for path in (LEGACY_WORKFLOW, LEGACY_SCRIPT, LEGACY_CALLER):
            self.assertTrue(os.path.exists(path), path)

    def test_should_leave_the_legacy_caller_contract_untouched(self):
        text = raw(LEGACY_CALLER)
        self.assertIn('workflows: ["CI"]', text)
        self.assertIn("llm-pr-review.yml@main", text)

    def test_should_keep_the_legacy_workflow_defaults_untouched(self):
        text = raw(LEGACY_WORKFLOW)
        self.assertIn("default: 500000", text)
        self.assertIn("scripts/llm_review.py", text)


if __name__ == "__main__":
    unittest.main()
