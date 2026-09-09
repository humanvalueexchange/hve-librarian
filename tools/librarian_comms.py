from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path("/home/hans/humanvalueexchange")
COMMUNICATIONS_ROOT = REPO_ROOT / "agent-communications"
GITHUB_REPOSITORY = "HansHWestphal/hve-knowledge-and-operations"
FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}-hve-[a-z0-9]+(?:-[a-z0-9]+)*-v\d+\.\d+\.md$"
)
AUTHORIZED_APPROVER = "Hans Westphal"
MAX_FILE_BYTES = 100_000
MAX_ISSUE_TITLE_LENGTH = 200
MAX_ISSUE_BODY_LENGTH = 50_000
ALLOWED_ISSUE_LABELS = {"enhancement", "documentation", "proposal"}
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class LibrarianCommsError(ValueError):
    pass


def _run(command: list[str], *, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LibrarianCommsError(f"Command failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LibrarianCommsError(detail or f"Command exited with status {result.returncode}")
    return result.stdout.strip()


def _approved(filename: str, content: str, approved_by: str) -> tuple[Path, str]:
    normalized = filename.strip()
    if not FILENAME_RE.fullmatch(normalized):
        raise LibrarianCommsError(
            "Filename must match YYYY-MM-DD-hve-topic-slug-vX.X.md."
        )
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    if not content.strip():
        raise LibrarianCommsError("Communication content must not be empty.")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise LibrarianCommsError("Communication exceeds the 100 KB limit.")
    return COMMUNICATIONS_ROOT / normalized, normalized


def _approved_path(filename: str, approved_by: str) -> tuple[Path, str]:
    normalized = filename.strip()
    if not FILENAME_RE.fullmatch(normalized):
        raise LibrarianCommsError(
            "Filename must match YYYY-MM-DD-hve-topic-slug-vX.X.md."
        )
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    return COMMUNICATIONS_ROOT / normalized, normalized


def write_communication(
    filename: str,
    content: str,
    *,
    approved_by: str,
) -> dict[str, str]:
    path, normalized = _approved(filename, content, approved_by)
    COMMUNICATIONS_ROOT.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise LibrarianCommsError(f"Communication already exists: {normalized}")
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return {"status": "written", "filename": normalized, "path": str(path)}


def publish_communication(
    filename: str,
    commit_message: str,
    *,
    approved_by: str,
) -> dict[str, str]:
    path, normalized = _approved_path(filename, approved_by)
    message = commit_message.strip()
    if not message:
        raise LibrarianCommsError("Commit message must not be empty.")
    if not path.is_file():
        raise LibrarianCommsError(f"Communication does not exist: {normalized}")

    _run(["git", "add", "--", str(path)])
    commit = _run(
        [
            "git",
            "commit",
            "--only",
            "-m",
            message,
            "-m",
            "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
            "--",
            str(path),
        ]
    )
    branch = _run(["git", "branch", "--show-current"])
    if not branch:
        raise LibrarianCommsError("Cannot publish from a detached HEAD.")
    _run(["git", "push", "origin", "HEAD"])
    commit_hash = _run(["git", "rev-parse", "--short", "HEAD"])
    return {
        "status": "published",
        "filename": normalized,
        "commit": commit_hash,
        "branch": branch,
        "repository": GITHUB_REPOSITORY,
        "details": commit.splitlines()[-1] if commit else "",
    }


def create_enhancement_issue(
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    approved_by: str,
) -> dict[str, str]:
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    clean_title = title.strip()
    clean_body = body.strip()
    if not clean_title or len(clean_title) > MAX_ISSUE_TITLE_LENGTH:
        raise LibrarianCommsError("Issue title must be present and at most 200 characters.")
    if not clean_body or len(clean_body) > MAX_ISSUE_BODY_LENGTH:
        raise LibrarianCommsError("Issue body must be present and at most 50,000 characters.")
    requested_labels = labels or ["enhancement"]
    if any(label not in ALLOWED_ISSUE_LABELS for label in requested_labels):
        raise LibrarianCommsError(
            f"Labels must be from: {', '.join(sorted(ALLOWED_ISSUE_LABELS))}."
        )
    command = [
        "gh",
        "issue",
        "create",
        "--repo",
        GITHUB_REPOSITORY,
        "--title",
        clean_title,
        "--body",
        clean_body,
    ]
    for label in requested_labels:
        command.extend(["--label", label])
    issue_url = _run(command)
    return {
        "status": "created",
        "issue_url": issue_url,
        "repository": GITHUB_REPOSITORY,
    }


def _issue_number(issue_number: int) -> str:
    try:
        number = int(issue_number)
    except (TypeError, ValueError) as exc:
        raise LibrarianCommsError("Issue number must be a positive integer.") from exc
    if number <= 0:
        raise LibrarianCommsError("Issue number must be a positive integer.")
    return str(number)


def comment_on_issue(
    issue_number: int,
    body: str,
    *,
    approved_by: str,
) -> dict[str, str]:
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    number = _issue_number(issue_number)
    clean_body = body.strip()
    if not clean_body or len(clean_body) > MAX_ISSUE_BODY_LENGTH:
        raise LibrarianCommsError("Comment must be present and at most 50,000 characters.")
    comment_url = _run(
        [
            "gh",
            "issue",
            "comment",
            number,
            "--repo",
            GITHUB_REPOSITORY,
            "--body",
            clean_body,
        ]
    )
    return {
        "status": "commented",
        "issue_number": number,
        "comment_url": comment_url,
        "repository": GITHUB_REPOSITORY,
    }


def comment_on_commit(
    commit_sha: str,
    body: str,
    *,
    approved_by: str,
) -> dict[str, str]:
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    sha = commit_sha.strip()
    if not COMMIT_SHA_RE.fullmatch(sha):
        raise LibrarianCommsError("Commit must be a 7-40 character hexadecimal SHA.")
    clean_body = body.strip()
    if not clean_body or len(clean_body) > MAX_ISSUE_BODY_LENGTH:
        raise LibrarianCommsError("Comment must be present and at most 50,000 characters.")
    comment_url = _run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{GITHUB_REPOSITORY}/commits/{sha}/comments",
            "--field",
            f"body={clean_body}",
            "--jq",
            ".html_url",
        ]
    )
    return {
        "status": "commented",
        "commit": sha,
        "comment_url": comment_url,
        "repository": GITHUB_REPOSITORY,
    }


