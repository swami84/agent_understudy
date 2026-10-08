#!/usr/bin/env python3
"""One chat() for the ingest builders, over either Ollama or an OpenAI-shaped server.

The builders used to POST straight to Ollama's /api/chat. That is a different code
path from the one the assistant replies on, so pointing OpenClaw at Strata left
ingest still loading qwen3.8:27b on demand — about 19.6 GB, on GPUs already full
with Strata.

Pick the backend with UNDERSTUDY_INGEST_URL, or --base-url on any builder:

    http://127.0.0.1:11434          Ollama native  (/api/chat)
    http://127.0.0.1:8080/v1        Strata direct  (/v1/chat/completions)

Strata goes direct, NOT through the gate proxy on :11435. The gate exists to clean
text bound for a human; a corpus card is a build artifact, and truncating one at
1600 chars would silently corrupt it.
"""
import json, os, re, urllib.error, urllib.request

DEFAULT_URL = os.environ.get("UNDERSTUDY_INGEST_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("UNDERSTUDY_INGEST_MODEL", "qwen3.8:27b")


def is_openai(base_url):
    return "/v1" in base_url


def endpoint(base_url):
    b = base_url.rstrip("/")
    return b + "/chat/completions" if is_openai(b) else b + "/api/chat"


def chat(prompt, model=None, base_url=None, timeout=600, json_mode=False,
         num_ctx=32768, temperature=0.2, keep_alive="5m"):
    """Return the assistant's text. Raises on transport or shape errors."""
    base_url = base_url or DEFAULT_URL
    model = model or DEFAULT_MODEL
    msgs = [{"role": "user", "content": prompt}]

    if is_openai(base_url):
        payload = {
            "model": model, "messages": msgs, "stream": False,
            "temperature": temperature,
            # Reasoning is billed against the same budget as the answer, and a
            # card needs none of it. Without this a long chain can consume the
            # whole allowance and return empty content.
            "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 4096,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
    else:
        payload = {
            "model": model, "messages": msgs, "stream": False,
            "think": False, "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        if json_mode:
            payload["format"] = "json"

    req = urllib.request.Request(
        endpoint(base_url), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())

    if is_openai(base_url):
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content")
        if not text:
            # Empty content with a length stop means reasoning ate the budget.
            raise ValueError(f"no content (finish_reason="
                             f"{(body.get('choices') or [{}])[0].get('finish_reason')})")
    else:
        text = (body.get("message") or {}).get("content")
        if not text:
            raise ValueError("no content in /api/chat response")
    return text.strip()


def parse_json(text):
    """Local models still fence JSON sometimes, even in JSON mode."""
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))


def unload(model, base_url=None):
    """Free an Ollama-resident model. No-op for OpenAI-shaped servers, which
    manage their own residency."""
    base_url = (base_url or DEFAULT_URL).rstrip("/")
    if is_openai(base_url):
        return False
    try:
        req = urllib.request.Request(
            base_url + "/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
