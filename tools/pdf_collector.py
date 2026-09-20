from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.knowledge_layer_client import KNOWLEDGE_ROOT, cli_command, cli_environment


HERMES_ROOT = Path.home() / ".hermes"
LEGACY_ATTACHMENT_ROOT = HERMES_ROOT / "cache" / "documents"
PROFILE_ROOT = HERMES_ROOT / "profiles"
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_CONTEXT_LENGTH = 20_000
CAPTURE_SOURCE = "hve_librarian"


class PdfCollectorError(RuntimeError):
    pass


def _sanitize_context(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned[:MAX_CONTEXT_LENGTH] or None


def _allowed_attachment_roots() -> tuple[Path, ...]:
    roots = [LEGACY_ATTACHMENT_ROOT]
    if PROFILE_ROOT.is_dir():
        roots.extend(
            profile / "cache" / "documents"
            for profile in PROFILE_ROOT.iterdir()
            if profile.is_dir()
        )
    return tuple(root.resolve() for root in roots)


def _safe_attachment_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PdfCollectorError(f"Attachment is not readable: {exc}") from exc

    if not any(
        resolved == root or root in resolved.parents
        for root in _allowed_attachment_roots()
    ):
        raise PdfCollectorError("PDF must come from an approved Hermes attachment cache")

    if resolved.suffix.lower() != ".pdf":
        raise PdfCollectorError("Only PDF attachments are accepted")
    if resolved.stat().st_size > MAX_PDF_BYTES:
        raise PdfCollectorError("PDF exceeds the 30 MB intake limit")
    return resolved


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    stem = Path(cleaned or "document").stem
    return f"{stem[:176]}.pdf"


def _document_id(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def _record_capture(
    document_id: str,
    capture_context: str | None,
    *,
    root: Path = KNOWLEDGE_ROOT,
) -> dict[str, Any]:
    manifest_path = root / "state" / "manifests" / f"{document_id}.json"
    if not manifest_path.is_file():
        return {"status": "indexed", "document_id": document_id}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_type"] = "pdf_document"
    manifest["capture_source"] = CAPTURE_SOURCE
    captured_at = datetime.now(timezone.utc).isoformat()
    manifest["capture_context"] = capture_context
    manifest["captured_at"] = manifest.get("captured_at") or captured_at
    captures = manifest.get("captures")
    if not isinstance(captures, list):
        captures = []
    captures.append(
        {
            "captured_at": captured_at,
            "capture_context": capture_context,
            "capture_source": CAPTURE_SOURCE,
        }
    )
    manifest["captures"] = captures
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "indexed" if manifest.get("status") == "indexed" else manifest.get("status", "processed"),
        "document_id": document_id,
        "title": manifest.get("title"),
        "source_path": manifest.get("source_path"),
        "indexed": manifest.get("status") == "indexed",
        "chunk_count": manifest.get("chunk_count", 0),
    }


def archive_pdf(
    pdf_path: str,
    capture_context: str | None = None,
    *,
    root: Path = KNOWLEDGE_ROOT,
) -> dict[str, Any]:
    source = _safe_attachment_path(pdf_path)
    context = _sanitize_context(capture_context)
    telegram_queue = root / "intake" / "telegram"
    telegram_queue.mkdir(parents=True, exist_ok=True)
    destination = telegram_queue / _safe_filename(source.name)
    if destination.exists():
        destination = telegram_queue / f"{source.stem}-{_document_id(source)}.pdf"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=telegram_queue,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        shutil.copy2(source, temporary_path)
        temporary_path.replace(destination)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

    output = ""
    error = ""
    for attempt in range(30):
        try:
            result = subprocess.run(
                cli_command("intake", "--pdf", str(destination), root=root),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=cli_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "status": "failed",
                "archived": False,
                "indexed": False,
                "document_id": _document_id(source),
                "error": f"{type(exc).__name__}: PDF intake pipeline failed",
            }
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if "SKIPPED reason=another library worker is active" not in output:
            break
        if attempt < 29:
            time.sleep(1)
    document_id = _document_id(source)
    if result.returncode != 0 or "SKIPPED reason=another library worker is active" in output:
        return {
            "status": "queued",
            "archived": True,
            "indexed": False,
            "document_id": document_id,
            "retryable": True,
            "pipeline_output": output[-2000:],
            "error": error or "PDF intake pipeline remained busy after retry window",
        }
    record = _record_capture(document_id, context, root=root)
    record.update(
        {
            "archived": True,
            "indexed": record.get("indexed", False),
            "pipeline_output": output[-2000:],
        }
    )
    return record
