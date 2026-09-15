#!/usr/bin/env python3
"""Trusted dispatcher for pushes to installed integration branches.

A result is bound to the base branch tip (``base_sha``), so advancing the base branch
silently invalidates every completed review of PRs that target it. This job enumerates
the affected open PRs and asks the existing reviewer (``workflow_dispatch``) to run
again. It never reviews code and never calls OpenRouter, and it holds no model secret.

``workflow_dispatch`` is one of the few token-generated events that *does* start a new
run, which is exactly why the refresh path uses it instead of a ``push``-triggered
review.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_v2 import config as _config  # noqa: E402
from reviewer_v2 import net as _net  # noqa: E402
from reviewer_v2 import schema as _schema  # noqa: E402
from reviewer_v2.extract import COMPACT_FENCE, FULL_FENCE, extract_block  # noqa: E402
from reviewer_v2.github_api import GitHub  # noqa: E402

REVIEW_WORKFLOW_FILE = "pr-llm-review.yml"
REFRESH_WORKFLOW_FILE = "pr-llm-review-refresh.yml"
DEFAULT_MAX_PRS = 25
DEFAULT_MAX_PAGES = 3


def result_identity(body, actors=None) -> dict:
    """Trusted metadata embedded in a review comment, or ``None`` when unreadable."""
    if body is None:
        return None
    for fence in (FULL_FENCE, COMPACT_FENCE):
        try:
            payload = extract_block(body, fence)
        except Exception:  # noqa: BLE001 - an unreadable comment is simply not trusted
            return None
        if payload is None:
            continue
        try:
            data = _schema.load_json_strict(payload)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        return {
            "schema_version": data.get("schema_version"),
            "prompt_version": data.get("prompt_version"),
            "head_sha": data.get("head_sha"),
            "base_sha": data.get("base_sha"),
            "inputs_hash": data.get("inputs_hash"),
            "config_hash": data.get("config_hash"),
            "review_state": data.get("review_state"),
            "verdict": data.get("verdict"),
        }
    return None


def expected_identity(pr, config) -> dict:
    return {
        "schema_version": _config.SCHEMA_VERSION,
        "prompt_version": _config.PROMPT_VERSION,
        "head_sha": (pr.get("head") or {}).get("sha") or "",
        "base_sha": (pr.get("base") or {}).get("sha") or "",
        "inputs_hash": _config.hash_object(
            {
                "title": pr.get("title") or "",
                "body": pr.get("body") or "",
                "base_ref": (pr.get("base") or {}).get("ref") or "",
                "base_sha": (pr.get("base") or {}).get("sha") or "",
                "draft": bool(pr.get("draft")),
            }
        ),
        "config_hash": config.config_hash(),
    }


def needs_refresh(pr, identity, status, expected) -> tuple:
    """Return ``(bool, reason)`` for one pull request (pure, fixture-testable)."""
    if pr.get("draft"):
        return False, "draft"
    if pr.get("state") != "open":
        return False, f"state {pr.get('state')!r}"
    if status and status.get("state") == "pending":
        return False, "a review is already in flight"
    if identity is None:
        return True, "no verifiable review comment for this pull request"
    for field in (
        "schema_version",
        "prompt_version",
        "head_sha",
        "base_sha",
        "inputs_hash",
        "config_hash",
    ):
        if identity.get(field) != expected.get(field):
            return True, f"{field} differs (reviewed {identity.get(field)!r})"
    if identity.get("review_state") != "complete":
        return True, f"previous result was {identity.get('review_state')!r}"
    return False, "an equivalent complete review exists"


def trusted_identity(github, pr_number, actors):
    """Identity from the newest trusted review comment of one PR (None if none)."""
    from reviewer_v2.extract import trusted_candidates

    comments = trusted_candidates(github.comments(pr_number), actors)
    if not comments:
        return None
    return result_identity(comments[-1].get("body") or "")


def open_pulls_for(github, branch, max_pages):
    prs, page = [], 1
    while page <= max_pages:
        batch = github.call(
            f"/repos/{github.repo}/pulls?state=open&base={branch}"
            f"&sort=updated&direction=desc&per_page=50&page={page}"
        )
        if not batch:
            break
        prs.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return prs


def plan_refresh(github, branch, config, actors, max_prs, publish=False):
    """Decide and (optionally) dispatch a review for every affected open PR."""
    decisions = []
    dispatched = 0
    for pr in open_pulls_for(github, branch, DEFAULT_MAX_PAGES):
        number = int(pr.get("number"))
        head_sha = (pr.get("head") or {}).get("sha") or ""
        expected = expected_identity(pr, config)
        status = None
        if head_sha:
            try:
                status = github.latest_status(head_sha, _config.STATUS_CONTEXT)
            except _net.ApiError:
                status = None
        identity = trusted_identity(github, number, actors)
        refresh, reason = needs_refresh(pr, identity, status, expected)
        decision = {"pr": number, "refresh": refresh, "reason": reason, "head_sha": head_sha}
        if refresh and dispatched >= max_prs:
            decision["refresh"] = False
            decision["reason"] = f"refresh budget of {max_prs} pull requests reached"
            refresh = False
        if refresh and publish:
            github.dispatch_workflow(REVIEW_WORKFLOW_FILE, branch, {"pr_number": str(number)})
            dispatched += 1
            decision["dispatched"] = True
        decisions.append(decision)
    return decisions


def main(env=None, github=None) -> int:
    env = os.environ if env is None else env
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or ""
    repo = env.get("GITHUB_REPOSITORY") or ""
    branch = env.get("GITHUB_REF_NAME") or ""
    if not token or not repo or not branch:
        print(
            "::error::GITHUB_TOKEN, GITHUB_REPOSITORY and GITHUB_REF_NAME are required",
            file=sys.stderr,
        )
        return 2
    try:
        config = _config.Config.from_env(env)
    except _config.ConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    max_prs = int(env.get("REFRESH_MAX_PRS") or DEFAULT_MAX_PRS)
    publish = str(env.get("REFRESH_PUBLISH", "true")).lower() == "true"
    client = github or GitHub(repo, token, _net.Deadline(900), log=None)
    decisions = plan_refresh(
        client, branch, config, config.trusted_actors, max_prs, publish=publish
    )
    refreshed = [item for item in decisions if item.get("dispatched")]
    print(f"base refresh on {branch}: {len(decisions)} open PR(s), {len(refreshed)} dispatched")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Base refresh on `{branch}`\n\n")
            for item in decisions:
                handle.write(
                    f"- PR #{item['pr']}: "
                    f"{'dispatched' if item.get('dispatched') else 'skipped'} — {item['reason']}\n"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
