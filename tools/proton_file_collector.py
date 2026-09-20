from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.link_collector import (
    _PinnedHTTPHandler,
    _PinnedHTTPSHandler,
    _SafeRedirectHandler,
    _validate_public_target,
    canonicalize_url,
    websocket_connect,
)
from tools.knowledge_layer_client import cli_command, cli_environment


KNOWLEDGE_ROOT = Path("/hve-library")
CDP_ENDPOINT = "http://127.0.0.1:9222"
PROTON_HOSTS = {
    "proton.me",
    "drive.proton.me",
    "protondrive.com",
    "drive.protondrive.com",
    "share.proton.me",
}
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CONTEXT_LENGTH = 20_000
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT = 180
LOCK_TIMEOUT = DOWNLOAD_TIMEOUT + 60
STALE_JOB_TIMEOUT = DOWNLOAD_TIMEOUT + 60
NOTIFICATION_RETRY_INTERVAL = 30
HERMES_BIN = Path("/home/hans/.hermes/hermes-agent/venv/bin/hermes")
NOTIFICATION_PROFILE = "hve-librarian"
NOTIFICATION_TARGET_ENV = "HVE_PROTON_NOTIFICATION_TARGET"
CAPTURE_SOURCE = "hve_librarian"

FILE_TYPES = {
    "pdf": {"mime": "application/pdf", "directory": "pdf"},
    "mp3": {"mime": "audio/mpeg", "directory": "audio"},
    "mp4": {"mime": "video/mp4", "directory": "video"},
}
MIME_TYPES = {
    "application/pdf": "pdf",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "video/mp4": "mp4",
}
EXTENSION_TYPES = {".pdf": "pdf", ".mp3": "mp3", ".mp4": "mp4"}
TERMINAL_JOB_STATUSES = {"completed", "duplicate", "failed", "cancelled"}
URL_RECORD_STATUSES = {"queued", "in_progress", "processing", *TERMINAL_JOB_STATUSES}


class ProtonFileCollectorError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.new")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_proton_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ProtonFileCollectorError("A Proton public share URL is required")
    cleaned = url.strip()
    if any(ord(char) < 32 for char in cleaned):
        raise ProtonFileCollectorError("URL contains control characters")
    parsed_input = urllib.parse.urlsplit(cleaned)
    if parsed_input.scheme.lower() != "https" or not parsed_input.netloc:
        raise ProtonFileCollectorError("A Proton HTTPS share URL is required")
    url_without_fragment = urllib.parse.urlunsplit(
        (parsed_input.scheme, parsed_input.netloc, parsed_input.path, parsed_input.query, "")
    )
    canonical_base = canonicalize_url(url_without_fragment)
    parsed = urllib.parse.urlsplit(canonical_base)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in PROTON_HOSTS and not any(host.endswith(f".{suffix}") for suffix in PROTON_HOSTS):
        raise ProtonFileCollectorError("URL hostname is not on the approved Proton allowlist")
    if host == "drive.proton.me" and parsed.path.startswith("/urls/") and not parsed_input.fragment:
        raise ProtonFileCollectorError(
            "Proton share URL is missing its access fragment; preserve the complete copied URL"
        )
    _validate_public_target(canonical_base)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed_input.fragment)
    )


