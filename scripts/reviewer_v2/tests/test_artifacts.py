"""T005/T006 regression tests: the required artifact stage (upload + verify)."""

import json
import os
import tempfile
import unittest

from reviewer_v2 import artifacts as A
from reviewer_v2 import result as _result
from reviewer_v2 import review as R
from reviewer_v2.tests import support


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.github = support.FakeGitHub(pr=support.pr_payload())

    def test_should_accept_an_artifact_of_this_run(self):
        self.github.artifacts = [{"id": 1, "name": "x", "expired": False}]
        self.assertEqual(1, A.verify(self.github, "555", "x")["id"])

    def test_should_reject_a_missing_artifact(self):
        with self.assertRaises(A.ArtifactError):
            A.verify(self.github, "555", "x")

    def test_should_reject_an_expired_artifact(self):
        self.github.artifacts = [{"id": 1, "name": "x", "expired": True}]
        with self.assertRaises(A.ArtifactError):
            A.verify(self.github, "555", "x")

    def test_should_refuse_to_upload_without_the_actions_runtime(self):
        with self.assertRaises(A.ArtifactError):
            A.upload("x", __file__, {})

    def test_should_compute_the_digest_of_the_stored_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "r.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"a": 1}, handle)
            self.assertEqual(64, len(A.sha256_file(path)))


class ArtifactNameTests(unittest.TestCase):
    def test_should_make_the_name_unique_per_attempt(self):
        first = _result.artifact_name(7, "a" * 40, 1)
        second = _result.artifact_name(7, "a" * 40, 2)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-a1"))
        self.assertTrue(second.endswith("-a2"))


class VerificationWiringTests(unittest.TestCase):
    """A review whose artifact cannot be seen on the run is never successful."""

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

    def uploader(self, github):
        """Same contract as perform's default uploader: store, then verify."""

        def store_and_verify(name, path):
            stored = {"name": name, "size": os.path.getsize(path)}
            A.verify(github, self.env["GITHUB_RUN_ID"], name)
            return stored

        return store_and_verify

    def request(self):
        def request(url, **kwargs):
            return support.model_reply(support.model_payload())

        return request

    def test_should_publish_complete_when_the_artifact_is_verifiable(self):
        github = support.FakeGitHub(pr=support.pr_payload())
        name = _result.artifact_name(7, support.HEAD, 1)
        github.artifacts = [{"id": 9, "name": name, "expired": False}]
        code, outputs, _, failures = R.perform(
            __import__("reviewer_v2.config", fromlist=["Config"]).Config.from_env({}),
            github,
            support.pr_payload(),
            request=self.request(),
            api_key="k",
            env=self.env,
            log=lambda fields: None,
            uploader=self.uploader(github),
        )
        self.assertEqual(0, code, failures)
        self.assertEqual("green", outputs["verdict"])

    def test_should_fail_the_review_when_the_artifact_is_not_visible(self):
        github = support.FakeGitHub(pr=support.pr_payload())
        github.artifacts = []
        config = __import__("reviewer_v2.config", fromlist=["Config"]).Config.from_env({})
        code, outputs, _, failures = R.perform(
            config,
            github,
            support.pr_payload(),
            request=self.request(),
            api_key="k",
            env=self.env,
            log=lambda fields: None,
            uploader=self.uploader(github),
        )
        self.assertEqual(1, code)
        self.assertEqual("error", outputs["review_state"])
        self.assertEqual("error", github.status_list[-1]["state"])
        self.assertTrue(any(f["category"] == "artifact" for f in failures))


if __name__ == "__main__":
    unittest.main()
