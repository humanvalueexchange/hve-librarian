#!/home/hans/.hermes-mcp-venv/bin/python

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.librarian_comms import (  # noqa: E402
    LibrarianCommsError,
    close_issue,
    comment_on_commit,
    comment_on_issue,
    create_enhancement_issue,
    publish_communication,
    update_issue,
    write_communication,
)


mcp = FastMCP(
    "HVE-Librarian Communications",
    instructions=(
        "Governed write access for HVE-Librarian communications. Write only new "
        "Markdown posts under agent-communications using the required filename "
        "format. Publishing, issue creation, issue comments, issue lifecycle "
        "changes, and issue updates require explicit Hans Westphal approval and "
        "use the fixed HVE operations repository."
    ),
)


@mcp.tool()
def write_agent_communication(
    filename: str,
    content: str,
    approved_by: str,
) -> dict[str, str]:
    """Write a new HVE communication Markdown file without overwriting existing files."""
    try:
        return write_communication(filename, content, approved_by=approved_by)
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def publish_agent_communication(
    filename: str,
    commit_message: str,
    approved_by: str,
) -> dict[str, str]:
    """Commit and push one approved communication file to GitHub."""
    try:
        return publish_communication(filename, commit_message, approved_by=approved_by)
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def create_enhancement_backlog_issue(
    title: str,
    body: str,
    approved_by: str,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create an approved enhancement issue in the HVE operations repository."""
    try:
        return create_enhancement_issue(
            title,
            body,
            labels=labels,
            approved_by=approved_by,
        )
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def comment_on_github_issue(
    issue_number: int,
    body: str,
    approved_by: str,
) -> dict[str, str]:
    """Add an approved append-only comment to an HVE GitHub issue."""
    try:
        return comment_on_issue(issue_number, body, approved_by=approved_by)
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def comment_on_github_commit(
    commit_sha: str,
    body: str,
    approved_by: str,
) -> dict[str, str]:
    """Add an approved append-only pointer comment to a GitHub commit."""
    try:
        return comment_on_commit(commit_sha, body, approved_by=approved_by)
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def close_github_issue(
    issue_number: int,
    approved_by: str,
    final_note: str | None = None,
    reopen: bool = False,
) -> dict[str, str]:
    """Close or reopen an approved HVE GitHub issue, optionally adding a final note."""
    try:
        return close_issue(
            issue_number,
            final_note,
            reopen=reopen,
            approved_by=approved_by,
        )
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


@mcp.tool()
def update_github_issue(
    issue_number: int,
    approved_by: str,
    title: str | None = None,
    body: str | None = None,
) -> dict[str, str]:
    """Update approved title and/or body fields on an HVE GitHub issue."""
    try:
        return update_issue(
            issue_number,
            title=title,
            body=body,
            approved_by=approved_by,
        )
    except LibrarianCommsError as exc:
        return {"status": "rejected", "error": str(exc)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
