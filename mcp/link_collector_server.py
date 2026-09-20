#!/home/hans/.hermes-mcp-venv/bin/python

from __future__ import annotations

import sys
from pathlib import Path

from fastmcp import FastMCP


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.link_collector import archive_link as collect_link, archive_youtube as collect_youtube  # noqa: E402
from tools.pdf_collector import archive_pdf as collect_pdf  # noqa: E402
from tools.proton_file_collector import archive_proton_file as collect_proton_file  # noqa: E402


mcp = FastMCP(
    "HVE-Librarian Knowledge Collector",
    instructions=(
            "This server exposes restricted tools for archiving public web links, "
            "YouTube transcripts, and approved PDF attachments into the local HVE "
            "knowledge library, including Proton-hosted files. Preserve provenance "
            "and do not modify originals. Proton results with status queued, "
            "in_progress, already_queued, duplicate, or failed are terminal "
            "responses; the agent must not retry them, even if the result is queued. "
            "Call archive_proton_file at most once per user request. Preserve the "
            "complete Proton URL, including its # access fragment."
    ),
)


@mcp.tool()
def archive_link(url: str, capture_context: str | None = None) -> dict:
    """Archive one public HTTP(S) URL; YouTube URLs also archive their transcript."""
    try:
        return collect_link(url, capture_context)
    except Exception as exc:
        return {
            "status": "internal_error",
            "archived": False,
            "fetched": False,
            "extracted": False,
            "indexed": False,
            "duplicate": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool()
def archive_youtube(url: str, capture_context: str | None = None) -> dict:
    """Archive a YouTube URL, transcript, provenance metadata, and indexable chunks."""
    try:
        return collect_youtube(url, capture_context)
    except Exception as exc:
        return {
            "status": "internal_error",
            "archived": False,
            "transcript_archived": False,
            "indexed": False,
            "duplicate": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool()
def archive_pdf(pdf_path: str, capture_context: str | None = None) -> dict:
    """Archive one approved PDF attachment into the durable HVE knowledge library."""
    try:
        return collect_pdf(pdf_path, capture_context)
    except Exception as exc:
        return {
            "status": "internal_error",
            "archived": False,
            "indexed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


@mcp.tool()
def archive_proton_file(url: str, capture_context: str | None = None) -> dict:
    """Queue one approved Proton file for the local one-shot intake worker."""
    try:
        return collect_proton_file(url, capture_context)
    except Exception as exc:
        return {
            "status": "failed",
            "archived": False,
            "indexed": False,
            "retryable": False,
            "agent_action": "Do not call archive_proton_file again for this URL in this turn.",
            "error": f"{type(exc).__name__}: {exc}",
        }


if __name__ == "__main__":
    mcp.run(transport="stdio")
