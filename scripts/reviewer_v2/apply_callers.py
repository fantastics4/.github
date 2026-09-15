#!/usr/bin/env python3
"""Branch-aware, repeatable installation of the v2 reviewer callers.

Replaces the default-branch-only behaviour of scripts/apply-callers.sh (kept for the
legacy rollout). It compares content before writing, uses one rollout branch per
(target branch, release), queries existing PRs explicitly, reports secret-name
readiness without reading values, and never writes during a dry run.

  python3 apply_callers.py --dry-run --release-sha <40-hex>
  python3 apply_callers.py           --release-sha <40-hex>   # opens PRs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ORG = "fantastics4"
TOOLING_REPO = ".github"
CALLER_PATH = ".github/workflows/pr-llm-review.yml"
REFRESH_PATH = ".github/workflows/pr-llm-review-refresh.yml"
CALLER_TEMPLATE = "callers/pr-llm-review-v2.yml"
REFRESH_TEMPLATE = "callers/pr-llm-review-refresh-v2.yml"
RELEASE_TOKEN = "__RELEASE_SHA__"
BRANCHES_TOKEN = "__BRANCHES__"
EXCLUDED = {
    TOOLING_REPO: "tooling repository itself (no caller needed)",
}
CUSTOMIZED_MARKERS = ("OPENROUTER_API_KEY",)


class InstallationError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def templates() -> dict:
    root = repo_root()
    return {
        CALLER_PATH: (root / CALLER_TEMPLATE).read_text(encoding="utf-8"),
        REFRESH_PATH: (root / REFRESH_TEMPLATE).read_text(encoding="utf-8"),
    }


def desired_files(release_sha, branches) -> dict:
    """Render the templates for one repository (release pin + branch list)."""
    rendered = {}
    for path, text in templates().items():
        if RELEASE_TOKEN not in text and path != REFRESH_PATH:
            raise InstallationError(f"{path} template lost its release placeholder")
        text = text.replace(RELEASE_TOKEN, release_sha)
        text = text.replace(BRANCHES_TOKEN, branches)
        rendered[path] = text
    return rendered


def looks_customized(existing_text, template_text) -> bool:
    """A caller that is neither the legacy template nor our v2 template is custom."""
    if existing_text is None:
        return False
    if RELEASE_TOKEN in existing_text:
        return False
    if "llm-pr-review-v2.yml" in existing_text:
        return False
    if "llm-pr-review.yml@main" in existing_text and "workflow_run" in existing_text:
        return False  # the known legacy caller, replaced by our rollout
    return any(marker in existing_text for marker in CUSTOMIZED_MARKERS)


def decide(repo, branch, existing, desired, customized_ok=False) -> dict:
    """Pure decision for one (repo, branch): install, update, skip or preserve.

    ``existing`` maps path -> current text (or None when the file is absent).
    """
    changes = {}
    preserved = []
    for path, text in desired.items():
        current = existing.get(path)
        if current == text:
            continue
        if looks_customized(current, text) and not customized_ok:
            preserved.append(path)
            continue
        changes[path] = text
    if preserved and not changes:
        return {
            "repo": repo,
            "branch": branch,
            "action": "preserve",
            "paths": preserved,
            "reason": "customized caller preserved",
        }
    if not changes:
        return {
            "repo": repo,
            "branch": branch,
            "action": "noop",
            "paths": [],
            "reason": "already installed at this release",
        }
    return {
        "repo": repo,
        "branch": branch,
        "action": "update",
        "paths": sorted(changes),
        "reason": "content differs",
        "preserved": preserved,
        "changes": changes,
    }


class GhClient:
    """Minimal ``gh api`` wrapper: failures raise, they are never swallowed."""

    def __init__(self, runner=None):
        self.runner = runner or self._run

    @staticmethod
    def _run(args):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise InstallationError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def json(self, args):
        output = self.runner(["api", *args])
        return json.loads(output) if output.strip() else None

    def list_repos(self):
        repos, page = [], 1
        while True:
            batch = self.json([f"orgs/{ORG}/repos?per_page=100&page={page}"])
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repos

    def branches(self, repo):
        names, page = [], 1
        while True:
            batch = self.json([f"repos/{ORG}/{repo}/branches?per_page=100&page={page}"])
            if not batch:
                break
            names.extend(item["name"] for item in batch)
            if len(batch) < 100:
                break
            page += 1
        return names

    def content(self, repo, path, ref):
        try:
            payload = self.json([f"repos/{ORG}/{repo}/contents/{path}?ref={ref}"])
        except InstallationError:
            return None, None
        if not payload:
            return None, None
        import base64

        return base64.b64decode(payload["content"]).decode("utf-8"), payload["sha"]

    def secret_names(self, repo):
        payload = self.json([f"repos/{ORG}/{repo}/actions/secrets?per_page=100"])
        return {item["name"] for item in (payload or {}).get("secrets", [])}

    def open_prs(self, repo, head):
        payload = self.json([f"repos/{ORG}/{repo}/pulls?state=open&head={ORG}:{head}"])
        return payload or []

    def ensure_branch(self, repo, branch, from_branch):
        try:
            self.json([f"repos/{ORG}/{repo}/git/ref/heads/{branch}"])
            return False
        except InstallationError:
            base = self.json([f"repos/{ORG}/{repo}/git/ref/heads/{from_branch}"])
            self.runner(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{ORG}/{repo}/git/refs",
                    "-f",
                    f"ref=refs/heads/{branch}",
                    "-f",
                    f"sha={base['object']['sha']}",
                ]
            )
            return True

    def put_file(self, repo, path, branch, content, sha):
        import base64

        args = [
            "api",
            "-X",
            "PUT",
            f"repos/{ORG}/{repo}/contents/{path}",
            "-f",
            "message=ci: install reviewer v2 caller",
            "-f",
            f"content={base64.b64encode(content.encode()).decode()}",
            "-f",
            f"branch={branch}",
        ]
        if sha:
            args += ["-f", f"sha={sha}"]
        self.runner(args)

    def create_pr(self, repo, head, base, title, body):
        self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{ORG}/{repo}/pulls",
                "-f",
                f"title={title}",
                "-f",
                f"head={head}",
                "-f",
                f"base={base}",
                "-f",
                f"body={body}",
            ]
        )

    def default_branch(self, repo):
        payload = self.json([f"repos/{ORG}/{repo}"])
        return payload["default_branch"]


def target_branches(available, inventory=None):
    """Maintained PR-target branches, from the inventory when provided."""
    if inventory is not None:
        return [branch for branch in inventory if branch in available]
    return [branch for branch in ("main", "develop", "master") if branch in available]


def rollout_branch(branch, release_sha):
    return f"chore/llm-review-v2-{branch}-{release_sha[:7]}"


def inventory_from(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def plan(client, release_sha, inventory=None, customized_ok=False, only=None):
    """Compute every installation decision without writing anything."""
    plan_entries = []
    for repo_info in client.list_repos():
        name = repo_info["name"]
        if name in EXCLUDED:
            plan_entries.append(
                {"repo": name, "branch": "-", "action": "exclude", "reason": EXCLUDED[name]}
            )
            continue
        if repo_info.get("archived"):
            plan_entries.append(
                {"repo": name, "branch": "-", "action": "exclude", "reason": "archived repository"}
            )
            continue
        if only and name not in only:
            continue
        available = client.branches(name)
        branches = target_branches(available, (inventory or {}).get(name, {}).get("branches"))
        if not branches:
            plan_entries.append(
                {
                    "repo": name,
                    "branch": "-",
                    "action": "blocked",
                    "reason": "no maintained PR-target branch found",
                }
            )
            continue
        rendered = desired_files(release_sha, "[" + ", ".join(branches) + "]")
        secrets = client.secret_names(name)
        secret_ready = "OPENROUTER_API_KEY" in secrets
        for branch in branches:
            existing = {}
            shas = {}
            for path in rendered:
                text, sha = client.content(name, path, branch)
                existing[path] = text
                shas[path] = sha
            decision = decide(name, branch, existing, rendered, customized_ok)
            decision["shas"] = shas
            decision["secret_ready"] = secret_ready
            if not secret_ready:
                decision["warning"] = (
                    "OPENROUTER_API_KEY is missing in this repository; the first review "
                    "will fail until it is set (name check only, no value read)"
                )
            plan_entries.append(decision)
    return plan_entries


def summarize(entries):
    lines = []
    for entry in entries:
        if entry["action"] in ("exclude", "blocked"):
            lines.append(f"{entry['action']:8} {entry['repo']:32} {entry['reason']}")
            continue
        paths = ",".join(entry.get("paths") or []) or "-"
        extra = f" | {entry.get('warning')}" if entry.get("warning") else ""
        lines.append(
            f"{entry['action']:8} {entry['repo']:32} {entry['branch']:8} {paths}"
            f" | {entry['reason']}{extra}"
        )
    return lines


def apply_plan(client, entries, release_sha, title):
    """Open one PR per (repo, target branch). No direct pushes, ever."""
    results = []
    for entry in entries:
        if entry["action"] != "update":
            continue
        repo, branch = entry["repo"], entry["branch"]
        head = rollout_branch(branch, release_sha)
        client.ensure_branch(repo, head, branch)
        for path, text in (entry.get("changes") or {}).items():
            current_sha = entry["shas"].get(path)
            if current_sha:
                current_text, current_sha = client.content(repo, path, head) or (None, None)
            client.put_file(repo, path, head, text, current_sha)
        existing_prs = client.open_prs(repo, head)
        if existing_prs:
            results.append(
                {
                    "repo": repo,
                    "branch": branch,
                    "pr": existing_prs[0]["html_url"],
                    "action": "pr-reused",
                }
            )
            continue
        client.create_pr(
            repo,
            head,
            branch,
            title,
            "Installs the hardened reviewer v2 caller pinned to the reviewed "
            f"release {release_sha}. Requires the `OPENROUTER_API_KEY` repository "
            "secret (name checked only).",
        )
        results.append({"repo": repo, "branch": branch, "pr": "created", "action": "pr-created"})
    return results


def main(argv=None, env=None, client=None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Install the v2 reviewer callers")
    parser.add_argument(
        "--release-sha", required=True, help="40-hex reviewed release commit of fantastics4/.github"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; performs no writes")
    parser.add_argument(
        "--inventory", default=None, help="JSON inventory of per-repository branches"
    )
    parser.add_argument("--only", default=None, help="comma-separated repositories")
    parser.add_argument(
        "--include-customized",
        action="store_true",
        help="replace a customized caller (off by default)",
    )
    parser.add_argument("--title", default="ci: install reviewer v2 caller")
    args = parser.parse_args(argv)
    if len(args.release_sha) != 40 or any(
        character not in "0123456789abcdef" for character in args.release_sha
    ):
        print(
            "::error::--release-sha must be a 40-character lowercase hex commit SHA",
            file=sys.stderr,
        )
        return 2
    client = client or GhClient()
    inventory = inventory_from(args.inventory)
    only = set(args.only.split(",")) if args.only else None
    entries = plan(
        client,
        args.release_sha,
        inventory=inventory,
        customized_ok=args.include_customized,
        only=only,
    )
    print("release", args.release_sha)
    for line in summarize(entries):
        print(line)
    if args.dry_run:
        print("dry-run: no writes performed")
        return 0
    results = apply_plan(client, entries, args.release_sha, args.title)
    for result in results:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
