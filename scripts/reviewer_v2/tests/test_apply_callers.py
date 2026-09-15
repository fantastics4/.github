"""T008 regression tests: branch-aware rollout tooling (no writes, no network)."""

import unittest

from reviewer_v2 import apply_callers as A
from reviewer_v2 import drift_check as D

SHA = "a" * 40


class FakeClient:
    def __init__(self, repos, branches=None, contents=None, secrets=None, prs=None):
        self.repos = repos
        self.branch_map = branches or {}
        self.content_map = contents or {}
        self.secret_map = secrets or {}
        self.pr_map = prs or {}
        self.writes = []

    def list_repos(self):
        return [{"name": name, "archived": archived} for name, archived in self.repos]

    def branches(self, repo):
        return list(self.branch_map.get(repo, ["main"]))

    def content(self, repo, path, ref):
        entry = self.content_map.get((repo, path, ref))
        return entry if entry else (None, None)

    def secret_names(self, repo):
        return set(self.secret_map.get(repo, []))

    def open_prs(self, repo, head):
        return list(self.pr_map.get((repo, head), []))

    def ensure_branch(self, repo, branch, from_branch):
        self.writes.append(("branch", repo, branch))
        return True

    def put_file(self, repo, path, branch, content, sha):
        self.writes.append(("file", repo, path, branch))

    def create_pr(self, repo, head, base, title, body):
        self.writes.append(("pr", repo, head, base))

    def default_branch(self, repo):
        return self.branch_map.get(repo, ["main"])[0]


class DesiredFilesTests(unittest.TestCase):
    def test_should_pin_the_release_sha_and_replace_branch_token(self):
        rendered = A.desired_files(SHA, "[main, develop]")
        caller = rendered[A.CALLER_PATH]
        refresh = rendered[A.REFRESH_PATH]
        self.assertIn(SHA, caller)
        self.assertNotIn(A.RELEASE_TOKEN, caller)
        self.assertNotIn("@main", caller)
        self.assertIn("branches: [main, develop]", refresh)
        self.assertNotIn(A.BRANCHES_TOKEN, refresh)

    def test_should_keep_the_model_secret_out_of_the_refresh_caller(self):
        rendered = A.desired_files(SHA, "[main]")
        self.assertNotIn("OPENROUTER", rendered[A.REFRESH_PATH])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.desired = A.desired_files(SHA, "[main]")

    def test_should_be_a_noop_when_the_content_already_matches(self):
        existing = {path: text for path, text in self.desired.items()}
        decision = A.decide("repo", "main", existing, self.desired)
        self.assertEqual("noop", decision["action"])

    def test_should_install_when_the_caller_is_missing(self):
        decision = A.decide("repo", "develop", {}, self.desired)
        self.assertEqual("update", decision["action"])
        self.assertEqual(sorted(self.desired), decision["paths"])

    def test_should_preserve_a_customized_caller(self):
        custom = {
            A.CALLER_PATH: "name: Custom\n# OPENROUTER_API_KEY custom job",
            A.REFRESH_PATH: self.desired[A.REFRESH_PATH],
        }
        decision = A.decide("repo", "main", custom, self.desired)
        self.assertEqual("preserve", decision["action"])

    def test_should_replace_the_known_legacy_caller(self):
        legacy = {
            A.CALLER_PATH: "uses: fantastics4/.github/.github/workflows/"
            "llm-pr-review.yml@main\n# OPENROUTER_API_KEY"
        }
        decision = A.decide("repo", "main", legacy, self.desired)
        self.assertEqual("update", decision["action"])

    def test_should_replace_a_customized_caller_only_when_asked(self):
        custom = {A.CALLER_PATH: "name: Custom\n# OPENROUTER_API_KEY"}
        decision = A.decide("repo", "main", custom, self.desired, customized_ok=True)
        self.assertEqual("update", decision["action"])


