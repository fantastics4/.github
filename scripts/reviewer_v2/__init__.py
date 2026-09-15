"""Reviewer v2 — versioned, staged replacement for ``scripts/llm_review.py``.

The legacy reviewer keeps its paths (``.github/workflows/llm-pr-review.yml``,
``scripts/llm_review.py``, ``callers/pr-llm-review.yml``) unchanged until every caller
has migrated. Nothing in this package may change legacy behaviour.

Import-safe by design: importing any module must not require credentials or perform
network calls, so the unit tests can import functions directly.
"""

__all__ = ["config", "schema", "net", "github_api", "diffcoverage", "result", "review"]
