from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from tools.link_collector import (
    LinkCollectorError,
    _extract_html,
    canonicalize_url,
    fetch_public_html,
)


DEFAULT_RUN_ROOT = Path("/hve-library/private/x402-watch")
DEFAULT_MODEL = "qwen3.8-hermes:27b-128k"
DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:11434/v1"
MIN_SYNTHESIS_CONTEXT = 65_536
DEFAULT_SYNTHESIS_MAX_TOKENS = 8_192
MAX_DISCOVERED_LINKS = 12
MAX_EVIDENCE_CHARS = 2400
DISCOVERY_KEYWORDS = (
    "x402",
    "agent commerce",
    "agentic commerce",
    "stablecoin",
    "genius act",
    "stripe",
    "openrouter",
    "card network",
    "payments",
)
LANE_KEYWORDS = {
    "x402-agent-commerce": ("x402", "agent commerce", "agentic commerce"),
    "genius-act": ("genius act", "stablecoin regulation"),
    "stablecoin-card-volume": ("stablecoin", "card network", "payment volume"),
    "stripe-openrouter": ("stripe", "openrouter"),
}
BLOCKED_CLAIM_TERMS = re.compile(
    r"\b(?:buy|sell|invest(?:ment|ing)?|investment advice|submit payment|pay with|"
    r"payment recommendation|hve policy|hve commitment)\b",
    re.IGNORECASE,
)


class SynthesisError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _run_id(timestamp: str) -> str:
    return timestamp.replace("-", "").replace("+00:00", "Z").replace(":", "")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _excerpt(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:MAX_EVIDENCE_CHARS]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value for key, value in attrs}
        self._href = values.get("href")
        self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        label = re.sub(r"\s+", " ", html.unescape("".join(self._parts))).strip()
        self.links.append((self._href, label))
        self._href = None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)


class _PublicationDateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta" or self.value:
            return
        values = {key.lower(): value for key, value in attrs}
        marker = (values.get("property") or values.get("name") or "").lower()
        if marker in {
            "article:published_time",
            "datepublished",
            "datepublishedtime",
            "publication_date",
            "published_time",
        }:
            content = (values.get("content") or "").strip()
            if content:
                self.value = content


def publication_date(body: bytes, charset: str = "utf-8") -> str | None:
    parser = _PublicationDateParser()
    parser.feed(body.decode(charset, errors="replace"))
    return parser.value


def classify_lane(url: str, title: str | None, excerpt: str) -> str:
    haystack = f"{url} {title or ''} {excerpt}".lower()
    for lane, keywords in LANE_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return lane
    return "unclassified"


def discover_links(
    base_url: str,
    body: bytes,
    charset: str = "utf-8",
    *,
    limit: int = MAX_DISCOVERED_LINKS,
) -> list[str]:
    decoded = body.decode(charset, errors="replace")
    parser = _LinkParser()
    parser.feed(decoded)
    selected: list[str] = []
    seen: set[str] = set()
    for href, label in parser.links:
        candidate = urllib.parse.urljoin(base_url, href)
        try:
            canonical = canonicalize_url(candidate)
        except LinkCollectorError:
            continue
        haystack = f"{label} {canonical}".lower()
        if not any(keyword in haystack for keyword in DISCOVERY_KEYWORDS):
            continue
        if canonical == canonicalize_url(base_url) or canonical in seen:
            continue
        seen.add(canonical)
        selected.append(canonical)
        if len(selected) >= limit:
            break
    return selected


def _resource_record(
    *,
    resource_id: str,
    lane: str,
    url: str,
    fetched_at: str,
    fetcher: Callable[[str], dict[str, Any]],
    discovery_only: bool = False,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "resource_id": resource_id,
        "lane": lane,
        "requested_url": url,
        "retrieved_at": fetched_at,
        "discovery_only": discovery_only,
    }
    try:
        fetched = fetcher(url)
        extracted = _extract_html(
            bytes(fetched["html_bytes"]),
            str(fetched.get("charset") or "utf-8"),
        )
        text = str(extracted.get("text") or "")
        record.update(
            {
                "status": "fetched" if text else "fetched_empty",
                "canonical_url": str(fetched.get("final_url") or url),
                "http_status": int(fetched.get("http_status") or 0),
                "content_type": fetched.get("content_type"),
                "title": extracted.get("title"),
                "publisher": extracted.get("author"),
                "publication_date": publication_date(
                    bytes(fetched["html_bytes"]),
                    str(fetched.get("charset") or "utf-8"),
                ),
                "content_sha256": _sha256(text),
                "evidence_excerpt": _excerpt(text),
                "lane": classify_lane(
                    str(fetched.get("final_url") or url),
                    extracted.get("title"),
                    text,
                ),
                "discovered_urls": discover_links(
                    str(fetched.get("final_url") or url),
                    bytes(fetched["html_bytes"]),
                    str(fetched.get("charset") or "utf-8"),
                ),
            }
        )
    except (LinkCollectorError, OSError, ValueError, KeyError, UnicodeError) as exc:
        record.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "discovered_urls": [],
            }
        )
    return record


