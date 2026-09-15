"""T003 regression tests: retries, budgets, typed errors (no real network)."""

import io
import json
import unittest
import urllib.error

from reviewer_v2 import config as _config
from reviewer_v2 import model as _model
from reviewer_v2 import net as _net
from reviewer_v2.tests import support


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Flaky:
    """Opener that raises the queued outcomes, then returns a response."""

    def __init__(self, outcomes, payload=None):
        self.outcomes = list(outcomes)
        self.payload = payload or {"ok": True}
        self.calls = 0
        self.request = None

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.request = request
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            raise urllib.error.HTTPError(
                request.full_url, outcome, "err", None, io.BytesIO(b'{"error":"x"}')
            )
        return FakeResponse(self.payload)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.clock = FakeClock()

    def sleeper(self, seconds):
        self.slept.append(seconds)
        self.clock.advance(seconds)

    def request(self, opener, attempts=3, deadline=None):
        return _net.request_json(
            "https://api.example/x",
            method="GET",
            headers={},
            attempts=attempts,
            opener=opener,
            sleeper=self.sleeper,
            clock=self.clock,
            deadline=deadline,
        )

    def test_should_retry_timeouts_then_succeed(self):
        opener = Flaky([TimeoutError("slow")], payload={"ok": 1})
        self.assertEqual({"ok": 1}, self.request(opener))
        self.assertEqual(2, opener.calls)
        self.assertEqual(1, len(self.slept))

    def test_should_retry_429_and_honour_retry_after(self):
        opener = Flaky([429], payload={"ok": 1})
        self.assertEqual({"ok": 1}, self.request(opener))
        self.assertLessEqual(self.slept[0], _net.MAX_RETRY_AFTER_SECONDS)

    def test_should_not_retry_permanent_auth_failure(self):
        opener = Flaky([401])
        with self.assertRaises(_net.PermanentApiError) as caught:
            self.request(opener)
        self.assertEqual(1, opener.calls)
        self.assertEqual(401, caught.exception.status)
        self.assertFalse(caught.exception.retryable)

    def test_should_raise_transient_after_exhausting_attempts(self):
        opener = Flaky([503, 503, 503])
        with self.assertRaises(_net.TransientApiError) as caught:
            self.request(opener)
        self.assertEqual(3, caught.exception.attempts)

    def test_should_mark_mutating_failures_ambiguous(self):
        opener = Flaky([TimeoutError("lost")])
        with self.assertRaises(_net.TransientApiError) as caught:
            _net.request_json(
                "https://api.example/x",
                method="POST",
                payload={},
                attempts=1,
                opener=opener,
                sleeper=self.sleeper,
                clock=self.clock,
            )
        self.assertTrue(caught.exception.ambiguous)

    def test_should_refuse_to_start_when_the_deadline_is_gone(self):
        deadline = _net.Deadline(10, clock=self.clock)
        self.clock.advance(11)
        opener = Flaky([])
        with self.assertRaises(_net.DeadlineExceeded):
            self.request(opener, deadline=deadline)
        self.assertEqual(0, opener.calls)

    def test_should_shrink_timeouts_to_the_remaining_budget(self):
        deadline = _net.Deadline(5, clock=self.clock)
        seen = {}

        def opener(request, timeout=None):
            seen["timeout"] = timeout
            return FakeResponse({"ok": 1})

        _net.request_json(
            "https://api.example/x",
            timeout=600,
            deadline=deadline,
            opener=opener,
            sleeper=self.sleeper,
            clock=self.clock,
        )
        self.assertLessEqual(seen["timeout"], 5)


