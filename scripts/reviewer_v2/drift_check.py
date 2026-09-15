#!/usr/bin/env python3
"""Read-only drift check: installed caller coverage and release pins vs the inventory.

A public tooling-repo token may not see private organization repositories; when the
repository list is empty or truncated this reports an access problem instead of a
false "everything is installed".

  python3 drift_check.py --release-sha <40-hex> [--inventory inventory.json]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_v2.apply_callers import (  # noqa: E402
    GhClient,
    InstallationError,
    inventory_from,
    plan,
    summarize,
)


def check(client, release_sha, inventory=None):
    entries = plan(client, release_sha, inventory=inventory)
    drift = [entry for entry in entries if entry["action"] == "update"]
    blocked = [entry for entry in entries if entry["action"] == "blocked"]
    preserved = [entry for entry in entries if entry["action"] == "preserve"]
    return {
        "entries": entries,
        "drift": drift,
        "blocked": blocked,
        "preserved": preserved,
    }


def main(argv=None, env=None, client=None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Reviewer v2 installation drift check")
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--inventory", default=None)
    args = parser.parse_args(argv)
    client = client or GhClient()
    try:
        report = check(client, args.release_sha, inventory_from(args.inventory))
    except InstallationError as exc:
        print(f"::error::drift check could not read GitHub: {exc}", file=sys.stderr)
        return 2
    for line in summarize(report["entries"]):
        print(line)
    if report["blocked"]:
        for entry in report["blocked"]:
            print(f"::warning::{entry['repo']}: {entry['reason']}")
    if report["preserved"]:
        for entry in report["preserved"]:
            print(f"::warning::{entry['repo']}@{entry['branch']}: {entry['reason']}")
    if report["drift"]:
        print(f"drift: {len(report['drift'])} repository/branch pair(s) need installation")
        return 1
    print("drift: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
