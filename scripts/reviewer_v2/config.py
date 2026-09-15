"""Effective configuration and budgets for reviewer v2.

Everything the reviewer can be tuned with lives here, so the Python defaults, the
reusable workflow inputs and the docs table can be kept in sync mechanically
(``tests/test_config.py`` asserts the documented table).

Importing this module never reads the real environment: call :func:`Config.from_env`
explicitly with a mapping (usually ``os.environ``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

SCHEMA_VERSION = 1
# Bump whenever the prompt or the deterministic policy changes: it is part of the
# reviewed-input identity, so a prompt change invalidates old results.
PROMPT_VERSION = 2

DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_MAX_DIFF_CHARS = 500000
DEFAULT_MAX_COMPLETION_TOKENS = 65536
DEFAULT_MAX_CHUNKS = 12
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TOTAL_BUDGET_SECONDS = 1500
DEFAULT_COMPLETION_RESERVE_TOKENS = 4096
DEFAULT_MAX_COMMENT_CHARS = 60000
DEFAULT_MODEL_CONTEXT_TOKENS = 200000
# Conservative documented bound (NOT a tokenizer): 4 characters per token. It is
# deliberately pessimistic, and any prompt whose bound exceeds the model context is
# split or reported as a budget failure instead of being sent blindly.
CHARS_PER_TOKEN_BOUND = 4

VALID_CATEGORIES = (
    "security",
    "data-loss",
    "breaking",
    "bug",
    "tests",
    "style",
    "docs",
    "perf",
    "other",
)
VALID_SEVERITIES = ("critical", "high", "medium", "low")
VALID_TEST_TYPES = ("unit", "integration", "e2e")
VALID_REASONING_EFFORTS = ("", "low", "medium", "high", "max")
REVIEW_STATES = ("complete", "incomplete", "error", "stale", "skipped")
DEFAULT_BLOCKING_CATEGORIES = ("security", "data-loss", "breaking")
DEFAULT_BLOCKING_BUG_SEVERITIES = ("critical", "high")
# Author of the trusted sticky comment. Other workflows can post as the same bot, so
# the extractor also verifies run/artifact provenance; this is only the first filter.
DEFAULT_TRUSTED_ACTORS = ("github-actions[bot]",)
STATUS_CONTEXT = "llm-review"
LABEL_GREEN = "llm-review:green"
LABEL_RED = "llm-review:red"
REQUIRED_ENV = ("GITHUB_TOKEN", "GH_REPO", "PR_NUMBER", "OPENROUTER_API_KEY")


class ConfigError(RuntimeError):
    """Raised for invalid or missing configuration (fail before paid inference)."""


def _int(env, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _bool(env, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip().lower()
    if value not in ("true", "false"):
        raise ConfigError(f"{name} must be 'true' or 'false', got {raw!r}")
    return value == "true"


def _csv(env, name: str, default: tuple) -> tuple:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return tuple(item.strip() for item in str(raw).split(",") if item.strip())


@dataclass(frozen=True)
class Policy:
    """Deterministic category/severity -> blocking mapping (never model-decided)."""

    blocking_categories: tuple
    blocking_bug_severities: tuple

    def blocks(self, category: str, severity: str) -> bool:
        category = (category or "").strip().lower()
        severity = (severity or "").strip().lower()
        if category in self.blocking_categories:
            return True
        return category == "bug" and severity in self.blocking_bug_severities


@dataclass(frozen=True)
class Budgets:
    max_diff_chars: int
    max_completion_tokens: int
    max_chunks: int
    request_timeout_seconds: int
    max_attempts: int
    total_budget_seconds: int
    completion_reserve_tokens: int
    max_comment_chars: int
    model_context_tokens: int

    def to_dict(self) -> dict:
        return {
            "max_diff_chars": self.max_diff_chars,
            "max_completion_tokens": self.max_completion_tokens,
            "max_chunks": self.max_chunks,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_attempts": self.max_attempts,
            "total_budget_seconds": self.total_budget_seconds,
            "completion_reserve_tokens": self.completion_reserve_tokens,
            "max_comment_chars": self.max_comment_chars,
            "model_context_tokens": self.model_context_tokens,
        }


@dataclass(frozen=True)
class Config:
    model: str
    reasoning_effort: str
    gate: bool
    allow_draft: bool
    openrouter_base_url: str
    test_conventions: str
    trusted_actors: tuple
    policy: Policy
    budgets: Budgets

    @classmethod
    def from_env(cls, env=None) -> Config:
        env = os.environ if env is None else env
        model = str(env.get("MODEL") or DEFAULT_MODEL).strip()
        if not model:
            raise ConfigError("MODEL must not be empty")

        effort = str(env.get("REASONING_EFFORT", DEFAULT_REASONING_EFFORT)).strip().lower()
        if effort not in VALID_REASONING_EFFORTS:
            raise ConfigError(
                "REASONING_EFFORT must be one of "
                f"{list(VALID_REASONING_EFFORTS)} (empty = model default), got {effort!r}"
            )

        categories = _csv(env, "BLOCKING_CATEGORIES", DEFAULT_BLOCKING_CATEGORIES)
        unknown = sorted(set(categories) - set(VALID_CATEGORIES))
        if unknown:
            raise ConfigError(f"BLOCKING_CATEGORIES has unknown categories: {unknown}")
        severities = _csv(env, "BLOCKING_BUG_SEVERITIES", DEFAULT_BLOCKING_BUG_SEVERITIES)
        unknown = sorted(set(severities) - set(VALID_SEVERITIES))
        if unknown:
            raise ConfigError(f"BLOCKING_BUG_SEVERITIES has unknown severities: {unknown}")

        base_url = str(env.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")
        if not base_url.startswith("https://"):
            raise ConfigError(f"OPENROUTER_BASE_URL must use https, got {base_url!r}")

        budgets = Budgets(
            max_diff_chars=_int(env, "MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS, 1000, 4000000),
            max_completion_tokens=_int(
                env, "MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION_TOKENS, 1024, 131072
            ),
            max_chunks=_int(env, "MAX_CHUNKS", DEFAULT_MAX_CHUNKS, 1, 100),
            request_timeout_seconds=_int(
                env, "REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS, 10, 3600
            ),
            max_attempts=_int(env, "MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, 1, 5),
            total_budget_seconds=_int(
                env, "TOTAL_BUDGET_SECONDS", DEFAULT_TOTAL_BUDGET_SECONDS, 60, 5400
            ),
            completion_reserve_tokens=_int(
                env, "COMPLETION_RESERVE_TOKENS", DEFAULT_COMPLETION_RESERVE_TOKENS, 256, 32768
            ),
            max_comment_chars=_int(
                env, "MAX_COMMENT_CHARS", DEFAULT_MAX_COMMENT_CHARS, 2000, 65000
            ),
            model_context_tokens=_int(
                env, "MODEL_CONTEXT_TOKENS", DEFAULT_MODEL_CONTEXT_TOKENS, 8000, 2000000
            ),
        )
        if budgets.completion_reserve_tokens >= budgets.model_context_tokens:
            raise ConfigError("COMPLETION_RESERVE_TOKENS must be smaller than MODEL_CONTEXT_TOKENS")

        trusted = _csv(env, "TRUSTED_ACTORS", DEFAULT_TRUSTED_ACTORS)
        if not trusted:
            raise ConfigError("TRUSTED_ACTORS must list at least one GitHub actor")

        return cls(
            model=model,
            reasoning_effort=effort,
            gate=_bool(env, "GATE", False),
            allow_draft=_bool(env, "ALLOW_DRAFT", False),
            openrouter_base_url=base_url,
            test_conventions=str(env.get("TEST_CONVENTIONS") or "").strip(),
            trusted_actors=trusted,
            policy=Policy(blocking_categories=categories, blocking_bug_severities=severities),
            budgets=budgets,
        )

    def identity(self) -> dict:
        """The part of the configuration that changes a verdict, used for hashing."""
        return {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "policy": {
                "blocking_categories": sorted(self.policy.blocking_categories),
                "blocking_bug_severities": sorted(self.policy.blocking_bug_severities),
            },
            "budgets": self.budgets.to_dict(),
        }

    def config_hash(self) -> str:
        return hash_object(self.identity())


def require_env(env, names=REQUIRED_ENV) -> dict:
    """Return the required variables or fail loudly before doing any paid work."""
    missing = [name for name in names if not str(env.get(name) or "").strip()]
    if missing:
        raise ConfigError(f"missing required environment variables: {missing}")
    return {name: str(env[name]).strip() for name in names}


def hash_object(value) -> str:
    """Stable sha256 over a JSON-serialisable value (sorted keys, no whitespace)."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
