import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.x402_telegram_router import route_control_message


class TelegramRouterTests(unittest.TestCase):
    def _run(self, root: Path, status: str = "draft") -> Path:
        run_dir = root / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"run_id": "run-1", "status": "partial"}) + "\n",
            encoding="utf-8",
        )
        (run_dir / "review-state.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "draft_hash": "hash-1",
                    "status": status,
                    "publication_status": "not_published",
                    "approval": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run_dir

    def test_ready_to_publish_is_consumed_and_binds_current_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run(root)
            reply = route_control_message(
                text="Ready   to publish",
                platform="telegram",
                source={"chat_id": "1477642616"},
                root=root,
            )
            self.assertEqual(
                reply,
                "Do you APPROVE x402 WATCH RUN run-1 FOR PUBLICATION? Reply YES or NO.",
            )
            state = json.loads(
                (root / "runs" / "run-1" / "review-state.json").read_text()
            )
            self.assertEqual(state["status"], "awaiting_approval")

    def test_yes_publishes_only_after_pending_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run(root, status="awaiting_approval")
            with patch(
                "tools.x402_telegram_router.publish_approved_x402_run",
                return_value="published",
            ) as publisher:
                reply = route_control_message(
                    text="YES",
                    platform="telegram",
                    source={"chat_id": "1477642616"},
                    root=root,
                    publish_fn=publisher,
                )
            self.assertEqual(reply, "published")
            publisher.assert_called_once_with(root / "runs" / "run-1")

    def test_unrelated_messages_and_wrong_chat_are_not_consumed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._run(root, status="awaiting_approval")
            self.assertIsNone(
                route_control_message(
                    text="yes, that sounds good",
                    platform="telegram",
                    source={"chat_id": "1477642616"},
                    root=root,
                )
            )
            self.assertIsNone(
                route_control_message(
                    text="Ready to publish",
                    platform="telegram",
                    source={"chat_id": "other"},
                    root=root,
                )
            )


if __name__ == "__main__":
    unittest.main()
