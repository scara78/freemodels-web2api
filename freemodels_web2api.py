#!/usr/bin/env python3
"""Local OpenAI-compatible adapter for the public FreeModels.pro chat endpoint.

This is intentionally a small, local-only interoperability experiment. It does
not bypass upstream limits, rotate identities, or expose provider credentials.
"""

from __future__ import annotations

import argparse
import collections
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Iterator

import httpx


VERSION = "0.1.0"
UPSTREAM_URL = os.environ.get(
    "FREEMODELS_UPSTREAM_URL",
    "https://freemodels-chat.freemodels.workers.dev",
)

MODEL_MAP = {
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-fable-5": "claude-fable-5",
    "claude-fable-5.1": "claude-fable-5.1",
    "gpt-5.6-sol": "sol",
    "sol": "sol",
    "gpt-5.6-terra": "terra",
    "terra": "terra",
    "glm-5.2": "glm-5.2",
    "kimi-k3": "kimi-k3",
}

MODEL_DESCRIPTIONS = {
    "claude-sonnet-5": "FreeModels.pro label; upstream identity is not attested",
    "claude-fable-5": "FreeModels.pro label; upstream identity is not attested",
    "claude-fable-5.1": "FreeModels.pro label; upstream identity is not attested",
    "gpt-5.6-sol": "FreeModels.pro 'sol' label; upstream identity is not attested",
    "gpt-5.6-terra": "FreeModels.pro 'terra' label; upstream identity is not attested",
    "glm-5.2": "FreeModels.pro label; upstream identity is not attested",
    "kimi-k3": "FreeModels.pro label; upstream identity is not attested",
}

MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(256 * 1024)))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "40"))
MAX_TOTAL_CHARS = int(os.environ.get("MAX_TOTAL_CHARS", "60000"))
MAX_REQUESTS_PER_MINUTE = int(os.environ.get("MAX_REQUESTS_PER_MINUTE", "10"))
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "2"))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "120"))
LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY", "")
ALLOW_UNPROTECTED_BIND = os.environ.get("ALLOW_UNPROTECTED_BIND", "").strip().lower() in {"1", "true", "yes"}

_concurrency = threading.BoundedSemaphore(MAX_CONCURRENCY)
_rate_lock = threading.Lock()
_request_times: dict[str, collections.deque[float]] = {}


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def public_models() -> list[str]:
    """Return canonical model IDs, excluding the short aliases."""
    return list(MODEL_DESCRIPTIONS)


def resolve_model(model: Any) -> tuple[str, str]:
    if not isinstance(model, str) or not model:
        model = "claude-sonnet-5"
    upstream_id = MODEL_MAP.get(model)
    if upstream_id is None:
        raise RequestError(f"Unknown model: {model}")
    canonical = {
        "sol": "gpt-5.6-sol",
        "terra": "gpt-5.6-terra",
    }.get(model, model)
    return canonical, upstream_id


def _content_char_count(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, str):
                total += len(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return 0


def validate_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list) or not messages:
        raise RequestError("messages must be a non-empty array")
    if len(messages) > MAX_MESSAGES:
        raise RequestError(f"messages exceeds the limit of {MAX_MESSAGES}", 413)

    total_chars = 0
    clean: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise RequestError(f"messages[{index}] must be an object")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise RequestError(f"messages[{index}].role is invalid")
        content = message.get("content", "")
        if not isinstance(content, (str, list)):
            raise RequestError(f"messages[{index}].content must be text or an array")
        total_chars += _content_char_count(content)
        clean.append({key: value for key, value in message.items() if key in {
            "role", "content", "name", "tool_call_id", "tool_calls"
        }})

    if total_chars > MAX_TOTAL_CHARS:
        raise RequestError(f"message text exceeds the limit of {MAX_TOTAL_CHARS} characters", 413)
    return clean


