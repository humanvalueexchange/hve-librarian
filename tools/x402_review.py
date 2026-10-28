from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_APPROVAL_TTL_DAYS = 7
TELEGRAM_SAFE_MESSAGE_LENGTH = 4_000
READY_PATTERN = re.compile(r"^\s*ready\s+to\s+publish\s*[.!]?\s*$", re.IGNORECASE)
YES_RESPONSES = {"yes", "y", "approve", "approved"}
NO_RESPONSES = {"no", "n", "reject", "rejected", "cancel"}


class ReviewStateError(RuntimeError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _draft_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".new",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _split_long_line(line: str, limit: int) -> list[str]:
    return [line[offset : offset + limit] for offset in range(0, len(line), limit)] or [""]


def split_telegram_messages(text: str, limit: int = TELEGRAM_SAFE_MESSAGE_LENGTH) -> list[str]:
    if limit < 128:
        raise ValueError("Telegram message limit is too small")
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    messages: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in normalized.split("\n\n"):
        pieces = _split_long_line(paragraph, limit) if len(paragraph) > limit else [paragraph]
        for piece in pieces:
            separator = 2 if current else 0
            if current and current_length + separator + len(piece) > limit:
                messages.append("\n\n".join(current))
                current = []
                current_length = 0
                separator = 0
            current.append(piece)
            current_length += separator + len(piece)
    if current:
        messages.append("\n\n".join(current))
    return messages


def _approval_prompt(run_id: str) -> str:
    return f"Do you APPROVE x402 WATCH RUN {run_id} FOR PUBLICATION? Reply YES or NO."


class ReviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "review-state.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewStateError(f"Review state could not be loaded: {exc}") from exc
        if not isinstance(value, dict):
            raise ReviewStateError("Review state must be a JSON object")
        return value

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        _atomic_write(self.path, state)
        return state

    def start_review(
        self,
        *,
        run_id: str,
        draft_text: str,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        if not run_id.strip() or not draft_text.strip():
            raise ReviewStateError("run_id and draft_text are required")
        timestamp = now or _now()
        state = {
            "run_id": run_id,
            "revision": 1,
            "draft_hash": _draft_hash(draft_text),
            "draft_text": draft_text,
            "created_at": _iso(timestamp),
            "updated_at": _iso(timestamp),
            "status": "draft",
            "approval": None,
            "publication_status": "not_published",
        }
        return self.save(state)

    def revise(
        self,
        *,
        run_id: str,
        draft_text: str,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        state = self._require_run(run_id)
        if not draft_text.strip():
            raise ReviewStateError("draft_text is required")
        timestamp = now or _now()
        state.update(
            {
                "revision": int(state.get("revision", 0)) + 1,
                "draft_hash": _draft_hash(draft_text),
                "draft_text": draft_text,
                "updated_at": _iso(timestamp),
                "status": "draft",
                "approval": None,
            }
        )
        return self.save(state)

    def request_publication(
        self,
        *,
        run_id: str,
        now: dt.datetime | None = None,
        ttl_days: int = DEFAULT_APPROVAL_TTL_DAYS,
    ) -> dict[str, Any]:
        if ttl_days <= 0:
            raise ReviewStateError("Approval TTL must be positive")
        state = self._require_run(run_id)
        timestamp = now or _now()
        expires_at = timestamp + dt.timedelta(days=ttl_days)
        state.update(
            {
                "status": "awaiting_approval",
                "updated_at": _iso(timestamp),
                "approval": {
                    "run_id": run_id,
                    "draft_hash": state["draft_hash"],
                    "requested_at": _iso(timestamp),
                    "expires_at": _iso(expires_at),
                    "prompt": _approval_prompt(run_id),
                },
            }
        )
        return self.save(state)

    def handle_operator_message(
        self,
        *,
        run_id: str,
        message: str,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        if READY_PATTERN.fullmatch(message):
            state = self.request_publication(run_id=run_id, now=now)
            return {
                "action": "approval_requested",
                "run_id": run_id,
                "prompt": format_ready_to_publish_response(state),
            }
        return self.handle_response(run_id=run_id, response=message, now=now)

    def handle_response(
        self,
        *,
        run_id: str,
        response: str,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        state = self._require_run(run_id)
        state = self._expire_if_needed(state, now=now)
        normalized = response.strip().lower()
        if state["status"] != "awaiting_approval":
            return {
                "action": "no_action",
                "status": state["status"],
                "message": "There is no active publication approval request for this run.",
            }
        if normalized in YES_RESPONSES:
            state.update({"status": "approved", "updated_at": _iso(now or _now())})
            self.save(state)
            return {
                "action": "approved",
                "run_id": run_id,
                "draft_hash": state["draft_hash"],
                "message": "Publication approved for the exact current draft.",
            }
        if normalized in NO_RESPONSES:
            state.update(
                {
                    "status": "rejected",
                    "updated_at": _iso(now or _now()),
                    "approval": None,
                }
            )
            self.save(state)
            return {
                "action": "rejected",
                "run_id": run_id,
                "message": "Publication cancelled; the private draft remains available for editing.",
            }
        return {
            "action": "clarify",
            "status": state["status"],
            "message": "Please reply YES or NO. Nothing has been published.",
        }

    def publishable(
        self,
        *,
        run_id: str,
        draft_hash: str,
        now: dt.datetime | None = None,
    ) -> bool:
        state = self._expire_if_needed(self._require_run(run_id), now=now)
        return (
            state["status"] == "approved"
            and state.get("draft_hash") == draft_hash
            and state.get("publication_status") == "not_published"
        )

    def _require_run(self, run_id: str) -> dict[str, Any]:
        state = self.load()
        if not state or state.get("run_id") != run_id:
            raise ReviewStateError(f"No active review state for run {run_id}")
        return state

    def _expire_if_needed(
        self,
        state: dict[str, Any],
        *,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        approval = state.get("approval")
        if state.get("status") != "awaiting_approval" or not isinstance(approval, dict):
            return state
        if _parse_time(str(approval["expires_at"])) <= (now or _now()):
            state.update({"status": "expired", "approval": None})
            self.save(state)
        return state


def format_review_messages(
    *,
    run_id: str,
    brief_text: str,
    coverage_status: str,
    limit: int = TELEGRAM_SAFE_MESSAGE_LENGTH,
) -> list[str]:
    header = (
        f"x402 Watch — private draft\n"
        f"Run: {run_id}\n"
        f"Coverage: {coverage_status}\n\n"
    )
    if limit <= len("[x402 Watch 999/999]\n"):
        raise ValueError("Telegram message limit is too small for review headers")
    messages = split_telegram_messages(
        header + brief_text,
        limit=limit - len("[x402 Watch 999/999]\n"),
    )
    if len(messages) > 1:
        return [
            f"[x402 Watch {index}/{len(messages)}]\n{message}"
            for index, message in enumerate(messages, start=1)
        ]
    return messages


def format_ready_to_publish_response(state: dict[str, Any]) -> str:
    approval = state.get("approval")
    if not isinstance(approval, dict):
        raise ReviewStateError("Review state has no pending approval")
    return str(approval["prompt"])
