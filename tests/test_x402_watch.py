from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.link_collector import LinkCollectorError
from tools.x402_watch import (
    SynthesisError,
    discover_links,
    run_batch,
    synthesize_evidence,
    validate_claims,
)


class X402WatchTests(unittest.TestCase):
    def test_discovers_relevant_links_and_ignores_unrelated_links(self) -> None:
        body = b"""
        <html><body>
        <a href="/x402-story">x402 adoption</a>
        <a href="https://example.com/weather">weather</a>
        <a href="/stablecoin-volume">Stablecoin volume</a>
        </body></html>
        """
        self.assertEqual(
            discover_links("https://www.donvitocodes.com/ai-news", body),
            [
                "https://www.donvitocodes.com/x402-story",
                "https://www.donvitocodes.com/stablecoin-volume",
            ],
        )

    def test_run_preserves_partial_coverage_and_private_output(self) -> None:
        responses = {
            "https://www.donvitocodes.com/ai-news": {
                "final_url": "https://www.donvitocodes.com/ai-news",
                "http_status": 200,
                "content_type": "text/html",
                "charset": "utf-8",
                "html_bytes": b'<html><title>AI News</title><body><a href="/x402">x402</a></body></html>',
            },
            "https://www.donvitocodes.com/x402": LinkCollectorError("HTTP 403"),
        }

        def fetcher(url: str) -> dict:
            value = responses[url]
            if isinstance(value, Exception):
                raise value
            return value

        with tempfile.TemporaryDirectory() as directory:
            result = run_batch(
                root=Path(directory),
                fetcher=fetcher,
                now="2026-10-27T17:00:00+00:00",
            )
            self.assertEqual(result["status"], "partial")
            self.assertFalse(result["canonical_knowledge_indexed"])
            run_file = Path(directory) / "runs" / "20261027T170000Z" / "run.json"
            self.assertTrue(run_file.is_file())
            stored = json.loads(run_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["publication_status"], "not_published")

    def test_claim_validation_requires_known_sources_and_complete_baseline(self) -> None:
        invalid = validate_claims(
            [
                {"claim": "No material change was observed.", "source_ids": ["unknown"]},
                {"claim": "This is an investment recommendation.", "source_ids": ["s1"]},
            ],
            {"s1"},
            run_status="partial",
            has_complete_baseline=False,
        )
        self.assertFalse(invalid["valid"])
        self.assertEqual(len(invalid["errors"]), 3)

        valid = validate_claims(
            [{"claim": "Adoption signal increased.", "source_ids": ["s1"]}],
            {"s1"},
            run_status="complete",
            has_complete_baseline=True,
        )
        self.assertTrue(valid["valid"])

    def test_synthesis_requires_local_context_and_validates_claims(self) -> None:
        evidence = {
            "run_id": "run-1",
            "status": "complete",
            "sources": [
                {
                    "source_id": "s1",
                    "status": "fetched",
                    "canonical_url": "https://example.com/source",
                    "evidence_excerpt": "A bounded source excerpt.",
                }
            ],
        }
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "Daily watch",
                                "executive_summary": "One supported signal.",
                                "claims": [
                                    {
                                        "claim": "Adoption signal increased.",
                                        "source_ids": ["s1"],
                                        "support": "Directly stated in the source.",
                                    }
                                ],
                                "coverage": "Complete for the configured sources.",
                                "open_questions": [],
                                "editorial_warnings": [],
                            }
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response).encode("utf-8")

        with mock.patch(
            "tools.x402_watch.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            draft = synthesize_evidence(evidence)
        self.assertEqual(draft["validation"], {"valid": True, "errors": []})
        self.assertEqual(draft["model"], "qwen3.8-hermes:27b-128k")

        with self.assertRaises(SynthesisError):
            synthesize_evidence(evidence, context_length=32_768)

    def test_synthesis_rejects_unknown_source_claim(self) -> None:
        evidence = {
            "status": "complete",
            "sources": [{"source_id": "s1", "status": "fetched"}],
        }
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "Daily watch",
                                "executive_summary": "Unsupported.",
                                "claims": [{"claim": "Unsupported.", "source_ids": ["s2"]}],
                                "coverage": "Complete.",
                            }
                        )
                    }
                }
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response).encode("utf-8")

        with mock.patch(
            "tools.x402_watch.urllib.request.urlopen",
            return_value=FakeResponse(),
        ):
            with self.assertRaises(SynthesisError):
                synthesize_evidence(evidence)


if __name__ == "__main__":
    unittest.main()
