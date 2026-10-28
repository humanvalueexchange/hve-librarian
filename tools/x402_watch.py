from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
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
    fetch_browser_rendered_html,
    fetch_public_html,
)
from tools.x402_review import ReviewStore


DEFAULT_RUN_ROOT = Path("/hve-library/private/x402-watch")
DEFAULT_MODEL = "qwen3.8-hermes:27b-128k"
DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:11434/v1"
MIN_SYNTHESIS_CONTEXT = 65_536
DEFAULT_SYNTHESIS_MAX_TOKENS = 8_192
SYNTHESIS_TIMEOUT_SECONDS = 300
MAX_DISCOVERED_LINKS = 12
BRAVE_SEARCH_RESULTS_PER_QUERY = 2
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
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
    "agent",
    "commerce",
    "oracle",
    "openai",
    "anthropic",
    "nvidia",
    "amd",
    "meta",
    "broadcom",
    "muse",
)
BRAVE_SEARCH_QUERIES = (
    "x402 protocol adoption agentic commerce",
    "agent to agent commerce payments AI agents",
)
WEALTH_TYPES = ("time", "physical", "mental", "social", "financial")
LANE_KEYWORDS = {
    "x402-agent-commerce": ("x402", "agent commerce", "agentic commerce"),
    "genius-act": ("genius act", "stablecoin regulation"),
    "stablecoin-card-volume": ("stablecoin", "card network", "payment volume"),
    "stripe-openrouter": ("stripe", "openrouter"),
}
BLOCKED_CLAIM_TERMS = re.compile(
    r"\b(?:buy|sell|investment advice|investment recommendation|submit payment|"
    r"pay with|payment recommendation|hve policy|hve commitment)\b",
    re.IGNORECASE,
)


class SynthesisError(RuntimeError):
    pass


def _configured_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    profile_root = os.environ.get("HERMES_PROFILE_ROOT", "").strip()
    env_paths = (
        (Path(profile_root) / ".env") if profile_root else None,
        Path("/home/hans/.hermes/profiles/hve-librarian/.env"),
    )
    for env_path in env_paths:
        if not env_path or not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                key, separator, candidate = line.partition("=")
                if separator and key.strip() == name:
                    return candidate.strip().strip("\"'")
        except OSError:
            continue
    return ""


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
        if fetched.get("extraction_method"):
            record["extraction_method"] = fetched["extraction_method"]
        if fetched.get("retrieval_attempts"):
            record["retrieval_attempts"] = fetched["retrieval_attempts"]
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


def fetch_x402_html(url: str) -> dict[str, Any]:
    """Fetch directly, then use the local Hermes browser for blocked/empty pages."""
    attempts: list[dict[str, str]] = []
    try:
        fetched = fetch_public_html(url)
        extracted = _extract_html(
            bytes(fetched["html_bytes"]),
            str(fetched.get("charset") or "utf-8"),
        )
        if len(str(extracted.get("text") or "").strip()) >= 400:
            fetched["extraction_method"] = "direct_http"
            fetched["retrieval_attempts"] = [{"method": "direct_http", "status": "success"}]
            return fetched
        attempts.append({"method": "direct_http", "status": "empty_or_thin"})
    except LinkCollectorError as exc:
        attempts.append({"method": "direct_http", "status": "failed", "error": str(exc)})
    try:
        rendered = fetch_browser_rendered_html(url)
    except LinkCollectorError as exc:
        attempts.append({"method": "browser_rendered", "status": "failed", "error": str(exc)})
        details = "; ".join(
            f"{attempt['method']}: {attempt['status']}"
            + (f" ({attempt['error']})" if attempt.get("error") else "")
            for attempt in attempts
        )
        raise LinkCollectorError(f"All x402 retrieval paths failed: {details}") from exc
    rendered["extraction_method"] = "browser_rendered"
    rendered_text = _extract_html(
        bytes(rendered["html_bytes"]),
        str(rendered.get("charset") or "utf-8"),
    ).get("text") or ""
    if len(rendered_text.strip()) < 400:
        attempts.append(
            {
                "method": "browser_rendered",
                "status": "empty_or_thin",
            }
        )
        raise LinkCollectorError(
            "All x402 retrieval paths returned empty or thin content: "
            + "; ".join(
                f"{attempt['method']}: {attempt['status']}"
                + (f" ({attempt['error']})" if attempt.get("error") else "")
                for attempt in attempts
            )
        )
    rendered["retrieval_attempts"] = attempts + [
        {"method": "browser_rendered", "status": "success"}
    ]
    return rendered


