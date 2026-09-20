from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import link_collector


class LinkCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = REPO_ROOT / ".test-link-collector"
        shutil.rmtree(self.root, ignore_errors=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_canonicalizes_and_removes_tracking_parameters(self) -> None:
        canonical = link_collector.canonicalize_url(
            "HTTPS://Example.COM:443/article?utm_source=tg&b=2&a=1#fragment"
        )
        self.assertEqual(canonical, "https://example.com/article?a=1&b=2")

    def test_rejects_private_network_targets(self) -> None:
        private_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ]
        with mock.patch("socket.getaddrinfo", return_value=private_resolution):
            result = link_collector.archive_link("https://internal.example", root=self.root)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["archived"])
        self.assertFalse(result["indexed"])

    def test_rejects_private_ipv6_resolution(self) -> None:
        private_resolution = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
        ]
        with mock.patch("socket.getaddrinfo", return_value=private_resolution):
            with self.assertRaises(link_collector.LinkCollectorError):
                link_collector._validate_public_target("https://internal.example")

    def test_redirect_destination_is_validated_before_following(self) -> None:
        public_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        private_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))
        ]
        request = link_collector.urllib.request.Request("https://example.com/start")
        with mock.patch(
            "socket.getaddrinfo",
            side_effect=[public_resolution, private_resolution],
        ):
            handler = link_collector._SafeRedirectHandler()
            first = handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://redirect.example/",
            )
            self.assertIsNotNone(first)
            with self.assertRaises(link_collector.LinkCollectorError):
                handler.redirect_request(
                    first,
                    None,
                    302,
                    "Found",
                    {},
                    "https://private.example/",
                )

    def test_pinned_connection_connects_to_validated_address(self) -> None:
        fake_socket = mock.Mock()
        public_address = link_collector.ipaddress.ip_address("93.184.216.34")
        with (
            mock.patch("tools.link_collector._public_addresses", return_value=[public_address]) as resolve,
            mock.patch("socket.create_connection", return_value=fake_socket) as connect,
        ):
            connection = link_collector._PinnedHTTPConnection("example.com", 443, timeout=5)
            connection.connect()

        resolve.assert_called_once_with("example.com", 443)
        connect.assert_called_once_with(("93.184.216.34", 443), 5)
        self.assertIs(connection.sock, fake_socket)

    def test_archives_with_provenance_without_false_index_claim(self) -> None:
        fetched = {
            "requested_url": "https://example.com/article",
            "final_url": "https://example.com/article",
            "http_status": 200,
            "content_type": "text/html",
            "charset": "utf-8",
            "etag": '"abc"',
            "last_modified": None,
            "html_bytes": (
                b"<html lang='en'><head><title>Example Article</title>"
                b"<meta name='author' content='Hans'></head>"
                b"<body><article><h1>Example Article</h1><p>Durable knowledge.</p>"
                b"<script>ignore me</script></article></body></html>"
            ),
        }
        public_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        with mock.patch("socket.getaddrinfo", return_value=public_resolution):
            result = link_collector.archive_link(
                "https://example.com/article?utm_source=telegram",
                "Hans sent this from Telegram.",
                root=self.root,
                fetcher=lambda _url: fetched,
                indexer=lambda _root, _chunks, _manifest: {
                    "indexed": False,
                    "status": "unavailable",
                    "error": "test index unavailable",
                },
            )

        self.assertEqual(result["status"], "archived_not_indexed")
        self.assertTrue(result["archived"])
        self.assertFalse(result["indexed"])
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["canonical_url"], "https://example.com/article")
        self.assertEqual(manifest["capture_source"], "hve_librarian")
        self.assertEqual(manifest["index_status"], "unavailable")
        self.assertTrue(Path(result["raw_html_path"]).is_file())
        self.assertTrue(Path(result["raw_metadata_path"]).is_file())
        text = Path(result["extracted_text_path"]).read_text(encoding="utf-8")
        self.assertIn("Hans sent this from Telegram.", text)
        self.assertIn("Durable knowledge.", text)
        self.assertNotIn("ignore me", text)

    def test_duplicate_adds_capture_context_without_refetching(self) -> None:
        fetched = {
            "requested_url": "https://example.com/",
            "final_url": "https://example.com/",
            "http_status": 200,
            "content_type": "text/html",
            "charset": "utf-8",
            "etag": None,
            "last_modified": None,
            "html_bytes": b"<html><title>Example</title><body>Reusable text.</body></html>",
        }
        public_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        indexer = lambda _root, _chunks, _manifest: {
            "indexed": True,
            "status": "verified",
            "table": "library_chunks",
            "indexed_at": "2026-08-13T00:00:00+00:00",
        }
        with mock.patch("socket.getaddrinfo", return_value=public_resolution):
            first = link_collector.archive_link(
                "https://example.com",
                "first",
                root=self.root,
                fetcher=lambda _url: fetched,
                indexer=indexer,
            )
            second = link_collector.archive_link(
                "https://example.com/?utm_campaign=again",
                "second",
                root=self.root,
                fetcher=mock.Mock(side_effect=AssertionError("duplicate refetched")),
                indexer=indexer,
            )

        self.assertTrue(first["indexed"])
        self.assertEqual(second["status"], "duplicate_indexed")
        self.assertTrue(second["duplicate"])
        manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual([item["capture_context"] for item in manifest["captures"]], ["first", "second"])

    def test_empty_html_is_archived_but_not_indexed(self) -> None:
        fetched = {
            "requested_url": "https://example.com/empty",
            "final_url": "https://example.com/empty",
            "http_status": 200,
            "content_type": "text/html",
            "charset": "utf-8",
            "etag": None,
            "last_modified": None,
            "html_bytes": b"<html><head><title>Empty</title></head><body><script>only script</script></body></html>",
        }
        public_resolution = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ]
        indexer = mock.Mock(side_effect=AssertionError("empty HTML was indexed"))
        with mock.patch("socket.getaddrinfo", return_value=public_resolution):
            result = link_collector.archive_link(
                "https://example.com/empty",
                root=self.root,
                fetcher=lambda _url: fetched,
                indexer=indexer,
            )

        self.assertEqual(result["status"], "archived_not_indexed")
        self.assertFalse(result["extracted"])
        self.assertFalse(result["indexed"])
        indexer.assert_not_called()

    def test_index_link_chunks_parses_compact_cli_success(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout='{"indexed":true,"status":"indexed","table":"library_chunks"}\n',
            stderr="",
        )
        with (
            mock.patch("pathlib.Path.is_file", return_value=True),
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch("tools.link_collector.subprocess.run", return_value=completed) as run,
        ):
            result = link_collector.index_link_chunks(
                self.root,
                self.root / "chunks.jsonl",
                self.root / "manifest.json",
            )

        self.assertTrue(result["indexed"])
        self.assertIn("--json", run.call_args.args[0])

    def test_index_link_chunks_preserves_cli_failure(self) -> None:
        completed = mock.Mock(
            returncode=1,
            stdout='{"indexed":false,"status":"failed","error":"library busy"}\n',
            stderr="",
        )
        with (
            mock.patch("pathlib.Path.is_file", return_value=True),
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch("tools.link_collector.subprocess.run", return_value=completed),
        ):
            result = link_collector.index_link_chunks(
                self.root,
                self.root / "chunks.jsonl",
                self.root / "manifest.json",
            )

        self.assertEqual(result, {"indexed": False, "status": "failed", "error": "library busy"})

    def test_detects_youtube_url_forms(self) -> None:
        video_id = "dQw4w9WgXcQ"
        for url in (
            f"https://www.youtube.com/watch?v={video_id}&si=tracking",
            f"https://youtu.be/{video_id}?si=tracking",
            f"https://youtube.com/shorts/{video_id}",
            f"https://www.youtube.com/embed/{video_id}",
            f"https://www.youtube.com/live/{video_id}",
        ):
            self.assertTrue(link_collector.is_youtube_url(url))
            self.assertEqual(
                link_collector.canonicalize_youtube_url(url),
                f"https://www.youtube.com/watch?v={video_id}",
            )

    def test_archives_youtube_transcript_with_provenance_and_deduplication(self) -> None:
        transcript = {
            "video_id": "dQw4w9WgXcQ",
            "segment_count": 2,
            "duration": "0:12",
            "language": "en",
            "full_text": "First transcript segment. Second transcript segment.",
            "timestamped_text": "0:00 First transcript segment.\n0:06 Second transcript segment.",
        }
        indexer = lambda _root, _chunks, _manifest: {
            "indexed": True,
            "status": "indexed",
            "table": "library_chunks",
            "indexed_at": "2026-08-29T00:00:00+00:00",
        }
        first = link_collector.archive_link(
            "https://youtu.be/dQw4w9WgXcQ?si=abc",
            "Telegram source",
            root=self.root,
            transcript_fetcher=lambda _url: transcript,
            indexer=indexer,
        )
        second = link_collector.archive_link(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "Duplicate source",
            root=self.root,
            transcript_fetcher=mock.Mock(side_effect=AssertionError("duplicate refetched")),
            indexer=indexer,
        )

        self.assertEqual(first["status"], "completed")
        self.assertTrue(first["archived"])
        self.assertTrue(first["transcript_archived"])
        self.assertTrue(first["indexed"])
        self.assertEqual(second["status"], "duplicate_completed")
        self.assertTrue(second["duplicate"])
        manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_type"], "youtube_video")
        self.assertEqual(manifest["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(manifest["transcript_status"], "completed")
        self.assertEqual(manifest["capture_count"], 2)
        self.assertTrue(Path(first["source_metadata_path"]).is_file())
        self.assertTrue(Path(first["transcript_path"]).is_file())
        self.assertTrue(Path(first["timestamped_transcript_path"]).is_file())
        chunks = Path(first["chunk_path"]).read_text(encoding="utf-8")
        self.assertIn('"chapter": "YouTube transcript"', chunks)

    def test_youtube_transcript_failure_is_explicit_and_retryable(self) -> None:
        result = link_collector.archive_youtube(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            root=self.root,
            transcript_fetcher=mock.Mock(
                side_effect=link_collector.LinkCollectorError("No transcript found")
            ),
        )

        self.assertEqual(result["status"], "no_transcript")
        self.assertTrue(result["archived"])
        self.assertFalse(result["transcript_archived"])
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["transcript_status"], "no_transcript")
        self.assertEqual(manifest["status"], "no_transcript")

    def test_pdf_capture_history_is_append_only(self) -> None:
        from tools import pdf_collector

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_root = Path(tmpdir) / "manifests"
            manifest_root.mkdir()
            manifest_path = manifest_root / "document.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "captured_at": "2026-08-19T00:00:00+00:00",
                        "captures": [
                            {
                                "captured_at": "2026-08-19T00:00:00+00:00",
                                "capture_context": "first",
                                "capture_source": "hve_librarian",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state_root = Path(tmpdir) / "library"
            (state_root / "state" / "manifests").mkdir(parents=True)
            (state_root / "state" / "manifests" / "document.json").write_text(
                manifest_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = pdf_collector._record_capture("document", "second", root=state_root)

            self.assertEqual(result["status"], "processed")
            manifest = json.loads(
                (state_root / "state" / "manifests" / "document.json").read_text(encoding="utf-8")
            )
            self.assertEqual([item["capture_context"] for item in manifest["captures"]], ["first", "second"])

    def test_pdf_intake_uses_private_telegram_queue(self) -> None:
        from tools import pdf_collector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = root / "cache"
            cache.mkdir()
            source = cache / "course.pdf"
            source.write_bytes(b"%PDF-test")
            manifest_path = root / "state" / "manifests" / "course.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps({"document_id": "course", "status": "indexed", "source_path": "raw/course.pdf"}),
                encoding="utf-8",
            )
            completed = mock.Mock(
                returncode=0,
                stdout="RESULT indexed=1 failures=0 skipped=0",
                stderr="",
            )
            with (
                mock.patch.object(pdf_collector, "_allowed_attachment_roots", return_value=(cache,)),
                mock.patch.object(pdf_collector, "_document_id", return_value="course"),
                mock.patch.object(pdf_collector.subprocess, "run", return_value=completed) as run,
            ):
                result = pdf_collector.archive_pdf(str(source), "telegram note", root=root)

            self.assertTrue(result["indexed"])
            self.assertTrue((root / "intake" / "telegram").exists())
            self.assertFalse((root / "intake" / "inbox").exists())
            self.assertIn(str(root / "intake" / "telegram" / "course.pdf"), run.call_args.args[0])

    def test_pdf_intake_retries_transient_library_lock(self) -> None:
        from tools import pdf_collector

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = root / "cache"
            cache.mkdir()
            source = cache / "course.pdf"
            source.write_bytes(b"%PDF-test")
            manifest_path = root / "state" / "manifests" / "course.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps({"document_id": "course", "status": "indexed", "source_path": "raw/course.pdf"}),
                encoding="utf-8",
            )
            busy = mock.Mock(
                returncode=0,
                stdout="SKIPPED reason=another library worker is active root=/hve-library",
                stderr="",
            )
            success = mock.Mock(returncode=0, stdout="RESULT indexed=1 failures=0", stderr="")
            with (
                mock.patch.object(pdf_collector, "_allowed_attachment_roots", return_value=(cache,)),
                mock.patch.object(pdf_collector, "_document_id", return_value="course"),
                mock.patch.object(pdf_collector.subprocess, "run", side_effect=[busy, success]) as run,
                mock.patch.object(pdf_collector.time, "sleep"),
            ):
                result = pdf_collector.archive_pdf(str(source), root=root)

            self.assertTrue(result["indexed"])
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