class PlanTests(unittest.TestCase):
    def test_should_cover_every_repository_with_a_decision_on_each_target_branch(self):
        client = FakeClient(
            repos=[("demo-repository", False), (".github", False), ("old", True)],
            branches={"demo-repository": ["main", "develop"]},
            secrets={"demo-repository": ["OPENROUTER_API_KEY"]},
        )
        entries = A.plan(client, SHA)
        actions = {(entry["repo"], entry["branch"], entry["action"]) for entry in entries}
        self.assertIn(("demo-repository", "main", "update"), actions)
        self.assertIn(("demo-repository", "develop", "update"), actions)
        self.assertIn((".github", "-", "exclude"), actions)
        self.assertIn(("old", "-", "exclude"), actions)

    def test_should_report_a_missing_secret_by_name_only(self):
        client = FakeClient(repos=[("demo-repository", False)], secrets={"demo-repository": []})
        entry = A.plan(client, SHA)[0]
        self.assertFalse(entry["secret_ready"])
        self.assertIn("OPENROUTER_API_KEY", entry["warning"])

    def test_should_use_the_inventory_branch_list_when_provided(self):
        client = FakeClient(
            repos=[("demo-repository", False)], branches={"demo-repository": ["main", "release"]}
        )
        inventory = {"demo-repository": {"branches": ["release"]}}
        entries = A.plan(client, SHA, inventory=inventory)
        self.assertEqual(["release"], [entry["branch"] for entry in entries])

    def test_should_use_distinct_rollout_branches_per_target_branch(self):
        self.assertNotEqual(A.rollout_branch("main", SHA), A.rollout_branch("develop", SHA))

    def test_should_write_nothing_during_a_dry_run(self):
        client = FakeClient(repos=[("demo-repository", False)])
        self.assertEqual(0, A.main(["--dry-run", "--release-sha", SHA], client=client))
        self.assertEqual([], client.writes)

    def test_should_reject_a_non_sha_release(self):
        client = FakeClient(repos=[])
        self.assertEqual(2, A.main(["--release-sha", "main"], client=client))
        self.assertEqual([], client.writes)

    def test_should_reuse_an_existing_open_pr_instead_of_creating_another(self):
        client = FakeClient(
            repos=[("demo-repository", False)],
            branches={"demo-repository": ["main"]},
            secrets={"demo-repository": ["OPENROUTER_API_KEY"]},
            prs={
                ("demo-repository", A.rollout_branch("main", SHA)): [
                    {"html_url": "https://example/pr/1"}
                ]
            },
        )
        code = A.main(["--release-sha", SHA], client=client)
        self.assertEqual(0, code)
        self.assertTrue(any(write[0] == "pr" for write in client.writes) is False)
        self.assertTrue(any(write[0] == "file" for write in client.writes))

    def test_should_run_a_no_write_drift_check(self):
        installed = A.desired_files(SHA, "[main]")
        contents = {
            ("demo-repository", path, "main"): (text, "sha") for path, text in installed.items()
        }
        clean = FakeClient(
            repos=[("demo-repository", False)],
            contents=contents,
            secrets={"demo-repository": ["OPENROUTER_API_KEY"]},
        )
        self.assertEqual(0, D.main(["--release-sha", SHA], client=clean))
        self.assertEqual([], clean.writes)

    def test_should_detect_a_missing_caller_and_a_wrong_pin(self):
        contents = {
            ("demo-repository", A.REFRESH_PATH, "main"): (
                A.desired_files("b" * 40, "[main]")[A.REFRESH_PATH],
                "sha",
            )
        }
        drifted = FakeClient(repos=[("demo-repository", False)], contents=contents)
        self.assertEqual(1, D.main(["--release-sha", SHA], client=drifted))
        self.assertEqual([], drifted.writes)

    def test_should_not_claim_success_when_github_is_unreadable(self):
        class Broken(FakeClient):
            def list_repos(self):
                raise A.InstallationError("HTTP 403: bad credentials")

        self.assertEqual(2, D.main(["--release-sha", SHA], client=Broken(repos=[])))


if __name__ == "__main__":
    unittest.main()
