"""T005 regression tests: publication order, freshness and status lifecycle."""

import os
import tempfile
import unittest

from reviewer_v2 import config as _config
from reviewer_v2 import github_api as _github
from reviewer_v2 import net as _net
from reviewer_v2 import review as R
from reviewer_v2.tests import support


def reply_request(model_payload, finish_reason="stop"):
    def request(
        url,
        method="GET",
        body=None,
        headers=None,
        timeout=None,
        deadline=None,
        attempts=1,
        log=None,
        parse=True,
        **kwargs,
    ):
        return support.model_reply(model_payload, finish_reason)

    return request


class FlowTest(unittest.TestCase):
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
        self.uploaded = []

    def uploader(self, name, path):
        self.uploaded.append((name, path))
        return {"name": name, "size": os.path.getsize(path)}

    def run_flow(self, github, payload=None, config=None, request=None):
        return R.perform(
            config or self.config,
            github,
            self.pr,
            request=request or reply_request(payload or support.model_payload()),
            api_key="test-key",
            env=self.env,
            log=lambda fields: None,
            uploader=self.uploader,
        )

    def test_should_publish_green_when_the_review_is_complete_and_clean(self):
        github = support.FakeGitHub(pr=self.pr)
        code, outputs, envelope, failures = self.run_flow(github)
        self.assertEqual(0, code, failures)
        self.assertEqual("green", outputs["verdict"])
        self.assertEqual("complete", outputs["review_state"])
        self.assertEqual("pending", github.status_list[0]["state"])
        self.assertEqual("success", github.status_list[-1]["state"])
        self.assertEqual(["llm-review:green"], github.labels)
        body = github.comment_list[-1]["body"]
        self.assertTrue(body.startswith(_github.MARKER))
        self.assertIn("llm-review-result-v1", body)
        self.assertEqual(1, len(github.comment_list))

    def test_should_publish_red_without_failing_the_job_when_gate_is_false(self):
        github = support.FakeGitHub(pr=self.pr)
        payload = support.model_payload(issues=[support.issue()])
        code, outputs, envelope, _ = self.run_flow(github, payload=payload)
        self.assertEqual(0, code)
        self.assertEqual("red", outputs["verdict"])
        self.assertEqual("failure", github.status_list[-1]["state"])
        self.assertEqual(["llm-review:red"], github.labels)
        self.assertIn("llm-review-result-v1", github.comment_list[-1]["body"])

    def test_should_fail_only_when_gate_is_true_for_a_red_result(self):
        github = support.FakeGitHub(pr=self.pr)
        payload = support.model_payload(issues=[support.issue()])
        config = _config.Config.from_env({"GATE": "true"})
        code, outputs, envelope, _ = self.run_flow(github, payload=payload, config=config)
        self.assertEqual(1, code)
        self.assertEqual("failure", github.status_list[-1]["state"])
        self.assertEqual(1, len(github.comment_list))

    def test_should_not_publish_a_stale_result_when_the_head_moves(self):
        github = support.FakeGitHub(pr=self.pr)
        github.queue_pr(support.pr_payload(head="c" * 40))
        code, outputs, envelope, _ = self.run_flow(github)
        self.assertEqual(0, code)
        self.assertEqual("stale", outputs["review_state"])
        self.assertEqual([], github.comment_list)
        self.assertEqual([], github.labels)
        self.assertEqual("error", github.status_list[-1]["state"])
        self.assertNotIn("success", [s["state"] for s in github.status_list])

    def test_should_not_publish_success_when_the_artifact_fails(self):
        github = support.FakeGitHub(pr=self.pr)

        def broken(name, path):
            raise _net.TransientApiError("POST", "https://x", attempts=1)

        code, outputs, envelope, failures = R.perform(
            self.config,
            github,
            self.pr,
            request=reply_request(support.model_payload()),
            api_key="k",
            env=self.env,
            log=lambda fields: None,
            uploader=broken,
        )
        self.assertEqual(1, code)
        self.assertEqual("error", outputs["review_state"])
        self.assertEqual("error", github.status_list[-1]["state"])
        self.assertTrue(any(f["category"] == "artifact" for f in failures))

    def test_should_publish_error_status_when_the_comment_fails(self):
        github = support.FakeGitHub(
            pr=self.pr,
            fail={
                "create_comment": _net.PermanentApiError("POST", "https://x", status=403),
            },
        )
        code, outputs, envelope, failures = self.run_flow(github)
        self.assertEqual(1, code)
        self.assertEqual("error", outputs["review_state"])
        self.assertEqual("error", github.status_list[-1]["state"])
        self.assertTrue(any(f["category"] == "publication" for f in failures))

    def test_should_keep_partial_findings_but_never_green_when_a_chunk_fails(self):
        big_patch = "@@ -1,80 +1,80 @@\n" + "\n".join(f"line{i}" for i in range(80)) + "\n"
        files = [
            support.file_entry("a.py", patch=big_patch),
            support.file_entry("b.py", patch=big_patch),
        ]
        config = _config.Config.from_env({"MAX_DIFF_CHARS": "1100"})

        class SequencedRequest:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return support.model_reply(
                        support.model_payload(issues=[support.issue(file="a.py", lines="2")])
                    )
                raise _net.TransientApiError(
                    "POST", "https://x", status=503, attempts=3, retryable=True
                )

        github = support.FakeGitHub(pr=self.pr, files=files)
        code, outputs, envelope, failures = R.perform(
            config,
            github,
            self.pr,
            request=SequencedRequest(),
            api_key="k",
            env=self.env,
            log=lambda fields: None,
            uploader=self.uploader,
        )
        self.assertEqual(1, code)
        self.assertEqual("incomplete", outputs["review_state"])
        self.assertEqual("", outputs["verdict"])
        self.assertIsNone(envelope["verdict"])
        self.assertTrue(failures)
        self.assertEqual(1, envelope["blocking_count"])
        self.assertEqual([], github.labels)
        self.assertEqual("error", github.status_list[-1]["state"])

    def test_should_skip_drafts_without_calling_the_model(self):
        github = support.FakeGitHub(pr=support.pr_payload(draft=True))
        calls = {"n": 0}

        def request(url, **kwargs):  # pragma: no cover - must not be called
            calls["n"] += 1
            raise AssertionError("the model must not be called for a draft")

        env = dict(self.env, GITHUB_TOKEN="t", PR_NUMBER="7", OPENROUTER_API_KEY="k")
        code = R.main([], env=env, github=github, request=request, uploader=self.uploader)
        self.assertEqual(0, code)
        self.assertEqual(0, calls["n"])
        self.assertEqual([], github.status_list)
        self.assertEqual([], github.comment_list)

    def test_should_reject_a_non_canonical_pr_number(self):
        env = dict(self.env, GITHUB_TOKEN="t", PR_NUMBER="086", OPENROUTER_API_KEY="k")
        self.assertEqual(2, R.main([], env=env, github=support.FakeGitHub(pr=self.pr)))

    def test_should_record_a_head_sha_shared_with_another_pull_request(self):
        other = support.pr_payload(number=9)  # same head SHA, different PR
        github = support.FakeGitHub(pr=self.pr, open_pulls=[self.pr, other])
        code, outputs, envelope, _ = self.run_flow(github)
        self.assertEqual(0, code)
        self.assertEqual([9], envelope.get("shared_head_prs"))
        self.assertTrue(any("shares this head SHA" in item for item in envelope["limitations"]))

    def test_should_not_flag_a_shared_head_when_none_exists(self):
        github = support.FakeGitHub(pr=self.pr)
        _, _, envelope, _ = self.run_flow(github)
        self.assertNotIn("shared_head_prs", envelope)

    def test_should_write_an_empty_legacy_verdict_for_non_complete_results(self):
        github = support.FakeGitHub(pr=self.pr)
        github.queue_pr(support.pr_payload(base="d" * 40))
        self.run_flow(github)
        with open(self.env["GITHUB_OUTPUT"], encoding="utf-8") as handle:
            written = handle.read()
        self.assertIn("verdict=\n", written)
        self.assertIn("review_state=stale", written)


if __name__ == "__main__":
    unittest.main()
