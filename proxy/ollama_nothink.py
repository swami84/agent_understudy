#!/usr/bin/env python3
"""Transparent Ollama proxy that forces think=false on chat requests.

OpenClaw's Ollama provider never sends `think`, so reasoning models (Qwen3)
emit their chain-of-thought as ordinary message content — which then gets
delivered straight into a WhatsApp group. Ollama honours `think: false` when it
is present, so this sits in front and adds it.

    python3 proxy/ollama_nothink.py          # listens on 127.0.0.1:11435
    OLLAMA_UPSTREAM=http://127.0.0.1:11434 PORT=11435 python3 ...

Point OpenClaw at http://127.0.0.1:11435 instead of :11434.
"""
import json, os, pathlib, sys, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- outbound gate -----------------------------------------------------------
# OpenClaw exposes no hook for filtering model output before it reaches a
# channel, so the gate is applied here: this proxy is the only point every
# completion passes through. A blocked message becomes the literal NO_REPLY
# token, which OpenClaw's own silent-reply handling suppresses cleanly — that
# is quieter than returning empty content, which makes it emit
# "No reply was generated for this message" into the chat.
GATE = os.environ.get("UNDERSTUDY_GATE", os.environ.get("SWAMAI_GATE", "1")) != "0"
# The gate must only touch conversational output from THIS assistant's model.
# Other workloads share this proxy (agentic_trading was found using it), and
# truncating their structured JSON at 400 chars corrupts their results.
_gm = os.environ.get("UNDERSTUDY_GATE_MODELS",
                     os.environ.get("SWAMAI_GATE_MODELS", "qwen3.8-27b-24k,qwen3-8b-24k,qwen3.8-flash-next-iq3_s"))
GATE_MODELS = {m.strip() for m in _gm.split(",") if m.strip()}
try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from guard.gate import check_message
except Exception:                                   # guard missing -> pass through
    check_message = None
    GATE = False


def gate_text(text):
    """Return (new_text, reasons). Empty reasons means untouched."""
    if not GATE or not check_message or not text or not text.strip():
        return text, []
    v = check_message(text)
    if v.action == "block":
        return "NO_REPLY", v.reasons
    if v.action == "strip":
        return v.text, v.reasons
    return text, []

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")
# OpenAI-shaped traffic (/v1/...) goes to Strata instead. Strata is a separate
# engine with its own wire format, and it needs no think=false: it returns
# reasoning in a distinct `reasoning_content` field rather than mixing it into
# `content`. Routing both shapes through one proxy keeps a single gate chokepoint.
V1_UPSTREAM = os.environ.get("STRATA_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")
PORT = int(os.environ.get("PORT", "11435"))
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authenticate", "proxy-authorization", "te", "trailers", "host"}


def _gateable(model, fmt, content):
    """Only conversational replies from our own model are eligible."""
    if not GATE:
        return False
    base = (model or "").split(":")[0]
    if base not in GATE_MODELS and (model or "") not in GATE_MODELS:
        return False
    if fmt:                       # structured-output request: never touch it
        return False
    t = (content or "").lstrip()
    if t.startswith("{") or t.startswith("["):   # looks like JSON
        return False
    return True


def apply_gate_to_body(raw, model=None, fmt=None):
    """Gate an /api/chat response body. Handles both stream and non-stream.

    Tool-call rounds pass through untouched — suppressing those would break the
    agent loop. Only assistant prose destined for a human is gated.
    """
    if not GATE or not raw:
        return raw
    text = raw.decode("utf-8", "replace")
    lines = [l for l in text.split("\n") if l.strip()]
    try:
        objs = [json.loads(l) for l in lines]
    except Exception:
        return raw                                   # not JSONL we understand

    # A round that calls tools is machinery, not a message.
    if any((o.get("message") or {}).get("tool_calls") for o in objs):
        return raw

    if len(objs) == 1 and objs[0].get("done") is not False:
        o = objs[0]
        msg = o.get("message") or {}
        if not _gateable(model or o.get("model"), fmt, msg.get("content", "")):
            return raw
        new, why = gate_text(msg.get("content", ""))
        if why:
            msg["content"] = new
            o["message"] = msg
            print(f"gate: {','.join(why)} -> {new[:48]!r}", flush=True)
            return json.dumps(o).encode()
        return raw

    # Streaming: assemble, gate, re-emit as one content chunk + the final frame.
    assembled = "".join((o.get("message") or {}).get("content", "") for o in objs)
    if not _gateable(model or objs[-1].get("model"), fmt, assembled):
        return raw
    new, why = gate_text(assembled)
    if not why:
        return raw
    print(f"gate: {','.join(why)} -> {new[:48]!r}", flush=True)
    final = objs[-1]
    head = dict(final)
    head["done"] = False
    head["message"] = {"role": "assistant", "content": new}
    tail = dict(final)
    tail["message"] = {"role": "assistant", "content": ""}
    tail["done"] = True
    return (json.dumps(head) + "\n" + json.dumps(tail) + "\n").encode()


