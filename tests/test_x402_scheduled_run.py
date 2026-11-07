import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tools import x402_scheduled_run


class ScheduledRunTests(unittest.TestCase):
    def test_cleanup_expired_runs_removes_only_runs_older_than_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_root = root / "runs"
            runs_root.mkdir()
            old_run = runs_root / "old-run"
            new_run = runs_root / "new-run"
            protected_run = runs_root / "protected-run"
            for run_dir, started_at in (
                (old_run, "2026-01-01T07:00:00+00:00"),
                (new_run, "2026-10-01T07:00:00+00:00"),
                (protected_run, "2026-01-01T07:00:00+00:00"),
            ):
                run_dir.mkdir()
                (run_dir / "run.json").write_text(
                    json.dumps({"started_at": started_at}) + "\n",
                    encoding="utf-8",
                )

            removed = x402_scheduled_run.cleanup_expired_runs(
                root,
                now=datetime(2026, 10, 29, tzinfo=timezone.utc),
                retention_days=90,
                protected_run_id="protected-run",
            )

            self.assertEqual(removed, ["old-run"])
            self.assertFalse(old_run.exists())
            self.assertTrue(new_run.exists())
            self.assertTrue(protected_run.exists())

    def test_scheduled_run_synthesizes_and_formats_current_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_id = "20261029T070000.000000Z"
            run_dir = root / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                json.dumps({"run_id": run_id, "status": "partial"}) + "\n",
                encoding="utf-8",
            )
            (run_dir / "brief.md").write_text("# Private brief\n", encoding="utf-8")
            (run_dir / "review-state.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "draft_hash": "hash-1",
                        "updated_at": "2026-10-29T07:00:00+00:00",
                        "status": "draft",
                        "publication_status": "not_published",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            batch = {"run_id": run_id, "status": "partial"}
            synthesis = {"run_id": run_id, "review_status": "draft"}
            with (
                patch.object(x402_scheduled_run, "run_batch", return_value=batch),
                patch.object(x402_scheduled_run, "synthesize_run", return_value=synthesis),
                patch.object(
                    x402_scheduled_run,
                    "publish_run",
                    return_value={
                        "run_id": run_id,
                        "commit_sha": "commit-1",
                        "pages_url": "https://hanshwestphal.github.io/x402-watch/",
                    },
                ) as publisher,
                patch.object(x402_scheduled_run, "_verify_pages", return_value=True),
                patch.object(x402_scheduled_run, "mark_published") as finalizer,
            ):
                messages = x402_scheduled_run.run_scheduled_batch(root=root)

            self.assertEqual(
                messages,
                [
                    f"x402 Watch run {run_id} is published.",
                    "GitHub Pages: https://hanshwestphal.github.io/x402-watch/",
                ],
            )
            publisher.assert_called_once_with(
                run_dir,
                x402_scheduled_run.PUBLIC_REPOSITORY_DIR,
                require_approval=False,
            )
            finalizer.assert_called_once_with(
                run_dir,
                commit_sha="commit-1",
                pages_url="https://hanshwestphal.github.io/x402-watch/",
                require_approval=False,
            )
            state = json.loads(
                (run_dir / "automation-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["collection_status"], "partial")
            self.assertEqual(state["synthesis_status"], "complete")
            self.assertEqual(state["publication_status"], "published")
            self.assertEqual(
                state["pages_url"],
                "https://hanshwestphal.github.io/x402-watch/",
            )


if __name__ == "__main__":
    unittest.main()
