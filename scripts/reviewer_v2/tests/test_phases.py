"""Phase-split tests: the workflow uploads the artifact between prepare and finalize.

Regression for the live canary failure: current runners do not expose the Actions
artifact runtime (ACTIONS_RUNTIME_URL/TOKEN) to run steps, so publication is split
around an actions/upload-artifact step. finalize must verify that artifact before
publishing the comment/status, and refuse success when it is missing.
"""

import json
import os
import tempfile
import unittest

from reviewer_v2 import config as _config
from reviewer_v2 import github_api as _github
from reviewer_v2 import review as R
from reviewer_v2.tests import support


def reply_request(model_payload):
    def request(url, **kwargs):
        return support.model_reply(model_payload)

    return request


class PhaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {
            "GITHUB_RUN_ID": "555",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SERVER_URL": "https://github.com",
            "GH_REPO": support.REPO,
            "RESULT_PATH": os.path.join(self.tmp, "result.json"),
            "GITHUB_OUTPUT": os.path.join(self.tmp, "out.txt"),
            "GITHUB_STEP_SUMMARY": os.path.join(self.tmp, "summary.md"),
        }
        self.config = _config.Config.from_env({"GATE": "false"})
        self.pr = support.pr_payload()

    def run_prepare(self, github, payload=None):
        return R.prepare(
            self.config,
            github,
            self.pr,
            request=reply_request(payload or support.model_payload()),
            api_key="test-key",
            env=self.env,
            log=lambda fields: None,
        )

    def run_finalize(self, github):
        return R.finalize(self.config, github, env=self.env, log=lambda fields: None)

    def prepared_artifact(self, github, run_id=555):
        return {
            "name": None,
            "expired": False,
            "id": 42,
            "size_in_bytes": 1234,
            "workflow_run": {"id": run_id},
        }

    def test_should_only_publish_pending_during_prepare(self):
        github = support.FakeGitHub(pr=self.pr)
        code, outputs, envelope = self.run_prepare(github)
        self.assertEqual(0, code)
        self.assertEqual("true", outputs["result_written"])
        self.assertTrue(outputs["artifact"].startswith("llm-review-result-"))
        self.assertEqual(["pending"], [item["state"] for item in github.status_list])
        self.assertEqual([], github.comment_list)
        self.assertEqual([], github.labels)
        with open(self.env["RESULT_PATH"], encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual("workflow", saved["artifact"]["upload"])
        self.assertEqual(outputs["artifact"], saved["artifact"]["name"])

    def test_should_publish_only_after_the_artifact_is_verified(self):
        github = support.FakeGitHub(pr=self.pr)
        _, outputs, _ = self.run_prepare(github)
        entry = self.prepared_artifact(github, int(self.env["GITHUB_RUN_ID"]))
        entry["name"] = outputs["artifact"]
        github.artifacts = [entry]
        code, final_outputs, envelope, failures = self.run_finalize(github)
        self.assertEqual(0, code, failures)
        self.assertEqual("green", final_outputs["verdict"])
        self.assertEqual("complete", final_outputs["review_state"])
        self.assertEqual(1, len(github.comment_list))
        self.assertTrue(github.comment_list[0]["body"].startswith(_github.MARKER))
        self.assertEqual("success", github.status_list[-1]["state"])
        self.assertEqual(["llm-review:green"], github.labels)
        self.assertEqual("verified", envelope["artifact"]["upload"])

    def test_should_refuse_success_when_the_workflow_artifact_is_missing(self):
        github = support.FakeGitHub(pr=self.pr)
        _, outputs, _ = self.run_prepare(github)
        code, final_outputs, envelope, failures = self.run_finalize(github)
        self.assertEqual(1, code)
        self.assertEqual("error", final_outputs["review_state"])
        self.assertEqual("", final_outputs["verdict"])
        self.assertTrue(any(item["category"] == "artifact" for item in failures))
        self.assertEqual("error", github.status_list[-1]["state"])
        self.assertEqual([], github.labels)

    def test_should_refuse_success_when_the_artifact_belongs_to_another_run(self):
        github = support.FakeGitHub(pr=self.pr)
        _, outputs, _ = self.run_prepare(github)
        entry = self.prepared_artifact(github, 999999)
        entry["name"] = outputs["artifact"]
        github.artifacts = [entry]
        code, final_outputs, _, failures = self.run_finalize(github)
        self.assertEqual(1, code)
        self.assertEqual("error", final_outputs["review_state"])
        self.assertTrue(any(item["category"] == "artifact" for item in failures))

    def test_finalize_should_discard_a_result_that_became_stale(self):
        github = support.FakeGitHub(pr=self.pr)
        _, outputs, _ = self.run_prepare(github)
        entry = self.prepared_artifact(github, int(self.env["GITHUB_RUN_ID"]))
        entry["name"] = outputs["artifact"]
        github.artifacts = [entry]
        github.queue_pr(support.pr_payload(base="d" * 40))
        code, final_outputs, _, failures = self.run_finalize(github)
        self.assertEqual(0, code)
        self.assertEqual("stale", final_outputs["review_state"])
        self.assertEqual("", final_outputs["verdict"])

    def test_finalize_should_fail_loudly_without_a_prepared_result(self):
        github = support.FakeGitHub(pr=self.pr)
        code, outputs, envelope, _ = self.run_finalize(github)
        self.assertEqual(2, code)
        self.assertIsNone(envelope)

    def test_main_should_run_the_prepare_phase_without_publishing(self):
        github = support.FakeGitHub(pr=self.pr)
        env = dict(
            self.env,
            GITHUB_TOKEN="t",
            PR_NUMBER=str(support.pr_payload()["number"]),
            OPENROUTER_API_KEY="k",
        )
        code = R.main(
            ["--phase", "prepare"],
            env=env,
            github=github,
            request=reply_request(support.model_payload()),
        )
        self.assertEqual(0, code)
        self.assertEqual(["pending"], [item["state"] for item in github.status_list])
        self.assertEqual([], github.comment_list)

    def test_main_should_run_the_finalize_phase_and_publish(self):
        github = support.FakeGitHub(pr=self.pr)
        env = dict(
            self.env,
            GITHUB_TOKEN="t",
            PR_NUMBER=str(support.pr_payload()["number"]),
            OPENROUTER_API_KEY="k",
        )
        R.main(
            ["--phase", "prepare"],
            env=env,
            github=github,
            request=reply_request(support.model_payload()),
        )
        with open(self.env["RESULT_PATH"], encoding="utf-8") as handle:
            name = json.load(handle)["artifact"]["name"]
        entry = self.prepared_artifact(github, int(self.env["GITHUB_RUN_ID"]))
        entry["name"] = name
        github.artifacts = [entry]
        code = R.main(["--phase", "finalize"], env=env, github=github)
        self.assertEqual(0, code)
        self.assertEqual(1, len(github.comment_list))
        self.assertEqual("success", github.status_list[-1]["state"])


if __name__ == "__main__":
    unittest.main()