def close_issue(
    issue_number: int,
    final_note: str | None = None,
    *,
    reopen: bool = False,
    approved_by: str,
) -> dict[str, str]:
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    number = _issue_number(issue_number)
    if final_note is not None:
        comment_on_issue(number, final_note, approved_by=approved_by)
    action = "reopen" if reopen else "close"
    _run(["gh", "issue", action, number, "--repo", GITHUB_REPOSITORY])
    return {
        "status": "reopened" if reopen else "closed",
        "issue_number": number,
        "repository": GITHUB_REPOSITORY,
    }


def update_issue(
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    approved_by: str,
) -> dict[str, str]:
    if approved_by.strip() != AUTHORIZED_APPROVER:
        raise LibrarianCommsError("Explicit approval by Hans Westphal is required.")
    number = _issue_number(issue_number)
    if title is None and body is None:
        raise LibrarianCommsError("At least one of title or body must be provided.")
    command = ["gh", "issue", "edit", number, "--repo", GITHUB_REPOSITORY]
    if title is not None:
        clean_title = title.strip()
        if not clean_title or len(clean_title) > MAX_ISSUE_TITLE_LENGTH:
            raise LibrarianCommsError("Issue title must be present and at most 200 characters.")
        command.extend(["--title", clean_title])
    if body is not None:
        clean_body = body.strip()
        if not clean_body or len(clean_body) > MAX_ISSUE_BODY_LENGTH:
            raise LibrarianCommsError("Issue body must be present and at most 50,000 characters.")
        command.extend(["--body", clean_body])
    _run(command)
    return {
        "status": "updated",
        "issue_number": number,
        "repository": GITHUB_REPOSITORY,
    }
