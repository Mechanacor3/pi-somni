"""Minimal, dependency-free model hook for offline dreaming.

Somni never calls a model on its own.  Dreaming takes any callable
``complete(system: str, prompt: str) -> str`` that returns a JSON object as
text.  This module provides one for the OpenAI Responses API using only the
standard library; supply your own for other providers.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class Usage:
    """Running token totals for every call made through one completer."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def as_dict(self):
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


def openai_completer(model, *, api_key=None, reasoning_effort="medium", base_url="https://api.openai.com/v1",
                     max_retries=6, timeout=600):
    """Return ``complete(system, prompt) -> str`` (JSON text) backed by the Responses API.

    The key comes from ``api_key`` or ``$OPENAI_API_KEY``; it is never logged.
    The returned function carries a ``usage`` attribute with running totals.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    usage = Usage()

    def complete(system, prompt):
        body = {"model": model, "instructions": system,
                "input": [{"role": "user", "content": prompt + "\n\nRespond with a single JSON object."}],
                "text": {"format": {"type": "json_object"}}}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        data = json.dumps(body).encode("utf-8")
        last = None
        for attempt in range(max_retries):
            request = urllib.request.Request(f"{base_url}/responses", data=data, method="POST", headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result = json.load(response)
                u = result.get("usage") or {}
                usage.calls += 1
                usage.input_tokens += int(u.get("input_tokens") or 0)
                usage.output_tokens += int(u.get("output_tokens") or 0)
                return "".join(part.get("text", "") for item in result.get("output", [])
                               if item.get("type") == "message" for part in item.get("content", [])
                               if part.get("type") == "output_text")
            except urllib.error.HTTPError as exc:
                last = RuntimeError(f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}")
                if exc.code not in RETRYABLE:
                    raise last from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(min(60, 2 ** attempt) + random.random())
        raise RuntimeError(f"model call failed after {max_retries} attempts: {last}")

    complete.usage = usage
    return complete
