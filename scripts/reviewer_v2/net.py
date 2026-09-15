"""Bounded HTTP with typed errors, retries and a global deadline.

Credentials are kept apart on purpose: :func:`github_headers` never returns provider
headers and vice versa, so a GitHub token can never be sent to OpenRouter (or the
reverse) by accident.
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 405, 409, 410, 415, 422, 451})
MAX_RETRY_AFTER_SECONDS = 60.0
NETWORK_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, OSError)


class DeadlineExceeded(RuntimeError):
    """The run-level budget is exhausted; no further request may start."""


class ApiError(RuntimeError):
    def __init__(
        self,
        method,
        url,
        status=None,
        body="",
        attempts=1,
        retryable=False,
        ambiguous=False,
        reason="",
    ):
        self.method = method
        self.url = url
        self.status = status
        self.body = (body or "")[:500]
        self.attempts = attempts
        self.retryable = retryable
        # True when the request may have reached the server (mutating call lost
        # in the network): callers must reconcile before retrying.
        self.ambiguous = ambiguous
        self.reason = reason
        detail = f"{method} {url} -> "
        detail += f"HTTP {status}" if status else (reason or type(self).__name__)
        if self.body:
            detail += f": {self.body}"
        super().__init__(detail)


class TransientApiError(ApiError):
    """Retryable failure: connection/timeout or an explicitly transient status."""


class PermanentApiError(ApiError):
    """Non-retryable failure: auth, permission, payment, invalid input."""


class Deadline:
    """Monotonic run budget shared by every request in one review."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.limit = float(seconds)
        self.started = clock()
        self._deadline = self.started + self.limit

    def remaining(self) -> float:
        return self._deadline - self.clock()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def ensure(self) -> None:
        if self.expired():
            raise DeadlineExceeded(
                f"run budget of {self.limit:.0f}s exhausted before starting another request"
            )


def _retry_after_seconds(headers) -> float | None:
    if not headers:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        delay = when.timestamp() - time.time()
        return max(0.0, delay)
    return None


def _backoff(attempt: int, retry_after: float | None, remaining: float) -> float:
    if retry_after is not None:
        delay = min(retry_after, MAX_RETRY_AFTER_SECONDS)
    else:
        delay = min(2.0 ** (attempt - 1), 30.0) * (0.5 + random.random())
    return max(0.0, min(delay, max(0.0, remaining)))


def github_headers(token: str) -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def openrouter_headers(api_key: str, referer: str = "", title: str = "") -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title


def request_json(
    url,
    method="GET",
    payload=None,
    headers=None,
    timeout=180.0,
    deadline=None,
    attempts=1,
    opener=urllib.request.urlopen,
    sleeper=time.sleep,
    clock=time.monotonic,
    log=None,
    parse=True,
):
    """Perform a bounded JSON request.

    Retries connection failures/timeouts and 408/425/429/5xx with exponential backoff
    honouring ``Retry-After``. Permanent statuses are never retried. ``attempts`` is
    bounded by the caller and by the shared :class:`Deadline`.
    """
    attempts = max(1, int(attempts))
    # Bytes go on the wire untouched (artifacts are uploaded as blobs); anything else is
    # serialised as JSON. Encoding bytes with json.dumps would corrupt the body and the
    # declared Content-Length.
    is_blob = isinstance(payload, bytes | bytearray)
    if is_blob:
        body_bytes = bytes(payload)
    elif payload is not None:
        body_bytes = json.dumps(payload).encode("utf-8")
    else:
        body_bytes = None
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        if deadline is not None:
            deadline.ensure()
            remaining = deadline.remaining()
            effective_timeout = max(1.0, min(float(timeout), remaining))
        else:
            remaining = float("inf")
            effective_timeout = float(timeout)

        request = urllib.request.Request(url, data=body_bytes, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if body_bytes is not None and not (headers or {}).get("Content-Type"):
            request.add_header("Content-Type", "application/json")
        if is_blob:
            request.add_header("Content-Length", str(len(body_bytes)))

        started = clock()
        try:
            with opener(request, timeout=effective_timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
            _log(
                log,
                method=method,
                url=url,
                attempt=attempt,
                status=status,
                latency=clock() - started,
            )
            if not parse:
                return raw
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - body already consumed/closed
                detail = ""
            _log(
                log,
                method=method,
                url=url,
                attempt=attempt,
                status=exc.code,
                latency=clock() - started,
                error=f"HTTP {exc.code}",
            )
            retryable = exc.code in RETRYABLE_STATUS
            if not retryable or attempt >= attempts:
                error_cls = TransientApiError if retryable else PermanentApiError
                raise error_cls(
                    method, url, status=exc.code, body=detail, attempts=attempt, retryable=retryable
                ) from exc
            delay = _backoff(
                attempt, _retry_after_seconds(getattr(exc, "headers", None)), remaining
            )
            last = exc
            if delay > 0:
                sleeper(delay)
        except NETWORK_ERRORS as exc:
            _log(
                log,
                method=method,
                url=url,
                attempt=attempt,
                latency=clock() - started,
                error=type(exc).__name__,
            )
            if attempt >= attempts:
                raise TransientApiError(
                    method,
                    url,
                    attempts=attempt,
                    retryable=True,
                    ambiguous=method.upper() != "GET",
                    reason=type(exc).__name__,
                ) from exc
            delay = _backoff(attempt, None, remaining)
            last = exc
            if delay > 0:
                sleeper(delay)

    raise TransientApiError(  # pragma: no cover - loop always returns or raises
        method, url, attempts=attempts, retryable=True, reason=str(last)
    )


def _log(log, **fields) -> None:
    if log is not None:
        log(fields)
