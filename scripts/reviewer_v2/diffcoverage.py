"""Complete diff accounting: every file/hunk is included, excluded with a documented
reason, missing, or failed -- never silently truncated.

The legacy reviewer stopped at the first oversized file and then appended a
"diff truncado" marker while still reporting a verdict. Here an oversized file is
split by hunk (coordinates preserved) and an unsplittable hunk marks the review
incomplete instead of silently dropping everything after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config as _config

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Intentional, documented exclusions: generated or non-source artefacts that carry no
# reviewable logic. Everything else must be included or reported as failed/missing.
GENERATED_PATTERNS = (
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)yarn\.lock$"),
    re.compile(r"(^|/)pnpm-lock\.yaml$"),
    re.compile(r"(^|/)Cargo\.lock$"),
    re.compile(r"(^|/)go\.sum$"),
    re.compile(r"(^|/)dist/"),
    re.compile(r"(^|/)build/"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)vendor/"),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.(png|jpg|jpeg|gif|ico|webp|pdf|woff2?|ttf|eot|zip|gz|snap)$"),
    re.compile(r"(^|/)__snapshots__/"),
)
GENERATED_REASON = "generated/binary artefact excluded by documented policy"
NO_PATCH_REASON = "no textual patch (binary file or GitHub omitted the diff)"


def tokens_bound(text: str) -> int:
    """Documented conservative bound (see ``config.CHARS_PER_TOKEN_BOUND``)."""
    return (len(text) + _config.CHARS_PER_TOKEN_BOUND - 1) // _config.CHARS_PER_TOKEN_BOUND


def is_generated(filename: str) -> bool:
    return any(pattern.search(filename) for pattern in GENERATED_PATTERNS)


@dataclass
class Chunk:
    index: int
    text: str
    files: list = field(default_factory=list)  # filenames included in this chunk
    tokens_bound: int = 0


@dataclass
class Coverage:
    total_files: int = 0
    included: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    unverifiable_findings: list = field(default_factory=list)
    complete: bool = True

    def fail(self, filename: str, reason: str) -> None:
        self.failed.append({"file": filename, "reason": reason})
        self.complete = False

    def to_dict(self) -> dict:
        return {
            "complete": self.complete,
            "total_files": self.total_files,
            "included": list(self.included),
            "excluded": list(self.excluded),
            "missing": list(self.missing),
            "failed": list(self.failed),
            "reasons": list(self.reasons),
            "unverifiable_findings": list(self.unverifiable_findings),
        }


@dataclass
class DiffPlan:
    chunks: list = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    # filename -> list of (start, end) line ranges present in the reviewed diff,
    # tracked for both the old (deletion) and the new (addition) side.
    line_index: dict = field(default_factory=dict)
    # new path -> old path for renames, so either name verifies.
    renamed: dict = field(default_factory=dict)

    def reviewed_files(self) -> set:
        return set(self.coverage.included)

    def location(self, filename: str):
        if filename in self.line_index:
            return self.line_index[filename]
        old = self.renamed.get(filename)
        if old is not None:
            return self.line_index.get(old)


def hunk_coordinates(patch: str) -> list:
    """Return [(old_start, old_end, new_start, new_end)] for every hunk header."""
    spans = []
    for line in (patch or "").splitlines():
        match = HUNK_HEADER.match(line)
        if not match:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or 1)
        new_start = int(match.group(3))
        new_count = int(match.group(4) or 1)
        spans.append(
            (
                old_start,
                old_start + max(old_count, 1) - 1,
                new_start,
                new_start + max(new_count, 1) - 1,
            )
        )
    return spans


def split_patch_by_hunk(filename: str, patch: str) -> list:
    """Split a patch into per-hunk blocks, each keeping its ``@@`` coordinates."""
    lines = patch.splitlines()
    groups, current = [], None
    for line in lines:
        if HUNK_HEADER.match(line):
            if current:
                groups.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
        else:  # pragma: no cover - GitHub patches always start with a hunk header
            current = [line]
    if current:
        groups.append(current)
    blocks = []
    for group in groups:
        span = hunk_coordinates(group[0])
        label = ""
        if span:
            old_start, old_end, new_start, new_end = span[0]
            label = f" [old {old_start}-{old_end} / new {new_start}-{new_end}]"
        blocks.append(f"### {filename}{label}\n```diff\n" + "\n".join(group) + "\n```")
    return blocks


def fetch_changed_files(github, pr_number: int):
    """Fetch every page of changed files, then reconcile with the PR metadata count."""
    files, page = [], 1
    while page <= 100:
        batch = github.changed_files_page(pr_number, page)
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def build_plan(files, pr, budgets, overhead_chars: int = 0) -> DiffPlan:
    """Build chunks and a complete coverage manifest for the whole PR."""
    plan = DiffPlan()
    coverage = plan.coverage
    coverage.total_files = len(files)

    expected = pr.get("changed_files") if isinstance(pr, dict) else None
    if isinstance(expected, int) and expected != len(files):
        coverage.missing.append(
            {
                "file": "*",
                "reason": f"enumeration mismatch: PR reports {expected} changed files, "
                f"the files API returned {len(files)}",
            }
        )
        coverage.complete = False

    overhead_tokens = tokens_bound("x" * max(0, overhead_chars))
    capacity_tokens = (
        budgets.model_context_tokens - budgets.completion_reserve_tokens - overhead_tokens
    )
    if capacity_tokens <= 0:
        coverage.fail("*", "prompt overhead plus completion reserve leaves no room for a diff")
        return plan
    capacity_chars = max(
        1000, min(budgets.max_diff_chars, capacity_tokens * _config.CHARS_PER_TOKEN_BOUND)
    )

    pending: list = []
    for entry in files:
        filename = entry.get("filename") or "?"
        status = entry.get("status") or "?"
        previous = entry.get("previous_filename")
        if previous:
            # Accept either path: renames must not hide a finding's location.
            plan.renamed[filename] = previous
            plan.renamed[previous] = filename
        header = (
            f"### {filename} ({status}, +{entry.get('additions', 0)}/-{entry.get('deletions', 0)})"
        )
        patch = entry.get("patch")
        if not patch:
            changes = int(entry.get("additions") or 0) + int(entry.get("deletions") or 0)
            if is_generated(filename) or changes == 0:
                # Binary/generated artefact, or a rename with no textual change:
                # intentionally out of scope, documented in the coverage manifest.
                reason = GENERATED_REASON if is_generated(filename) else NO_PATCH_REASON
                coverage.excluded.append({"file": filename, "reason": reason})
            else:
                # A file that does change but has no textual patch cannot be reviewed.
                coverage.missing.append(
                    {
                        "file": filename,
                        "reason": "GitHub returned no textual patch for a file that changes",
                    }
                )
                coverage.complete = False
            continue
        if is_generated(filename):
            coverage.excluded.append({"file": filename, "reason": GENERATED_REASON})
            continue
        spans = hunk_coordinates(patch)
        if spans:
            ranges = []
            for old_start, old_end, new_start, new_end in spans:
                ranges.append((old_start, old_end))
                ranges.append((new_start, new_end))
            plan.line_index[filename] = ranges
            if previous:
                plan.line_index[previous] = ranges
        block = f"{header}\n```diff\n{patch}\n```"
        if len(block) <= capacity_chars:
            pending.append((filename, block))
            continue
        # Oversized file: split by hunk instead of dropping every later file.
        for piece in split_patch_by_hunk(filename, patch):
            if len(piece) <= capacity_chars:
                pending.append((filename, piece))
            else:
                coverage.fail(
                    filename,
                    f"a single hunk of {filename} needs {len(piece)} chars, "
                    f"above the per-request budget of {capacity_chars}",
                )

    chunks, current, current_files, current_size = [], [], [], 0
    for filename, block in pending:
        if current and current_size + len(block) + 2 > capacity_chars:
            chunks.append((current, current_files))
            current, current_files, current_size = [], [], 0
        current.append(block)
        current_files.append(filename)
        current_size += len(block) + 2
    if current:
        chunks.append((current, current_files))

    if len(chunks) > budgets.max_chunks:
        for _dropped, dropped_files in chunks[budgets.max_chunks :]:
            for filename in dict.fromkeys(dropped_files):
                coverage.fail(filename, f"exceeds MAX_CHUNKS={budgets.max_chunks}")
        chunks = chunks[: budgets.max_chunks]

    for index, (blocks, names) in enumerate(chunks):
        text = "\n\n".join(blocks)
        plan.chunks.append(
            Chunk(
                index=index,
                text=text,
                files=list(dict.fromkeys(names)),
                tokens_bound=tokens_bound(text),
            )
        )
    if len(plan.chunks) > 1:
        coverage.reasons.append(
            "the diff was split across chunks; cross-chunk context is not visible "
            "to the model in a single pass"
        )
    for chunk in plan.chunks:
        coverage.included.extend(name for name in chunk.files if name not in coverage.included)
    if coverage.total_files == 0:
        coverage.reasons.append("the pull request has no changed files")
    return plan


def _numbers(text: str) -> list:
    return [int(value) for value in re.findall(r"\d+", text or "")]


def validate_findings(plan: DiffPlan, issues: list, policy) -> tuple:
    """Split findings into verifiable ones and unverifiable ones.

    A finding is verifiable when its file was reviewed and, if it cites line numbers,
    at least one cited number falls inside a hunk range present in the reviewed diff.
    Renames are accepted under either the old or the new path.
    """
    verified, unverifiable = [], []
    for issue in issues:
        filename = str(issue.get("file") or "").strip()
        accepted = filename in plan.reviewed_files()
        if not accepted:
            previous = plan.renamed.get(filename)
            accepted = previous is not None and previous in plan.reviewed_files()
        if not accepted:
            unverifiable.append(
                {
                    "id": issue.get("id"),
                    "file": filename,
                    "lines": issue.get("lines"),
                    "reason": "file is not part of the reviewed diff",
                    "blocking_eligible": policy.blocks(
                        issue.get("category"), issue.get("severity")
                    ),
                }
            )
            continue
        spans = plan.location(filename) or []
        cited = _numbers(issue.get("lines"))
        if cited and spans:
            inside = any(any(start <= number <= end for start, end in spans) for number in cited)
            if not inside:
                unverifiable.append(
                    {
                        "id": issue.get("id"),
                        "file": filename,
                        "lines": issue.get("lines"),
                        "reason": "cited line numbers are outside every reviewed hunk",
                        "blocking_eligible": policy.blocks(
                            issue.get("category"), issue.get("severity")
                        ),
                    }
                )
                continue
        verified.append(issue)
    return verified, unverifiable


def deduplicate_issues(issues: list) -> list:
    """Drop overlapping duplicates produced by chunked reviews, keeping stable ids."""
    seen, unique = set(), []
    for issue in issues:
        key = (
            str(issue.get("file") or "").strip().lower(),
            str(issue.get("title") or "").strip().lower(),
            str(issue.get("category") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    for position, issue in enumerate(unique, start=1):
        issue["id"] = f"ISSUE-{position}"
    return unique
