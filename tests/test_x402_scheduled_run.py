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
            batch = {"run_id": run_id, "status": "partial"}
            synthesis = {"run_id": run_id, "review_status": "draft"}
            with (
                patch.object(x402_scheduled_run, "run_batch", return_value=batch),
                patch.object(x402_scheduled_run, "synthesize_run", return_value=synthesis),
                patch.object(
                    x402_scheduled_run,
                    "format_review_messages",
                    return_value=["[x402 Watch 1/1]\n# Private brief"],
                ) as format_messages,
            ):
                messages = x402_scheduled_run.run_scheduled_batch(root=root)

            self.assertEqual(messages, ["[x402 Watch 1/1]\n# Private brief"])
            format_messages.assert_called_once_with(
                run_id=run_id,
                brief_text="# Private brief\n",
                coverage_status="partial",
            )
            state = json.loads(
                (run_dir / "automation-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["collection_status"], "partial")
            self.assertEqual(state["synthesis_status"], "complete")
            self.assertEqual(state["delivery"], "delegated_to_hermes_cron")


if __name__ == "__main__":
    unittest.main()
