"""GitHub REST client for reviewer v2.

Two rules matter more than convenience here:

* the sticky comment is only recognised when it *starts* with the marker **and** was
  written by the expected actor (``github-actions[bot]`` by default);
* a mutating call that may have reached GitHub (ambiguous network failure) is
  reconciled by reading the data back before retrying, so a comment is never
  duplicated.
"""

from __future__ import annotations

from . import net as _net

API = "https://api.github.com"
# A comment is only trusted when the body starts with this marker (after leading
# whitespace) and the author is the expected bot.
MARKER = "<!-- llm-pr-review -->"


class GitHub:
    """Thin, bounded REST client. All credentials stay inside ``headers``."""

    def __init__(
        self,
        repo,
        token,
        deadline,
        api=API,
        timeout=60.0,
        attempts=3,
        log=None,
        opener=None,
        sleeper=None,
        clock=None,
    ):
        self.repo = repo
        self.api = api.rstrip("/")
        self.deadline = deadline
        self.timeout = timeout
        self.attempts = attempts
        self.log = log
        self.headers = _net.github_headers(token)
        self._trusted_actors = ()
        self._opener = opener
        self._sleeper = sleeper
        self._clock = clock

    # -- plumbing ---------------------------------------------------------------
    def call(self, path, method="GET", payload=None, idempotent=True, parse=True):
        kwargs = {
            "method": method,
            "payload": payload,
            "headers": self.headers,
            "timeout": self.timeout,
            "deadline": self.deadline,
            "attempts": self.attempts if idempotent else 1,
            "log": self.log,
            "parse": parse,
        }
        if self._opener is not None:
            kwargs["opener"] = self._opener
        if self._sleeper is not None:
            kwargs["sleeper"] = self._sleeper
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return _net.request_json(f"{self.api}{path}", **kwargs)

    def paginated(self, path, limit=50):
        separator = "&" if "?" in path else "?"
        results = []
        for page in range(1, limit + 1):
            batch = self.call(f"{path}{separator}per_page=100&page={page}")
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
        return results

    # -- pull requests ----------------------------------------------------------
    def pull(self, pr_number):
        return self.call(f"/repos/{self.repo}/pulls/{pr_number}")

    def changed_files_page(self, pr_number, page):
        return self.call(f"/repos/{self.repo}/pulls/{pr_number}/files?per_page=100&page={page}")

    def open_pulls(self, base=None):
        query = "state=open&sort=updated&direction=desc"
        if base:
            query += f"&base={base}"
        return self.paginated(f"/repos/{self.repo}/pulls?{query}")

    # -- comments --------------------------------------------------------------
    @property
    def trusted_actors(self):
        return getattr(self, "_trusted_actors", ())

    def set_trusted_actors(self, actors):
        self._trusted_actors = tuple(actors)

    def comments(self, pr_number):
        return self.paginated(f"/repos/{self.repo}/issues/{pr_number}/comments")

    def trusted_comments(self, pr_number):
        """Comments authored by a trusted actor whose body starts with the marker."""
        actors = self.trusted_actors
        found = []
        for comment in self.comments(pr_number):
            login = ((comment.get("user") or {}).get("login") or "").strip()
            body = comment.get("body") or ""
            if login in actors and body.lstrip().startswith(MARKER):
                found.append(comment)
        found.sort(key=lambda item: item.get("id") or 0)
        return found

    def create_comment(self, pr_number, body):
        return self.call(
            f"/repos/{self.repo}/issues/{pr_number}/comments",
            method="POST",
            payload={"body": body},
            idempotent=False,
        )

    def update_comment(self, comment_id, body):
        return self.call(
            f"/repos/{self.repo}/issues/comments/{comment_id}",
            method="PATCH",
            payload={"body": body},
            idempotent=False,
        )

    def upsert_comment(self, pr_number, body):
        """Create or update the single trusted sticky comment, never duplicating it."""
        existing = self.trusted_comments(pr_number)
        if len(existing) > 1:
            ids = [comment.get("id") for comment in existing]
            self._emit("warning", f"duplicate trusted review comments {ids}; updating the newest")
        if existing:
            newest = existing[-1]
            return self.update_comment(newest["id"], body)
        try:
            return self.create_comment(pr_number, body)
        except _net.TransientApiError as exc:
            if not exc.ambiguous:
                raise
            # The POST may already have created the comment: reconcile before retrying.
            if self.trusted_comments(pr_number):
                self._emit("warning", "reconciled an ambiguous comment POST; not duplicating")
                return self.trusted_comments(pr_number)[-1]
            return self.create_comment(pr_number, body)

    # -- statuses and labels ---------------------------------------------------
    def statuses(self, sha):
        payload = self.call(f"/repos/{self.repo}/commits/{sha}/status")
        return (payload or {}).get("statuses", [])

    def latest_status(self, sha, context):
        for status in self.statuses(sha):
            if status.get("context") == context:
                return status
        return None

    def set_status(self, sha, state, context, description, target_url=""):
        payload = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        return self.call(
            f"/repos/{self.repo}/statuses/{sha}",
            method="POST",
            payload=payload,
            idempotent=False,
        )

    def add_labels(self, pr_number, labels):
        return self.call(
            f"/repos/{self.repo}/issues/{pr_number}/labels",
            method="POST",
            payload={"labels": list(labels)},
        )

    def set_verdict_label(self, pr_number, label, stale_label=None):
        result = self.add_labels(pr_number, [label])
        if stale_label:
            try:
                self.call(
                    f"/repos/{self.repo}/issues/{pr_number}/labels/{stale_label}",
                    method="DELETE",
                )
            except _net.PermanentApiError:
                pass
        return result

    # -- runs, artifacts and dispatches ----------------------------------------
    def run(self, run_id):
        return self.call(f"/repos/{self.repo}/actions/runs/{run_id}")

    def run_artifacts(self, run_id):
        payload = self.call(f"/repos/{self.repo}/actions/runs/{run_id}/artifacts")
        return (payload or {}).get("artifacts", [])

    def download_artifact(self, artifact_id):
        """Return the raw zip bytes of an artifact (no parsing)."""
        return self.call(f"/repos/{self.repo}/actions/artifacts/{artifact_id}/zip", parse=False)

    def dispatch_workflow(self, workflow_file, ref, inputs):
        return self.call(
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/dispatches",
            method="POST",
            payload={"ref": ref, "inputs": dict(inputs)},
            idempotent=False,
        )

    def _emit(self, level, message):
        if self.log:
            self.log({"message": message, "level": level})
