"""Profile-owned x402 Watch Telegram control router."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path("/home/hans/.hve-knowledge/venv/bin/python3")
LOGGER = logging.getLogger(__name__)


def _route_in_repository(*, platform: str, source: dict, text: str) -> str | None:
    payload = json.dumps({"platform": platform, "source": source, "text": text})
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPOSITORY_ROOT)
    completed = subprocess.run(
        [str(PYTHON), "-m", "tools.x402_telegram_router"],
        cwd=REPOSITORY_ROOT,
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        env=env,
        timeout=120,
    )
    result = json.loads(completed.stdout)
    reply = result.get("reply")
    return reply if isinstance(reply, str) else None


async def _consume(*, platform: str, source: dict, text: str, **_: object) -> dict[str, str] | None:
    if platform != "telegram":
        return None
    try:
        reply = await asyncio.to_thread(
            _route_in_repository,
            text=text,
            platform=platform,
            source=source,
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        LOGGER.error("x402 Telegram control routing failed closed: %s", exc)
        return {
            "action": "handled",
            "reply": "x402 Watch control routing is unavailable; no action was taken.",
        }
    if reply is None:
        return None
    return {"action": "handled", "reply": reply}


def register(ctx) -> None:
    ctx.register_hook("post_gateway_admission", _consume)
