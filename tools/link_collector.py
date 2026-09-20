from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from typing import Any, Callable

from tools.knowledge_layer_client import (
    EMBEDDING_CONTRACT_MODEL,
    KNOWLEDGE_INSTALL_ROOT,
    KNOWLEDGE_PYTHON,
    cli_command,
    cli_environment,
)

from websockets.sync.client import connect as websocket_connect


DEFAULT_ROOT = Path("/hve-library")
MAX_URL_LENGTH = 4096
MAX_CONTEXT_LENGTH = 20_000
MAX_HTML_BYTES = 5 * 1024 * 1024
CDP_ENDPOINT = "http://127.0.0.1:9222"
CDP_RENDER_TIMEOUT = 30
DEFAULT_CHUNK_SIZE = 2400
DEFAULT_CHUNK_OVERLAP = 250
HERMES_PYTHON = Path(
    os.environ.get(
        "HERMES_PYTHON",
        str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"),
    )
)
YOUTUBE_TRANSCRIPT_HELPER = Path(
    os.environ.get(
        "HVE_YOUTUBE_TRANSCRIPT_HELPER",
        str(
            Path.home()
            / ".hermes"
            / "hermes-agent"
            / "skills"
            / "media"
            / "youtube-content"
            / "scripts"
            / "fetch_transcript.py"
        ),
    )
)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
CAPTURE_SOURCE = "hve_librarian"
TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref_src",
}
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}


class LinkCollectorError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.skip_depth = 0
        self.in_title = False
        self.author: str | None = None
        self.language: str | None = None
        self.declared_canonical_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value for key, value in attrs}
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "html" and attributes.get("lang"):
            self.language = attributes["lang"]
        elif tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            content = attributes.get("content")
            if name in {"author", "article:author"} and content and not self.author:
                self.author = content.strip()
        elif tag == "link":
            rel = (attributes.get("rel") or "").lower().split()
            if "canonical" in rel and attributes.get("href"):
                self.declared_canonical_url = attributes["href"]
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
            return
        self.parts.append(data)

    def result(self) -> dict[str, str | None]:
        text = html.unescape("".join(self.parts)).replace("\r", "\n")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        normalized: list[str] = []
        previous_blank = True
        for line in lines:
            if line:
                normalized.append(line)
                previous_blank = False
            elif not previous_blank:
                normalized.append("")
                previous_blank = True
        title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip() or None
        return {
            "text": "\n".join(normalized).strip(),
            "title": title,
            "author": self.author,
            "language": self.language,
            "declared_canonical_url": self.declared_canonical_url,
        }


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_id(canonical_url: str) -> str:
    return _sha256_text(canonical_url)[:16]


def _sanitize_context(capture_context: str | None) -> str | None:
    if capture_context is None:
        return None
    cleaned = capture_context.replace("\x00", "").strip()
    if not cleaned:
        return None
    return cleaned[:MAX_CONTEXT_LENGTH]