def build_upstream_request(request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    canonical_model, upstream_model = resolve_model(request.get("model"))
    messages = validate_messages(request.get("messages"))
    payload = {
        "messages": messages,
        "modelId": upstream_model,
        "thinking": bool(request.get("thinking", False)),
        "deepSearch": bool(request.get("deep_search", request.get("deepSearch", False))),
        "stream": True,
    }
    return canonical_model, payload


def parse_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """Yield SSE data payloads while ignoring keep-alives and comments."""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def normalize_chunk(chunk: dict[str, Any], model: str) -> dict[str, Any]:
    normalized = dict(chunk)
    normalized.setdefault("id", f"chatcmpl-{uuid.uuid4().hex[:20]}")
    normalized.setdefault("object", "chat.completion.chunk")
    normalized.setdefault("created", int(time.time()))
    normalized["model"] = model
    return normalized


def collect_completion(data_items: Iterable[str], model: str) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_details: list[Any] = []
    usage: dict[str, Any] = {}
    finish_reason = "stop"
    response_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
    created = int(time.time())

    for data in data_items:
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        response_id = chunk.get("id") or response_id
        created = chunk.get("created") or created
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        text = delta.get("content")
        if isinstance(text, str):
            content_parts.append(text)
        reasoning = delta.get("reasoning")
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        details = delta.get("reasoning_details")
        if isinstance(details, list):
            reasoning_details.extend(details)
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if reasoning_parts:
        message["reasoning"] = "".join(reasoning_parts)
    if reasoning_details:
        message["reasoning_details"] = reasoning_details

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def _rate_limit_ok(client_key: str) -> bool:
    now = time.monotonic()
    cutoff = now - 60
    with _rate_lock:
        queue = _request_times.setdefault(client_key, collections.deque())
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= MAX_REQUESTS_PER_MINUTE:
            return False
        queue.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = f"freemodels-web2api/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, error_type: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": error_type}})

    def _authorized(self) -> bool:
        if not LOCAL_API_KEY:
            return True
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = self.headers.get("x-api-key", "")
        return hmac.compare_digest(supplied, LOCAL_API_KEY)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("invalid Content-Length") from exc
        if length <= 0:
            raise RequestError("request body is required")
        if length > MAX_BODY_BYTES:
            raise RequestError(f"request body exceeds {MAX_BODY_BYTES} bytes", 413)
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise RequestError("invalid JSON body") from exc
        if not isinstance(value, dict):
            raise RequestError("request body must be an object")
        return value

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"status": "ok", "version": VERSION})
            return
        if path == "/v1/models":
            if not self._authorized():
                self._error(401, "invalid API key", "authentication_error")
                return
            self._send_json(200, {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": 0,
                        "owned_by": "freemodels.pro-unverified",
                        "description": MODEL_DESCRIPTIONS[model],
                    }
                    for model in public_models()
                ],
            })
            return
        self._error(404, "not found")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            self._error(404, "not found")
            return
        if not self._authorized():
            self._error(401, "invalid API key", "authentication_error")
            return

        client_key = self.client_address[0] if self.client_address else "local"
        if not _rate_limit_ok(client_key):
            self._error(429, "local rate limit exceeded", "rate_limit_error")
            return
        if not _concurrency.acquire(blocking=False):
            self._error(429, "local concurrency limit exceeded", "rate_limit_error")
            return

        try:
            request = self._read_json()
            model, payload = build_upstream_request(request)
            wants_stream = bool(request.get("stream", False))
            self._call_upstream(payload, model, wants_stream)
        except RequestError as exc:
            self._error(exc.status, str(exc))
        except httpx.TimeoutException:
            self._error(504, "upstream request timed out", "upstream_error")
        except httpx.HTTPError as exc:
            self._error(502, f"upstream request failed: {exc}", "upstream_error")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _concurrency.release()

    def _call_upstream(self, payload: dict[str, Any], model: str, wants_stream: bool) -> None:
        timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=15.0)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": f"freemodels-web2api/{VERSION} local-interoperability-test",
        }
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("POST", UPSTREAM_URL, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_body = response.read()[:1000].decode("utf-8", errors="replace")
                    message = error_body
                    try:
                        parsed = json.loads(error_body)
                        if isinstance(parsed, dict):
                            message = str(parsed.get("error") or parsed)
                    except json.JSONDecodeError:
                        pass
                    raise RequestError(f"upstream returned {response.status_code}: {message}", 502)

                data_items = parse_sse_data(response.iter_lines())
                if wants_stream:
                    self._proxy_stream(data_items, model)
                else:
                    completion = collect_completion(data_items, model)
                    self._send_json(200, completion)

    def _proxy_stream(self, data_items: Iterable[str], model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        for data in data_items:
            if data == "[DONE]":
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            normalized = normalize_chunk(chunk, model)
            wire = f"data: {json.dumps(normalized, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(wire)
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8082")))
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not LOCAL_API_KEY and not ALLOW_UNPROTECTED_BIND:
        raise SystemExit("Refusing a non-loopback bind without LOCAL_API_KEY (or set ALLOW_UNPROTECTED_BIND=true)")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"freemodels-web2api {VERSION}")
    print(f"Listening: http://{args.host}:{args.port}")
    print(f"Base URL:  http://{args.host}:{args.port}/v1")
    print("Mode:      local interoperability test")
    print("Notice:    displayed model names are not upstream identity proof")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()