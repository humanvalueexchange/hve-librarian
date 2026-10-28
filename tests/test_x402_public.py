from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from tools.x402_public import PublicProjectionError, build_public_projection, write_public_projection
from tools.x402_review import ReviewStore


UTC = dt.timezone.utc


class X402PublicProjectionTests(unittest.TestCase):
    def _run_dir(self, directory: str) -> Path:
        run_dir = Path(directory) / "run"
        run_dir.mkdir()
        brief = "# Private brief\n\nApproved copy."
        (run_dir / "brief.md").write_text(brief, encoding="utf-8")
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": "run-public-1",
                    "status": "complete",
                    "sources": [
                        {
                            "resource_id": "source-1",
                            "canonical_url": "https://example.com/article",
                            "requested_url": "https://example.com/article",
                            "publisher": "Example",
                            "publication_date": "2026-10-27",
                            "retrieved_at": "2026-10-27T20:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "synthesis.json").write_text(
            json.dumps(
                {
                    "run_id": "run-public-1",
                    "draft": {
                        "title": "x402 Watch",
                        "executive_summary": "A reviewed summary.",
                        "coverage": "complete",
                        "claims": [
                            {
                                "claim": "Example claim.",
                                "source_ids": ["source-1"],
                                "support": "Direct source support.",
                            }
                        ],
                        "open_questions": ["What changes next?"],
                        "editorial_warnings": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        now = dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
        store = ReviewStore(run_dir)
        store.start_review(run_id="run-public-1", draft_text=brief, now=now)
        store.request_publication(run_id="run-public-1", now=now)
        store.handle_response(run_id="run-public-1", response="YES", now=now)
        return run_dir

    def test_public_projection_contains_only_sanitized_fields(self) -> None:
        now = dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._run_dir(directory)
            projection = build_public_projection(run_dir, now=now)
            self.assertEqual(projection["manifest"]["run_id"], "run-public-1")
            self.assertEqual(projection["brief"]["sources"][0]["url"], "https://example.com/article")
            self.assertNotIn("evidence_excerpt", projection["brief"]["sources"][0])
            self.assertNotIn("draft_text", projection["brief"])

    def test_projection_requires_explicit_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._run_dir(directory)
            state = ReviewStore(run_dir).load()
            state["status"] = "draft"
            ReviewStore(run_dir).save(state)
            with self.assertRaises(PublicProjectionError):
                build_public_projection(run_dir)

    def test_projection_writes_manifest_and_brief(self) -> None:
        now = dt.datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            run_dir = self._run_dir(directory)
            output = Path(directory) / "public"
            manifest = write_public_projection(run_dir, output, now=now)
            self.assertTrue((output / "data" / "manifest.json").is_file())
            self.assertTrue((output / "data" / "brief.json").is_file())
            self.assertEqual(manifest["publication_status"], "approved")


if __name__ == "__main__":
    unittest.main()