def _strip_reasoning(obj):
    """Remove reasoning_content wherever it appears.

    Belt and braces. Strata keeps chain-of-thought out of `content` on its own,
    so this is not load-bearing today — but whether reasoning reaches a group
    chat should not depend on a downstream client choosing not to concatenate a
    field it was handed. Dropping it here makes the leak structurally impossible.
    """
    changed = False
    for ch in obj.get("choices") or []:
        for slot in ("message", "delta"):
            m = ch.get(slot)
            if isinstance(m, dict) and m.pop("reasoning_content", None) is not None:
                changed = True
    return changed


def apply_gate_to_openai(raw, model=None):
    """Gate an OpenAI /v1/chat/completions response. Non-stream JSON or SSE.

    Same contract as the Ollama path: tool-call rounds are machinery and pass
    through untouched, only assistant prose bound for a human is gated.
    """
    if not raw:
        return raw
    text = raw.decode("utf-8", "replace")

    # ---- non-streaming: one JSON object
    if not text.lstrip().startswith("data:"):
        try:
            o = json.loads(text)
        except Exception:
            return raw
        touched = _strip_reasoning(o)
        choices = o.get("choices") or []
        if any((c.get("message") or {}).get("tool_calls") for c in choices):
            return json.dumps(o).encode() if touched else raw
        for c in choices:
            msg = c.get("message") or {}
            content = msg.get("content") or ""
            # Reasoning tokens are billed against the same completion budget as
            # the answer. A long chain can exhaust max_tokens before any prose is
            # emitted, leaving content null with finish_reason "length". Left
            # alone, OpenClaw puts "No reply was generated for this message" into
            # the chat; NO_REPLY makes it fail silent instead.
            if not content.strip() and c.get("finish_reason") == "length":
                msg["content"] = "NO_REPLY"
                c["message"] = msg
                touched = True
                print("gate: truncated-before-content -> 'NO_REPLY'", flush=True)
                continue
            if not _gateable(model or o.get("model"), None, content):
                continue
            new, why = gate_text(content)
            if why:
                msg["content"] = new
                c["message"] = msg
                touched = True
                print(f"gate: {','.join(why)} -> {new[:48]!r}", flush=True)
        return json.dumps(o).encode() if touched else raw

    # ---- streaming: SSE frames
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            frames.append(json.loads(payload))
        except Exception:
            return raw
    if not frames:
        return raw
    if any((c.get("delta") or {}).get("tool_calls")
           for f in frames for c in (f.get("choices") or [])):
        return raw

    assembled = "".join((c.get("delta") or {}).get("content") or ""
                        for f in frames for c in (f.get("choices") or []))
    if not _gateable(model or frames[-1].get("model"), None, assembled):
        return raw
    new, why = gate_text(assembled)
    if not why:
        return raw
    print(f"gate: {','.join(why)} -> {new[:48]!r}", flush=True)
    last = frames[-1]
    head = {"id": last.get("id"), "object": "chat.completion.chunk",
            "created": last.get("created"), "model": last.get("model"),
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": new},
                         "finish_reason": None}]}
    tail = {"id": last.get("id"), "object": "chat.completion.chunk",
            "created": last.get("created"), "model": last.get("model"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return (f"data: {json.dumps(head)}\n\n"
            f"data: {json.dumps(tail)}\n\n"
            f"data: [DONE]\n\n").encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _relay(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        path = self.path.rstrip("/")
        is_v1_chat = path.endswith("/v1/chat/completions")
        upstream = V1_UPSTREAM if self.path.startswith("/v1/") else UPSTREAM

        # Inject think=false for chat completions.
        req_model = req_format = None
        if method == "POST" and is_v1_chat and body:
            # Strata separates reasoning itself; there is nothing to inject here.
            # Read the model only so the gate can tell whose output this is.
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    req_model = payload.get("model")
            except Exception:
                pass
        elif method == "POST" and path.endswith("/api/chat") and body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    req_model = payload.get("model")
                    req_format = payload.get("format")
                    if "think" not in payload:
                        payload["think"] = False
                        body = json.dumps(payload).encode()
            except Exception:
                pass  # not JSON we understand: pass through untouched

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(upstream + self.path, data=body or None,
                                     headers=headers, method=method)
        # Buffer anything the gate must inspect: it needs the whole reply before
        # it can decide, so these paths cannot stream through.
        is_chat = path.endswith("/api/chat") or is_v1_chat
        try:
            with urllib.request.urlopen(req, timeout=1800) as up:
                raw = up.read() if is_chat else None
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() in HOP or k.lower() == "content-length":
                        continue
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                if is_chat:
                    out = (apply_gate_to_openai(raw, req_model) if is_v1_chat
                           else apply_gate_to_body(raw, req_model, req_format))
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(out), out))
                else:
                    while True:
                        chunk = up.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            msg = json.dumps({"error": f"proxy: {e}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_GET(self):    self._relay("GET")
    def do_POST(self):   self._relay("POST")
    def do_DELETE(self): self._relay("DELETE")
    def do_HEAD(self):   self._relay("HEAD")


if __name__ == "__main__":
    print(f"understudy gate proxy: 127.0.0.1:{PORT}\n  /api/*  -> {UPSTREAM}  (ollama, think=false injected)\n  /v1/*   -> {V1_UPSTREAM}  (strata, reasoning_content dropped)", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
