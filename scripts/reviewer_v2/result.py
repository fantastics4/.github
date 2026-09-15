"""Trusted result envelope and bounded comment rendering.

The model supplies findings only. Identity (repository/PR/SHAs), the input hash, the
coverage manifest, the blocking flags and the verdict are computed here, in trusted
Python, and are the only fields a consumer may rely on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config as _config
from .config import hash_object  # re-exported for callers/tests

OBJECTIVE_MARKER = "<!-- llm-pr-objective -->"
ARTIFACT_PREFIX = "llm-review-result"
# The compact envelope is what the comment carries when the full result is too large;
# it is small enough to always fit and contains only trusted metadata.
COMPACT_FIELDS = (
    "schema_version",
    "repository",
    "pr_number",
    "head_sha",
    "base_sha",
    "inputs_hash",
    "model",
    "config_hash",
    "run_id",
    "run_attempt",
    "timestamp",
    "review_state",
    "verdict",
    "blocking_count",
)


class ResultError(RuntimeError):
    """A result could not be rendered inside the comment budget."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def objective_block(body: str, limit: int = 4000) -> str:
    """Return the PR objective contract declared with ``<!-- llm-pr-objective -->``."""
    if not body:
        return ""
    start = body.find(OBJECTIVE_MARKER)
    if start < 0:
        return ""
    rest = body[start + len(OBJECTIVE_MARKER) :]
    for stopper in ("\n<!--", "\n---"):
        index = rest.find(stopper)
        if index >= 0:
            rest = rest[:index]
    return rest.strip()[:limit]


def inputs_hash(pr) -> str:
    """Hash of everything a review depends on besides the diff content itself."""
    return hash_object(
        {
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "base_ref": (pr.get("base") or {}).get("ref") or "",
            "base_sha": (pr.get("base") or {}).get("sha") or "",
            "draft": bool(pr.get("draft")),
        }
    )


def artifact_name(pr_number, head_sha: str, attempt=1) -> str:
    """Unique per attempt: the artifact API rejects a duplicate name within one run."""
    return f"{ARTIFACT_PREFIX}-{pr_number}-{head_sha[:12]}-a{int(attempt or 1)}"


def annotate(issues, policy):
    """Compute ``blocking`` per issue and the blocking count from the policy."""
    annotated, count = [], 0
    for issue in issues or []:
        item = dict(issue)
        item["blocking"] = bool(policy.blocks(item.get("category"), item.get("severity")))
        count += 1 if item["blocking"] else 0
        annotated.append(item)
    return annotated, count


def build_envelope(
    config,
    pr,
    *,
    repository,
    pr_number,
    run_id,
    run_attempt,
    tooling_revision,
    issues,
    summary,
    tests_to_add,
    human_gates,
    definition_of_done,
    diff_risks,
    confidence,
    coverage,
    review_state,
    verdict,
    extra=None,
):
    envelope = {
        "schema_version": _config.SCHEMA_VERSION,
        "prompt_version": _config.PROMPT_VERSION,
        "repository": repository,
        "pr_number": int(pr_number),
        "head_sha": (pr.get("head") or {}).get("sha") or "",
        "base_sha": (pr.get("base") or {}).get("sha") or "",
        "inputs_hash": inputs_hash(pr),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "config_hash": config.config_hash(),
        "tooling_revision": tooling_revision,
        "run_id": str(run_id),
        "run_attempt": int(run_attempt or 1),
        "timestamp": utc_now(),
        "review_state": review_state,
        "verdict": verdict if review_state == "complete" else None,
        "gate": config.gate,
        "blocking_count": sum(1 for issue in issues if issue.get("blocking")),
        "issues": issues,
        "summary": summary or "",
        "tests_to_add": tests_to_add or [],
        "human_gates": human_gates or [],
        "definition_of_done": definition_of_done or [],
        "diff_risks": diff_risks or [],
        "confidence": confidence,
        "coverage": coverage,
        "limitations": (extra or {}).get("limitations", []),
        "chunks": (extra or {}).get("chunks", 0),
        "unverifiable_findings": (extra or {}).get("unverifiable_findings", []),
        "failures": (extra or {}).get("failures", []),
        "usage": (extra or {}).get("usage", {}),
    }
    return envelope


