from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
import argparse
from pathlib import Path
from typing import Any

from tools.x402_review import ReviewStateError, ReviewStore


class PublicProjectionError(RuntimeError):
    pass


PUBLIC_REPOSITORY = "HansHWestphal/x402-watch"
PUBLIC_REPOSITORY_URL = f"https://github.com/{PUBLIC_REPOSITORY}.git"
PUBLIC_PAGES_URL = "https://hanshwestphal.github.io/x402-watch/"
PUBLIC_FILES = ("data/brief.json", "data/manifest.json")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicProjectionError(f"{label} could not be loaded: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicProjectionError(f"{label} must be a JSON object")
    return value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".new",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _approved_inputs(
    run_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    review = ReviewStore(run_dir).load()
    if not review:
        raise PublicProjectionError("Private review state is missing")
    run = _load_json(run_dir / "run.json", "Private run")
    synthesis = _load_json(run_dir / "synthesis.json", "Synthesis")
    try:
        brief = (run_dir / "brief.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise PublicProjectionError(f"Private brief could not be loaded: {exc}") from exc
    draft_hash = _sha256(brief)
    if review.get("run_id") != run.get("run_id") or review.get("run_id") != synthesis.get("run_id"):
        raise PublicProjectionError("Run IDs do not agree across private artifacts")
    if review.get("draft_hash") != draft_hash:
        raise PublicProjectionError("Review hash does not match the current private brief")
    if review.get("status") != "approved":
        raise PublicProjectionError("Public projection requires explicit approval")
    if not ReviewStore(run_dir).publishable(
        run_id=str(review["run_id"]),
        draft_hash=draft_hash,
        now=now,
    ):
        raise PublicProjectionError("Approval is expired, changed, or already published")
    draft = synthesis.get("draft")
    if not isinstance(draft, dict):
        raise PublicProjectionError("Synthesis draft is missing")
    return run, synthesis, review, draft, draft_hash


def build_public_projection(
    run_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    run, synthesis, review, draft, draft_hash = _approved_inputs(run_dir, now=now)
    sources_by_id = {
        str(source.get("resource_id")): source
        for source in run.get("sources", [])
        if isinstance(source, dict) and source.get("resource_id")
    }
    public_sources: dict[str, dict[str, Any]] = {}
    for claim in draft.get("claims", []):
        if not isinstance(claim, dict):
            raise PublicProjectionError("Synthesis contains an invalid claim")
        for source_id in claim.get("source_ids", []):
            source = sources_by_id.get(str(source_id))
            if not source:
                raise PublicProjectionError(f"Claim cites unknown source {source_id}")
            public_sources[str(source_id)] = {
                "source_id": str(source_id),
                "publisher": source.get("publisher"),
                "url": source.get("canonical_url") or source.get("requested_url"),
                "publication_date": source.get("publication_date"),
                "retrieved_at": source.get("retrieved_at"),
            }
    public_claims = [
        {
            "claim": str(claim["claim"]),
            "source_ids": [str(source_id) for source_id in claim["source_ids"]],
            "support": str(claim.get("support") or ""),
        }
        for claim in draft.get("claims", [])
    ]
    publication_time = now or _now()
    brief = {
        "schema_version": "1.0",
        "run_id": str(run["run_id"]),
        "title": str(draft.get("title") or "x402 Watch"),
        "executive_summary": str(draft.get("executive_summary") or ""),
        "x402_relevance": str(draft.get("x402_relevance") or ""),
        "wealth_lens": {
            wealth_type: str(draft["wealth_lens"][wealth_type])
            for wealth_type in ("time", "physical", "mental", "social", "financial")
        },
        "coverage": str(draft.get("coverage") or run.get("status") or ""),
        "claims": public_claims,
        "sources": list(public_sources.values()),
        "open_questions": [str(item) for item in draft.get("open_questions", [])],
        "editorial_warnings": [str(item) for item in draft.get("editorial_warnings", [])],
        "publication": {
            "draft_sha256": draft_hash,
            "published_at": _iso(publication_time),
        },
    }
    manifest = {
        "schema_version": "1.0",
        "generated_at": _iso(publication_time),
        "run_id": str(run["run_id"]),
        "draft_sha256": draft_hash,
        "coverage": str(run.get("status") or ""),
        "brief": "data/brief.json",
        "source_count": len(public_sources),
        "publication_status": "approved",
        "private_fields_excluded": True,
    }
    return {"manifest": manifest, "brief": brief, "review": review, "synthesis": synthesis}


def write_public_projection(
    run_dir: Path,
    output_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    projection = build_public_projection(run_dir, now=now)
    _atomic_write(
        output_dir / "data" / "brief.json",
        json.dumps(projection["brief"], indent=2, ensure_ascii=False) + "\n",
    )
    _atomic_write(
        output_dir / "data" / "manifest.json",
        json.dumps(projection["manifest"], indent=2, ensure_ascii=False) + "\n",
    )
    return projection["manifest"]


def mark_published(
    run_dir: Path,
    *,
    commit_sha: str,
    pages_url: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    run, _, review, _, draft_hash = _approved_inputs(run_dir, now=now)
    if not commit_sha.strip() or not pages_url.strip():
        raise PublicProjectionError("commit_sha and pages_url are required")
    timestamp = _iso(now or _now())
    review.update(
        {
            "status": "published",
            "publication_status": "published",
            "published_at": timestamp,
            "publication": {
                "run_id": str(run["run_id"]),
                "draft_sha256": draft_hash,
                "commit_sha": commit_sha,
                "pages_url": pages_url,
                "published_at": timestamp,
            },
        }
    )
    try:
        ReviewStore(run_dir).save(review)
    except ReviewStateError as exc:
        raise PublicProjectionError(str(exc)) from exc
    return review["publication"]


def _git(
    repo_dir: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublicProjectionError(f"Git operation failed: {exc}") from exc


def validate_public_repository(repo_dir: Path) -> None:
    if not (repo_dir / ".git").exists():
        raise PublicProjectionError(f"Public repository checkout is missing: {repo_dir}")
    remote = _git(repo_dir, "remote", "get-url", "origin").stdout.strip()
    if remote not in {PUBLIC_REPOSITORY_URL, f"git@github.com:{PUBLIC_REPOSITORY}.git"}:
        raise PublicProjectionError(
            f"Refusing publication to unexpected repository remote: {remote or '<missing>'}"
        )
    branch = _git(repo_dir, "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise PublicProjectionError(f"Public repository must be on main, not {branch or '<detached>'}")
    status = _git(repo_dir, "status", "--short").stdout.strip()
    if status:
        raise PublicProjectionError("Public repository checkout has unrelated local changes")


def publish_approved_run(
    run_dir: Path,
    repo_dir: Path,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Publish only an explicitly approved run to the fixed x402 Watch repository."""
    validate_public_repository(repo_dir)
    _git(repo_dir, "pull", "--ff-only", "origin", "main")
    projection = write_public_projection(run_dir, repo_dir, now=now)
    changed = [
        line
        for line in _git(repo_dir, "status", "--short").stdout.splitlines()
        if line.strip()
    ]
    changed_paths = [line[3:].strip() for line in changed if len(line) >= 4]
    if any(path not in PUBLIC_FILES for path in changed_paths):
        raise PublicProjectionError(
            f"Refusing publication with unexpected changed files: {changed_paths}"
        )
    if changed_paths:
        _git(
            repo_dir,
            "add",
            "--",
            *PUBLIC_FILES,
        )
        _git(
            repo_dir,
            "commit",
            "-m",
            f"Publish x402 Watch run {projection['run_id']}",
            "-m",
            "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
        )
        _git(repo_dir, "push", "origin", "main")
    commit_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    return {
        "repository": PUBLIC_REPOSITORY,
        "commit_sha": commit_sha,
        "pages_url": PUBLIC_PAGES_URL,
        "run_id": projection["run_id"],
        "draft_sha256": projection["draft_sha256"],
        "changed": bool(changed_paths),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish an approved x402 Watch projection")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path("/hve-library/public/x402-watch"),
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--commit-sha")
    parser.add_argument("--pages-url", default=PUBLIC_PAGES_URL)
    args = parser.parse_args(argv)
    if args.publish == args.finalize:
        parser.error("choose exactly one of --publish or --finalize")
    if args.publish:
        result = publish_approved_run(args.run_dir, args.repo_dir)
    else:
        if not args.commit_sha:
            parser.error("--commit-sha is required with --finalize")
        result = mark_published(
            args.run_dir,
            commit_sha=args.commit_sha,
            pages_url=args.pages_url,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
