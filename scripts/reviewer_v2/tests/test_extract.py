"""T006 regression tests: authenticated extraction (actor, provenance, artifact)."""

import io
import json
import unittest
import zipfile

from reviewer_v2 import extract as E
from reviewer_v2 import github_api as _github
from reviewer_v2.tests import support


class FakeGh:
    def __init__(self, pr, comments, run=None, artifacts=None, blob=None, status=None):
        self.pr = pr
        self.comment_list = comments
        self.run = run
        self.artifacts = artifacts or []
        self.blob = blob
        self.status = status or {"state": "success", "context": "llm-review"}
        self.warnings = []

    def api(self, path, paginate=False, slurp=False):
        if "/pulls/" in path:
            return self.pr
        if "/actions/runs/" in path and path.endswith("/artifacts"):
            return {"artifacts": self.artifacts}
        if "/actions/runs/" in path:
            return self.run
        if "/commits/" in path and path.endswith("/status"):
            return {"statuses": [self.status] if self.status else []}
        raise AssertionError(path)

    def comments(self, repo, pr):
        return self.comment_list

    def artifact_bytes(self, repo, artifact_id):
        return self.blob


def full_comment(envelope, actor=support.ACTOR, prefix=None):
    body = (
        _github.MARKER
        + "\n\n## Review\n\n```json llm-review-result-v1\n"
        + json.dumps(envelope)
        + "\n```\n"
    )
    if prefix:
        body = prefix + body
    return {"id": 1, "body": body, "user": {"login": actor}}


def compact_comment(envelope, actor=support.ACTOR):
    compact = {
        key: envelope.get(key)
        for key in (
            "schema_version",
            "repository",
            "pr_number",
            "head_sha",
            "base_sha",
            "inputs_hash",
            "run_id",
            "run_attempt",
            "review_state",
            "verdict",
            "artifact",
        )
    }
    body = _github.MARKER + "\n\n```json llm-review-compact-v1\n" + json.dumps(compact) + "\n```\n"
    return {"id": 2, "body": body, "user": {"login": actor}}


def run_payload(
    path=".github/workflows/pr-llm-review.yml", event="pull_request", attempt=1, pr_number=7
):
    return {
        "path": path,
        "event": event,
        "run_attempt": attempt,
        "pull_requests": [{"number": pr_number}],
    }


def zip_of(envelope):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("result.json", json.dumps(envelope))
    return buffer.getvalue()


class InlineExtractionTests(unittest.TestCase):
    def setUp(self):
        self.pr = support.pr_payload()
        self.envelope = support.envelope_for(self.pr)

    def extract(self, gh):
        return E.extract(support.REPO, 7, gh=gh)

    def test_should_extract_a_complete_green_result(self):
        gh = FakeGh(self.pr, [full_comment(self.envelope)], run=run_payload())
        result = self.extract(gh)
        self.assertEqual("green", result["verdict"])

    def test_should_extract_a_complete_red_result(self):
        envelope = support.envelope_for(
            self.pr,
            verdict="red",
            issues=[dict(support.issue(), blocking=True)],
        )
        gh = FakeGh(
            self.pr,
            [full_comment(envelope)],
            run=run_payload(),
            status={"state": "failure", "context": "llm-review"},
        )
        payload = E.fixer_payload(self.extract(gh))
        self.assertEqual(1, payload["blocking_count"])
        self.assertTrue(payload["issues"][0]["blocking"])
        self.assertNotIn("blocking_issues", payload)

    def test_should_reject_a_human_comment_that_spoofs_the_marker(self):
        gh = FakeGh(self.pr, [full_comment(self.envelope, actor="tomas")], run=run_payload())
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(1, caught.exception.code)

    def test_should_reject_a_marker_that_is_not_at_the_start(self):
        gh = FakeGh(self.pr, [full_comment(self.envelope, prefix="hi there\n")], run=run_payload())
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(1, caught.exception.code)

    def test_should_reject_forged_provenance_from_another_workflow(self):
        gh = FakeGh(
            self.pr,
            [full_comment(self.envelope)],
            run=run_payload(path=".github/workflows/other.yml"),
        )
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(3, caught.exception.code)

    def test_should_reject_a_stale_head(self):
        self.pr["head"]["sha"] = "f" * 40
        gh = FakeGh(self.pr, [full_comment(self.envelope)], run=run_payload())
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(3, caught.exception.code)

    def test_should_reject_a_non_terminal_status(self):
        gh = FakeGh(
            self.pr,
            [full_comment(self.envelope)],
            run=run_payload(),
            status={"state": "pending", "context": "llm-review"},
        )
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(3, caught.exception.code)

    def test_should_reject_an_incomplete_result(self):
        envelope = support.envelope_for(self.pr, state="incomplete", verdict=None)
        gh = FakeGh(self.pr, [full_comment(envelope)], run=run_payload())
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(2, caught.exception.code)

    def test_should_reject_a_legacy_verdict_comment(self):
        body = _github.MARKER + '\n```json llm-review-verdict\n{"verdict": "green"}\n```'
        gh = FakeGh(
            self.pr, [{"id": 1, "body": body, "user": {"login": support.ACTOR}}], run=run_payload()
        )
        with self.assertRaises(E.ExtractionError) as caught:
            self.extract(gh)
        self.assertEqual(2, caught.exception.code)

    def test_should_use_the_newest_of_duplicate_trusted_comments(self):
        newer = full_comment(support.envelope_for(self.pr, run_id="777"))
        newer["id"] = 9
        older = full_comment(support.envelope_for(self.pr, run_id="111"))
        older["id"] = 5
        gh = FakeGh(self.pr, [older, newer], run=run_payload())
        result = self.extract(gh)
        self.assertEqual("777", result["run_id"])


class ArtifactExtractionTests(unittest.TestCase):
    def setUp(self):
        self.pr = support.pr_payload()
        self.envelope = support.envelope_for(self.pr)

    def artifact_case(self, digest=None, expired=False):
        envelope = dict(self.envelope)
        envelope["artifact"] = {
            "name": "llm-review-result-7-aaaaaaaaaaaa",
            "run_id": "555",
            "payload_digest": digest or E._result.hash_object(self.envelope),
        }
        comment = compact_comment(envelope)
        artifacts = [{"id": 42, "name": envelope["artifact"]["name"], "expired": expired}]
        return FakeGh(
            self.pr, [comment], run=run_payload(), artifacts=artifacts, blob=zip_of(envelope)
        )

    def test_should_verify_and_extract_an_artifact_result(self):
        result = E.extract(support.REPO, 7, gh=self.artifact_case())
        self.assertEqual("green", result["verdict"])

    def test_should_reject_a_digest_mismatch(self):
        with self.assertRaises(E.ExtractionError) as caught:
            E.extract(support.REPO, 7, gh=self.artifact_case(digest="0" * 64))
        self.assertEqual(3, caught.exception.code)

    def test_should_reject_an_expired_artifact(self):
        with self.assertRaises(E.ExtractionError) as caught:
            E.extract(support.REPO, 7, gh=self.artifact_case(expired=True))
        self.assertEqual(2, caught.exception.code)

    def test_should_reject_a_compact_result_without_an_artifact(self):
        envelope = support.envelope_for(self.pr)
        gh = FakeGh(self.pr, [compact_comment(envelope)], run=run_payload())
        with self.assertRaises(E.ExtractionError) as caught:
            E.extract(support.REPO, 7, gh=gh)
        self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