def compact_envelope(envelope, artifact=None) -> dict:
    """Small, trusted subset used in the comment when the full result is huge."""
    compact = {key: envelope.get(key) for key in COMPACT_FIELDS}
    coverage = envelope.get("coverage") or {}
    compact["coverage"] = {
        "complete": coverage.get("complete"),
        "total_files": coverage.get("total_files"),
        "included": len(coverage.get("included") or []),
        "excluded": len(coverage.get("excluded") or []),
        "failed": len(coverage.get("failed") or []),
        "missing": len(coverage.get("missing") or []),
    }
    failures = envelope.get("failures") or []
    compact["reason"] = (failures[0] or {}).get("reason") if failures else None
    compact["artifact"] = artifact


def _prose(text, limit=1500) -> str:
    """Single-line, fence-safe prose for the comment (never applied to JSON)."""
    text = str(text or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = text.replace("```", "ʼʼʼ")
    text = " ".join(text.split())
    return text[:limit]


def _headline(envelope) -> str:
    state = envelope.get("review_state")
    verdict = envelope.get("verdict")
    if state == "complete" and verdict == "green":
        return "## ✅ LLM Review — GREEN (puede subir)"
    if state == "complete" and verdict == "red":
        return f"## ❌ LLM Review — RED ({envelope.get('blocking_count')} bloqueantes)"
    return f"## ⚠️ LLM Review — {str(state).upper()} (sin veredicto)"


def _coverage_lines(envelope) -> list:
    coverage = envelope.get("coverage") or {}
    lines = [
        "### 🔎 Cobertura del diff",
        "",
        f"- archivos: {coverage.get('total_files', 0)} · incluidos: "
        f"{len(coverage.get('included') or [])} · excluidos: "
        f"{len(coverage.get('excluded') or [])} · fallidos: "
        f"{len(coverage.get('failed') or [])} · faltantes: "
        f"{len(coverage.get('missing') or [])}",
        f"- completa: {'sí' if coverage.get('complete') else 'NO'}",
    ]
    for item in (coverage.get("excluded") or [])[:10]:
        lines.append(f"  - excluido `{item.get('file')}`: {_prose(item.get('reason'), 160)}")
    for item in (coverage.get("failed") or [])[:10]:
        lines.append(f"  - FALLÓ `{item.get('file')}`: {_prose(item.get('reason'), 160)}")
    for item in (coverage.get("missing") or [])[:10]:
        lines.append(f"  - FALTA `{item.get('file')}`: {_prose(item.get('reason'), 160)}")
    for limitation in (envelope.get("limitations") or [])[:10]:
        lines.append(f"  - limitación: {_prose(limitation, 200)}")
    lines.append("")
    return lines


def render(envelope, run_url, marker, max_chars) -> str:
    """Render the sticky comment inside ``max_chars``, never breaking the JSON block."""
    blocking = [issue for issue in envelope.get("issues") or [] if issue.get("blocking")]
    advisory = [issue for issue in envelope.get("issues") or [] if not issue.get("blocking")]
    header = [
        marker,
        _headline(envelope),
        "",
        _prose(envelope.get("summary"), 1200),
        "",
        f"<sub>modelo: `{envelope.get('model')}` · estado: `{envelope.get('review_state')}`"
        f" · head: `{str(envelope.get('head_sha'))[:12]}`"
        f" · base: `{str(envelope.get('base_sha'))[:12]}`"
        f" · config: `{str(envelope.get('config_hash'))[:12]}`"
        f" · run: [{envelope.get('run_id')}]({run_url})</sub>",
        "",
    ]

    blocking_lines = []
    if blocking:
        blocking_lines.append("### 🔴 Bloquean el merge")
        for issue in blocking:
            blocking_lines += _render_issue(issue)

    advisory_lines = []
    if advisory:
        advisory_lines.append(f"### 🟡 Sugerencias (no bloquean · {len(advisory)})")
        for issue in advisory:
            advisory_lines.append(
                f"- **{issue.get('id', '?')} · [{issue.get('category')}/{issue.get('severity')}] "
                f"{_prose(issue.get('title'), 200)}** — `{_prose(issue.get('file'), 120)}`: "
                f"{_prose(issue.get('exact_fix') or issue.get('problem'), 300)}"
            )
        advisory_lines.append("")

    extra_lines = []
    gates = envelope.get("human_gates") or []
    if gates:
        extra_lines.append("### 🙋 Requiere humano / producción (no bloquea)")
        for gate in gates:
            extra_lines.append(
                f"- **{_prose(gate.get('title'), 200)}** — {_prose(gate.get('why'), 300)} "
                f"→ {_prose(gate.get('action'), 200)}"
            )
        extra_lines.append("")
    tests = envelope.get("tests_to_add") or []
    if tests:
        extra_lines.append(f"### 🧪 Tests a añadir ({len(tests)})")
        extra_lines.append("")
        extra_lines.append("| ID | Tipo | Nombre | Archivo |")
        extra_lines.append("|---|---|---|---|")
        for test in tests[:20]:
            extra_lines.append(
                f"| {_prose(test.get('id'), 40)} | {_prose(test.get('type'), 20)} "
                f"| {_prose(test.get('name'), 200)} | `{_prose(test.get('file'), 120)}` |"
            )
        extra_lines.append("")

    risks = envelope.get("diff_risks") or []
    if risks:
        extra_lines.append("### ⚠️ Riesgos")
        extra_lines.append("")
        for risk in risks[:20]:
            extra_lines.append(f"- {_prose(risk, 300)}")
        extra_lines.append("")

    done = envelope.get("definition_of_done") or []
    if done:
        extra_lines.append("### ✅ Verificaciones")
        extra_lines.append("")
        for item in done[:20]:
            extra_lines.append(f"- `{_prose(item, 200)}`")
        extra_lines.append("")

    root = header + blocking_lines + extra_lines + _coverage_lines(envelope)
    footer = (
        "**Entrada para el fixer** — validar `review_state`, `verdict` y metadata "
        "contra el PR actual antes de aplicar nada."
    )
    full_payload = json.dumps(envelope, ensure_ascii=False, indent=2)
    full_block = ["", footer, "", "```json llm-review-result-v1", full_payload, "```"]
    candidate = "\n".join(root + advisory_lines + full_block)
    if len(candidate) <= max_chars:
        return candidate

    # Too large: the advisory list is the first thing to go (it never blocks).
    summary_line = []
    if advisory:
        summary_line = [
            f"### 🟡 Sugerencias (no bloquean · {len(advisory)})",
            "",
            f"_{len(advisory)} sugerencias omitidas del comentario por tamaño; "
            "están completas en el artefacto._",
            "",
        ]
    candidate = "\n".join(root + summary_line + full_block)
    if len(candidate) <= max_chars:
        return candidate

    # Still too large: publish a compact, trusted envelope; the artifact holds the rest.
    compact_payload = json.dumps(
        compact_envelope(envelope, artifact=envelope.get("artifact")),
        ensure_ascii=False,
        indent=2,
    )
    compact_block = [
        "",
        "_El resultado completo supera el máximo del comentario y se publica como "
        "artefacto verificable._",
        "",
        "```json llm-review-compact-v1",
        compact_payload,
        "```",
    ]
    candidate = "\n".join(root + summary_line + compact_block)
    if len(candidate) <= max_chars:
        return candidate
    raise ResultError(
        f"rendered comment needs {len(candidate)} chars, above MAX_COMMENT_CHARS={max_chars}"
    )


def _render_issue(issue) -> list:
    lines = [
        f"**{issue.get('id', '?')} · [{issue.get('category')}/{issue.get('severity')}] "
        f"{_prose(issue.get('title'), 300)}**",
        f"- **Archivo:** `{_prose(issue.get('file'), 200)}` (líneas "
        f"`{_prose(issue.get('lines'), 80)}`)",
        f"- **Problema:** {_prose(issue.get('problem'), 600)}",
        f"- **Por qué importa:** {_prose(issue.get('why_it_matters'), 500)}",
        f"- **Arreglo exacto:** {_prose(issue.get('exact_fix'), 600)}",
        "",
    ]
    return lines