def _sanitize_context(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned[:MAX_CONTEXT_LENGTH] or None


def _telegram_notification_target() -> tuple[str | None, str | None]:
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower()
    chat_id = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    if platform == "telegram" and re.fullmatch(r"-?\d+", chat_id):
        thread_id = os.environ.get("HERMES_SESSION_THREAD_ID", "").strip() or None
        if thread_id is not None and not thread_id.isdigit():
            thread_id = None
        return chat_id, thread_id

    # Persistent MCP subprocesses intentionally do not inherit per-turn
    # session context. A deployment-scoped target is the safe fallback for
    # the single HVE-Librarian Telegram channel.
    configured = os.environ.get(NOTIFICATION_TARGET_ENV, "").strip()
    match = re.fullmatch(r"telegram:(-?\d+)(?::(\d+))?", configured)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _safe_filename(name: str, file_type: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._")
    stem = Path(cleaned or "proton-file").stem[:176]
    return f"{stem}.{file_type}"


def _signature_type(header: bytes) -> str | None:
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"ID3"):
        return "mp3"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "mp3"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "mp4"
    return None


def _metadata_type(filename: str, mime_type: str | None) -> str | None:
    extension_type = EXTENSION_TYPES.get(Path(filename).suffix.lower())
    mime_normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    mime_file_type = MIME_TYPES.get(mime_normalized)
    if extension_type and mime_file_type and extension_type != mime_file_type:
        raise ProtonFileCollectorError(
            f"Filename and MIME type disagree: {extension_type} vs {mime_file_type}"
        )
    return extension_type or mime_file_type


def detect_file_type(filename: str, mime_type: str | None, header: bytes) -> str:
    signature_type = _signature_type(header)
    if signature_type is None:
        raise ProtonFileCollectorError(
            "Downloaded content does not match a supported PDF, MP3, or MP4 signature"
        )
    metadata_type = _metadata_type(filename, mime_type)
    if metadata_type and metadata_type != signature_type:
        raise ProtonFileCollectorError(
            f"Downloaded signature does not match filename/MIME type: "
            f"{signature_type} vs {metadata_type}"
        )
    return signature_type


def _url_key(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _url_base(canonical_url: str) -> str:
    parsed = urllib.parse.urlsplit(canonical_url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _recover_proton_url(root: Path, url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.fragment or parsed.hostname != "drive.proton.me" or not parsed.path.startswith("/urls/"):
        return url
    base = _url_base(url)
    for record in reversed(_records(root)):
        candidate = str(record.get("canonical_url") or "")
        if candidate and _url_base(candidate) == base and urllib.parse.urlsplit(candidate).fragment:
            return candidate
    return url


def _lock_path(root: Path, canonical_url: str) -> Path:
    return root / "state" / "locks" / f"proton-{_url_key(canonical_url)}.json"


def _job_lock_path(root: Path, job_id: str) -> Path:
    return root / "state" / "locks" / f"{job_id}.json"


def _content_lock_path(root: Path, digest: str) -> Path:
    return root / "state" / "locks" / f"proton-content-{digest}.json"


def _job_path(root: Path, job_id: str) -> Path:
    return root / "state" / "jobs" / f"{job_id}.json"


def _cancel_path(root: Path, job_id: str) -> Path:
    return root / "state" / "jobs" / f"{job_id}.cancel"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _records(root: Path) -> list[dict[str, Any]]:
    paths = [root / "state" / "jobs", root / "state" / "manifests"]
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in paths:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            record = _load_json(path)
            if not record:
                continue
            key = str(record.get("job_id") or path)
            if key in seen:
                continue
            seen.add(key)
            record.setdefault("_path", str(path))
            records.append(record)
    return records


def _existing_record(root: Path, canonical_url: str) -> dict[str, Any] | None:
    matches = [
        record
        for record in _records(root)
        if record.get("canonical_url") == canonical_url
        and record.get("status") in {"queued", "in_progress", "processing", "completed", "duplicate"}
    ]
    if not matches:
        return None
    matches.sort(key=lambda value: str(value.get("updated_at") or value.get("created_at") or ""))
    return matches[-1]


def _existing_digest_record(root: Path, digest: str) -> dict[str, Any] | None:
    for record in _records(root):
        if record.get("sha256") == digest and record.get("status") in URL_RECORD_STATUSES:
            return record
    inbox = root / "intake" / "inbox"
    if inbox.exists():
        for path in inbox.iterdir():
            if path.is_file() and not path.name.startswith("."):
                try:
                    if _sha256(path) == digest:
                        return {
                            "status": "completed",
                            "source_path": str(path),
                            "sha256": digest,
                        }
                except OSError:
                    continue
    return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _exclusive_lock(path: Path, *, key: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"key": key, "pid": os.getpid(), "created_at": time.time(), "token": secrets.token_hex(8)}
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except FileExistsError:
        existing = _load_json(path) or {}
        try:
            age = time.time() - float(existing.get("created_at", 0))
            pid = int(existing.get("pid", 0))
        except (TypeError, ValueError):
            age, pid = 0, 0
        if age > LOCK_TIMEOUT and (not pid or not _pid_exists(pid)):
            path.unlink(missing_ok=True)
            try:
                with path.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle)
            except FileExistsError as exc:
                raise ProtonFileCollectorError("An archive lock is already active") from exc
        else:
            raise ProtonFileCollectorError("An archive lock is already active")
    try:
        yield
    finally:
        current = _load_json(path)
        if current and current.get("token") == payload["token"]:
            path.unlink(missing_ok=True)


@contextmanager
def _proton_url_lock(root: Path, canonical_url: str):
    with _exclusive_lock(_lock_path(root, canonical_url), key=canonical_url):
        yield


@contextmanager
def _job_lock(root: Path, job_id: str):
    with _exclusive_lock(_job_lock_path(root, job_id), key=job_id):
        yield


def _download(
    url: str,
    destination: Path,
    *,
    opener: Any | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> tuple[str, str | None, int]:
    canonical_url = _validate_proton_url(url)
    request = urllib.request.Request(
        canonical_url,
        headers={
            "Accept": "application/pdf,audio/mpeg,video/mp4;q=0.9,*/*;q=0.1",
            "User-Agent": "HVE-Librarian-Proton-Collector/1.0",
        },
    )
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PinnedHTTPHandler(),
            _PinnedHTTPSHandler(),
            _SafeRedirectHandler(),
        )
    try:
        with opener.open(request, timeout=60) as response:
            final_url = _validate_proton_url(response.geturl())
            content_type = response.headers.get("Content-Type")
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > max_bytes:
                raise ProtonFileCollectorError("Downloaded file exceeds the 2 GB limit")
            total = 0
            with destination.open("wb") as handle:
                while True:
                    block = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise ProtonFileCollectorError("Downloaded file exceeds the 2 GB limit")
                    handle.write(block)
            return final_url, content_type, total
    except urllib.error.HTTPError as exc:
        raise ProtonFileCollectorError(f"Proton download failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ProtonFileCollectorError(f"Proton download failed: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise ProtonFileCollectorError(f"Proton download failed: {exc}") from exc


def _download_via_browser(url: str, destination: Path) -> tuple[str, str | None, int]:
    """Perform one controlled Proton browser download in one CDP target."""
    canonical_url = _validate_proton_url(url)
    download_dir = destination.parent / f".browser-{secrets.token_hex(8)}"
    download_dir.mkdir(parents=True, exist_ok=False)
    target_id: str | None = None
    try:
        request = urllib.request.Request(f"{CDP_ENDPOINT}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(request, timeout=5) as response:
            target = json.loads(response.read().decode("utf-8"))
        target_id = str(target["id"])
        websocket_url = str(target["webSocketDebuggerUrl"])
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT
        with websocket_connect(websocket_url, open_timeout=5, close_timeout=2, max_size=2**20) as socket:
            next_id = 0
            download_guid: str | None = None
            download_name: str | None = None
            download_url: str | None = None
            download_state: str | None = None
            response_mime: str | None = None
            response_mimes: dict[str, str] = {}

            def handle_event(message: dict[str, Any]) -> None:
                nonlocal download_guid, download_name, download_url, download_state, response_mime
                method = message.get("method", "")
                params = message.get("params", {})
                if method.endswith(".downloadWillBegin"):
                    candidate_guid = str(params.get("guid") or "")
                    if download_guid and candidate_guid != download_guid:
                        raise ProtonFileCollectorError("More than one browser download was observed")
                    download_guid = candidate_guid
                    download_name = str(params.get("suggestedFilename") or "")
                    download_url = str(params.get("url") or "")
                    response_mime = response_mimes.get(download_url)
                elif method.endswith(".downloadProgress"):
                    download_state = str(params.get("state") or "")
                elif method == "Network.responseReceived":
                    response = params.get("response", {})
                    candidate = str(response.get("url") or "")
                    mime = str(response.get("mimeType") or "")
                    if candidate and mime:
                        response_mimes[candidate] = mime
                    if download_url and candidate == download_url:
                        response_mime = mime or response_mime

            def receive(timeout: float) -> dict[str, Any]:
                try:
                    message = json.loads(socket.recv(timeout=timeout))
                except TimeoutError:
                    return {}
                if message.get("method"):
                    handle_event(message)
                return message

            def command(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal next_id
                next_id += 1
                request_id = next_id
                socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                while time.monotonic() < deadline:
                    message = receive(max(0.1, deadline - time.monotonic()))
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise ProtonFileCollectorError(f"CDP command failed: {message['error']}")
                        return message.get("result", {})
                raise ProtonFileCollectorError("Timed out waiting for a browser response")

            command("Page.enable")
            command("Network.enable")
            command("Page.bringToFront")
            try:
                command(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(download_dir), "eventsEnabled": True},
                )
            except ProtonFileCollectorError:
                command("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)})
            command("Page.navigate", {"url": canonical_url})
            time.sleep(5)

            control = command(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(async () => { "
                        "const visible = node => node && !node.disabled && "
                        "(node.offsetWidth || node.offsetHeight); "
                        "const waitFor = find => new Promise((resolve, reject) => { "
                        "const findNode = () => find(); "
                        "const immediate = findNode(); if (immediate) { resolve(immediate); return; } "
                        "const observer = new MutationObserver(() => { const node = findNode(); if (node) { observer.disconnect(); resolve(node); } }); "
                        "observer.observe(document.documentElement, {childList: true, subtree: true, attributes: true}); "
                        "setTimeout(() => { observer.disconnect(); reject(new Error('Proton download control')); }, 60000); }); "
                        "await waitFor(() => document.readyState === 'complete'); "
                        "const top = await waitFor(() => { "
                        "const node = document.querySelector('[data-testid=\"dropdown-download-button\"]'); "
                        "return visible(node) ? node : null; }); "
                        "const rect = top.getBoundingClientRect(); "
                        "return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}; })()"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if control.get("exceptionDetails"):
                raise ProtonFileCollectorError(
                    "Proton PDF did not finish loading or its top Download button was unavailable"
                )
            top_rect = control.get("result", {}).get("value")
            if not isinstance(top_rect, dict) or not all(
                isinstance(top_rect.get(key), (int, float)) for key in ("x", "y")
            ):
                raise ProtonFileCollectorError("Proton top Download control did not become clickable")

            def mouse_click(x: float, y: float) -> None:
                command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})

            mouse_click(float(top_rect["x"]), float(top_rect["y"]))
            command("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": float(top_rect["x"]) + 8,
                "y": float(top_rect["y"]) + 8,
            })
            menu = command(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(async () => { "
                        "const visible = node => node && !node.disabled && "
                        "(node.offsetWidth || node.offsetHeight); "
                        "const waitFor = find => new Promise((resolve, reject) => { "
                        "const findNode = () => find(); "
                        "const immediate = findNode(); if (immediate) { resolve(immediate); return; } "
                        "const observer = new MutationObserver(() => { const node = findNode(); if (node) { observer.disconnect(); resolve(node); } }); "
                        "observer.observe(document.documentElement, {childList: true, subtree: true, attributes: true}); "
                        "setTimeout(() => { observer.disconnect(); reject(new Error('Proton download menu')); }, 60000); }); "
                        "const top = document.querySelector('[data-testid=\"dropdown-download-button\"]'); "
                        "const item = await waitFor(() => [...document.querySelectorAll('button,[role=\"button\"]')]"
                        ".find(node => node !== top && visible(node) && node.innerText.trim() === 'Download')); "
                        "const rect = item.getBoundingClientRect(); "
                        "return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}; })()"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if menu.get("exceptionDetails"):
                raise ProtonFileCollectorError(
                    "Proton top Download click did not open the download menu"
                )
            menu_rect = menu.get("result", {}).get("value")
            if not isinstance(menu_rect, dict) or not all(
                isinstance(menu_rect.get(key), (int, float)) for key in ("x", "y")
            ):
                raise ProtonFileCollectorError(
                    "Proton menu Download item did not become clickable"
                )
            mouse_click(float(menu_rect["x"]), float(menu_rect["y"]))

            while time.monotonic() < deadline:
                receive(1)
                if download_state == "canceled":
                    raise ProtonFileCollectorError("Browser download was canceled")
                if download_guid and download_state == "completed" and download_name:
                    source = download_dir / Path(download_name).name
                    if not source.is_file():
                        continue
                    size = source.stat().st_size
                    if size <= 0:
                        raise ProtonFileCollectorError("Browser download completed with an empty file")
                    source.replace(destination)
                    mime = response_mime or mimetypes.guess_type(download_name)[0]
                    return f"{canonical_url}#{download_name}", mime, size
            raise ProtonFileCollectorError(
                "Proton menu Download click produced no completed browser download"
            )
    except ProtonFileCollectorError:
        raise
    except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ProtonFileCollectorError(f"Browser download failed: {exc}") from exc
    finally:
        if target_id:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(f"{CDP_ENDPOINT}/json/close/{target_id}", method="GET"),
                    timeout=3,
                ).close()
            except (OSError, urllib.error.URLError):
                pass
        if download_dir.exists():
            shutil.rmtree(download_dir, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_result(record: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    result = {key: value for key, value in record.items() if not key.startswith("_")}
    result["status"] = status or str(record.get("status", "unknown"))
    result.setdefault("archived", result["status"] == "completed")
    result.setdefault("indexed", False)
    result["retryable"] = False
    result["agent_action"] = "Do not call archive_proton_file again for this URL in this turn."
    return result


def _notification_target(job: dict[str, Any]) -> str | None:
    configured_target = str(job.get("notify_target") or "").strip()
    if configured_target == "telegram":
        return configured_target
    chat_id = str(job.get("notify_chat_id") or "").strip()
    if not re.fullmatch(r"-?\d+", chat_id):
        return None
    thread_id = str(job.get("notify_thread_id") or "").strip()
    return f"telegram:{chat_id}:{thread_id}" if thread_id.isdigit() else f"telegram:{chat_id}"


def _indexed_notification_message(job: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        "HVE-Librarian intake complete",
        "",
        "Status: indexed",
        f"File: {manifest.get('filename') or Path(str(manifest.get('source_path', 'document'))).name}",
        f"Type: {manifest.get('file_type', 'unknown')}",
        f"Size: {int(manifest.get('size_bytes') or manifest.get('file_size_bytes') or 0):,} bytes",
        f"SHA-256: {manifest.get('sha256', 'unavailable')}",
        f"Pages: {manifest.get('page_count', 'unavailable')}",
        f"Chunks: {manifest.get('chunk_count', 'unavailable')}",
        f"Library path: {manifest.get('source_path', 'unavailable')}",
        f"Manifest: {job.get('manifest_path') or 'unavailable'}",
    ]
    return "\n".join(lines)


def _provenance_error(job: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    digest = manifest.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "manifest sha256 is missing or invalid"
    if job.get("sha256") != digest:
        return "job and manifest sha256 values do not match"
    source_path = Path(str(manifest.get("source_path") or ""))
    if not source_path.is_file():
        return f"manifest source file is missing: {source_path}"
    if _sha256(source_path) != digest:
        return f"source sha256 does not match manifest: {source_path}"
    return None


def _send_indexed_notification(root: Path, job: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    if job.get("notification_status") == "sent":
        return {"status": "sent", "skipped": True}
    provenance_error = _provenance_error(job, manifest)
    if provenance_error:
        _job_update(
            root,
            job,
            notification_status="provenance_incomplete",
            notification_error=provenance_error,
        )
        return {"status": "provenance_incomplete", "error": provenance_error}
    target = _notification_target(job)
    if target is None:
        _job_update(
            root,
            job,
            notification_status="not_configured",
            notification_error=None,
        )
        return {"status": "not_configured", "skipped": True}

    attempts = int(job.get("notification_attempts") or 0) + 1
    _job_update(
        root,
        job,
        notification_status="sending",
        notification_attempts=attempts,
        notification_last_attempt_at=_now(),
    )
    command = [
        str(HERMES_BIN),
        "--profile",
        NOTIFICATION_PROFILE,
        "send",
        "--to",
        target,
        "--json",
        _indexed_notification_message(job, manifest),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=os.environ.get("HERMES_PROFILE_ROOT", str(Path.home())),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = f"{type(exc).__name__}: notification command failed"
        _job_update(root, job, notification_status="failed", notification_error=error)
        return {"status": "failed", "error": error}
    if result.returncode != 0:
        error = f"notification command exited with status {result.returncode}"
        _job_update(root, job, notification_status="failed", notification_error=error)
        return {"status": "failed", "error": error}
    _job_update(
        root,
        job,
        notification_status="sent",
        notification_sent_at=_now(),
        notification_error=None,
    )
    return {"status": "sent"}


def notify_indexed_proton_jobs(root: Path = KNOWLEDGE_ROOT) -> list[dict[str, Any]]:
    """Notify the originating Telegram chat after async indexing completes."""
    root = Path(root)
    notifications: list[dict[str, Any]] = []
    now = time.time()
    for path in sorted((root / "state" / "jobs").glob("proton-*.json")):
        job = _load_json(path)
        if not job or job.get("status") != "completed":
            continue
        if job.get("processing_status") != "completed":
            continue
        if job.get("notification_status") == "sent":
            continue
        try:
            last_attempt = datetime.fromisoformat(
                str(job.get("notification_last_attempt_at"))
            ).timestamp()
        except (TypeError, ValueError, OverflowError):
            last_attempt = 0
        if last_attempt and now - last_attempt < NOTIFICATION_RETRY_INTERVAL:
            continue
        manifest_path = Path(str(job.get("manifest_path") or ""))
        manifest = _load_json(manifest_path) if manifest_path else None
        if not manifest or manifest.get("status") != "indexed":
            continue
        if manifest.get("processing_status") != "completed":
            continue
        with _job_lock(root, str(job["job_id"])):
            current = _load_json(path)
            if not current or current.get("status") != "completed":
                continue
            outcome = _send_indexed_notification(root, current, manifest)
        notifications.append({"job_id": current["job_id"], **outcome})
    return notifications


def archive_proton_file(
    url: str,
    capture_context: str | None = None,
    *,
    root: Path = KNOWLEDGE_ROOT,
    downloader: Callable[..., tuple[str, str | None, int]] | None = None,
) -> dict[str, Any]:
    """Validate and enqueue a Proton URL; browser work is performed only by the local worker."""
    del downloader  # Retained for API compatibility; enqueueing must never download.
    canonical_url = _validate_proton_url(_recover_proton_url(Path(root), url))
    context = _sanitize_context(capture_context)
    root = Path(root)
    try:
        with _proton_url_lock(root, canonical_url):
            existing = _existing_record(root, canonical_url)
            if existing:
                status = str(existing.get("status"))
                if status in {"queued", "completed"}:
                    status = "already_queued"
                elif status == "processing":
                    status = "in_progress"
                return _record_result(existing, status=status)
            job_id = f"proton-{secrets.token_hex(8)}"
            created_at = _now()
            job = {
                "job_id": job_id,
                "job_type": "proton_file",
                "canonical_url": canonical_url,
                "capture_context": context,
                "status": "queued",
                "processing_status": "pending",
                "created_at": created_at,
                "updated_at": created_at,
                "worker_pid": None,
                "error": None,
                "manifest_path": None,
                "source_path": None,
            }
            notify_chat_id, notify_thread_id = _telegram_notification_target()
            job.update(
                {
                    "notify_platform": "telegram" if notify_chat_id else None,
                    "notify_chat_id": notify_chat_id,
                    "notify_thread_id": notify_thread_id,
                    "notify_target": (
                        f"telegram:{notify_chat_id}:{notify_thread_id}"
                        if notify_chat_id and notify_thread_id
                        else f"telegram:{notify_chat_id}"
                        if notify_chat_id
                        else "telegram"
                    ),
                    "notification_status": "pending",
                    "notification_attempts": 0,
                    "notification_error": None,
                }
            )
            _atomic_write_json(_job_path(root, job_id), job)
    except ProtonFileCollectorError:
        existing = _existing_record(root, canonical_url)
        if existing:
            existing_status = str(existing.get("status", "in_progress"))
            if existing_status in {"queued", "completed"}:
                existing_status = "already_queued"
            elif existing_status == "processing":
                existing_status = "in_progress"
            return _record_result(existing, status=existing_status)
        return {
            "status": "in_progress",
            "archived": False,
            "indexed": False,
            "canonical_url": canonical_url,
            "error": "An archive job is already in progress for this Proton URL",
        }
    return {
        "status": "queued",
        "archived": False,
        "indexed": False,
        "retryable": False,
        "agent_action": "Do not call archive_proton_file again for this URL in this turn.",
        "job_id": job_id,
        "canonical_url": canonical_url,
        "processing_status": "pending",
        "job_path": str(_job_path(root, job_id)),
        "manifest_path": None,
        "source_path": None,
        "worker_entrypoint": "tools.proton_file_collector:run_proton_worker",
    }


def _job_update(root: Path, job: dict[str, Any], **changes: Any) -> dict[str, Any]:
    job.update(changes)
    job["updated_at"] = _now()
    _atomic_write_json(_job_path(root, str(job["job_id"])), job)
    return job


def _cleanup_job_temporary(root: Path, job_id: str) -> None:
    staging = root / "state" / "proton-downloads"
    if not staging.exists():
        return
    for path in staging.glob(f".{job_id}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _cancel_requested(root: Path, job_id: str) -> bool:
    return _cancel_path(root, job_id).exists()


def _filename_from_url(final_url: str) -> str:
    parsed = urllib.parse.urlsplit(final_url)
    fragment = parsed.fragment.strip().rsplit("#", 1)[-1]
    if fragment:
        return urllib.parse.unquote(fragment)
    return Path(urllib.request.url2pathname(parsed.path)).name


def _stage_download(
    root: Path,
    temporary_path: Path,
    filename: str,
    file_type: str,
    digest: str,
) -> Path:
    staging_dir = root / "intake" / "proton"
    staging_dir.mkdir(parents=True, exist_ok=True)
    destination = staging_dir / _safe_filename(filename or f"proton-{digest}", file_type)
    if destination.exists():
        destination = staging_dir / f"{destination.stem}-{digest[:16]}.{file_type}"
    staged = staging_dir / f".{destination.name}.{secrets.token_hex(6)}.part"
    try:
        with temporary_path.open("rb") as source, staged.open("wb") as target:
            shutil.copyfileobj(source, target, DOWNLOAD_CHUNK_SIZE)
            target.flush()
            os.fsync(target.fileno())
        os.replace(staged, destination)
        return destination
    finally:
        staged.unlink(missing_ok=True)


def _run_knowledge_intake(root: Path, pdf_path: Path) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            cli_command("intake", "--pdf", str(pdf_path), root=root),
            capture_output=True,
            text=True,
            check=False,
            timeout=LOCK_TIMEOUT,
            env=cli_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: knowledge-layer intake failed"
    output = (result.stdout or "").strip()
    if output:
        print(output)
    if result.returncode != 0:
        return False, (result.stderr or "").strip() or "Knowledge-layer intake failed"
    return True, None


def process_proton_job(
    job_id: str,
    *,
    root: Path = KNOWLEDGE_ROOT,
    downloader: Callable[..., tuple[str, str | None, int]] | None = None,
) -> dict[str, Any]:
    """Process one queued job exactly once and persist its terminal outcome."""
    root = Path(root)
    job_path = _job_path(root, job_id)
    job = _load_json(job_path)
    if not job:
        return {"status": "failed", "archived": False, "indexed": False, "error": "Proton job not found"}
    with _job_lock(root, job_id):
        job = _load_json(job_path) or job
        status = str(job.get("status", "failed"))
        if status != "queued":
            return _record_result(job, status=status)
        _job_update(root, job, status="in_progress", processing_status="in_progress", worker_pid=os.getpid())
        staging = root / "state" / "proton-downloads"
        staging.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        destination: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=staging,
                prefix=f".{job_id}.",
                suffix=".part",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
            fetch = downloader or _download_via_browser
            final_url, content_type, reported_size = fetch(str(job["canonical_url"]), temporary_path)
            if _cancel_requested(root, job_id):
                raise ProtonFileCollectorError("Proton job was cancelled")
            final_url = str(final_url)
            if not temporary_path.is_file():
                raise ProtonFileCollectorError("Downloader did not create a file")
            actual_size = temporary_path.stat().st_size
            if actual_size <= 0 or actual_size > MAX_FILE_BYTES:
                raise ProtonFileCollectorError("Downloaded file has an invalid size")
            if int(reported_size) != actual_size:
                raise ProtonFileCollectorError("Downloaded file size does not match the download event")
            filename = _filename_from_url(final_url)
            if Path(filename).suffix.lower() not in EXTENSION_TYPES:
                raise ProtonFileCollectorError("Downloaded filename has no supported extension")
            if not content_type:
                content_type = mimetypes.guess_type(filename)[0]
            if not content_type:
                raise ProtonFileCollectorError("Downloaded file MIME type is missing")
            normalized_mime = content_type.split(";", 1)[0].strip().lower()
            if normalized_mime not in MIME_TYPES:
                raise ProtonFileCollectorError(f"Downloaded file MIME type is not supported: {normalized_mime}")
            header = temporary_path.read_bytes()[:512]
            file_type = detect_file_type(filename, content_type, header)
            digest = _sha256(temporary_path)
            with _exclusive_lock(_content_lock_path(root, digest), key=digest):
                duplicate = _existing_digest_record(root, digest)
                if duplicate and duplicate.get("job_id") != job_id:
                    _cleanup_job_temporary(root, job_id)
                    _job_update(
                        root,
                        job,
                        status="duplicate",
                        processing_status="duplicate",
                        sha256=digest,
                        duplicate_of=duplicate.get("job_id"),
                        manifest_path=duplicate.get("manifest_path"),
                        source_path=duplicate.get("source_path"),
                        worker_pid=None,
                    )
                    return _record_result(job, status="duplicate")
                if _cancel_requested(root, job_id):
                    raise ProtonFileCollectorError("Proton job was cancelled")
                destination = _stage_download(root, temporary_path, filename, file_type, digest)
            temporary_path.unlink(missing_ok=True)
            temporary_path = None
            if _cancel_requested(root, job_id):
                raise ProtonFileCollectorError("Proton job was cancelled")
            document_id = digest[:16]
            manifest_path = root / "state" / "manifests" / f"{document_id}.json"
            manifest = {
                "job_id": job_id,
                "document_id": document_id,
                "source_type": "proton_file",
                "capture_source": CAPTURE_SOURCE,
                "canonical_url": job["canonical_url"],
                "final_url": final_url,
                "capture_context": job.get("capture_context"),
                "captured_at": job.get("created_at"),
                "completed_at": _now(),
                "file_type": file_type,
                "mime_type": content_type.split(";", 1)[0].strip().lower(),
                "filename": Path(destination).name,
                "size_bytes": actual_size,
                "sha256": digest,
                "source_path": str(destination),
                "status": "completed",
                "processing_status": "pending",
            }
            _atomic_write_json(manifest_path, manifest)
            _job_update(
                root,
                job,
                status="completed",
                processing_status="pending",
                document_id=document_id,
                sha256=digest,
                file_type=file_type,
                mime_type=manifest["mime_type"],
                size_bytes=actual_size,
                source_path=str(destination),
                manifest_path=str(manifest_path),
                worker_pid=None,
            )
            indexed, intake_error = _run_knowledge_intake(root, destination)
            manifest = _load_json(manifest_path)
            if manifest is None:
                raise ProtonFileCollectorError("Knowledge-layer intake removed the document manifest")
            manifest["processing_status"] = "completed" if indexed else "failed"
            _atomic_write_json(manifest_path, manifest)
            if not indexed:
                _job_update(
                    root,
                    job,
                    processing_status="failed",
                    error=intake_error or "Knowledge-layer intake failed",
                )
            else:
                _job_update(root, job, processing_status="completed", error=None)
            _cancel_path(root, job_id).unlink(missing_ok=True)
            return _record_result({**job, **manifest}, status="completed")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
            if destination and destination.exists():
                destination.unlink(missing_ok=True)
            _cleanup_job_temporary(root, job_id)
            cancelled = _cancel_requested(root, job_id)
            _cancel_path(root, job_id).unlink(missing_ok=True)
            _job_update(
                root,
                job,
                status="cancelled" if cancelled else "failed",
                processing_status="cancelled" if cancelled else "failed",
                worker_pid=None,
                error=error,
            )
            return _record_result(job, status="cancelled" if cancelled else "failed") | {"error": error}


def recover_stale_proton_jobs(root: Path = KNOWLEDGE_ROOT) -> list[str]:
    """Mark abandoned in-progress jobs failed; stale jobs are never retried."""
    root = Path(root)
    recovered: list[str] = []
    for job_path in sorted((root / "state" / "jobs").glob("proton-*.json")):
        job = _load_json(job_path)
        if not job or job.get("status") != "in_progress":
            continue
        try:
            age = time.time() - datetime.fromisoformat(str(job.get("updated_at"))).timestamp()
        except (TypeError, ValueError, OverflowError):
            age = STALE_JOB_TIMEOUT + 1
        pid = int(job.get("worker_pid") or 0)
        if age <= STALE_JOB_TIMEOUT and _pid_exists(pid):
            continue
        job_id = str(job.get("job_id") or job_path.stem)
        try:
            with _job_lock(root, job_id):
                current = _load_json(job_path) or job
                if current.get("status") == "in_progress":
                    _job_update(
                        root,
                        current,
                        status="failed",
                        processing_status="failed",
                        worker_pid=None,
                        error="Stale Proton worker recovered; job was not retried",
                    )
                    _cleanup_job_temporary(root, job_id)
                    _cancel_path(root, job_id).unlink(missing_ok=True)
                    recovered.append(job_id)
        except ProtonFileCollectorError:
            continue
    return recovered


def cancel_proton_job(job_id: str, *, root: Path = KNOWLEDGE_ROOT) -> dict[str, Any]:
    """Cancel a queued job, or request cancellation of an active worker."""
    root = Path(root)
    job_path = _job_path(root, job_id)
    job = _load_json(job_path)
    if not job:
        return {"status": "failed", "archived": False, "indexed": False, "error": "Proton job not found"}
    if job.get("status") == "in_progress":
        _atomic_write_json(
            _cancel_path(root, job_id),
            {"job_id": job_id, "requested_at": _now(), "pid": os.getpid()},
        )
        job["cancel_requested"] = True
        return _record_result(job, status="in_progress")
    try:
        with _job_lock(root, job_id):
            job = _load_json(job_path) or job
            if job.get("status") == "queued":
                _cleanup_job_temporary(root, job_id)
                _job_update(root, job, status="cancelled", processing_status="cancelled", cancelled_at=_now())
            return _record_result(job)
    except ProtonFileCollectorError as exc:
        return _record_result(job, status="in_progress") | {"error": str(exc)}


def run_proton_worker(
    *,
    root: Path = KNOWLEDGE_ROOT,
    job_id: str | None = None,
    downloader: Callable[..., tuple[str, str | None, int]] | None = None,
) -> dict[str, Any]:
    """Local worker entry point for one job or the current queued Proton batch."""
    root = Path(root)
    recovered = recover_stale_proton_jobs(root)
    if job_id:
        result = process_proton_job(job_id, root=root, downloader=downloader)
        notify_indexed_proton_jobs(root)
        return result
    results = []
    for path in sorted((root / "state" / "jobs").glob("proton-*.json")):
        job = _load_json(path)
        if job and job.get("status") == "queued":
            results.append(process_proton_job(str(job["job_id"]), root=root, downloader=downloader))
    notify_indexed_proton_jobs(root)
    return {
        "status": "completed"
        if results and all(item.get("status") == "completed" for item in results)
        else "failed"
        if results or recovered
        else "queued",
        "processed": len(results),
        "recovered": recovered,
        "jobs": results,
    }


process_queued_proton_jobs = run_proton_worker
cancel_proton_file = cancel_proton_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Process queued Proton intake jobs once")
    parser.add_argument("--root", type=Path, default=KNOWLEDGE_ROOT)
    parser.add_argument("--job-id")
    parser.add_argument("--cancel")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.cancel:
        print(json.dumps(cancel_proton_job(args.cancel, root=args.root), indent=2))
    elif args.watch:
        while True:
            run_proton_worker(root=args.root, job_id=args.job_id)
            time.sleep(2)
    else:
        print(json.dumps(run_proton_worker(root=args.root, job_id=args.job_id), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
