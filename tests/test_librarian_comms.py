from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import librarian_comms


class LibrarianCommsTests(unittest.TestCase):
    def test_writes_new_communication_with_required_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.object(librarian_comms, "COMMUNICATIONS_ROOT", root):
                result = librarian_comms.write_communication(
                    "2026-08-30-hve-test-note-v1.0.md",
                    "# Test\n",
                    approved_by="Hans Westphal",
                )
            self.assertEqual(result["status"], "written")
            self.assertEqual((root / "2026-08-30-hve-test-note-v1.0.md").read_text(), "# Test\n")

    def test_rejects_existing_file_and_unapproved_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.mkdir(exist_ok=True)
            path = root / "2026-08-30-hve-test-note-v1.0.md"
            path.write_text("original\n")
            with mock.patch.object(librarian_comms, "COMMUNICATIONS_ROOT", root):
                with self.assertRaisesRegex(ValueError, "Explicit approval"):
                    librarian_comms.write_communication(
                        path.name,
                        "replacement",
                        approved_by="Someone Else",
                    )
                with self.assertRaisesRegex(ValueError, "already exists"):
                    librarian_comms.write_communication(
                        path.name,
                        "replacement",
                        approved_by="Hans Westphal",
                    )
            self.assertEqual(path.read_text(), "original\n")

    def test_rejects_invalid_filename(self) -> None:
        with self.assertRaisesRegex(ValueError, "Filename must match"):
            librarian_comms.write_communication(
                "notes.md",
                "content",
                approved_by="Hans Westphal",
            )

    def test_comments_on_issue_with_fixed_repository(self) -> None:
        with mock.patch.object(librarian_comms, "_run", return_value="https://github.com/comment/1") as run:
            result = librarian_comms.comment_on_issue(
                12,
                "Status update",
                approved_by="Hans Westphal",
            )
        self.assertEqual(result["status"], "commented")
        self.assertIn("HansHWestphal/hve-knowledge-and-operations", run.call_args.args[0])

    def test_comments_on_commit_with_fixed_repository(self) -> None:
        with mock.patch.object(librarian_comms, "_run", return_value="https://github.com/comment/2") as run:
            result = librarian_comms.comment_on_commit(
                "57ee02c18f8abd86d3f947c717916c474db9bfa9",
                "Brief pointer",
                approved_by="Hans Westphal",
            )
        self.assertEqual(result["status"], "commented")
        self.assertEqual(result["comment_url"], "https://github.com/comment/2")
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "api", "--method", "POST"])
        self.assertIn(
            "repos/HansHWestphal/hve-knowledge-and-operations/commits/"
            "57ee02c18f8abd86d3f947c717916c474db9bfa9/comments",
            command,
        )

    def test_rejects_invalid_commit_sha(self) -> None:
        with self.assertRaisesRegex(ValueError, "hexadecimal SHA"):
            librarian_comms.comment_on_commit(
                "not-a-sha",
                "Brief pointer",
                approved_by="Hans Westphal",
            )

    def test_issue_lifecycle_requires_approval_and_supports_reopen(self) -> None:
        with mock.patch.object(librarian_comms, "_run", return_value="") as run:
            result = librarian_comms.close_issue(
                12,
                reopen=True,
                approved_by="Hans Westphal",
            )
        self.assertEqual(result["status"], "reopened")
        self.assertEqual(run.call_args.args[0][:3], ["gh", "issue", "reopen"])

    def test_updates_issue_fields(self) -> None:
        with mock.patch.object(librarian_comms, "_run", return_value="") as run:
            result = librarian_comms.update_issue(
                12,
                title="Updated title",
                approved_by="Hans Westphal",
            )
        self.assertEqual(result["status"], "updated")
        self.assertIn("--title", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
