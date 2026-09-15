"""T004 regression tests: complete diff accounting and finding locations."""

import unittest

from reviewer_v2 import config as _config
from reviewer_v2 import diffcoverage as D
from reviewer_v2.tests import support

BUDGETS = _config.Config.from_env({}).budgets
POLICY = _config.Config.from_env({}).policy


def patch(lines=3, prefix=""):
    return (
        f"@@ -1,{lines} +1,{lines} @@\n"
        + "\n".join(f"{prefix}line{i}" for i in range(lines))
        + "\n"
    )


class PlanTests(unittest.TestCase):
    def plan(self, files, changed_files=None, budgets=BUDGETS):
        pr = support.pr_payload(changed_files=changed_files)
        return D.build_plan(files, pr, budgets)

    def test_should_keep_later_files_after_an_oversized_first_file(self):
        many_hunks = "".join(patch(20) for _ in range(12))
        big = support.file_entry("big.py", patch=many_hunks)
        small = support.file_entry("small.py", patch=patch(3))
        budgets = _config.Config.from_env({"MAX_DIFF_CHARS": "2000"}).budgets
        plan = self.plan([big, small], budgets=budgets)
        self.assertIn("big.py", plan.coverage.included)
        self.assertIn("small.py", plan.coverage.included)
        self.assertTrue(plan.coverage.complete, plan.coverage.to_dict())

    def test_should_mark_an_unsplittable_hunk_as_failed_not_truncated(self):
        huge = support.file_entry("huge.py", patch=patch(5000))
        small = support.file_entry("small.py", patch=patch(2))
        budgets = _config.Config.from_env({"MAX_DIFF_CHARS": "1200"}).budgets
        plan = self.plan([huge, small], budgets=budgets)
        self.assertFalse(plan.coverage.complete)
        self.assertTrue(plan.coverage.failed)
        self.assertIn("small.py", plan.coverage.included)

    def test_should_report_enumeration_mismatch_as_missing(self):
        plan = self.plan([support.file_entry()], changed_files=3)
        self.assertFalse(plan.coverage.complete)
        self.assertTrue(plan.coverage.missing)

    def test_should_exclude_binary_and_generated_files_with_a_reason(self):
        files = [
            support.file_entry("logo.png", patch=None, additions=0, deletions=0),
            support.file_entry("poetry.lock", patch=None, additions=5, deletions=1),
        ]
        plan = self.plan(files)
        self.assertTrue(plan.coverage.complete)
        self.assertEqual(2, len(plan.coverage.excluded))
        self.assertTrue(all(item["reason"] for item in plan.coverage.excluded))

    def test_should_report_a_changing_file_without_patch_as_missing(self):
        plan = self.plan([support.file_entry("src/x.py", patch=None, additions=4)])
        self.assertFalse(plan.coverage.complete)
        self.assertEqual("src/x.py", plan.coverage.missing[0]["file"])

    def test_should_treat_an_empty_pull_request_as_complete_with_a_reason(self):
        plan = self.plan([])
        self.assertTrue(plan.coverage.complete)
        self.assertTrue(plan.coverage.reasons)

    def test_should_split_a_wide_diff_into_bounded_chunks(self):
        files = [support.file_entry(f"f{i}.py", patch=patch(120)) for i in range(6)]
        budgets = _config.Config.from_env({"MAX_DIFF_CHARS": "4000"}).budgets
        plan = self.plan(files, budgets=budgets)
        self.assertGreater(len(plan.chunks), 1)
        for chunk in plan.chunks:
            self.assertLessEqual(len(chunk.text), budgets.max_diff_chars + 200)
        limited = _config.Config.from_env({"MAX_DIFF_CHARS": "1800", "MAX_CHUNKS": "2"}).budgets
        plan = self.plan(files, budgets=limited)
        self.assertFalse(plan.coverage.complete)
        self.assertTrue(any("MAX_CHUNKS" in item["reason"] for item in plan.coverage.failed))


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.plan = D.build_plan(
            [support.file_entry("src/app.py", patch="@@ -10,2 +10,3 @@\n a\n+b\n c\n")],
            support.pr_payload(),
            BUDGETS,
        )

    def test_should_accept_a_location_inside_a_reviewed_hunk(self):
        issues = [support.issue(file="src/app.py", lines="12")]
        verified, unverifiable = D.validate_findings(self.plan, issues, POLICY)
        self.assertEqual(1, len(verified))
        self.assertEqual([], unverifiable)

    def test_should_flag_a_fabricated_file_as_unverifiable(self):
        issues = [support.issue(file="src/never-touched.py", lines="1")]
        verified, unverifiable = D.validate_findings(self.plan, issues, POLICY)
        self.assertEqual([], verified)
        self.assertTrue(unverifiable[0]["blocking_eligible"])

    def test_should_flag_lines_outside_every_hunk(self):
        issues = [support.issue(file="src/app.py", lines="900-950")]
        _, unverifiable = D.validate_findings(self.plan, issues, POLICY)
        self.assertEqual(1, len(unverifiable))

    def test_should_accept_a_rename_under_the_old_path(self):
        plan = D.build_plan(
            [
                support.file_entry(
                    "src/new.py", previous="src/old.py", patch="@@ -1,2 +1,2 @@\n a\n+b\n"
                )
            ],
            support.pr_payload(),
            BUDGETS,
        )
        issues = [support.issue(file="src/old.py", lines="2")]
        verified, unverifiable = D.validate_findings(plan, issues, POLICY)
        self.assertEqual(1, len(verified))
        self.assertEqual([], unverifiable)

    def test_should_deduplicate_overlapping_chunk_findings(self):
        issues = [support.issue(file="a.py"), support.issue(file="a.py")]
        unique = D.deduplicate_issues(issues)
        self.assertEqual(1, len(unique))
        self.assertEqual("ISSUE-1", unique[0]["id"])

    def test_should_ignore_non_numeric_line_references(self):
        issues = [support.issue(file="src/app.py", lines="whole file")]
        verified, _ = D.validate_findings(self.plan, issues, POLICY)
        self.assertEqual(1, len(verified))


if __name__ == "__main__":
    unittest.main()
