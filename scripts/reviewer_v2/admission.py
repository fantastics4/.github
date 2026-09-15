#!/usr/bin/env python3
"""Decide whether an event should start a review, and with which canonical PR number.

Runs in the caller's admission job *before* the review concurrency slot is taken, so
an irrelevant edit, an invalid dispatch or a skipped draft can never cancel a running
valid review. This job holds no OpenRouter secret and never executes PR files.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_v2 import net as _net  # noqa: E402

CANONICAL_NUMBER = r"[1-9][0-9]*"
AUTOMATIC_TYPES = ("opened", "synchronize", "reopened", "ready_for_review")
REVIEW_INPUT_CHANGES = ("title", "body", "base")


@dataclass
class Admission:
    should_review: bool
    pr_number: str = ""
    reason: str = ""
    allow_draft: bool = False

    def outputs(self) -> dict:
        return {
            "should_review": "true" if self.should_review else "false",
            "pr_number": self.pr_number,
            "reason": self.reason,
            "allow_draft": "true" if self.allow_draft else "false",
        }


def _canonical(value) -> str:
    # No stripping: padding, signs, whitespace and exponent notation are rejected so
    # two spellings of the same PR can never produce different concurrency keys.
    text = str(value or "")
    return text if re.fullmatch(CANONICAL_NUMBER, text) else ""


def decide(event_name, event, repository=None, pr=None) -> Admission:
    """Pure decision function: no network, no environment, easy to fixture-test."""
    event = event or {}
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        number = _canonical(inputs.get("pr_number"))
        if not number:
            return Admission(False, reason="manual dispatch requires a canonical pr_number")
        if pr is None:
            return Admission(True, number, "manual dispatch", allow_draft=True)
        problem = _pr_problem(pr, repository, number)
        if problem:
            return Admission(False, number, problem)
        return Admission(True, number, "manual dispatch", allow_draft=True)

    if event_name != "pull_request":
        return Admission(False, reason=f"event {event_name!r} does not start a review")

    pr = event.get("pull_request") or {}
    action = event.get("action") or ""
    number = _canonical(pr.get("number"))
    if not number:
        return Admission(False, reason="event has no canonical pull request number")
    if action in ("closed",):
        return Admission(False, number, "pull request is closed")
    head_repo = str((pr.get("head") or {}).get("repo", {}).get("full_name") or "")
    if repository and head_repo and head_repo != repository:
        return Admission(
            False,
            number,
            "fork or restricted-secret pull request: the reusable workflow cannot read "
            "the secret here; dispatch a manual review from the base repository instead",
        )
    if action == "edited":
        changes = (event.get("changes") or {}).keys()
        if not set(changes).intersection(REVIEW_INPUT_CHANGES):
            return Admission(
                False, number, "edit changed neither title, body/objective nor base branch"
            )
        return Admission(True, number, "review-relevant edit")
    if action not in AUTOMATIC_TYPES:
        return Admission(False, number, f"action {action!r} is not reviewable")
    if pr.get("draft"):
        return Admission(False, number, "draft pull request (review manually)")
    problem = _pr_problem(pr, repository, number)
    if problem:
        return Admission(False, number, problem)
    return Admission(True, number, f"automatic event {action!r}")


def _pr_problem(pr, repository, number) -> str:
    full = str(((pr.get("base") or {}).get("repo") or {}).get("full_name") or "")
    if repository and full and full != repository:
        return f"pull request belongs to {full}, not {repository}"
    if pr.get("state") and pr.get("state") != "open":
        return f"pull request state is {pr.get('state')!r}"
    if str(pr.get("number") or "") != str(number):
        return "pull request number mismatch"
    return ""


def fetch_pr(repository, number, token, timeout=30.0):
    """Read PR metadata for a manual dispatch (to confirm the PR belongs to the repo)."""
    headers = _net.github_headers(token)
    return _net.request_json(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers=headers,
        timeout=timeout,
        attempts=2,
    )


def load_event(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_outputs(env, values) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main(argv=None, env=None, fetch=None) -> int:
    env = os.environ if env is None else env
    event_name = env.get("GITHUB_EVENT_NAME") or ""
    event = load_event(env.get("GITHUB_EVENT_PATH"))
    repository = env.get("GITHUB_REPOSITORY") or ""
    pr = None
    if event_name == "workflow_dispatch":
        number = _canonical((event.get("inputs") or {}).get("pr_number"))
        token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        if number and token and repository:
            fetch = fetch or fetch_pr
            try:
                pr = fetch(repository, number, token)
            except _net.ApiError as exc:
                result = Admission(False, number, f"could not read the pull request: {exc}")
                _write_outputs(env, result.outputs())
                print(f"admission: {result.reason}")
                return 0
    result = decide(event_name, event, repository=repository, pr=pr)
    _write_outputs(env, result.outputs())
    print(
        f"admission: should_review={result.should_review} pr={result.pr_number} "
        f"reason={result.reason}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
