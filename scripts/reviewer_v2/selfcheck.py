#!/usr/bin/env python3
"""Static self-check for the release bundle (used by tooling CI, no network).

python3 selfcheck.py                  # triggers, pins, placeholders, legacy paths
python3 selfcheck.py --check-migration # the wrapper and the tooling ship together
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent.parent
PIN = re.compile(r"uses:\s*\S+@([0-9a-f]{40})\b")
CALLER = ROOT / "callers" / "pr-llm-review-v2.yml"
REFRESH = ROOT / "callers" / "pr-llm-review-refresh-v2.yml"
REUSABLE = ROOT / ".github" / "workflows" / "llm-pr-review-v2.yml"
LEGACY = (
    ROOT / "callers" / "pr-llm-review.yml",
    ROOT / ".github" / "workflows" / "llm-pr-review.yml",
    ROOT / "scripts" / "llm_review.py",
)


def problems() -> list:
    found = []
    for path in (CALLER, REFRESH, REUSABLE):
        if not path.exists():
            found.append(f"missing {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("uses:") and "__RELEASE_SHA__" not in stripped:
                if not PIN.search(stripped):
                    found.append(f"unpinned action in {path.name}: {stripped}")
        if path != REUSABLE and "@main" in text:
            found.append(f"branch pin instead of a release pin in {path.name}")
        if path != REUSABLE and "__RELEASE_SHA__" not in text:
            found.append(f"{path.name} lost its release placeholder")
        if "workflow_run" in text:
            found.append(f"{path.name} still depends on workflow_run")
    caller = CALLER.read_text(encoding="utf-8") if CALLER.exists() else ""
    if "edited" not in caller:
        found.append("the caller does not trigger on review-relevant edits")
    if "needs.admission.outputs.pr_number" not in caller:
        found.append("the caller does not key concurrency on the admitted PR number")
    refresh = REFRESH.read_text(encoding="utf-8") if REFRESH.exists() else ""
    if "OPENROUTER" in refresh:
        found.append("the refresh dispatcher must not hold the model secret")
    for path in LEGACY:
        if not path.exists():
            found.append(f"legacy path removed before migration finished: {path.name}")
    return found


def migration_problems() -> list:
    """The wrapper and the tooling must ship from the same repository revision."""
    found = []
    text = REUSABLE.read_text(encoding="utf-8") if REUSABLE.exists() else ""
    if "job.workflow_repository" not in text or "job.workflow_sha" not in text:
        found.append("the reusable workflow does not resolve its own repository/revision")
    if "scripts/reviewer_v2/review.py" not in text:
        found.append("the reusable workflow does not run the v2 entry point")
    if "persist-credentials: false" not in text:
        found.append("the tooling checkout keeps persisted credentials")
    entry = ROOT / "scripts" / "reviewer_v2" / "review.py"
    if not entry.exists():
        found.append("the v2 entry point is missing")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reviewer v2 release self-check")
    parser.add_argument("--check-migration", action="store_true")
    args = parser.parse_args(argv)
    found = migration_problems() if args.check_migration else problems()
    for problem in found:
        print(f"::error::{problem}")
    if found:
        return 1
    print("self-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
