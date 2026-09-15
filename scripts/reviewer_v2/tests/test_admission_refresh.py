"""T007 regression tests: admission matrix and base-refresh de-duplication."""

import unittest

from reviewer_v2 import admission as A
from reviewer_v2 import base_refresh as B
from reviewer_v2 import config as _config
from reviewer_v2.tests import support

REPO = support.REPO


def event(action, draft=False, head_repo=REPO, state="open"):
    return {
        "action": action,
        "pull_request": {
            "number": 86,
            "draft": draft,
            "state": state,
            "base": {"ref": "develop", "sha": "b" * 40, "repo": {"full_name": REPO}},
            "head": {"ref": "f", "sha": "a" * 40, "repo": {"full_name": head_repo}},
        },
    }


class AdmissionTests(unittest.TestCase):
    def decide(self, name, payload, **kwargs):
        return A.decide(name, payload, repository=REPO, **kwargs)

    def test_should_admit_open_and_push_and_reopen_and_ready(self):
        for action in ("opened", "synchronize", "reopened", "ready_for_review"):
            with self.subTest(action=action):
                result = self.decide("pull_request", event(action))
                self.assertTrue(result.should_review)
                self.assertEqual("86", result.pr_number)
                self.assertFalse(result.allow_draft)

    def test_should_skip_drafts_and_closed_pull_requests(self):
        self.assertFalse(self.decide("pull_request", event("opened", draft=True)).should_review)
        self.assertFalse(self.decide("pull_request", event("closed", state="closed")).should_review)

    def test_should_admit_only_review_relevant_edits(self):
        relevant = event("edited")
        relevant["changes"] = {"body": {"from": "old"}}
        self.assertTrue(self.decide("pull_request", relevant).should_review)
        for changed in ("title", "base"):
            payload = event("edited")
            payload["changes"] = {changed: {"from": "x"}}
            self.assertTrue(self.decide("pull_request", payload).should_review)
        irrelevant = event("edited")
        irrelevant["changes"] = {"labels": {}}
        self.assertFalse(self.decide("pull_request", irrelevant).should_review)

    def test_should_skip_fork_pull_requests_and_offer_manual_review(self):
        result = self.decide("pull_request", event("opened", head_repo="someone/fork"))
        self.assertFalse(result.should_review)
        self.assertIn("manual", result.reason)

    def test_should_require_a_canonical_number_for_manual_dispatch(self):
        for value in ("086", " 86", "86 ", "8e1", "+86", "0", "abc", ""):
            with self.subTest(value=value):
                result = self.decide("workflow_dispatch", {"inputs": {"pr_number": value}})
                self.assertFalse(result.should_review)
        allowed = self.decide("workflow_dispatch", {"inputs": {"pr_number": "86"}})
        self.assertTrue(allowed.should_review)
        self.assertTrue(allowed.allow_draft)

    def test_should_ignore_push_and_other_events(self):
        for name in ("push", "schedule", "workflow_run"):
            with self.subTest(name=name):
                self.assertFalse(self.decide(name, {}).should_review)

    def test_should_reject_a_manual_dispatch_for_another_repository(self):
        pr = support.pr_payload(number=86)
        pr["base"]["repo"]["full_name"] = "other/repo"
        result = self.decide("workflow_dispatch", {"inputs": {"pr_number": "86"}}, pr=pr)
        self.assertFalse(result.should_review)

    def test_should_emit_workflow_outputs(self):
        outputs = self.decide("pull_request", event("opened")).outputs()
        self.assertEqual("true", outputs["should_review"])
        self.assertEqual("86", outputs["pr_number"])


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.config = _config.Config.from_env({})
        self.pr = support.pr_payload(number=86)
        self.expected = B.expected_identity(self.pr, self.config)

    def identity(self, **overrides):
        data = dict(self.expected, review_state="complete", verdict="green")
        data.update(overrides)
        return data

    def test_should_skip_when_an_equivalent_complete_review_exists(self):
        refresh, reason = B.needs_refresh(self.pr, self.identity(), None, self.expected)
        self.assertFalse(refresh, reason)

    def test_should_refresh_when_the_base_moved(self):
        moved = dict(self.expected, base_sha="e" * 40)
        refresh, reason = B.needs_refresh(self.pr, self.identity(), None, moved)
        self.assertTrue(refresh)
        self.assertIn("base_sha", reason)

    def test_should_refresh_when_the_config_changed(self):
        changed = dict(self.expected, config_hash="1" * 64)
        refresh, _ = B.needs_refresh(self.pr, self.identity(), None, changed)
        self.assertTrue(refresh)

    def test_should_refresh_when_no_trusted_comment_exists(self):
        refresh, reason = B.needs_refresh(self.pr, None, None, self.expected)
        self.assertTrue(refresh)
        self.assertIn("no verifiable", reason)

    def test_should_skip_while_a_review_is_in_flight(self):
        refresh, reason = B.needs_refresh(
            self.pr, self.identity(), {"state": "pending"}, self.expected
        )
        self.assertFalse(refresh)
        self.assertIn("in flight", reason)

    def test_should_refresh_a_previous_incomplete_result(self):
        refresh, reason = B.needs_refresh(
            self.pr, self.identity(review_state="incomplete"), None, self.expected
        )
        self.assertTrue(refresh)
        self.assertIn("incomplete", reason)

    def test_should_respect_the_dispatch_budget_and_deduplicate(self):
        others = [support.pr_payload(number=n) for n in (90, 91)]
        github = support.FakeGitHub(pr=self.pr, open_pulls=[self.pr] + others)
        decisions = B.plan_refresh(
            github, "develop", self.config, (support.ACTOR,), max_prs=1, publish=True
        )
        dispatched = [item for item in decisions if item.get("dispatched")]
        self.assertEqual(1, len(dispatched))
        self.assertEqual([("pr-llm-review.yml", "develop", {"pr_number": "86"})], github.dispatched)
        self.assertTrue(any(not item["refresh"] for item in decisions))

    def test_should_never_dispatch_for_a_draft(self):
        draft = support.pr_payload(number=88, draft=True)
        github = support.FakeGitHub(pr=self.pr, open_pulls=[draft])
        decisions = B.plan_refresh(github, "develop", self.config, (support.ACTOR,), 5)
        self.assertEqual([], github.dispatched)
        self.assertEqual("draft", decisions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