def canonicalize_url(url: str) -> str:
    if not isinstance(url, str):
        raise LinkCollectorError("URL must be a string")
    cleaned = url.strip()
    if not cleaned or len(cleaned) > MAX_URL_LENGTH or any(ord(char) < 32 for char in cleaned):
        raise LinkCollectorError("URL is empty, too long, or contains control characters")

    try:
        parsed = urllib.parse.urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise LinkCollectorError(f"Invalid URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise LinkCollectorError("Only http:// and https:// URLs are accepted")
    if parsed.username is not None or parsed.password is not None:
        raise LinkCollectorError("URLs containing credentials are not accepted")
    if not parsed.hostname:
        raise LinkCollectorError("URL must include a hostname")

    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise LinkCollectorError("URL hostname is invalid") from exc
    if not host:
        raise LinkCollectorError("URL hostname is invalid")

    default_port = 80 if scheme == "http" else 443
    netloc = f"[{host}]" if ":" in host else host
    if port and port != default_port:
        netloc = f"{netloc}:{port}"

    path = parsed.path or "/"
    path = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda match: (
            chr(int(match.group(1), 16))
            if chr(int(match.group(1), 16))
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            else f"%{match.group(1).upper()}"
        ),
        path,
    )
    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_items = [
        (key, value)
        for key, value in query_items
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    query = urllib.parse.urlencode(sorted(query_items), doseq=True)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _public_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        direct_ip = ipaddress.ip_address(host)
        addresses = [direct_ip]
    except ValueError:
        try:
            addresses = list(
                {
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(
                        host,
                        port,
                        type=socket.SOCK_STREAM,
                    )
                }
            )
        except (OSError, ValueError) as exc:
            raise LinkCollectorError(f"Hostname could not be resolved: {host}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise LinkCollectorError("Private, loopback, reserved, or link-local targets are not accepted")
    return addresses


def _validate_public_target(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LinkCollectorError("Only public HTTP(S) targets are accepted")
    if parsed.port not in {None, 80, 443}:
        raise LinkCollectorError("Only standard HTTP and HTTPS ports are accepted")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise LinkCollectorError("Local network targets are not accepted")

    _public_addresses(host, parsed.port or (443 if parsed.scheme == "https" else 80))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        addresses = _public_addresses(self.host, self.port)
        last_error: OSError | None = None
        for address in addresses:
            try:
                self.sock = socket.create_connection((str(address), self.port), self.timeout)
                return
            except OSError as exc:
                last_error = exc
        raise OSError(f"Unable to connect to validated public target {self.host}") from last_error


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        if self._tunnel_host:
            raise OSError("HTTP proxy tunneling is not permitted")
        addresses = _public_addresses(self.host, self.port)
        last_error: OSError | None = None
        for address in addresses:
            raw_socket: socket.socket | None = None
            try:
                raw_socket = socket.create_connection((str(address), self.port), self.timeout)
                context = self._context or ssl.create_default_context()
                self.sock = context.wrap_socket(raw_socket, server_hostname=self.host)
                return
            except OSError as exc:
                if raw_socket is not None:
                    raw_socket.close()
                last_error = exc
        raise OSError(f"Unable to connect to validated public target {self.host}") from last_error


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        canonical_target = canonicalize_url(target)
        _validate_public_target(canonical_target)
        return super().redirect_request(req, fp, code, msg, headers, canonical_target)


def fetch_public_html(url: str, timeout: int = 15) -> dict[str, Any]:
    canonical_request = canonicalize_url(url)
    _validate_public_target(canonical_request)
    request = urllib.request.Request(
        canonical_request,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9",
            "User-Agent": "HVE-Link-Collector/1.0 (+https://humanvalueexchange.com)",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(),
        _PinnedHTTPSHandler(),
        _SafeRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = canonicalize_url(response.geturl())
            _validate_public_target(final_url)
            status = int(getattr(response, "status", 200))
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise LinkCollectorError(f"Unsupported content type: {content_type}")
            declared_length = response.headers.get("Content-Length")
            if declared_length and int(declared_length) > MAX_HTML_BYTES:
                raise LinkCollectorError("HTML response exceeds the 5 MiB limit")
            body = response.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                raise LinkCollectorError("HTML response exceeds the 5 MiB limit")
            charset = response.headers.get_content_charset() or "utf-8"
            return {
                "requested_url": canonical_request,
                "final_url": final_url,
                "http_status": status,
                "content_type": content_type,
                "charset": charset,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "html_bytes": body,
            }
    except urllib.error.HTTPError as exc:
        raise LinkCollectorError(f"HTTP fetch failed with status {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise LinkCollectorError(f"Network fetch failed: {exc.reason}") from exc
    except (OSError, ValueError) as exc:
        raise LinkCollectorError(f"Network fetch failed: {exc}") from exc


def _extract_html(body: bytes, charset: str) -> dict[str, str | None]:
    try:
        decoded = body.decode(charset, errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(decoded)
    parser.close()
    return parser.result()


def fetch_browser_rendered_html(url: str, timeout: int = CDP_RENDER_TIMEOUT) -> dict[str, Any]:
    """Render a validated public URL in the local Hermes browser via CDP."""
    canonical_request = canonicalize_url(url)
    _validate_public_target(canonical_request)
    target_id: str | None = None
    try:
        create_request = urllib.request.Request(
            f"{CDP_ENDPOINT}/json/new?{urllib.parse.quote(canonical_request, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(create_request, timeout=5) as response:
            target = json.loads(response.read().decode("utf-8"))
        target_id = str(target["id"])
        websocket_url = str(target["webSocketDebuggerUrl"])
        deadline = time.monotonic() + timeout
        with websocket_connect(websocket_url, open_timeout=5, close_timeout=2, max_size=MAX_HTML_BYTES * 2) as socket:
            next_id = 0

            def command(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal next_id
                next_id += 1
                request_id = next_id
                socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                while True:
                    remaining = max(0.1, deadline - time.monotonic())
                    message = json.loads(socket.recv(timeout=remaining))
                    if message.get("method") == "Fetch.requestPaused":
                        paused = message.get("params") or {}
                        paused_id = str(paused.get("requestId") or "")
                        request_url = str((paused.get("request") or {}).get("url") or "")
                        try:
                            target_url = canonicalize_url(request_url)
                            _validate_public_target(target_url)
                        except LinkCollectorError:
                            socket.send(
                                json.dumps(
                                    {
                                        "id": next_id + 1,
                                        "method": "Fetch.failRequest",
                                        "params": {"requestId": paused_id, "errorReason": "BlockedByClient"},
                                    }
                                )
                            )
                        else:
                            socket.send(
                                json.dumps(
                                    {
                                        "id": next_id + 1,
                                        "method": "Fetch.continueRequest",
                                        "params": {"requestId": paused_id},
                                    }
                                )
                            )
                        next_id += 1
                        continue
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise LinkCollectorError(f"CDP command failed: {message['error']}")
                        return message.get("result", {})

            command("Page.enable")
            command("Runtime.enable")
            command("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            command("Page.navigate", {"url": canonical_request})
            time.sleep(min(2, max(0, timeout)))
            rendered = command(
                "Runtime.evaluate",
                {
                    "expression": (
                        "JSON.stringify({html: document.documentElement.outerHTML, "
                        "href: location.href, title: document.title})"
                    ),
                    "returnByValue": True,
                },
            )
            value = json.loads(rendered.get("result", {}).get("value") or "{}")
            body = str(value.get("html") or "").encode("utf-8")
            if not body:
                raise LinkCollectorError("CDP returned an empty document")
            if len(body) > MAX_HTML_BYTES:
                raise LinkCollectorError("Rendered HTML exceeds the 5 MiB limit")
            final_url = canonicalize_url(str(value.get("href") or canonical_request))
            _validate_public_target(final_url)
            return {
                "requested_url": canonical_request,
                "final_url": final_url,
                "http_status": 200,
                "content_type": "text/html",
                "charset": "utf-8",
                "etag": None,
                "last_modified": None,
                "html_bytes": body,
                "extraction_method": "browser_rendered",
            }
    except LinkCollectorError:
        raise
    except Exception as exc:
        raise LinkCollectorError(f"Browser rendering failed: {exc}") from exc
    finally:
        if target_id:
            close_request = urllib.request.Request(f"{CDP_ENDPOINT}/json/close/{target_id}", method="GET")
            try:
                urllib.request.urlopen(close_request, timeout=3).close()
            except (OSError, urllib.error.URLError):
                pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@contextlib.contextmanager
def _collector_lock(root: Path):
    lock_path = root / "state" / "link-collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_chunk_settings() -> tuple[int, int]:
    config_path = Path(__file__).resolve().parents[1] / "config" / "knowledge-layer" / "knowledge-layer.yaml"
    try:
        import yaml

        retrieval = yaml.safe_load(config_path.read_text(encoding="utf-8"))["retrieval"]
        return int(retrieval["chunk_size_chars"]), int(retrieval["chunk_overlap_chars"])
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        return DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs and text.strip():
        paragraphs = [text.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        remaining = paragraph
        while remaining:
            available = chunk_size - len(current) - (2 if current else 0)
            if available <= 0:
                chunks.append(current.strip())
                current = current[-overlap:].strip() if overlap else ""
                continue
            if len(remaining) <= available:
                current = f"{current}\n\n{remaining}".strip()
                remaining = ""
                continue
            split_at = remaining.rfind(" ", 0, available + 1)
            if split_at < max(1, available // 2):
                split_at = available
            piece = remaining[:split_at].strip()
            current = f"{current}\n\n{piece}".strip()
            chunks.append(current)
            current = current[-overlap:].strip() if overlap else ""
            remaining = remaining[split_at:].strip()
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _build_chunk_records(
    document_id: str,
    raw_html_path: Path,
    content_sha256: str,
    title: str,
    author: str | None,
    text: str,
    created_at: str,
    chapter: str = "Web article",
) -> list[dict[str, Any]]:
    chunk_size, overlap = _load_chunk_settings()
    return [
        {
            "chunk_id": f"{document_id}-{index:05d}",
            "document_id": document_id,
            "source_path": str(raw_html_path),
            "sha256": content_sha256,
            "book": title,
            "author": author,
            "chapter": chapter,
            "page_start": 1,
            "page_end": 1,
            "chunk_index": index,
            "text": chunk,
            "embedding_model": EMBEDDING_CONTRACT_MODEL,
            "chunk_hash": _sha256_text(chunk),
            "created_at": created_at,
            "publisher": None,
            "publication_year": None,
        }
        for index, chunk in enumerate(_chunk_text(text, chunk_size, overlap))
    ]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    content = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    _atomic_write(path, content.encode("utf-8"))


def index_link_chunks(root: Path, chunk_path: Path, manifest_path: Path) -> dict[str, Any]:
    if not KNOWLEDGE_PYTHON.is_file() or not KNOWLEDGE_INSTALL_ROOT.exists():
        return {
            "indexed": False,
            "status": "unavailable",
            "error": "LanceDB index runtime is unavailable",
        }
    try:
        result = subprocess.run(
            cli_command(
                "--json",
                "index",
                "--chunk-file",
                str(chunk_path),
                "--manifest",
                str(manifest_path),
                root=root,
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            env=cli_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"indexed": False, "status": "failed", "error": str(exc)}
    output = (result.stdout or "").strip().splitlines()
    try:
        payload = json.loads(output[-1]) if output else {}
    except json.JSONDecodeError:
        payload = {}
    if result.returncode == 0 and payload.get("indexed") is True:
        return payload
    error = payload.get("error") or (result.stderr or "").strip() or "LanceDB indexing failed"
    return {"indexed": False, "status": payload.get("status", "failed"), "error": error}


def _capture_event(requested_url: str, capture_context: str | None) -> dict[str, Any]:
    return {
        "captured_at": _now_iso(),
        "requested_url": requested_url,
        "capture_context": capture_context,
        "capture_source": CAPTURE_SOURCE,
    }


def extract_youtube_video_id(url: str) -> str | None:
    """Return the stable YouTube video ID for supported public URL forms."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
    elif parsed.path.startswith("/watch"):
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    else:
        candidate = parsed.path.strip("/").split("/", 1)[-1]
        if parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = parsed.path.split("/", 2)[2] if len(parsed.path.split("/", 2)) > 2 else ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def canonicalize_youtube_url(url: str) -> str:
    video_id = extract_youtube_video_id(url)
    if not video_id:
        raise LinkCollectorError("URL is not a supported YouTube video URL")
    return f"https://www.youtube.com/watch?v={video_id}"


def is_youtube_url(url: str) -> bool:
    return extract_youtube_video_id(url) is not None


def _youtube_error_status(error: str) -> str:
    lowered = error.lower()
    if "disabled" in lowered or "no transcript" in lowered or "not found" in lowered:
        return "no_transcript"
    if any(marker in lowered for marker in ("private", "unavailable", "video unavailable", "does not exist")):
        return "unavailable"
    if "youtube-transcript-api not installed" in lowered:
        return "dependency_missing"
    return "failed"


def _fetch_youtube_transcript(canonical_url: str) -> dict[str, Any]:
    if not HERMES_PYTHON.is_file() or not YOUTUBE_TRANSCRIPT_HELPER.is_file():
        raise LinkCollectorError("YouTube transcript runtime is unavailable")
    try:
        completed = subprocess.run(
            [str(HERMES_PYTHON), str(YOUTUBE_TRANSCRIPT_HELPER), canonical_url, "--timestamps"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LinkCollectorError(f"Transcript extraction failed: {exc}") from exc
    output = (completed.stdout or "").strip()
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError as exc:
        raise LinkCollectorError("Transcript helper returned invalid JSON") from exc
    if completed.returncode != 0 or payload.get("error"):
        raise LinkCollectorError(str(payload.get("error") or (completed.stderr or "").strip() or "Transcript extraction failed"))
    if not payload.get("full_text") or not payload.get("video_id"):
        raise LinkCollectorError("Transcript helper returned no transcript text")
    return payload


def _youtube_result(manifest: dict[str, Any], *, duplicate: bool, error: str | None = None) -> dict[str, Any]:
    if error:
        status = (
            "completed_not_indexed"
            if manifest.get("transcript_status") == "completed"
            else manifest.get("transcript_status", "failed")
        )
    elif duplicate:
        status = "duplicate_completed"
    else:
        status = "completed"
    return {
        "status": status,
        "archived": manifest.get("source_archive_status") == "completed",
        "transcript_archived": manifest.get("transcript_status") == "completed",
        "indexed": manifest.get("indexed") is True,
        "duplicate": duplicate,
        "video_id": manifest.get("video_id"),
        "document_id": manifest.get("document_id"),
        "canonical_url": manifest.get("canonical_url"),
        "manifest_path": manifest.get("manifest_path"),
        "source_metadata_path": manifest.get("source_metadata_path"),
        "transcript_path": manifest.get("transcript_path"),
        "timestamped_transcript_path": manifest.get("timestamped_transcript_path"),
        "chunk_path": manifest.get("chunk_path"),
        "chunk_count": manifest.get("chunk_count", 0),
        "index_table": manifest.get("index_table"),
        "provenance": {
            "capture_source": manifest.get("capture_source"),
            "captured_at": manifest.get("captured_at"),
            "source_sha256": manifest.get("source_sha256"),
            "transcript_sha256": manifest.get("transcript_sha256"),
            "capture_count": len(manifest.get("captures", [])),
        },
        "error": error or manifest.get("transcript_error") or manifest.get("index_error"),
    }


def archive_youtube(
    url: str,
    capture_context: str | None = None,
    *,
    root: Path = DEFAULT_ROOT,
    transcript_fetcher: Callable[[str], dict[str, Any]] = _fetch_youtube_transcript,
    indexer: Callable[[Path, Path, Path], dict[str, Any]] = index_link_chunks,
) -> dict[str, Any]:
    """Archive a YouTube URL and its transcript as linked, provenance-rich artifacts."""
    context = _sanitize_context(capture_context)
    try:
        canonical_url = canonicalize_youtube_url(url)
    except LinkCollectorError as exc:
        return {"status": "rejected", "archived": False, "transcript_archived": False, "indexed": False, "error": str(exc)}

    video_id = extract_youtube_video_id(canonical_url)
    assert video_id is not None
    document_id = _document_id(canonical_url)
    manifest_path = root / "state" / "manifests" / f"{document_id}.json"
    source_metadata_path = root / "raw" / "youtube" / f"{video_id}.metadata.json"
    transcript_path = root / "processed" / "transcripts" / f"{video_id}.json"
    timestamped_path = root / "processed" / "transcripts" / f"{video_id}.timestamps.txt"
    chunk_path = root / "processed" / "chunks" / f"{document_id}.jsonl"
    capture = _capture_event(canonical_url, context)
    previous_manifest: dict[str, Any] = {}

    with _collector_lock(root):
        existing = _read_json(manifest_path, {})
        if isinstance(existing, dict) and existing.get("source_type") == "youtube_video":
            previous_manifest = existing
            captures = existing.setdefault("captures", [])
            captures.append(capture)
            existing["capture_count"] = len(captures)
            _write_json(manifest_path, existing)
            if existing.get("transcript_status") == "completed":
                return _youtube_result(existing, duplicate=True)

    source_metadata = {
        "document_id": document_id,
        "source_type": "youtube_video",
        "video_id": video_id,
        "canonical_url": canonical_url,
        "capture_source": CAPTURE_SOURCE,
        "capture_context": context,
        "captured_at": capture["captured_at"],
        "source_archive_status": "completed",
        "source_sha256": _sha256_text(canonical_url),
    }
    manifest = {
        **source_metadata,
        "source_path": str(source_metadata_path),
        "source_metadata_path": str(source_metadata_path),
        "captures": [*previous_manifest.get("captures", []), capture],
        "capture_count": len(previous_manifest.get("captures", [])) + 1,
        "discovered_at": capture["captured_at"],
        "transcript_status": "processing",
        "transcript_error": None,
        "transcript_path": str(transcript_path),
        "timestamped_transcript_path": str(timestamped_path),
        "extracted_text_path": str(timestamped_path),
        "fetch_status": "completed",
        "extraction_status": "processing",
        "chunk_status": "not_attempted",
        "chunk_count": 0,
        "chunk_path": str(chunk_path),
        "index_status": "not_attempted",
        "index_table": None,
        "index_error": None,
        "indexed": False,
        "manifest_version": "1.0",
        "pipeline_version": "youtube-transcript-v1",
        "manifest_path": str(manifest_path),
    }
    with _collector_lock(root):
        _write_json(source_metadata_path, source_metadata)
        _write_json(manifest_path, manifest)

    try:
        transcript = transcript_fetcher(canonical_url)
        full_text = str(transcript["full_text"]).strip()
        timestamped_text = str(transcript.get("timestamped_text") or full_text).strip()
        fetched_at = _now_iso()
        transcript_payload = {
            "document_id": document_id,
            "source_type": "youtube_video",
            "video_id": video_id,
            "canonical_url": canonical_url,
            "captured_at": capture["captured_at"],
            "fetched_at": fetched_at,
            "language": transcript.get("language"),
            "segment_count": transcript.get("segment_count"),
            "duration": transcript.get("duration"),
            "full_text": full_text,
            "timestamped_text": timestamped_text,
        }
        processed_text = (
            f"YouTube transcript: {canonical_url}\n"
            f"Video ID: {video_id}\n\n{timestamped_text}"
        )
        records = _build_chunk_records(
            document_id,
            transcript_path,
            _sha256_text(full_text),
            str(transcript.get("title") or f"YouTube video {video_id}"),
            None,
            processed_text,
            fetched_at,
            chapter="YouTube transcript",
        )
        manifest.update(
            {
                "title": transcript.get("title") or f"YouTube video {video_id}",
                "language": transcript.get("language"),
                "duration": transcript.get("duration"),
                "segment_count": transcript.get("segment_count"),
                "fetched_at": fetched_at,
                "transcript_status": "completed",
                "transcript_sha256": _sha256_text(full_text),
                "extraction_status": "completed",
                "chunk_status": "completed",
                "chunk_count": len(records),
                "index_status": "pending" if records else "not_attempted",
            }
        )
        with _collector_lock(root):
            _write_json(transcript_path, transcript_payload)
            _atomic_write(timestamped_path, (timestamped_text + "\n").encode("utf-8"))
            _write_jsonl(chunk_path, records)
            _write_json(manifest_path, manifest)
        result = _index_and_finalize(root, manifest_path, manifest, indexer, duplicate=False)
        manifest = _read_json(manifest_path, manifest)
        manifest["indexed"] = result.get("indexed") is True
        _write_json(manifest_path, manifest)
        return _youtube_result(manifest, duplicate=False, error=result.get("error"))
    except Exception as exc:
        error = str(exc)
        manifest["transcript_status"] = _youtube_error_status(error)
        manifest["transcript_error"] = error
        manifest["status"] = manifest["transcript_status"]
        _write_json(manifest_path, manifest)
        return _youtube_result(manifest, duplicate=False, error=error)


def _result(manifest: dict[str, Any], *, duplicate: bool, indexed: bool, error: str | None = None) -> dict[str, Any]:
    if indexed:
        status = "duplicate_indexed" if duplicate else "indexed"
    else:
        status = "duplicate_archived_not_indexed" if duplicate else "archived_not_indexed"
    return {
        "status": status,
        "archived": manifest.get("fetch_status") == "completed",
        "fetched": manifest.get("fetch_status") == "completed",
        "extracted": manifest.get("extraction_status") == "completed",
        "indexed": indexed,
        "duplicate": duplicate,
        "document_id": manifest.get("document_id"),
        "canonical_url": manifest.get("canonical_url"),
        "final_url": manifest.get("final_url"),
        "manifest_path": manifest.get("manifest_path"),
        "raw_html_path": manifest.get("raw_html_path"),
        "raw_metadata_path": manifest.get("raw_metadata_path"),
        "extracted_text_path": manifest.get("extracted_text_path"),
        "chunk_path": manifest.get("chunk_path"),
        "chunk_count": manifest.get("chunk_count", 0),
        "index_table": manifest.get("index_table") if indexed else None,
        "provenance": {
            "capture_source": manifest.get("capture_source"),
            "captured_at": manifest.get("captured_at"),
            "fetched_at": manifest.get("fetched_at"),
            "content_sha256": manifest.get("sha256"),
            "capture_count": len(manifest.get("captures", [])),
        },
        "error": error,
    }


def _index_and_finalize(
    root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    indexer: Callable[[Path, Path, Path], dict[str, Any]],
    *,
    duplicate: bool,
) -> dict[str, Any]:
    chunk_path_value = manifest.get("chunk_path")
    if not chunk_path_value or not Path(chunk_path_value).is_file() or not manifest.get("chunk_count"):
        manifest["index_status"] = "not_attempted"
        manifest["status"] = "archived"
        _write_json(manifest_path, manifest)
        return _result(
            manifest,
            duplicate=duplicate,
            indexed=False,
            error="No extractable text chunks were available for indexing",
        )

    try:
        index_result = indexer(root, Path(chunk_path_value), manifest_path)
    except Exception as exc:
        index_result = {
            "indexed": False,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if index_result.get("indexed") is True:
        manifest["index_status"] = "completed"
        manifest["indexed_at"] = index_result.get("indexed_at") or _now_iso()
        manifest["index_table"] = index_result.get("table", "library_chunks")
        manifest["index_error"] = None
        manifest["status"] = "indexed"
        manifest["ingest_status"] = "ingested"
        _write_json(manifest_path, manifest)
        return _result(manifest, duplicate=duplicate, indexed=True)

    error = str(index_result.get("error") or "LanceDB indexing did not complete")
    manifest["index_status"] = index_result.get("status", "failed")
    manifest["index_error"] = error
    manifest["status"] = "archived"
    manifest["ingest_status"] = "archived"
    _write_json(manifest_path, manifest)
    return _result(manifest, duplicate=duplicate, indexed=False, error=error)


def _existing_manifest(root: Path, requested_canonical: str) -> tuple[Path, dict[str, Any]] | None:
    aliases = _read_json(root / "state" / "link-url-aliases.json", {})
    alias = aliases.get(requested_canonical) if isinstance(aliases, dict) else None
    document_id = alias.get("document_id") if isinstance(alias, dict) else _document_id(requested_canonical)
    manifest_path = root / "state" / "manifests" / f"{document_id}.json"
    if not manifest_path.is_file():
        return None
    manifest = _read_json(manifest_path, {})
    if not isinstance(manifest, dict) or manifest.get("source_type") != "web_link":
        return None
    return manifest_path, manifest


def archive_link(
    url: str,
    capture_context: str | None = None,
    *,
    root: Path = DEFAULT_ROOT,
    fetcher: Callable[[str], dict[str, Any]] = fetch_public_html,
    transcript_fetcher: Callable[[str], dict[str, Any]] = _fetch_youtube_transcript,
    indexer: Callable[[Path, Path, Path], dict[str, Any]] = index_link_chunks,
) -> dict[str, Any]:
    context = _sanitize_context(capture_context)
    if is_youtube_url(url):
        return archive_youtube(
            url,
            context,
            root=root,
            transcript_fetcher=transcript_fetcher,
            indexer=indexer,
        )
    try:
        requested_canonical = canonicalize_url(url)
        _validate_public_target(requested_canonical)
    except LinkCollectorError as exc:
        return {
            "status": "rejected",
            "archived": False,
            "fetched": False,
            "extracted": False,
            "indexed": False,
            "duplicate": False,
            "error": str(exc),
        }

    with _collector_lock(root):
        existing = _existing_manifest(root, requested_canonical)
        if existing and existing[1].get("fetch_status") == "completed":
            manifest_path, manifest = existing
            if manifest.get("extraction_status") == "completed" or fetcher is not fetch_public_html:
                captures = manifest.setdefault("captures", [])
                captures.append(_capture_event(requested_canonical, context))
                manifest["capture_count"] = len(captures)
                _write_json(manifest_path, manifest)
                return _index_and_finalize(root, manifest_path, manifest, indexer, duplicate=True)

    try:
        fetched = fetcher(requested_canonical)
        final_canonical = canonicalize_url(str(fetched["final_url"]))
        _validate_public_target(final_canonical)
    except Exception as exc:
        document_id = _document_id(requested_canonical)
        manifest_path = root / "state" / "manifests" / f"{document_id}.json"
        with _collector_lock(root):
            previous = _read_json(manifest_path, {})
            captures = previous.get("captures", []) if isinstance(previous, dict) else []
            captures.append(_capture_event(requested_canonical, context))
            failure = {
                **(previous if isinstance(previous, dict) else {}),
                "document_id": document_id,
                "source_type": "web_link",
                "source_path": requested_canonical,
                "requested_url": requested_canonical,
                "canonical_url": requested_canonical,
                "capture_source": CAPTURE_SOURCE,
                "captured_at": captures[0]["captured_at"],
                "captures": captures,
                "capture_count": len(captures),
                "fetch_status": "failed",
                "fetch_error": str(exc),
                "extraction_status": "not_attempted",
                "index_status": "not_attempted",
                "status": "failed",
                "ingest_status": "failed",
                "manifest_version": "1.0",
                "pipeline_version": "link-collector-v1",
                "manifest_path": str(manifest_path),
            }
            _write_json(manifest_path, failure)
        return {
            "status": "fetch_failed",
            "archived": False,
            "fetched": False,
            "extracted": False,
            "indexed": False,
            "duplicate": False,
            "document_id": document_id,
            "canonical_url": requested_canonical,
            "manifest_path": str(manifest_path),
            "error": str(exc),
        }

    document_id = _document_id(final_canonical)
    manifest_path = root / "state" / "manifests" / f"{document_id}.json"
    raw_html_path = root / "raw" / "links" / f"{document_id}.html"
    raw_metadata_path = root / "raw" / "links" / f"{document_id}.metadata.json"
    text_path = root / "processed" / "text" / f"{document_id}.txt"
    chunk_path = root / "processed" / "chunks" / f"{document_id}.jsonl"
    previous_manifest: dict[str, Any] | None = None

    with _collector_lock(root):
        existing = _existing_manifest(root, final_canonical)
        if existing and existing[1].get("fetch_status") == "completed":
            existing_path, manifest = existing
            if manifest.get("extraction_status") == "completed" or fetcher is not fetch_public_html:
                captures = manifest.setdefault("captures", [])
                captures.append(_capture_event(requested_canonical, context))
                manifest["capture_count"] = len(captures)
                aliases = _read_json(root / "state" / "link-url-aliases.json", {})
                aliases[requested_canonical] = {
                    "document_id": manifest["document_id"],
                    "canonical_url": manifest["canonical_url"],
                    "updated_at": _now_iso(),
                }
                _write_json(root / "state" / "link-url-aliases.json", aliases)
                _write_json(existing_path, manifest)
                return _index_and_finalize(root, existing_path, manifest, indexer, duplicate=True)
            previous_manifest = manifest

        body = bytes(fetched["html_bytes"])
        extracted = _extract_html(body, str(fetched.get("charset") or "utf-8"))
        browser_error: str | None = None
        if not extracted.get("text") and fetcher is fetch_public_html:
            try:
                rendered = fetch_browser_rendered_html(requested_canonical)
                rendered_extracted = _extract_html(
                    bytes(rendered["html_bytes"]),
                    str(rendered.get("charset") or "utf-8"),
                )
                if rendered_extracted.get("text"):
                    fetched = rendered
                    final_canonical = canonicalize_url(str(rendered["final_url"]))
                    _validate_public_target(final_canonical)
                    body = bytes(rendered["html_bytes"])
                    extracted = rendered_extracted
            except LinkCollectorError as exc:
                browser_error = str(exc)
        fetched_at = _now_iso()
        title = extracted.get("title") or urllib.parse.urlsplit(final_canonical).hostname or final_canonical
        capture = _capture_event(requested_canonical, context)
        content_sha256 = _sha256_bytes(body)
        context_block = f"\n\nCapture context:\n{context}" if context else ""
        extracted_text = str(extracted.get("text") or "")
        processed_text = (
            f"{title}\n\nSource URL: {final_canonical}{context_block}\n\n"
            f"{extracted_text}"
        ).strip()
        records = (
            _build_chunk_records(
                document_id,
                raw_html_path,
                content_sha256,
                str(title),
                extracted.get("author"),
                processed_text,
                fetched_at,
            )
            if extracted_text
            else []
        )
        metadata = {
            "document_id": document_id,
            "source_type": "web_link",
            "requested_url": requested_canonical,
            "canonical_url": final_canonical,
            "final_url": fetched.get("final_url"),
            "declared_canonical_url": extracted.get("declared_canonical_url"),
            "capture_source": CAPTURE_SOURCE,
            "capture_context": context,
            "captured_at": capture["captured_at"],
            "fetched_at": fetched_at,
            "http_status": fetched.get("http_status"),
            "content_type": fetched.get("content_type"),
            "charset": fetched.get("charset"),
            "etag": fetched.get("etag"),
            "last_modified": fetched.get("last_modified"),
            "extraction_method": fetched.get("extraction_method", "static_html"),
            "browser_fallback_error": browser_error,
            "content_sha256": content_sha256,
            "retrieval_policy": {
                "public_http_only": True,
                "credentials_sent": False,
                "cookies_sent": False,
                "private_networks_blocked": True,
                "dns_rebinding_mitigated": True,
                "browser_request_interception": True,
                "max_html_bytes": MAX_HTML_BYTES,
            },
        }
        previous_captures = previous_manifest.get("captures", []) if previous_manifest else []
        manifest = {
            "document_id": document_id,
            "source_type": "web_link",
            "source_path": str(raw_html_path),
            "requested_url": requested_canonical,
            "canonical_url": final_canonical,
            "final_url": fetched.get("final_url"),
            "declared_canonical_url": extracted.get("declared_canonical_url"),
            "sha256": content_sha256,
            "text_sha256": _sha256_text(processed_text),
            "file_size_bytes": len(body),
            "discovered_at": capture["captured_at"],
            "captured_at": capture["captured_at"],
            "capture_source": CAPTURE_SOURCE,
            "captures": [*previous_captures, capture],
            "capture_count": len(previous_captures) + 1,
            "fetched_at": fetched_at,
            "http_status": fetched.get("http_status"),
            "content_type": fetched.get("content_type"),
            "fetch_status": "completed",
            "fetch_error": None,
            "title": title,
            "author": extracted.get("author"),
            "publisher": None,
            "publication_year": None,
            "language": extracted.get("language"),
            "page_count": 1,
            "raw_html_path": str(raw_html_path),
            "raw_metadata_path": str(raw_metadata_path),
            "extraction_status": "completed" if extracted_text else "empty",
            "extraction_error": (
                None
                if extracted_text
                else browser_error or "No visible HTML text was extracted"
            ),
            "extraction_method": fetched.get("extraction_method", "static_html"),
            "extracted_text_path": str(text_path),
            "extracted_at": fetched_at,
            "chunk_status": "completed" if records else "empty",
            "chunk_count": len(records),
            "chunk_path": str(chunk_path),
            "chunked_at": fetched_at,
            "index_status": "pending" if records else "not_attempted",
            "index_table": None,
            "index_error": None,
            "status": "archived",
            "ingest_status": "archived",
            "manifest_version": "1.0",
            "pipeline_version": "link-collector-v1",
            "manifest_path": str(manifest_path),
        }
        aliases = _read_json(root / "state" / "link-url-aliases.json", {})
        for alias_url in {requested_canonical, final_canonical}:
            aliases[alias_url] = {
                "document_id": document_id,
                "canonical_url": final_canonical,
                "updated_at": fetched_at,
            }

        _atomic_write(raw_html_path, body)
        _write_json(raw_metadata_path, metadata)
        _atomic_write(text_path, (processed_text + "\n").encode("utf-8"))
        _write_jsonl(chunk_path, records)
        _write_json(root / "state" / "link-url-aliases.json", aliases)
        _write_json(manifest_path, manifest)
        return _index_and_finalize(root, manifest_path, manifest, indexer, duplicate=False)
