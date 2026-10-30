"""Run one private x402 Watch batch for the Hermes cron scheduler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path("/home/hans/hve-librarian")
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.x402_review import format_review_messages
from tools.x402_watch import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_RUN_ROOT,
    SynthesisError,
    run_batch,
    synthesize_run,
)


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


def _write_automation_state(
    run_dir: Path,
    *,
    collection_status: str,
    synthesis_status: str,
    error: str | None = None,
) -> None:
    state = {
        "collection_status": collection_status,
        "synthesis_status": synthesis_status,
        "delivery": "delegated_to_hermes_cron",
    }
    if error:
        state["error"] = error
    (run_dir / "automation-state.json").write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


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
            error=str(exc),
        )
        raise
    _write_automation_state(
        run_dir,
        collection_status=str(result["status"]),
        synthesis_status="complete",
    )
    brief = (run_dir / "brief.md").read_text(encoding="utf-8")
    return format_review_messages(
        run_id=str(result["run_id"]),
        brief_text=brief,
        coverage_status=str(result["status"]),
    )


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
