"""Shared offline fixtures: fake GitHub client and valid model payloads."""

from __future__ import annotations

import copy

from reviewer_v2 import github_api as _github
from reviewer_v2 import result as _result

REPO = "fantastics4/demo"
HEAD = "a" * 40
BASE = "b" * 40
ACTOR = "github-actions[bot]"


def pr_payload(
    number=7,
    head=HEAD,
    base=BASE,
    draft=False,
    state="open",
    title="T",
    body=None,
    changed_files=None,
):
    payload = {
        "number": number,
        "state": state,
        "draft": draft,
        "title": title,
        "body": body if body is not None else "body without objective",
        "base": {"ref": "develop", "sha": base, "repo": {"full_name": REPO}},
        "head": {"ref": "feature", "sha": head, "repo": {"full_name": REPO}},
    }
    if changed_files is not None:
        payload["changed_files"] = changed_files
    return payload


_UNSET = object()
DEFAULT_PATCH = "@@ -1,3 +1,4 @@\n line\n+new\n line\n"


def file_entry(
    filename="src/app.py", patch=_UNSET, status="modified", additions=1, deletions=1, previous=None
):
    entry = {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": DEFAULT_PATCH if patch is _UNSET else patch,
    }
    if previous:
        entry["previous_filename"] = previous
    return entry


def model_payload(issues=None, summary="Resumen", confidence=0.9, **extra):
    payload = {
        "summary": summary,
        "issues": issues or [],
        "tests_to_add": extra.get("tests_to_add", []),
        "human_gates": extra.get("human_gates", []),
        "definition_of_done": extra.get("definition_of_done", []),
        "diff_risks": extra.get("diff_risks", []),
        "confidence": confidence,
    }
    return payload


def issue(**overrides):
    data = {
        "id": "ISSUE-1",
        "category": "bug",
        "severity": "high",
        "title": "Something breaks",
        "file": "src/app.py",
        "lines": "2",
        "problem": "p",
        "why_it_matters": "w",
        "exact_fix": "f",
        "suggested_patch": None,
    }
    data.update(overrides)
    return data


def model_reply(payload, finish_reason="stop"):
    return {
        "model": "z-ai/glm-5.3-flash",
        "choices": [
            {"finish_reason": finish_reason, "message": {"content": _schema_json(payload)}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _schema_json(payload):
    import json

    return json.dumps(payload, ensure_ascii=False)


def envelope_for(
    pr,
    state="complete",
    verdict="green",
    issues=None,
    coverage=None,
    run_id="555",
    run_attempt=1,
    artifact=None,
    config_hash="c" * 64,
):
    payload = {
        "schema_version": 1,
        "prompt_version": 2,
        "repository": REPO,
        "pr_number": pr["number"],
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "inputs_hash": _result.inputs_hash(pr),
        "model": "z-ai/glm-5.3-flash",
        "config_hash": config_hash,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "timestamp": "2026-09-15T00:00:00Z",
        "review_state": state,
        "verdict": verdict,
        "blocking_count": sum(1 for i in (issues or []) if i.get("blocking")),
        "issues": issues or [],
        "summary": "summary",
        "coverage": coverage
        or {
            "complete": True,
            "total_files": 1,
            "included": ["src/app.py"],
            "excluded": [],
            "missing": [],
            "failed": [],
        },
    }
    if artifact:
        payload["artifact"] = artifact
    return payload


class FakeGitHub:
    """In-memory stand-in for :class:`reviewer_v2.github_api.GitHub`."""

    def __init__(
        self,
        pr=None,
        files=None,
        comments=None,
        status_list=None,
        artifacts=None,
        fail=None,
        open_pulls=None,
    ):
        self.pr = pr or pr_payload()
        self.open_pulls = list(open_pulls or [])
        self.files = files if files is not None else [file_entry()]
        self.comment_list = list(comments or [])
        self.status_list = list(status_list or [])
        self.artifacts = list(artifacts or [])
        self.fail = fail or {}
        self.repo = REPO
        self.actors = (ACTOR,)
        self.labels = []
        self.dispatched = []
        self.calls = []
        self._next_comment_id = 100
        self.pull_reads = 0
        self.shares = []

    # -- configuration helpers -------------------------------------------------
    def set_trusted_actors(self, actors):
        self.actors = tuple(actors)

    def queue_pr(self, pr):
        """Make the next pull() call return a different PR (freshness checks)."""
        self.shares.append(copy.deepcopy(pr))

    # -- GitHub surface --------------------------------------------------------
    def pull(self, number):
        if self.fail.get("pull"):
            raise self.fail["pull"]
        self.pull_reads += 1
        if self.shares:
            return self.shares.pop(0)
        return copy.deepcopy(self.pr)

    def changed_files_page(self, number, page):
        return copy.deepcopy(self.files) if page == 1 else []

    def comments(self, number):
        if self.fail.get("comments"):
            raise self.fail["comments"]
        return [dict(item) for item in self.comment_list]

    def trusted_comments(self, number):
        return [
            item
            for item in self.comment_list
            if (item.get("user") or {}).get("login") in self.actors
            and (item.get("body") or "").lstrip().startswith(_github.MARKER)
        ]

    def create_comment(self, number, body):
        if self.fail.get("create_comment"):
            raise self.fail["create_comment"]
        self._next_comment_id += 1
        comment = {
            "id": self._next_comment_id,
            "body": body,
            "user": {"login": ACTOR},
            "html_url": f"https://example/comment/{self._next_comment_id}",
        }
        self.comment_list.append(comment)
        return comment

    def update_comment(self, comment_id, body):
        for comment in self.comment_list:
            if comment["id"] == comment_id:
                comment["body"] = body
                return comment
        raise AssertionError(f"no comment {comment_id}")

    def upsert_comment(self, number, body):
        existing = self.trusted_comments(number)
        if existing:
            return self.update_comment(existing[-1]["id"], body)
        return self.create_comment(number, body)

    def statuses(self, sha):
        return [item for item in self.status_list if item["sha"] == sha]

    def latest_status(self, sha, context):
        for item in reversed(self.status_list):
            if item["sha"] == sha and item["context"] == context:
                return item
        return None

    def set_status(self, sha, state, context, description, target_url=""):
        if self.fail.get("set_status"):
            raise self.fail["set_status"]
        entry = {
            "sha": sha,
            "state": state,
            "context": context,
            "description": description,
            "target_url": target_url,
        }
        self.status_list.append(entry)
        if self.fail.get("latest_status"):
            pass
        return entry

    def add_labels(self, number, labels):
        self.labels.extend(labels)
        return labels

    def set_verdict_label(self, number, label, stale_label=None):
        self.labels.append(label)
        return [label]

    def run_artifacts(self, run_id):
        return list(self.artifacts)

    def call(self, path, method="GET", payload=None, idempotent=True, parse=True):
        self.calls.append((method, path, payload))
        if method == "DELETE" and "/labels/" in path:
            return None
        if "/pulls?" in path:
            return list(self.open_pulls) if "page=1" in path else []
        if "/actions/runs/" in path and path.endswith("/artifacts"):
            return {"artifacts": list(self.artifacts)}
        if "/commits/" in path and path.endswith("/status"):
            return {"statuses": self.statuses(payload or "")}
        return None

    def dispatch_workflow(self, workflow_file, ref, inputs):
        self.dispatched.append((workflow_file, ref, dict(inputs)))
        return None
