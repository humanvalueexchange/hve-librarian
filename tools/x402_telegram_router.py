"""Deterministic Telegram control routing for the private x402 Watch workflow."""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.x402_public import PUBLIC_PAGES_URL, PublicProjectionError, publish_approved_run
from tools.x402_review import ReviewStateError, ReviewStore, format_ready_to_publish_response

DEFAULT_CHAT_ID = "1477642616"
DEFAULT_RUN_ROOT = Path("/hve-library/private/x402-watch")
READY_TEXT = "ready to publish"


def _active_run(root: Path) -> tuple[Path, dict[str, Any]] | None:
    pointer = root / "active-run.json"
    candidates: list[Path] = []
    if pointer.is_file():
        try:
            pointer_data = json.loads(pointer.read_text(encoding="utf-8"))
            candidates.append(root / "runs" / str(pointer_data["run_id"]))
        except (KeyError, OSError, TypeError, json.JSONDecodeError):
            pass
    candidates.extend(sorted((root / "runs").glob("*/review-state.json"), reverse=True))
    for candidate in candidates:
        run_dir = candidate.parent if candidate.name == "review-state.json" else candidate
        try:
            review = json.loads((run_dir / "review-state.json").read_text(encoding="utf-8"))
            run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if review.get("publication_status") == "not_published" and run.get("status") in {
            "complete",
            "partial",
        }:
            return run_dir, review
    return None


def _write_active_run(root: Path, run_dir: Path, review: dict[str, Any]) -> None:
    (root / "active-run.json").write_text(
        json.dumps(
            {
                "run_id": review["run_id"],
                "draft_hash": review["draft_hash"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "run_dir": str(run_dir),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _verify_pages(run_id: str, *, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    url = f"{PUBLIC_PAGES_URL.rstrip('/')}/data/manifest.json"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                manifest = json.loads(response.read().decode("utf-8"))
            if (
                manifest.get("publication_status") == "published"
                and manifest.get("run_id") == run_id
            ):
                return True
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(5)
    return False


def publish_approved_x402_run(run_dir: Path) -> str:
    result = publish_approved_run(run_dir, Path("/hve-library/public/x402-watch"))
    if not _verify_pages(str(result["run_id"])):
        return (
            f"Publication commit {result['commit_sha']} was pushed, but Pages verification "
            f"has not completed. The run remains pending finalization. Check {result['pages_url']}."
        )
    from tools.x402_public import mark_published

    mark_published(
        run_dir,
        commit_sha=str(result["commit_sha"]),
        pages_url=str(result["pages_url"]),
    )
    return (
        f"x402 Watch run {result['run_id']} is published. "
        f"Commit: {result['commit_sha']}. Pages: {result['pages_url']}"
    )


def route_control_message(
    *,
    text: str,
    platform: str,
    source: dict[str, Any],
    root: Path = DEFAULT_RUN_ROOT,
    publish_fn: Callable[[Path], str] = publish_approved_x402_run,
) -> str | None:
    """Consume only x402 control messages for the configured private Telegram chat."""
    if platform != "telegram" or str(source.get("chat_id")) != DEFAULT_CHAT_ID:
        return None
    normalized = " ".join(text.split()).strip()
    active = _active_run(root)
    if normalized.casefold() == READY_TEXT:
        if active is None:
            return "x402 Watch has no unpublished run awaiting review."
        run_dir, review = active
        try:
            if review["status"] == "awaiting_approval":
                return format_ready_to_publish_response(review)
            if review["status"] != "draft":
                return f"x402 Watch run {review['run_id']} is not in an editable draft state."
            updated = ReviewStore(run_dir).request_publication(run_id=str(review["run_id"]))
            _write_active_run(root, run_dir, updated)
            return format_ready_to_publish_response(updated)
        except (KeyError, ReviewStateError, OSError) as exc:
            return f"x402 Watch approval request failed safely: {exc}"

    if normalized.casefold() not in {"yes", "no"} or active is None:
        return None
    run_dir, review = active
    if review.get("status") != "awaiting_approval":
        return None
    try:
        result = ReviewStore(run_dir).handle_response(
            run_id=str(review["run_id"]),
            response=normalized,
        )
        if result["action"] == "rejected":
            return "x402 Watch publication cancelled. The private draft remains available for editing."
        if result["action"] == "clarify":
            return str(result["message"])
        if result["action"] == "approved":
            return publish_fn(run_dir)
        return "x402 Watch approval was not accepted; nothing was published."
    except (KeyError, PublicProjectionError, ReviewStateError, OSError, ValueError) as exc:
        return f"x402 Watch publication failed closed: {exc}"


def main() -> int:
    payload = json.load(sys.stdin)
    reply = route_control_message(
        text=str(payload.get("text", "")),
        platform=str(payload.get("platform", "")),
        source=payload.get("source") or {},
    )
    json.dump({"reply": reply}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
