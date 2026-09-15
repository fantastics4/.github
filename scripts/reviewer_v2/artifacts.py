"""Upload and verify the trusted result artifact.

The artifact is a *required* publication stage: if the reviewer cannot store and then
see the artifact on the run, the review is not complete. Verification goes through the
documented REST endpoint (``actions/runs/{id}/artifacts``) rather than trusting the
upload response.
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse

from . import net as _net

ARTIFACT_API_VERSION = "6.0-preview"


class ArtifactError(RuntimeError):
    """The artifact could not be stored or verified."""


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_env(env) -> tuple:
    url = (env.get("ACTIONS_RUNTIME_URL") or "").rstrip("/") + "/"
    token = env.get("ACTIONS_RUNTIME_TOKEN") or ""
    run_id = env.get("GITHUB_RUN_ID") or ""
    if not url.strip("/") or not token or not run_id:
        raise ArtifactError(
            "the Actions artifact runtime is unavailable "
            "(need ACTIONS_RUNTIME_URL, ACTIONS_RUNTIME_TOKEN and GITHUB_RUN_ID)"
        )
    return url, token, run_id


def upload(name: str, path: str, env, request=None, log=None) -> dict:
    """Store ``path`` as ``name`` through the Actions runtime API (blob + finalize)."""
    request = request or _net.request_json
    runtime_url, token, run_id = _runtime_env(env)
    headers = {"Authorization": f"Bearer {token}"}
    size = os.path.getsize(path)
    created = request(
        f"{runtime_url}_apis/pipelines/workflows/{run_id}/artifacts"
        f"?api-version={ARTIFACT_API_VERSION}",
        method="POST",
        payload={"Type": "actions_storage", "Name": name},
        headers=headers,
        attempts=2,
        log=log,
    )
    if not isinstance(created, dict) or not created.get("fileContainerResourceUrl"):
        raise ArtifactError(f"artifact container creation returned an unexpected body: {created}")
    container = created["fileContainerResourceUrl"].rstrip("/")
    item = f"{name}/{os.path.basename(path)}"
    with open(path, "rb") as handle:
        blob = handle.read()
    file_headers = dict(headers)
    file_headers["Content-Type"] = "application/octet-stream"
    file_headers["Content-Range"] = f"bytes 0-{max(size - 1, 0)}/{size}"
    request(
        f"{container}?itemPath={urllib.parse.quote(item)}",
        method="PUT",
        payload=blob,
        headers={**file_headers, "Content-Length": str(size)},
        attempts=2,
        parse=False,
        log=log,
    )
    finalize_url = created.get("url")
    if finalize_url:
        request(
            f"{finalize_url}?api-version={ARTIFACT_API_VERSION}",
            method="PATCH",
            payload={"Size": size},
            headers=headers,
            attempts=2,
            log=log,
        )
    return {"name": name, "digest": sha256_file(path), "size": size, "run_id": str(run_id)}


def verify(github, run_id, name: str, log=None) -> dict:
    """Confirm the artifact exists, belongs to this run and is not expired."""
    artifacts = github.run_artifacts(run_id)
    for artifact in artifacts or []:
        if artifact.get("name") == name:
            if artifact.get("expired"):
                raise ArtifactError(f"artifact {name!r} is already expired")
            if str(artifact.get("workflow_run", {}).get("id") or run_id) != str(run_id):
                raise ArtifactError(f"artifact {name!r} is not associated with run {run_id}")
            if log:
                log({"message": "artifact verified", "artifact": artifact.get("id")})
            return artifact
    raise ArtifactError(f"artifact {name!r} was not found on run {run_id} after upload")
