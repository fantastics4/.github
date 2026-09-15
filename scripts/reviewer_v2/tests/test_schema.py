"""T002 regression tests: parser and strict schema validation."""

import json
import unittest

from reviewer_v2 import schema as S
from reviewer_v2.tests import support


class FenceTests(unittest.TestCase):
    def test_should_preserve_internal_fences_when_stripping_one_outer_fence(self):
        body = '```json\n{"a": "```diff\\n-x\\n+y\\n```"}\n```'
        stripped = S.strip_outer_fence(body)
        self.assertTrue(stripped.startswith('{"a"'))
        self.assertIn("```diff", stripped)
        self.assertTrue(stripped.endswith('"}'))

    def test_should_not_strip_when_only_the_first_line_is_a_fence(self):
        body = '```json\n{"a": 1}'
        self.assertEqual(body, S.strip_outer_fence(body))

    def test_should_ignore_surrounding_blank_lines(self):
        body = '\n\n```json\n{"a": 1}\n```\n\n'
        self.assertEqual('{"a": 1}', S.strip_outer_fence(body))


class ParseTests(unittest.TestCase):
    def payload(self, **kwargs):
        return support.model_payload(**kwargs)

    def test_should_accept_valid_payload_with_nested_fence_and_escapes(self):
        payload = self.payload(
            issues=[support.issue(suggested_patch="```diff\\n-\\d+\\n+C:\\\\path\\\\file\\n```")]
        )
        text = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        outcome = S.parse_model_json(text)
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(1, len(outcome.data["issues"]))

    def test_should_accept_zero_issues_as_a_complete_response(self):
        outcome = S.parse_model_json(json.dumps(self.payload()))
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual([], outcome.data["issues"])

    def test_should_reject_malformed_json(self):
        outcome = S.parse_model_json('{"summary": "x", "issues": [')
        self.assertFalse(outcome.ok)
        self.assertIn("invalid JSON", outcome.errors[0])

    def test_should_reject_duplicate_keys(self):
        outcome = S.parse_model_json('{"summary": "a", "summary": "b", "issues": []}')
        self.assertFalse(outcome.ok)
        self.assertIn("duplicate", outcome.errors[0])

    def test_should_reject_non_finite_numbers(self):
        outcome = S.parse_model_json('{"confidence": NaN}')
        self.assertFalse(outcome.ok)
        self.assertIn("non-finite", outcome.errors[0])

    def test_should_reject_null_empty_and_non_string_content(self):
        for value in (None, "", "   ", 42):
            with self.subTest(value=value):
                self.assertFalse(S.parse_model_json(value).ok)

    def test_should_reject_empty_object(self):
        outcome = S.parse_model_json("{}")
        self.assertFalse(outcome.ok)
        self.assertTrue(any("missing required key" in e for e in outcome.errors))

    def test_should_reject_null_issue_list(self):
        payload = self.payload()
        payload["issues"] = None
        self.assertFalse(S.parse_model_json(json.dumps(payload)).ok)

    def test_should_reject_unknown_category(self):
        payload = self.payload(issues=[support.issue(category="unknown-cat")])
        self.assertFalse(S.parse_model_json(json.dumps(payload)).ok)

    def test_should_reject_unknown_severity_and_wrong_types(self):
        payload = self.payload(issues=[support.issue(severity="catastrophic")])
        self.assertFalse(S.parse_model_json(json.dumps(payload)).ok)
        payload = self.payload(confidence="high")
        errors = S.parse_model_json(json.dumps(payload)).errors
        self.assertTrue(any("$.confidence" in e for e in errors), errors)

    def test_should_reject_incomplete_issue_fields(self):
        payload = self.payload(issues=[{"id": "ISSUE-1", "category": "bug"}])
        self.assertFalse(S.parse_model_json(json.dumps(payload)).ok)


class ResultEnvelopeTests(unittest.TestCase):
    def test_should_reject_unsupported_schema_version(self):
        payload = support.envelope_for(support.pr_payload())
        payload["schema_version"] = 99
        outcome = S.parse_result_json(json.dumps(payload))
        self.assertFalse(outcome.ok)
        self.assertIn("unsupported schema_version", outcome.errors[0])

    def test_should_accept_a_complete_envelope(self):
        payload = support.envelope_for(support.pr_payload())
        self.assertTrue(S.parse_result_json(json.dumps(payload)).ok)

    def test_should_reject_invalid_verdict_and_state(self):
        payload = support.envelope_for(support.pr_payload())
        payload["verdict"] = "maybe"
        self.assertFalse(S.parse_result_json(json.dumps(payload)).ok)
        payload = support.envelope_for(support.pr_payload())
        payload["review_state"] = "unknown"
        self.assertFalse(S.parse_result_json(json.dumps(payload)).ok)


if __name__ == "__main__":
    unittest.main()
