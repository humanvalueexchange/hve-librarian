"""Run one private x402 Watch batch for the Hermes cron scheduler."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.x402_public import (
    PUBLIC_PAGES_URL,
    PublicProjectionError,
    mark_published,
    publish_run,
)
from tools.x402_watch import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_RUN_ROOT,
    SynthesisError,
    run_batch,
    synthesize_run,
)

PUBLIC_REPOSITORY_DIR = Path("/hve-library/public/x402-watch")


def _has_complete_baseline(root: Path, current_run_id: str) -> bool:
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return False
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == current_run_id:
            continue
        try:
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run.get("status") == "complete":
            return True
    return False


def cleanup_expired_runs(
    root: Path,
    *,
    now: datetime | None = None,
    retention_days: int = 90,
    protected_run_id: str | None = None,
) -> list[str]:
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    runs_root = (root / "runs").resolve()
    if not runs_root.is_dir():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    removed: list[str] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == protected_run_id:
            continue
        resolved = run_dir.resolve()
        if resolved.parent != runs_root:
            continue
        try:
            run = json.loads((resolved / "run.json").read_text(encoding="utf-8"))
            started_at = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if started_at < cutoff:
            shutil.rmtree(resolved)
            removed.append(run_dir.name)
    return removed


def _write_automation_state(
    run_dir: Path,
    *,
    collection_status: str,
    synthesis_status: str,
    publication_status: str = "not_started",
    pages_url: str | None = None,
    error: str | None = None,
) -> None:
    state = {
        "collection_status": collection_status,
        "synthesis_status": synthesis_status,
        "publication_status": publication_status,
    }
    if pages_url:
        state["pages_url"] = pages_url
    if error:
        state["error"] = error
    (run_dir / "automation-state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def _verify_pages(run_id: str, *, timeout: float = 60.0) -> bool:
    import json as _json
    import time
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"{PUBLIC_PAGES_URL.rstrip('/')}/data/manifest.json"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                manifest = _json.loads(response.read().decode("utf-8"))
            if (
                manifest.get("publication_status") == "approved"
                and manifest.get("run_id") == run_id
            ):
                return True
        except (OSError, ValueError, _json.JSONDecodeError):
            pass
        time.sleep(5)
    return False


def run_scheduled_batch(
    *,
    root: Path = DEFAULT_RUN_ROOT,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_MODEL_BASE_URL,
    context_length: int = 131_072,
) -> list[str]:
    result = run_batch(root=root)
    run_dir = root / "runs" / str(result["run_id"])
    _write_automation_state(
        run_dir,
        collection_status=str(result["status"]),
        synthesis_status="in_progress",
    )
    if result["status"] in {"failed", "no_usable_sources"}:
        _write_automation_state(
            run_dir,
            collection_status=str(result["status"]),
            synthesis_status="not_run",
            publication_status="not_started",
            error="Collection did not produce usable evidence.",
        )
        raise SynthesisError(
            f"x402 Watch run {result['run_id']} ended in {result['status']}"
        )
    try:
        synthesis = synthesize_run(
            root=root,
            run_id=str(result["run_id"]),
            has_complete_baseline=_has_complete_baseline(root, str(result["run_id"])),
            base_url=base_url,
            model=model,
            context_length=context_length,
        )
    except (OSError, SynthesisError, ValueError) as exc:
        _write_automation_state(
            run_dir,
            collection_status=str(result["status"]),
            synthesis_status="failed",
            publication_status="not_started",
            error=str(exc),
        )
        raise
    _write_automation_state(
        run_dir,
        collection_status=str(result["status"]),
        synthesis_status="complete",
    )
    review_state = json.loads(
        (run_dir / "review-state.json").read_text(encoding="utf-8")
    )
    (root / "active-run.json").write_text(
        json.dumps(
            {
                "run_id": review_state["run_id"],
                "draft_hash": review_state["draft_hash"],
                "updated_at": review_state["updated_at"],
                "run_dir": str(run_dir),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cleanup_expired_runs(root, protected_run_id=str(result["run_id"]))
    try:
        publication = publish_run(
            run_dir,
            PUBLIC_REPOSITORY_DIR,
            require_approval=False,
        )
        _write_automation_state(
            run_dir,
            collection_status=str(result["status"]),
            synthesis_status="complete",
            publication_status="committed",
            pages_url=str(publication["pages_url"]),
        )
        if not _verify_pages(str(publication["run_id"])):
            raise PublicProjectionError(
                "GitHub Pages did not expose the new publication within the verification window."
            )
        mark_published(
            run_dir,
            commit_sha=str(publication["commit_sha"]),
            pages_url=str(publication["pages_url"]),
            require_approval=False,
        )
    except (OSError, PublicProjectionError, ValueError) as exc:
        _write_automation_state(
            run_dir,
            collection_status=str(result["status"]),
            synthesis_status="complete",
            publication_status="failed",
            error=str(exc),
        )
        raise
    _write_automation_state(
        run_dir,
        collection_status=str(result["status"]),
        synthesis_status="complete",
        publication_status="published",
        pages_url=str(publication["pages_url"]),
    )
    return [
        f"x402 Watch run {publication['run_id']} is published.",
        f"GitHub Pages: {publication['pages_url']}",
    ]


def main() -> int:
    try:
        messages = run_scheduled_batch()
    except (OSError, SynthesisError, ValueError) as exc:
        print(f"x402 Watch scheduled run failed: {exc}", file=sys.stderr)
        return 1
    print("\n\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