def validate_claims(
    claims: list[dict[str, Any]],
    source_ids: set[str],
    *,
    run_status: str,
    has_complete_baseline: bool,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    for index, claim in enumerate(claims):
        text = str(claim.get("claim") or "").strip()
        cited = claim.get("source_ids")
        cited_ids = cited if isinstance(cited, list) else []
        if not text:
            errors.append({"claim_index": index, "error": "claim is empty"})
        if not cited_ids:
            errors.append({"claim_index": index, "error": "claim has no source_ids"})
        unknown = sorted(set(map(str, cited_ids)) - source_ids)
        if unknown:
            errors.append(
                {"claim_index": index, "error": "unknown source IDs", "source_ids": unknown}
            )
        if BLOCKED_CLAIM_TERMS.search(text):
            errors.append(
                {"claim_index": index, "error": "claim contains blocked policy/payment/investment language"}
            )
        if "no material change" in text.lower() and (
            run_status != "complete" or not has_complete_baseline
        ):
            errors.append(
                {
                    "claim_index": index,
                    "error": "no material change requires complete current and baseline runs",
                }
            )
    return {"valid": not errors, "errors": errors}


def _synthesis_prompt(
    evidence: dict[str, Any],
    *,
    has_complete_baseline: bool,
) -> str:
    return (
        "You are the local HVE-Librarian research synthesizer. Produce only one "
        "JSON object for a private x402 Watch draft. Do not browse, publish, "
        "recommend payments or investments, access Bitcoin or Lightning data, "
        "or invent facts. Treat aggregators as discovery aids and cite the "
        "underlying source IDs supplied in the evidence.\n\n"
        "Required JSON shape:\n"
        '{"title": string, "executive_summary": string, '
        '"claims": [{"claim": string, "source_ids": [string], '
        '"support": string}], "coverage": string, '
        '"open_questions": [string], "editorial_warnings": [string]}\n\n'
        f"Complete prior baseline available: {has_complete_baseline}\n"
        "A claim saying 'no material change' is allowed only when the current "
        "run is complete and a complete baseline is available. Keep publication "
        "date and retrieval date distinct. Return valid JSON only.\n\n"
        "Evidence packet:\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


def _extract_model_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SynthesisError("Model response did not contain message content") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    raise SynthesisError("Model message content was not text")


def _request_synthesis(
    prompt: str,
    *,
    base_url: str,
    model: str,
    context_length: int,
    max_tokens: int = DEFAULT_SYNTHESIS_MAX_TOKENS,
    requester: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if context_length < MIN_SYNTHESIS_CONTEXT:
        raise SynthesisError("Synthesis context must be at least 65536 tokens")
    requester = requester or urllib.request.urlopen
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return valid JSON only. Do not use markdown fences.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with requester(request, timeout=180) as response:
            body = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise SynthesisError(f"Local synthesis request failed: {exc}") from exc
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SynthesisError("Local synthesis returned invalid JSON envelope") from exc
    if not isinstance(decoded, dict):
        raise SynthesisError("Local synthesis response envelope was not an object")
    return decoded


def synthesize_evidence(
    evidence: dict[str, Any],
    *,
    has_complete_baseline: bool = False,
    base_url: str = DEFAULT_MODEL_BASE_URL,
    model: str = DEFAULT_MODEL,
    context_length: int = 131_072,
    requester: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    response = _request_synthesis(
        _synthesis_prompt(evidence, has_complete_baseline=has_complete_baseline),
        base_url=base_url,
        model=model,
        context_length=context_length,
        requester=requester,
    )
    try:
        draft = json.loads(_extract_model_content(response))
    except (json.JSONDecodeError, TypeError) as exc:
        raise SynthesisError("Local synthesis content was not valid JSON") from exc
    if not isinstance(draft, dict):
        raise SynthesisError("Local synthesis content was not an object")
    claims = draft.get("claims")
    if not isinstance(claims, list):
        raise SynthesisError("Synthesis draft did not contain a claims list")
    source_ids = {
        str(source.get("source_id"))
        for source in evidence.get("sources", [])
        if isinstance(source, dict) and source.get("status") in {"fetched", "fetched_empty"}
    }
    validation = validate_claims(
        [claim for claim in claims if isinstance(claim, dict)],
        source_ids,
        run_status=str(evidence.get("status") or "failed"),
        has_complete_baseline=has_complete_baseline,
    )
    if not validation["valid"]:
        raise SynthesisError(
            "Synthesis draft failed claim validation: "
            + json.dumps(validation["errors"], ensure_ascii=False)
        )
    required_strings = ("title", "executive_summary", "coverage")
    if any(not isinstance(draft.get(field), str) for field in required_strings):
        raise SynthesisError("Synthesis draft omitted a required text field")
    draft["validation"] = validation
    draft["model"] = model
    draft["context_length"] = context_length
    return draft


def render_private_brief(
    draft: dict[str, Any],
    *,
    run_id: str,
    run_status: str,
) -> str:
    lines = [
        f"# x402 Watch — {draft['title']}",
        "",
        f"- **Run:** `{run_id}`",
        f"- **Coverage:** `{run_status}` — {draft['coverage']}",
        f"- **Model:** `{draft['model']}` at `{draft['context_length']}` context",
        "",
        "## Executive summary",
        "",
        draft["executive_summary"],
        "",
        "## Claims",
        "",
    ]
    for index, claim in enumerate(draft["claims"], start=1):
        sources = ", ".join(f"`{source_id}`" for source_id in claim["source_ids"])
        lines.extend(
            [
                f"{index}. {claim['claim']}",
                f"   - Sources: {sources}",
                f"   - Support: {claim.get('support', '')}",
            ]
        )
    lines.extend(["", "## Open questions", ""])
    lines.extend(f"- {item}" for item in draft.get("open_questions", []))
    lines.extend(["", "## Editorial warnings", ""])
    lines.extend(f"- {item}" for item in draft.get("editorial_warnings", []))
    lines.append("")
    return "\n".join(lines)


def synthesize_run(
    *,
    root: Path = DEFAULT_RUN_ROOT,
    run_id: str,
    has_complete_baseline: bool = False,
    base_url: str = DEFAULT_MODEL_BASE_URL,
    model: str = DEFAULT_MODEL,
    context_length: int = 131_072,
    requester: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    run_dir = root / "runs" / run_id
    try:
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        evidence = json.loads((run_dir / "synthesis-input.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesisError(f"Private run evidence could not be loaded: {exc}") from exc
    draft = synthesize_evidence(
        evidence,
        has_complete_baseline=has_complete_baseline,
        base_url=base_url,
        model=model,
        context_length=context_length,
        requester=requester,
    )
    synthesis = {
        "run_id": run_id,
        "run_status": run.get("status"),
        "draft": draft,
        "publication_status": "not_published",
    }
    (run_dir / "synthesis.json").write_text(
        json.dumps(synthesis, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "brief.md").write_text(
        render_private_brief(draft, run_id=run_id, run_status=str(run.get("status"))),
        encoding="utf-8",
    )
    return synthesis


def run_batch(
    *,
    root: Path = DEFAULT_RUN_ROOT,
    resources: list[dict[str, Any]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] = fetch_public_html,
    now: str | None = None,
) -> dict[str, Any]:
    started_at = now or _now()
    run_id = _run_id(started_at)
    configured = resources or [
        {
            "resource_id": "donvitocodes-ai-news",
            "lane": "ai-news-discovery",
            "url": "https://www.donvitocodes.com/ai-news",
            "discovery_only": True,
        }
    ]
    records: list[dict[str, Any]] = []
    for resource in configured:
        records.append(
            _resource_record(
                resource_id=str(resource["resource_id"]),
                lane=str(resource["lane"]),
                url=str(resource["url"]),
                fetched_at=started_at,
                fetcher=fetcher,
                discovery_only=bool(resource.get("discovery_only", False)),
            )
        )

    discovered_count = 0
    for parent in list(records):
        for offset, url in enumerate(parent.get("discovered_urls", [])):
            discovered_count += 1
            records.append(
                _resource_record(
                    resource_id=f"{parent['resource_id']}-discovered-{offset + 1}",
                    lane=str(parent["lane"]),
                    url=url,
                    fetched_at=started_at,
                    fetcher=fetcher,
                )
            )

    failed = sum(record["status"] == "failed" for record in records)
    usable = sum(record["status"] == "fetched" for record in records)
    if not records:
        status = "failed"
    elif usable == 0:
        status = "no_usable_sources"
    elif failed:
        status = "partial"
    else:
        status = "complete"
    result = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": _now(),
        "status": status,
        "source_count": len(records),
        "discovered_count": discovered_count,
        "failed_count": failed,
        "sources": records,
        "canonical_knowledge_indexed": False,
        "publication_status": "not_published",
    }
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    synthesis_input = {
        "run_id": run_id,
        "status": status,
        "sources": [
            {
                "source_id": f"{record['resource_id']}",
                "lane": record["lane"],
                "canonical_url": record.get("canonical_url", record["requested_url"]),
                "publication_date": record.get("publication_date"),
                "retrieved_at": record["retrieved_at"],
                "evidence_excerpt": record.get("evidence_excerpt"),
                "status": record["status"],
            }
            for record in records
        ],
    }
    (run_dir / "synthesis-input.json").write_text(
        json.dumps(synthesis_input, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a private bounded x402 Watch collection batch")
    parser.add_argument("--root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--synthesize", metavar="RUN_ID")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_MODEL_BASE_URL)
    parser.add_argument("--context-length", type=int, default=131_072)
    args = parser.parse_args(argv)
    if args.synthesize:
        result = synthesize_run(
            root=args.root,
            run_id=args.synthesize,
            base_url=args.base_url,
            model=args.model,
            context_length=args.context_length,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    result = run_batch(root=args.root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"complete", "partial", "no_usable_sources"} else 1


if __name__ == "__main__":
    sys.exit(main())
