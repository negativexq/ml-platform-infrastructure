"""What the gateway needs to know about OpenAI-style chat completions: preparing a request
for the serving runtime, and reading how many tokens a reply used (streamed or not)."""

from __future__ import annotations

import json
from dataclasses import dataclass

MAX_METERED_BYTES = 8 * 1024 * 1024  # a non-streamed reply larger than this is not parsed


class InvalidChatRequest(ValueError):
    pass


def prepare_chat(body: bytes, served_name: str) -> bytes:
    """The caller's request, addressed to the served model and, when streamed, asking for
    token usage in the last event (so quotas can count a streamed reply)."""
    try:
        request = json.loads(body or b"null")
    except ValueError as exc:
        raise InvalidChatRequest("the body is not JSON") from exc
    if not isinstance(request, dict):
        raise InvalidChatRequest("the body is a JSON object")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidChatRequest('send "messages": [{"role": "user", "content": "..."}]')
    request["model"] = served_name  # callers need not know what the model is called inside
    if request.get("stream"):
        options = request.get("stream_options")
        request["stream_options"] = {
            **(options if isinstance(options, dict) else {}),
            "include_usage": True,
        }
    return json.dumps(request).encode()


def reserve_chat(body: bytes, limit: int) -> tuple[bytes, int]:
    """Reserve byte-based prompt estimate plus bounded output before upstream work.

    This estimate is deliberately conservative for text; it is not a model tokenizer.
    Usage above the estimate becomes debt. Missing usage keeps the full reservation.
    """
    request = json.loads(body)
    # Include tools, schemas and message framing in the prompt estimate.
    prompt = {
        k: v
        for k, v in request.items()
        if k not in {"max_tokens", "max_completion_tokens", "stream", "stream_options"}
    }
    estimated = len(json.dumps(prompt, ensure_ascii=False).encode()) + 64 * len(request["messages"])
    caps = [request[k] for k in ("max_tokens", "max_completion_tokens") if k in request]
    if any(type(cap) is not int or cap <= 0 for cap in caps):
        raise InvalidChatRequest("max_tokens and max_completion_tokens must be positive integers")
    if request.get("n", 1) != 1 or request.get("best_of", 1) != 1:
        raise InvalidChatRequest("token reservations require n=1 and best_of=1")
    cap = min(caps) if caps else min(256, limit - estimated)
    if cap <= 0 or estimated + cap > limit:
        raise InvalidChatRequest("prompt and requested output exceed the per-minute token budget")
    request.pop("max_tokens", None)
    request.pop("max_completion_tokens", None)
    request["max_tokens"] = cap
    return json.dumps(request).encode(), estimated + cap


@dataclass
class TokenMeter:
    """Reads `usage` from a reply as it streams past, without holding it: server-sent
    events are scanned line by line; a plain JSON reply is parsed once it has ended."""

    streamed: bool
    prompt: int = 0
    completion: int = 0
    usage_reported: bool = False
    _overflow: bool = False
    _pending: bytes = b""
    _whole: bytearray | None = None

    def __post_init__(self) -> None:
        if not self.streamed:
            self._whole = bytearray()

    def feed(self, chunk: bytes) -> None:
        if self._overflow:
            return
        if self._whole is not None:
            if len(self._whole) + len(chunk) <= MAX_METERED_BYTES:
                self._whole.extend(chunk)
            else:
                self._overflow = True
                self._whole.clear()
                self.usage_reported = False
            return
        if len(self._pending) + len(chunk) > MAX_METERED_BYTES:
            self._overflow = True
            self._pending = b""
            self.usage_reported = False
            return
        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for line in lines:
            self._event(line)

    def finish(self) -> None:
        if self._overflow:
            return
        if self._whole is not None:
            self._usage(bytes(self._whole))
        elif self._pending:
            self._event(self._pending)

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def _event(self, line: bytes) -> None:
        line = line.strip()
        if line.startswith(b"data:") and b'"usage"' in line:
            self._usage(line[5:].strip())

    def _usage(self, raw: bytes) -> None:
        try:
            usage = json.loads(raw).get("usage")
        except (ValueError, AttributeError):
            return
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
                self.prompt, self.completion = prompt, completion
                self.usage_reported = True
