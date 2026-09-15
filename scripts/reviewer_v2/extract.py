#!/usr/bin/env python3
"""Authenticated extraction of a v2 review result for the fixer.

Exit codes (documented in docs/llm-fixer.md):
  0  complete result extracted (verdict may be green or red)
  1  the comment/artifact/API could not be read
  2  a result exists but is not usable (unsupported schema, incomplete, error, stale)
  3  verification failed (actor, provenance, metadata, digest or status mismatch)

Only ``issues[]`` with ``blocking: true`` is the fixer's work list; there is no
``blocking_issues[]`` field. A human or unrelated-bot comment is never accepted.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_v2 import config as _config  # noqa: E402
from reviewer_v2 import github_api as _github  # noqa: E402
from reviewer_v2 import result as _result  # noqa: E402
from reviewer_v2 import schema as _schema  # noqa: E402

FULL_FENCE = re.compile(r"^```json llm-review-result-v1\s*$")
COMPACT_FENCE = re.compile(r"^```json llm-review-compact-v1\s*$")
LEGACY_FENCE = re.compile(r"^```json llm-review-verdict\s*$")
CLOSE_FENCE = re.compile(r"^```\s*$")
EXPECTED_WORKFLOW_PATH = os.environ.get(
    "EXPECTED_WORKFLOW_PATH", ".github/workflows/pr-llm-review.yml"
)


class ExtractionError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class GhError(RuntimeError):
    pass


def default_runner(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:4])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


class Gh:
    """Thin ``gh api`` wrapper; failures propagate (no error suppression)."""

    def __init__(self, runner=None):
        self.runner = runner or default_runner

    def api(self, path, paginate=False, slurp=False):
        args = ["api"]
        if paginate:
            args.append("--paginate")
        if slurp:
            args.append("--slurp")
        args.append(path)
        output = self.runner(args)
        if not output.strip():
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GhError(f"gh returned non-JSON output for {path}: {exc}") from exc

    def comments(self, repo, pr):
        payload = self.api(f"repos/{repo}/issues/{pr}/comments", paginate=True, slurp=True)
        if payload is None:
            return []
        if payload and isinstance(payload[0], list):
            return [item for page in payload for item in page]
        return list(payload)

    def artifact_bytes(self, repo, artifact_id):
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}/zip"],
            capture_output=True,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise GhError(
                f"could not download artifact {artifact_id}: "
                f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
            )
        return proc.stdout


def extract_block(body, opener):
    """Return the payload of the first fenced block opened by ``opener``."""
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        if opener.match(line):
            collected = []
            for candidate in lines[index + 1 :]:
                if CLOSE_FENCE.match(candidate):
                    return "\n".join(collected)
                collected.append(candidate)
            raise ExtractionError(2, "the result block is not closed")
    return None


def trusted_candidates(comments, actors):
    """Comments written by a trusted actor whose body STARTS with the marker."""
    found = []
    for comment in comments or []:
        login = str((comment.get("user") or {}).get("login") or "").strip()
        body = comment.get("body") or ""
        if login in actors and body.lstrip().startswith(_github.MARKER):
            found.append(comment)
    found.sort(key=lambda item: item.get("id") or 0)
    return found


def read_inline(body):
    payload = extract_block(body, FULL_FENCE)
    if payload is not None:
        return "full", payload
    payload = extract_block(body, COMPACT_FENCE)
    if payload is not None:
        return "compact", payload
    if extract_block(body, LEGACY_FENCE) is not None:
        raise ExtractionError(
            2,
            "this PR only has a legacy `llm-review-verdict` result; re-run the v2 "
            "reviewer before consuming it (the legacy contract is not supported)",
        )
    raise ExtractionError(2, "the trusted comment has no recognised result block")


def load_artifact_envelope(gh, repo, envelope, run_id):
    """Download the run artifact and verify its digest against the trusted envelope."""
    artifact = envelope.get("artifact") or {}
    name = artifact.get("name")
    if artifact.get("error"):
        raise ExtractionError(2, f"the run reported an artifact failure: {artifact['error']}")
    if not name:
        raise ExtractionError(2, "the result does not reference an artifact to verify")
    artifacts = gh.api(f"repos/{repo}/actions/runs/{run_id}/artifacts") or {}
    candidates = [item for item in artifacts.get("artifacts", []) if item.get("name") == name]
    if not candidates:
        raise ExtractionError(3, f"artifact {name!r} is not present on run {run_id}")
    record = candidates[0]
    if record.get("expired"):
        raise ExtractionError(2, f"artifact {name!r} has expired")
    blob = gh.artifact_bytes(repo, record["id"])
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [n for n in archive.namelist() if n.endswith(".json")]
            if not names:
                raise ExtractionError(2, "the artifact archive carries no JSON result")
            raw = archive.read(names[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise ExtractionError(2, f"the artifact archive is unreadable: {exc}") from exc
    outcome = _schema.parse_result_json(raw)
    if not outcome.ok:
        raise ExtractionError(
            3, "the artifact result failed validation: " + "; ".join(outcome.errors[:3])
        )
    stored = outcome.data
    digest = _result.hash_object({key: value for key, value in stored.items() if key != "artifact"})
    expected = artifact.get("payload_digest")
    if expected and digest != expected:
        raise ExtractionError(3, "the artifact payload digest does not match the comment")
    return stored


def parse_compact(payload):
    """Compact envelopes have no findings: validate the trusted subset by hand."""
    try:
        data = _schema.load_json_strict(payload)
    except (ValueError, _schema.JsonRejection) as exc:
        raise ExtractionError(2, f"the compact result is not valid JSON: {exc}") from exc
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
    ):
        if key not in data:
            raise ExtractionError(2, f"the compact result is missing {key!r}")
    if data.get("schema_version") != _config.SCHEMA_VERSION:
        raise ExtractionError(2, f"unsupported schema_version {data.get('schema_version')!r}")
    return data


def verify_metadata(envelope, repo, number, pr):
    if str(envelope.get("repository")) != repo:
        raise ExtractionError(3, f"result is for {envelope.get('repository')!r}, not {repo!r}")
    if int(envelope.get("pr_number") or 0) != int(number):
        raise ExtractionError(3, "result is for a different pull request")
    head = (pr.get("head") or {}).get("sha") or ""
    base = (pr.get("base") or {}).get("sha") or ""
    if envelope.get("head_sha") != head:
        raise ExtractionError(
            3, f"result head {envelope.get('head_sha')} != current head {head} (stale)"
        )
    if envelope.get("base_sha") != base:
        raise ExtractionError(
            3, f"result base {envelope.get('base_sha')} != current base {base} (stale)"
        )
    if envelope.get("inputs_hash") != _result.inputs_hash(pr):
        raise ExtractionError(3, "the PR title/body/objective changed after the review (stale)")
    return True


def verify_provenance(
    gh, repo, envelope, expected_path=EXPECTED_WORKFLOW_PATH, expected_tooling_sha=None
):
    run_id = str(envelope.get("run_id") or "")
    if not run_id.isdigit():
        raise ExtractionError(3, f"the result has no usable run id ({run_id!r})")
    run = gh.api(f"repos/{repo}/actions/runs/{run_id}")
    if not run:
        raise ExtractionError(3, f"workflow run {run_id} does not exist")
    path = str(run.get("path") or "")
    if not path.endswith(expected_path):
        raise ExtractionError(
            3,
            f"run {run_id} was produced by {path!r}, not by the expected caller {expected_path!r}",
        )
    if run.get("event") not in ("pull_request", "workflow_dispatch"):
        raise ExtractionError(3, f"run {run_id} was triggered by {run.get('event')!r}")
    if int(run.get("run_attempt") or 0) != int(envelope.get("run_attempt") or 1):
        raise ExtractionError(3, "run attempt in the result does not match the workflow run")
    associated = [item.get("number") for item in (run.get("pull_requests") or [])]
    if associated and int(envelope.get("pr_number")) not in associated:
        raise ExtractionError(3, f"run {run_id} is not associated with this pull request")
    if expected_tooling_sha:
        # The REST API does not expose the reusable workflow revision, so consumers can
        # pin the expected release: the reviewer records the value it checked out.
        recorded = str(envelope.get("tooling_revision") or "")
        if recorded != expected_tooling_sha:
            raise ExtractionError(
                3,
                f"the result was produced by tooling {recorded!r}, not the pinned "
                f"release {expected_tooling_sha!r}",
            )
    return run


def verify_status(gh, repo, envelope):
    payload = gh.api(f"repos/{repo}/commits/{envelope['head_sha']}/status") or {}
    statuses = payload.get("statuses") or []
    match = next((item for item in statuses if item.get("context") == _config.STATUS_CONTEXT), None)
    if match is None:
        raise ExtractionError(3, "there is no llm-review status on the reviewed commit")
    state = match.get("state")
    if state not in ("success", "failure"):
        raise ExtractionError(3, f"the latest llm-review status is {state!r}: review not terminal")
    expected = "success" if envelope.get("verdict") == "green" else "failure"
    if state != expected:
        raise ExtractionError(
            3, f"status {state!r} disagrees with verdict {envelope.get('verdict')!r}"
        )
    return match


def fixer_payload(envelope):
    """The fixer contract: the annotated ``issues[]`` plus the review context.

    There is deliberately no ``blocking_issues[]`` field: the fixer filters
    ``issues[]`` by ``blocking == true``.
    """
    issues = list(envelope.get("issues") or [])
    return {
        "schema_version": envelope.get("schema_version"),
        "repository": envelope.get("repository"),
        "pr_number": envelope.get("pr_number"),
        "head_sha": envelope.get("head_sha"),
        "base_sha": envelope.get("base_sha"),
        "review_state": envelope.get("review_state"),
        "verdict": envelope.get("verdict"),
        "blocking_count": sum(1 for item in issues if item.get("blocking")),
        "summary": envelope.get("summary"),
        "issues": issues,
        "tests_to_add": envelope.get("tests_to_add") or [],
        "human_gates": envelope.get("human_gates") or [],
        "definition_of_done": envelope.get("definition_of_done") or [],
        "diff_risks": envelope.get("diff_risks") or [],
        "coverage": envelope.get("coverage") or {},
    }


def extract(repo, number, gh=None, actors=None):
    """Return the verified, complete envelope for ``repo#number`` or raise."""
    gh = gh or Gh()
    actors = tuple(actors or _config.DEFAULT_TRUSTED_ACTORS)
    pr = gh.api(f"repos/{repo}/pulls/{number}")
    if not pr:
        raise ExtractionError(1, f"pull request {repo}#{number} could not be read")
    candidates = trusted_candidates(gh.comments(repo, number), actors)
    if not candidates:
        raise ExtractionError(1, f"no trusted LLM review comment on {repo}#{number}")
    if len(candidates) > 1:
        print(
            f"warning: {len(candidates)} trusted review comments found "
            f"{[c.get('id') for c in candidates]}; using the newest and report a cleanup",
            file=sys.stderr,
        )
    comment = candidates[-1]
    kind, payload = read_inline(comment.get("body") or "")
    if kind == "full":
        outcome = _schema.parse_result_json(payload)
        if not outcome.ok:
            raise ExtractionError(
                3, "the inline result failed validation: " + "; ".join(outcome.errors[:3])
            )
        trusted = outcome.data
    else:
        trusted = parse_compact(payload)

    verify_metadata(trusted, repo, number, pr)
    if trusted.get("review_state") != "complete":
        raise ExtractionError(
            2,
            f"the trusted result is {trusted.get('review_state')!r}, not a complete "
            "review; nothing is consumable as approval",
        )
    verify_status(gh, repo, trusted)
    verify_provenance(
        gh, repo, trusted, expected_tooling_sha=os.environ.get("EXPECTED_TOOLING_SHA") or None
    )

    envelope = trusted
    artifact = trusted.get("artifact") or {}
    if artifact.get("name"):
        envelope = load_artifact_envelope(gh, repo, trusted, str(trusted["run_id"]))
        verify_metadata(envelope, repo, number, pr)
        if envelope.get("head_sha") != trusted.get("head_sha") or envelope.get(
            "inputs_hash"
        ) != trusted.get("inputs_hash"):
            raise ExtractionError(3, "the artifact result does not match the comment metadata")
    elif kind == "compact":
        raise ExtractionError(2, "the compact result references no artifact to verify")

    state = envelope.get("review_state")
    if state != "complete":
        raise ExtractionError(
            2,
            f"the review is {state!r}, not a complete result (verdict:"
            f" {envelope.get('verdict')!r}); nothing is consumable as approval",
        )
    if envelope.get("verdict") not in ("green", "red"):
        raise ExtractionError(2, "a complete result must carry a green/red verdict")

    # Detect a change picked up mid-extraction (comment/artifact/status race).
    latest = gh.api(f"repos/{repo}/pulls/{number}")
    verify_metadata(envelope, repo, number, latest)
    return envelope


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Extract a verified v2 review result")
    parser.add_argument("repo", help="owner/repository")
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument(
        "--format",
        choices=("fixer", "full"),
        default="fixer",
        help="fixer payload (default) or the complete envelope",
    )
    parser.add_argument(
        "--actor",
        action="append",
        default=None,
        help="trusted comment author (repeatable; default github-actions[bot])",
    )
    return parser.parse_args(argv)


def main(argv=None, gh=None, actors=None) -> int:
    args = parse_args(argv)
    try:
        envelope = extract(args.repo, args.pr, gh=gh, actors=args.actor or actors)
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = envelope if args.format == "full" else fixer_payload(envelope)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