def _brave_search(
    query: str,
    *,
    requester: Callable[..., Any] | None = None,
    count: int = BRAVE_SEARCH_RESULTS_PER_QUERY,
) -> list[dict[str, Any]]:
    api_key = _configured_secret("BRAVE_SEARCH_API_KEY")
    if not api_key:
        raise LinkCollectorError("BRAVE_SEARCH_API_KEY is not configured")
    requester = requester or urllib.request.urlopen
    params = urllib.parse.urlencode({"q": query, "count": count})
    request = urllib.request.Request(
        f"{BRAVE_SEARCH_ENDPOINT}?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
        method="GET",
    )
    try:
        with requester(request, timeout=30) as response:
            body = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise LinkCollectorError(f"Brave search failed: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkCollectorError("Brave search returned invalid JSON") from exc
    results = payload.get("web", {}).get("results", [])
    if not isinstance(results, list):
        raise LinkCollectorError("Brave search response did not contain web results")
    return [
        {
            "url": str(item["url"]),
            "title": str(item.get("title") or ""),
            "description": str(item.get("description") or ""),
        }
        for item in results[:count]
        if isinstance(item, dict) and item.get("url")
    ]


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
        '"open_questions": [string], "editorial_warnings": [string], '
        '"x402_relevance": string, "wealth_lens": {"time": string, '
        '"physical": string, "mental": string, "social": string, '
        '"financial": string}}\n\n'
        "Write a deeper executive summary: explain the signal, why it matters "
        "for x402 or agent-to-agent commerce, what remains unverified, and the "
        "practical implications without giving payment or investment advice. "
        "Frame every brief through HVE's Five Wealth lens: Time, Physical, "
        "Mental, Social, and Financial wealth. Use 'not material' when a "
        "wealth dimension is not supported by the evidence.\n\n"
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
        with requester(request, timeout=SYNTHESIS_TIMEOUT_SECONDS) as response:
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
    wealth_lens = draft.get("wealth_lens")
    if (
        not isinstance(wealth_lens, dict)
        or set(wealth_lens) != set(WEALTH_TYPES)
        or any(not isinstance(wealth_lens.get(wealth_type), str) for wealth_type in WEALTH_TYPES)
    ):
        raise SynthesisError("Synthesis draft omitted the complete Five Wealth lens")
    if not isinstance(draft.get("x402_relevance"), str):
        raise SynthesisError("Synthesis draft omitted x402 relevance")
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
        "## x402 relevance",
        "",
        draft["x402_relevance"],
        "",
        "## Five Wealth lens",
        "",
    ]
    lines.extend(
        f"- **{wealth_type.title()} wealth:** {draft['wealth_lens'][wealth_type]}"
        for wealth_type in WEALTH_TYPES
    )
    lines.extend(
        [
            "",
        "## Claims",
        "",
        ]
    )
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
    review_state = ReviewStore(run_dir).start_review(
        run_id=run_id,
        draft_text=(run_dir / "brief.md").read_text(encoding="utf-8"),
    )
    synthesis["review_status"] = review_state["status"]
    return synthesis


def run_batch(
    *,
    root: Path = DEFAULT_RUN_ROOT,
    resources: list[dict[str, Any]] | None = None,
    fetcher: Callable[[str], dict[str, Any]] = fetch_x402_html,
    searcher: Callable[[str], list[dict[str, Any]]] = _brave_search,
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
    for query_index, query in enumerate(BRAVE_SEARCH_QUERIES, start=1):
        try:
            search_results = searcher(query)
        except (LinkCollectorError, OSError, ValueError) as exc:
            records.append(
                {
                    "resource_id": f"brave-search-{query_index}",
                    "lane": "x402-agent-commerce",
                    "requested_url": BRAVE_SEARCH_ENDPOINT,
                    "retrieved_at": started_at,
                    "discovery_only": True,
                    "search_query": query,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "discovered_urls": [],
                }
            )
            continue
        for result_index, result in enumerate(
            search_results[:BRAVE_SEARCH_RESULTS_PER_QUERY],
            start=1,
        ):
            result_url = str(result.get("url") or "").strip()
            if not result_url:
                continue
            record = _resource_record(
                resource_id=f"brave-search-{query_index}-{result_index}",
                lane="x402-agent-commerce",
                url=result_url,
                fetched_at=started_at,
                fetcher=fetcher,
            )
            record["discovery_source"] = "brave_search"
            record["search_query"] = query
            record["search_title"] = result.get("title")
            record["search_description"] = result.get("description")
            records.append(record)

    discovered_count = 0
    for parent in list(records):
        if not parent.get("discovery_only"):
            continue
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