class ModelEnvelopeTests(unittest.TestCase):
    def reply(self, **overrides):
        payload = support.model_reply(support.model_payload())
        payload.update(overrides)
        return _model.validate_reply(payload)

    def test_should_accept_a_valid_reply(self):
        self.assertEqual("stop", self.reply().finish_reason)

    def test_should_reject_length_finish_reason(self):
        with self.assertRaises(_model.ModelError) as caught:
            self.reply(choices=[{"finish_reason": "length", "message": {"content": "{}"}}])
        self.assertEqual("finish_reason:length", caught.exception.category)

    def test_should_reject_provider_errors_and_empty_choices(self):
        with self.assertRaises(_model.ModelError):
            _model.validate_reply({"error": {"message": "nope"}})
        with self.assertRaises(_model.ModelError):
            _model.validate_reply({"choices": []})

    def test_should_reject_null_and_invalid_content(self):
        for content in (None, "", "not json", "{}"):
            with self.subTest(content=content):
                with self.assertRaises(_model.ModelError):
                    self.reply(choices=[{"finish_reason": "stop", "message": {"content": content}}])

    def test_should_request_strict_structured_output(self):
        config = _config.Config.from_env({})
        payload = _model.build_payload(config, "s", "u")
        self.assertEqual("json_schema", payload["response_format"]["type"])
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual("high", payload["reasoning"]["effort"])

    def test_should_retry_once_on_schema_failure_and_never_accept_weaker_output(self):
        config = _config.Config.from_env({})
        bad = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        calls = []

        def request(url, **kwargs):
            calls.append(url)
            return bad

        with self.assertRaises(_model.ModelError):
            _model.request_review(config, "s", "u", "key", _net.Deadline(30), request=request)
        self.assertEqual(_model.MAX_SCHEMA_REPAIR_RETRIES + 1, len(calls))

    def test_should_not_accept_usage_as_zero_when_absent(self):
        reply = _model.validate_reply(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(support.model_payload())},
                    }
                ]
            }
        )
        self.assertEqual({}, reply.usage)


if __name__ == "__main__":
    unittest.main()


class BlobTests(unittest.TestCase):
    def test_should_send_bytes_untouched_instead_of_json_encoding_them(self):
        captured = {}

        def opener(request, timeout=None):
            captured["body"] = request.data
            captured["headers"] = dict(request.header_items())
            return FakeResponse({"ok": True})

        blob = b"PK\x03\x04\x00\xff\xfe zip bytes"
        _net.request_json(
            "https://upload.example/x",
            method="PUT",
            payload=blob,
            headers={"Content-Type": "application/octet-stream"},
            opener=opener,
            sleeper=lambda _s: None,
        )
        self.assertEqual(blob, captured["body"])
        self.assertEqual(str(len(blob)), captured["headers"]["Content-length"])

    def test_should_still_json_encode_regular_payloads(self):
        captured = {}

        def opener(request, timeout=None):
            captured["body"] = request.data
            return FakeResponse({"ok": True})

        _net.request_json(
            "https://api.example/x",
            method="POST",
            payload={"a": 1},
            opener=opener,
            sleeper=lambda _s: None,
        )
        self.assertEqual(b'{"a": 1}', captured["body"])


class OpenRouterHeadersTests(unittest.TestCase):
    """Regression: openrouter_headers used to build the dict without returning it,
    so every model request left without an Authorization header (live HTTP 401:
    'No cookie auth credentials found')."""

    def test_should_return_the_authorization_header(self):
        headers = _net.openrouter_headers("sk-or-test")
        self.assertEqual("Bearer sk-or-test", headers["Authorization"])
        self.assertEqual("application/json", headers["Accept"])

    def test_should_include_the_optional_attribution_headers(self):
        headers = _net.openrouter_headers(
            "sk-or-test", referer="https://github.com/fantastics4/.github", title="llm-pr-review"
        )
        self.assertEqual("Bearer sk-or-test", headers["Authorization"])
        self.assertEqual("https://github.com/fantastics4/.github", headers["HTTP-Referer"])
        self.assertEqual("llm-pr-review", headers["X-Title"])

    def test_should_attach_the_authorization_header_to_the_actual_request(self):
        captured = {}

        def opener(request, timeout=None):
            captured["auth"] = request.get_header("Authorization")
            return FakeResponse({"ok": True})

        _net.request_json(
            "https://openrouter.ai/api/v1/chat/completions",
            method="POST",
            payload={"a": 1},
            headers=_net.openrouter_headers("sk-or-live-regression"),
            opener=opener,
            sleeper=lambda _s: None,
        )
        self.assertEqual("Bearer sk-or-live-regression", captured["auth"])
