from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import urllib.parse
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
    args = parser.parse_args(argv)
    result = run_batch(root=args.root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"complete", "partial", "no_usable_sources"} else 1


if __name__ == "__main__":
    sys.exit(main())
