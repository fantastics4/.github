"""Reviewer v2 — versioned replacement for the retired ``scripts/llm_review.py``.

Every caller runs v2 from a reviewed release pin; the legacy ``@main`` reviewer
(``.github/workflows/llm-pr-review.yml``, ``scripts/llm_review.py``,
``callers/pr-llm-review.yml``) was removed in spec 004.

Import-safe by design: importing any module must not require credentials or perform
network calls, so the unit tests can import functions directly.
"""

__all__ = ["config", "schema", "net", "github_api", "diffcoverage", "result", "review"]
