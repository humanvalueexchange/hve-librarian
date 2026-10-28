from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tools.x402_review import (
    ReviewStore,
    format_review_messages,
    format_ready_to_publish_response,
    split_telegram_messages,
)


UTC = dt.timezone.utc


class X402ReviewTests(unittest.TestCase):
    def test_splits_full_brief_with_ordered_message_headers(self) -> None:
        messages = format_review_messages(
            run_id="20261028T110000Z",
            coverage_status="complete",
            brief_text=("Paragraph one.\n\n" + ("Long paragraph. " * 120)),
            limit=160,
        )
        self.assertGreater(len(messages), 1)
        for index, message in enumerate(messages, start=1):
            self.assertLessEqual(len(message), 160 + len("[x402 Watch 99/99]\n"))
            self.assertTrue(message.startswith(f"[x402 Watch {index}/"))

    def test_review_requires_ready_then_explicit_yes(self) -> None:
        now = dt.datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            store = ReviewStore(Path(directory))
            state = store.start_review(run_id="run-1", draft_text="# Draft", now=now)
            self.assertEqual(state["status"], "draft")

            request = store.handle_operator_message(
                run_id="run-1",
                message="ready to publish",
                now=now,
            )
            self.assertEqual(request["action"], "approval_requested")
            pending = store.load()
            self.assertEqual(pending["status"], "awaiting_approval")
            self.assertIn("Do you APPROVE x402 WATCH RUN run-1", format_ready_to_publish_response(pending))

            unclear = store.handle_response(run_id="run-1", response="looks good", now=now)
            self.assertEqual(unclear["action"], "clarify")
            self.assertEqual(store.load()["status"], "awaiting_approval")

            approved = store.handle_response(run_id="run-1", response="YES", now=now)
            self.assertEqual(approved["action"], "approved")
            self.assertTrue(
                store.publishable(
                    run_id="run-1",
                    draft_hash=state["draft_hash"],
                    now=now,
                )
            )

    def test_no_cancels_and_revision_invalidates_previous_approval(self) -> None:
        now = dt.datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            store = ReviewStore(Path(directory))
            initial = store.start_review(run_id="run-2", draft_text="Draft", now=now)
            store.request_publication(run_id="run-2", now=now)
            rejected = store.handle_response(run_id="run-2", response="NO", now=now)
            self.assertEqual(rejected["action"], "rejected")
            self.assertFalse(store.publishable(run_id="run-2", draft_hash=initial["draft_hash"], now=now))

            revised = store.revise(run_id="run-2", draft_text="Revised draft", now=now)
            self.assertEqual(revised["revision"], 2)
            self.assertIsNone(revised["approval"])
            self.assertEqual(revised["status"], "draft")

    def test_expired_approval_cannot_publish(self) -> None:
        requested = dt.datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
        expired = requested + dt.timedelta(days=7, seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            store = ReviewStore(Path(directory))
            state = store.start_review(run_id="run-3", draft_text="Draft", now=requested)
            store.request_publication(run_id="run-3", now=requested)
            self.assertFalse(
                store.publishable(
                    run_id="run-3",
                    draft_hash=state["draft_hash"],
                    now=expired,
                )
            )
            self.assertEqual(store.load()["status"], "expired")

    def test_empty_split_is_empty(self) -> None:
        self.assertEqual(split_telegram_messages(""), [])


if __name__ == "__main__":
    unittest.main()
