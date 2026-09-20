from __future__ import annotations

import hashlib
import tempfile
import json
import unittest
from pathlib import Path
from unittest import mock

from tools.proton_file_collector import (
    ProtonFileCollectorError,
    _validate_proton_url,
    _lock_path,
    _download_via_browser,
    _stage_download,
    archive_proton_file,
    detect_file_type,
    process_proton_job,
    notify_indexed_proton_jobs,
)


class ProtonFileCollectorTests(unittest.TestCase):
    def test_detects_supported_types_from_signatures(self) -> None:
        self.assertEqual(detect_file_type("guide.pdf", "application/pdf", b"%PDF-1.7"), "pdf")
        self.assertEqual(detect_file_type("track.mp3", "audio/mpeg", b"ID3\x04"), "mp3")
        self.assertEqual(
            detect_file_type("video.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42"),
            "mp4",
        )

    def test_rejects_extension_or_mime_mismatch(self) -> None:
        with self.assertRaisesRegex(ProtonFileCollectorError, "does not match"):
            detect_file_type("guide.pdf", "application/pdf", b"ID3\x04")
        with self.assertRaisesRegex(ProtonFileCollectorError, "disagree"):
            detect_file_type("guide.pdf", "audio/mpeg", b"%PDF-1.7")

    def test_requires_approved_proton_hostname(self) -> None:
        with self.assertRaisesRegex(ProtonFileCollectorError, "approved Proton allowlist"):
            archive_proton_file("https://example.com/share/file")

    def test_preserves_proton_access_fragment(self) -> None:
        url = "https://drive.proton.me/urls/FHVD1TPKYC#c8JGGgNCM8Er"
        self.assertEqual(_validate_proton_url(url), url)

    def test_rejects_proton_share_without_access_fragment(self) -> None:
        with self.assertRaisesRegex(ProtonFileCollectorError, "missing its access fragment"):
            _validate_proton_url("https://drive.proton.me/urls/FHVD1TPKYC")

    def test_recovers_fragment_from_prior_proton_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            complete_url = "https://drive.proton.me/urls/example#key"
            job_path = root / "state" / "jobs" / "proton-prior.json"
            job_path.parent.mkdir(parents=True)
            job_path.write_text(
                json.dumps({"job_id": "proton-prior", "canonical_url": complete_url, "status": "failed"}),
                encoding="utf-8",
            )

            result = archive_proton_file(
                "https://drive.proton.me/urls/example",
                root=root,
            )

            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["canonical_url"], complete_url)

    def test_enqueue_does_not_download_or_hold_browser_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            downloader = mock.Mock(side_effect=AssertionError("enqueue must not download"))
            result = archive_proton_file("https://drive.proton.me/urls/example#key", root=root, downloader=downloader)

            self.assertEqual(result["status"], "queued")
            self.assertTrue(result["job_id"].startswith("proton-"))
            self.assertFalse(downloader.called)
            job = json.loads(Path(result["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["status"], "queued")
            self.assertIsNone(job["manifest_path"])

    def test_captures_telegram_origin_for_completion_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_CHAT_ID": "1477642616",
                    "HERMES_SESSION_THREAD_ID": "",
                },
                clear=False,
            ):
                result = archive_proton_file(
                    "https://drive.proton.me/urls/example#key",
                    root=root,
                )

            job = json.loads(Path(result["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["notify_platform"], "telegram")
            self.assertEqual(job["notify_chat_id"], "1477642616")
            self.assertEqual(job["notification_status"], "pending")

    def test_uses_validated_deployment_target_when_mcp_context_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {
                    "HVE_PROTON_NOTIFICATION_TARGET": "telegram:1477642616:42",
                    "HERMES_SESSION_PLATFORM": "",
                    "HERMES_SESSION_CHAT_ID": "",
                    "HERMES_SESSION_THREAD_ID": "",
                },
                clear=False,
            ):
                result = archive_proton_file(
                    "https://drive.proton.me/urls/example#key",
                    root=root,
                )

            job = json.loads(Path(result["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["notify_platform"], "telegram")
            self.assertEqual(job["notify_chat_id"], "1477642616")
            self.assertEqual(job["notify_thread_id"], "42")
            self.assertEqual(job["notify_target"], "telegram:1477642616:42")

    def test_rejects_untrusted_deployment_notification_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {"HVE_PROTON_NOTIFICATION_TARGET": "telegram:1477642616;rm"},
                clear=False,
            ):
                result = archive_proton_file(
                    "https://drive.proton.me/urls/example#key",
                    root=root,
                )

            job = json.loads(Path(result["job_path"]).read_text(encoding="utf-8"))
            self.assertIsNone(job["notify_platform"])
            self.assertEqual(job["notification_status"], "pending")

    def test_notifies_telegram_only_after_manifest_is_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_CHAT_ID": "1477642616",
                    "HERMES_SESSION_THREAD_ID": "",
                },
                clear=False,
            ):
                queued = archive_proton_file(
                    "https://drive.proton.me/urls/example#key",
                    root=root,
                )

            def fake_downloader(_url: str, destination: Path):
                destination.write_bytes(b"%PDF-1.7\n")
                return "https://drive.proton.me/download/course.pdf", "application/pdf", 9

            with mock.patch(
                "tools.proton_file_collector._run_knowledge_intake",
                return_value=(True, None),
            ):
                completed = process_proton_job(
                    queued["job_id"],
                    root=root,
                    downloader=fake_downloader,
                )
            self.assertEqual(notify_indexed_proton_jobs(root), [])

            manifest_path = Path(completed["manifest_path"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "status": "indexed",
                    "processing_status": "completed",
                    "page_count": 2,
                    "chunk_count": 3,
                    "manifest_path": str(manifest_path),
                }
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed_send = mock.Mock(return_value=mock.Mock(returncode=0))
            with mock.patch(
                "tools.proton_file_collector.subprocess.run",
                completed_send,
            ):
                notifications = notify_indexed_proton_jobs(root)

            self.assertEqual(notifications, [{"job_id": queued["job_id"], "status": "sent"}])
            command = completed_send.call_args.args[0]
            self.assertEqual(command[:6], [
                "/home/hans/.hermes/hermes-agent/venv/bin/hermes",
                "--profile",
                "hve-librarian",
                "send",
                "--to",
                "telegram:1477642616",
            ])
            job = json.loads(Path(queued["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["notification_status"], "sent")

    def test_does_not_notify_when_manifest_sha256_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_CHAT_ID": "1477642616",
                    "HERMES_SESSION_THREAD_ID": "",
                },
                clear=False,
            ):
                queued = archive_proton_file(
                    "https://drive.proton.me/urls/missing-hash#key",
                    root=root,
                )

            source = root / "raw.pdf"
            source.write_bytes(b"%PDF-1.7\n")
            manifest_path = root / "state" / "manifests" / "missing.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "indexed",
                        "processing_status": "completed",
                        "source_path": str(source),
                    }
                ),
                encoding="utf-8",
            )
            job_path = Path(queued["job_path"])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.update(
                {
                    "status": "completed",
                    "processing_status": "completed",
                    "manifest_path": str(manifest_path),
                    "notification_status": "pending",
                }
            )
            job_path.write_text(json.dumps(job), encoding="utf-8")

            with mock.patch("tools.proton_file_collector.subprocess.run") as send:
                notifications = notify_indexed_proton_jobs(root)

            self.assertEqual(notifications[0]["status"], "provenance_incomplete")
            send.assert_not_called()
            updated = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["notification_status"], "provenance_incomplete")

    def test_does_not_notify_when_manifest_sha256_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            queued = archive_proton_file(
                "https://drive.proton.me/urls/malformed-hash#key",
                root=root,
            )
            source = root / "raw.pdf"
            source.write_bytes(b"%PDF-1.7\n")
            manifest_path = root / "state" / "manifests" / "malformed.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "indexed",
                        "processing_status": "completed",
                        "source_path": str(source),
                        "sha256": "not-a-sha256",
                    }
                ),
                encoding="utf-8",
            )
            job_path = Path(queued["job_path"])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.update(
                {
                    "status": "completed",
                    "processing_status": "completed",
                    "manifest_path": str(manifest_path),
                    "sha256": "not-a-sha256",
                    "notification_status": "pending",
                }
            )
            job_path.write_text(json.dumps(job), encoding="utf-8")

            with mock.patch("tools.proton_file_collector.subprocess.run") as send:
                notifications = notify_indexed_proton_jobs(root)

            self.assertEqual(notifications[0]["status"], "provenance_incomplete")
            send.assert_not_called()

    def test_does_not_notify_when_source_hash_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(
                "os.environ",
                {
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_CHAT_ID": "1477642616",
                    "HERMES_SESSION_THREAD_ID": "",
                },
                clear=False,
            ):
                queued = archive_proton_file(
                    "https://drive.proton.me/urls/hash-mismatch#key",
                    root=root,
                )

            source = root / "raw.pdf"
            source.write_bytes(b"%PDF-1.7\n")
            manifest_path = root / "state" / "manifests" / "mismatch.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "indexed",
                        "processing_status": "completed",
                        "source_path": str(source),
                        "sha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            job_path = Path(queued["job_path"])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.update(
                {
                    "status": "completed",
                    "processing_status": "completed",
                    "manifest_path": str(manifest_path),
                    "sha256": "a" * 64,
                    "notification_status": "pending",
                }
            )
            job_path.write_text(json.dumps(job), encoding="utf-8")

            with mock.patch("tools.proton_file_collector.subprocess.run") as send:
                notifications = notify_indexed_proton_jobs(root)

            self.assertEqual(notifications[0]["status"], "provenance_incomplete")
            send.assert_not_called()

    def test_does_not_notify_when_job_and_manifest_hashes_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            queued = archive_proton_file(
                "https://drive.proton.me/urls/job-manifest-mismatch#key",
                root=root,
            )
            source = root / "raw.pdf"
            source.write_bytes(b"%PDF-1.7\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest_path = root / "state" / "manifests" / "mismatch.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "indexed",
                        "processing_status": "completed",
                        "source_path": str(source),
                        "sha256": digest,
                    }
                ),
                encoding="utf-8",
            )
            job_path = Path(queued["job_path"])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job.update(
                {
                    "status": "completed",
                    "processing_status": "completed",
                    "manifest_path": str(manifest_path),
                    "sha256": "b" * 64,
                    "notification_status": "pending",
                }
            )
            job_path.write_text(json.dumps(job), encoding="utf-8")

            with mock.patch("tools.proton_file_collector.subprocess.run") as send:
                notifications = notify_indexed_proton_jobs(root)

            self.assertEqual(notifications[0]["status"], "provenance_incomplete")
            send.assert_not_called()

    def test_returns_in_progress_when_url_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            url = "https://drive.proton.me/urls/example#key"
            lock_path = _lock_path(root, _validate_proton_url(url))
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(
                json.dumps({"pid": 999999, "created_at": 9999999999, "token": "active"}),
                encoding="utf-8",
            )

            result = archive_proton_file(url, root=root, downloader=lambda *_: self.fail("downloaded"))

            self.assertEqual(result["status"], "in_progress")

    def test_returns_already_queued_for_duplicate_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            url = "https://drive.proton.me/urls/example#key"

            def fake_downloader(_url: str, destination: Path):
                destination.write_bytes(b"%PDF-1.7\n")
                return "https://proton.example/download/course.pdf", "application/pdf", 9

            first = archive_proton_file(url, root=root)
            second = archive_proton_file(url, root=root, downloader=fake_downloader)

            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "already_queued")
            self.assertEqual(second["job_id"], first["job_id"])

    def test_worker_downloads_one_job_once_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = archive_proton_file("https://drive.proton.me/urls/one#key", root=root)
            calls: list[str] = []

            def fake_downloader(url: str, destination: Path):
                calls.append(url)
                destination.write_bytes(b"%PDF-1.7\n")
                return "https://drive.proton.me/download/course.pdf", "application/pdf", 9

            with mock.patch(
                "tools.proton_file_collector._run_knowledge_intake",
                return_value=(True, None),
            ):
                completed = process_proton_job(result["job_id"], root=root, downloader=fake_downloader)
                again = process_proton_job(result["job_id"], root=root, downloader=fake_downloader)

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(again["status"], "completed")
            self.assertEqual(calls, ["https://drive.proton.me/urls/one#key"])
            self.assertTrue(Path(completed["source_path"]).is_file())
            manifest = json.loads(Path(completed["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["processing_status"], "completed")
            self.assertEqual(manifest["sha256"], completed["sha256"])

    def test_marks_manifest_processing_failed_when_intake_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            queued = archive_proton_file("https://drive.proton.me/urls/failed-intake#key", root=root)

            def fake_downloader(_url: str, destination: Path):
                destination.write_bytes(b"%PDF-1.7\n")
                return "https://drive.proton.me/download/course.pdf", "application/pdf", 9

            with mock.patch(
                "tools.proton_file_collector._run_knowledge_intake",
                return_value=(False, "indexing failed"),
            ):
                completed = process_proton_job(
                    queued["job_id"],
                    root=root,
                    downloader=fake_downloader,
                )

            self.assertEqual(completed["status"], "completed")
            manifest = json.loads(Path(completed["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["processing_status"], "failed")
            job = json.loads(Path(queued["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["processing_status"], "failed")

    def test_proton_downloads_use_private_staging_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            temporary = root / "download.part"
            temporary.write_bytes(b"%PDF-1.7\n")

            destination = _stage_download(root, temporary, "course.pdf", "pdf", "a" * 64)

            self.assertEqual(destination.parent, root / "intake" / "proton")
            self.assertFalse((root / "intake" / "inbox").exists())
            self.assertTrue(destination.is_file())

    def test_browser_worker_uses_two_click_download_sequence(self) -> None:
        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

            def close(self):
                return None

        class Socket:
            def __init__(self):
                self.sent: list[dict] = []
                self.events: list[dict] = []
                self.download_path: Path | None = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def send(self, payload: str):
                message = json.loads(payload)
                self.sent.append(message)
                method = message["method"]
                if method == "Browser.setDownloadBehavior":
                    self.download_path = Path(message["params"]["downloadPath"])
                if method == "Input.dispatchMouseEvent" and message["params"].get("type") == "mouseReleased":
                    if self.download_path:
                        (self.download_path / "course.pdf").write_bytes(b"%PDF-1.7\n")
                if method == "Runtime.evaluate":
                    evaluate_count = len([item for item in self.sent if item["method"] == "Runtime.evaluate"])
                    result = {"result": {"value": {"x": 100, "y": 100} if evaluate_count in {1, 2} else {"x": 200, "y": 200}}}
                else:
                    result = {}
                self.events.append({"id": message["id"], "result": result})
                if method == "Runtime.evaluate" and len(
                    [item for item in self.sent if item["method"] == "Runtime.evaluate"]
                ) == 1:
                    self.events.extend(
                        [
                            {
                                "method": "Browser.downloadWillBegin",
                                "params": {
                                    "guid": "download-1",
                                    "suggestedFilename": "course.pdf",
                                    "url": "https://drive.proton.me/download/course.pdf",
                                },
                            },
                            {
                                "method": "Browser.downloadProgress",
                                "params": {"guid": "download-1", "state": "completed"},
                            },
                        ]
                    )

            def recv(self, timeout=None):
                if not self.events:
                    raise TimeoutError
                return json.dumps(self.events.pop(0))

        socket = Socket()

        def open_url(request, timeout=0):
            if "/json/new?" in request.full_url:
                return Response(
                    json.dumps(
                        {"id": "target-1", "webSocketDebuggerUrl": "ws://target-1"}
                    ).encode()
                )
            return Response(b"")

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch("tools.proton_file_collector._validate_public_target"),
            mock.patch("tools.proton_file_collector.urllib.request.urlopen", side_effect=open_url),
            mock.patch("tools.proton_file_collector.websocket_connect", return_value=socket),
            mock.patch("tools.proton_file_collector.time.sleep"),
        ):
            destination = Path(tmpdir) / "downloaded.pdf"
            final_url, mime_type, size = _download_via_browser(
                "https://drive.proton.me/urls/browser#key", destination
            )
            download_exists = destination.is_file()

        runtime = [item for item in socket.sent if item["method"] == "Runtime.evaluate"]
        self.assertEqual(len(runtime), 2)
        self.assertEqual(
            len([item for item in socket.sent if item["method"] == "Input.dispatchMouseEvent" and item["params"]["type"] == "mouseReleased"]),
            2,
        )
        self.assertIn("dropdown-download-button", runtime[0]["params"]["expression"])
        self.assertIn("innerText.trim() === 'Download'", runtime[1]["params"]["expression"])
        self.assertLess(
            next(i for i, item in enumerate(socket.sent) if item["method"] == "Page.navigate"),
            next(i for i, item in enumerate(socket.sent) if item["method"] == "Runtime.evaluate"),
        )
        self.assertEqual(final_url.rsplit("#", 1)[-1], "course.pdf")
        self.assertEqual(mime_type, "application/pdf")
        self.assertEqual(size, 9)
        self.assertTrue(download_exists)

    def test_duplicate_content_is_not_staged_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = archive_proton_file("https://drive.proton.me/urls/first#key", root=root)
            second = archive_proton_file("https://drive.proton.me/urls/second#key", root=root)

            def fake_downloader(_url: str, destination: Path):
                destination.write_bytes(b"%PDF-1.7\n")
                return "https://drive.proton.me/download/course.pdf", "application/pdf", 9

            with mock.patch(
                "tools.proton_file_collector._run_knowledge_intake",
                return_value=(True, None),
            ):
                first_result = process_proton_job(first["job_id"], root=root, downloader=fake_downloader)
                second_result = process_proton_job(second["job_id"], root=root, downloader=fake_downloader)

            self.assertEqual(first_result["status"], "completed")
            self.assertEqual(second_result["status"], "duplicate")
            self.assertEqual(second_result["duplicate_of"], first["job_id"])
            self.assertEqual(len(list((root / "intake" / "proton").glob("*"))), 1)

    def test_failed_worker_cleans_partial_download_and_marks_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            queued = archive_proton_file("https://drive.proton.me/urls/bad#key", root=root)

            def failing_downloader(_url: str, destination: Path):
                destination.write_bytes(b"not a supported file")
                return "https://drive.proton.me/download/bad.pdf", "application/pdf", destination.stat().st_size

            failed = process_proton_job(queued["job_id"], root=root, downloader=failing_downloader)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("does not match", failed["error"])
            self.assertEqual(list((root / "state" / "proton-downloads").glob("*")), [])
            job = json.loads(Path(queued["job_path"]).read_text(encoding="utf-8"))
            self.assertEqual(job["status"], "failed")


if __name__ == "__main__":
    unittest.main()
