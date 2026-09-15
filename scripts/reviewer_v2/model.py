"""OpenRouter chat completion with strict structured output.

The response envelope (choices, finish reason, error) and the content schema are both
validated before any trust is granted. A provider that ignores ``response_format``
cannot produce a green verdict: the local schema check rejects it.
"""

from __future__ import annotations

import time

from . import net as _net
from . import schema as _schema

# Provider-side reasons that mean "the answer is not usable as a review".
UNUSABLE_FINISH_REASONS = (
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
    "error",
)
MAX_SCHEMA_REPAIR_RETRIES = 1


class ModelError(RuntimeError):
    """Any unusable model response: API error, refusal, truncation, schema failure."""

    def __init__(self, category, message, retryable=False, usage=None):
        self.category = category
        self.retryable = retryable
        self.usage = usage or {}
        super().__init__(f"[{category}] {message}")


class ModelReply:
    def __init__(
        self, data, *, model, provider, finish_reason, usage, attempts, latency, content_chars
    ):
        self.data = data
        self.model = model
        self.provider = provider
        self.finish_reason = finish_reason
        self.usage = usage
        self.attempts = attempts
        self.latency = latency
        self.content_chars = content_chars

    def log_fields(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "finish_reason": self.finish_reason,
            "attempts": self.attempts,
            "latency_seconds": round(self.latency, 3),
            "usage": self.usage or None,
            "content_chars": self.content_chars,
        }


def build_payload(config, system_prompt, user_prompt) -> dict:
    """Build the request body, requesting strict JSON Schema output."""
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # Strict structured output; `require_parameters` stops OpenRouter from routing
        # to a provider that would silently drop `response_format`.
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "llm_review_result",
                "strict": True,
                "schema": _schema.MODEL_RESPONSE_SCHEMA,
            },
        },
        "provider": {"require_parameters": True},
        "max_tokens": config.budgets.max_completion_tokens,
    }
    if config.reasoning_effort:
        payload["reasoning"] = {"effort": config.reasoning_effort}
    return payload


def _content_text(message) -> str:
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _usage(payload) -> dict:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    known = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        value = usage.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            known[key] = value
    return known


def validate_reply(payload) -> ModelReply:
    """Validate the HTTP envelope, then the content schema (both mandatory)."""
    if payload is None or not isinstance(payload, dict):
        raise ModelError("envelope", "empty or non-object response body")
    usage = _usage(payload)
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise ModelError("api_error", f"provider error: {message}", retryable=True, usage=usage)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError("envelope", "response has no choices", retryable=True, usage=usage)
    choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = choice.get("finish_reason")
    attempts = int(payload.get("__attempts") or 1)
    model = str(payload.get("model") or "")
    provider = str(payload.get("provider") or "")
    if finish_reason in UNUSABLE_FINISH_REASONS:
        raise ModelError(
            f"finish_reason:{finish_reason}",
            f"completion was not usable (finish_reason={finish_reason!r}); "
            "the review is incomplete",
            usage=usage,
        )
    if finish_reason not in (None, "stop"):
        raise ModelError(
            "finish_reason",
            f"unexpected finish_reason={finish_reason!r}",
            retryable=True,
            usage=usage,
        )
    content = _content_text(choice.get("message"))
    outcome = _schema.parse_model_json(content)
    if not outcome.ok:
        raise ModelError(
            "schema",
            "model output failed schema validation: " + "; ".join(outcome.errors[:5]),
            retryable=True,
            usage=usage,
        )
    return ModelReply(
        outcome.data,
        model=model,
        provider=provider,
        finish_reason=finish_reason,
        usage=usage,
        attempts=attempts,
        latency=0.0,
        content_chars=len(content),
    )


def request_review(
    config, system_prompt, user_prompt, api_key, deadline, request=None, log=None, attempts=None
):
    """Call OpenRouter and return a validated, schema-checked reply.

    ``request`` is injectable for offline tests. The only retry performed here is a
    single bounded schema-repair retry; transient network/HTTP retries happen inside
    :mod:`reviewer_v2.net` and never accept a weaker response.
    """
    request = request or _net.request_json
    budget_attempts = attempts or config.budgets.max_attempts
    payload = build_payload(config, system_prompt, user_prompt)
    url = f"{config.openrouter_base_url}/chat/completions"
    headers = _net.openrouter_headers(
        api_key, referer="https://github.com/fantastics4/.github", title="llm-pr-review"
    )
    last_error = None
    for call_attempt in range(1, MAX_SCHEMA_REPAIR_RETRIES + 2):
        deadline.ensure()
        started = time.monotonic()
        try:
            raw = request(
                url,
                method="POST",
                payload=payload,
                headers=headers,
                timeout=config.budgets.request_timeout_seconds,
                deadline=deadline,
                attempts=budget_attempts,
                log=log,
            )
        except _net.DeadlineExceeded:
            raise
        except _net.ApiError as exc:
            raise ModelError("http", str(exc), retryable=exc.retryable, usage={}) from exc
        if isinstance(raw, dict):
            raw["__attempts"] = call_attempt
        try:
            reply = validate_reply(raw)
        except ModelError as exc:
            last_error = exc
            if exc.retryable and call_attempt <= MAX_SCHEMA_REPAIR_RETRIES:
                if log:
                    log(
                        {
                            "message": "retrying after unusable model output",
                            "category": exc.category,
                            "attempt": call_attempt,
                        }
                    )
                continue
            raise
        reply.latency = time.monotonic() - started
        reply.attempts = call_attempt
        if log:
            log({"message": "model call succeeded", **reply.log_fields()})
        return reply
    raise ModelError("schema", f"model output was never valid: {last_error}")
    return payload
