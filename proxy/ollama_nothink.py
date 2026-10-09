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
import json, os, pathlib, re, sys, urllib.error, urllib.request
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
DUMP = os.environ.get("UNDERSTUDY_DUMP_REQUESTS", "")
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


# System prompts OpenClaw uses for its own machinery rather than for a reply.
# Gating these is what broke compaction: a session summary is not a chat message,
# and truncating it to 400 chars made every compaction fail with guard_blocked,
# which in turn produced no reply at all.
_INTERNAL_SYSTEM = (
    "context summarization assistant",
    "do not continue the conversation",
    "produce a structured summary",
)
# The persona marker a genuine reply request carries.
_REPLY_SYSTEM = ("openclaw:attempt:", "personal assistant running inside openclaw")


def is_internal_request(payload):
    """True when this call is OpenClaw talking to itself, not composing a reply.

    Deliberately fail-closed: anything that does not positively look like
    machinery stays gated. A mangled summary costs a missed reply; an ungated
    reply puts chain-of-thought or a system prompt into someone's chat.
    """
    if not isinstance(payload, dict):
        return False
    sys_text = " ".join(
        (m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")))
        or ""
        for m in (payload.get("messages") or [])
        if m.get("role") == "system"
    ).lower()
    if any(k in sys_text for k in _INTERNAL_SYSTEM):
        return True
    # Reply rounds carry the agent's tool catalog; summarisation rounds carry none.
    if not (payload.get("tools") or []) and not any(k in sys_text for k in _REPLY_SYSTEM):
        return True
    return False


# OpenClaw stamps each inbound message: "[Thu 2026-10-08 14:20 EDT] say OK".
STAMP = re.compile(r"^\s*\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*([A-Z]{2,5})?\]")

# Minutes after which an inbound message is history, not a request. 0 disables.
STALE_AFTER_MIN = int(os.environ.get("UNDERSTUDY_STALE_MIN", "30"))


CTX_JSON = re.compile(r"\u27e6openclaw:ctx\u27e7\s*```json\s*(\{.*?\})\s*```", re.S)


def _text_of(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _last_user_text(payload):
    """The newest user message that carries OpenClaw's timestamp stamp.

    NOT simply the last user message. On a real channel turn OpenClaw appends a
    large unstamped runtime-context block as the final user message, so taking
    the last one found no stamp and every real message read as "not stale" —
    the guard was inert on exactly the traffic it was built for.
    """
    msgs = payload.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") != "user":
            continue
        t = _text_of(m)
        if STAMP.match(t):
            return t
    return ""


def openclaw_ctx(payload):
    """Merged ⟦openclaw:ctx⟧ JSON blocks: chat_id, sender, group_subject, ..."""
    out = {}
    for m in payload.get("messages") or []:
        if m.get("role") != "user":
            continue
        for blob in CTX_JSON.findall(_text_of(m)):
            try:
                out.update(json.loads(blob))
            except Exception:
                pass
    return out


def active_message_body(payload):
    """The human's actual words, with stamp and ctx block stripped."""
    t = _last_user_text(payload)
    if not t:
        return ""
    t = STAMP.sub("", t, count=1)
    t = CTX_JSON.sub("", t, count=1)
    return t.replace("Conversation info:", "", 1).strip()


def message_age_minutes(payload, now=None):
    """Age of the message being answered, from OpenClaw's own timestamp stamp.

    Returns None when there is no stamp — do NOT treat that as stale, or every
    untimestamped internal call would be suppressed.
    """
    import datetime as _dt
    m = STAMP.match(_last_user_text(payload))
    if not m:
        return None
    try:
        when = _dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    now = now or _dt.datetime.now()
    return (now - when).total_seconds() / 60.0


def is_stale(payload, now=None):
    """True when this message is old enough that replying would be surprising.

    WhatsApp redelivers everything missed while the gateway was down, and
    OpenClaw replays it through the normal path. With a 237-member group live,
    a restart after an outage would answer a backlog of messages whose authors
    have long moved on — the shape of the 93-message incident, from a different
    cause. There is no age cutoff anywhere in OpenClaw; this is it.
    """
    if STALE_AFTER_MIN <= 0:
        return False
    age = message_age_minutes(payload, now)
    return age is not None and age > STALE_AFTER_MIN


# Require the summon word in group messages. 0/empty disables.
SUMMON_NAME = os.environ.get("UNDERSTUDY_NAME", "")
REQUIRE_SUMMON = os.environ.get("UNDERSTUDY_REQUIRE_SUMMON", "1") != "0"


def _summon_name():
    if SUMMON_NAME:
        return SUMMON_NAME.strip()
    try:
        from guard.gate import _assistant_name
        return _assistant_name()
    except Exception:
        return ""


def needs_summon(payload):
    """True when this is a group message that never named the assistant.

    OpenClaw counts a reply to one of the bot's own messages as an implicit
    mention ("quoted_bot") and bypasses requireMention. That is reasonable where
    the bot has its own identity, but this assistant runs on the OWNER's number,
    so `self` is the owner: any reply to anything the owner posts summons it.
    The behaviour is hardcoded in the WhatsApp plugin — `implicitMentions` is a
    Mattermost setting and the key is rejected here — so the check lives at the
    one chokepoint every reply passes through.
    """
    if not REQUIRE_SUMMON:
        return False
    name = _summon_name()
    if not name:
        return False
    ctx = openclaw_ctx(payload)
    chat_id = str(ctx.get("chat_id") or "")
    if not chat_id.endswith("@g.us"):
        return False                      # DMs never need the summon word
    body = active_message_body(payload)
    if not body:
        return False                      # nothing parsed: do not suppress blindly
    # Replying to the assistant quotes its own message, and that quote carries
    # "<emoji> Name:". Left in, the quote would satisfy this check and a reply
    # would still count as a summon — the exact case being fixed. Drop the
    # assistant's attribution, using the same emoji rule as the mention pattern
    # so a human typing "Name: ..." (no emoji) still counts.
    body = re.sub(rf"\W{{1,4}}\s*{re.escape(name)}\s*:", " ", body, flags=re.I)
    return not re.search(rf"\b{re.escape(name)}\b", body, re.I)


def no_reply_response(model, stream):
    """A well-formed completion whose only content is NO_REPLY.

    OpenClaw maps a bare NO_REPLY to an empty reply and delivers nothing —
    verified end to end, status ok with zero payloads. Returning this instead of
    forwarding means a stale message never reaches the model, so it costs no GPU
    either.
    """
    if not stream:
        return json.dumps({
            "id": "chatcmpl-stale", "object": "chat.completion", "created": 0,
            "model": model or "unknown",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "NO_REPLY"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }).encode()
    head = {"id": "chatcmpl-stale", "object": "chat.completion.chunk", "created": 0,
            "model": model or "unknown",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "NO_REPLY"},
                         "finish_reason": None}]}
    tail = {"id": "chatcmpl-stale", "object": "chat.completion.chunk", "created": 0,
            "model": model or "unknown",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return (f"data: {json.dumps(head)}\n\n"
            f"data: {json.dumps(tail)}\n\n"
            f"data: [DONE]\n\n").encode()


# Sampling for assistant replies. OpenClaw's openai-completions provider does not
# forward models[].params — verified by capturing the outgoing request, where
# temperature, top_p and max_tokens all arrived as null — so the engine fell back
# to --greedy (argmax). That is why a looping reply once repeated verbatim.
TEMPERATURE = os.environ.get("UNDERSTUDY_TEMPERATURE", "0.7")
TOP_P = os.environ.get("UNDERSTUDY_TOP_P", "0.8")


def apply_sampling(payload):
    """Set sampling on a reply request, in place. True if it changed anything.

    Never applied to internal calls: compaction and summarisation want the
    deterministic default, not creative variation.
    """
    changed = False
    if TEMPERATURE and "temperature" not in payload:
        payload["temperature"] = float(TEMPERATURE)
        changed = True
    if TOP_P and "top_p" not in payload:
        payload["top_p"] = float(TOP_P)
        changed = True
    return changed


def suppress_thinking(payload):
    """Turn reasoning off for a request, in place. True if it changed anything.

    Strata honours chat_template_kwargs.enable_thinking; a bare top-level
    enable_thinking is ignored and still emits a full chain of thought.
    """
    if "reasoning_effort" in payload or "chat_template_kwargs" in payload:
        return False
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    return True


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


def apply_gate_to_openai(raw, model=None, internal=False):
    """Gate an OpenAI /v1/chat/completions response. Non-stream JSON or SSE.

    Same contract as the Ollama path: tool-call rounds are machinery and pass
    through untouched, only assistant prose bound for a human is gated.
    """
    if not raw:
        return raw
    text = raw.decode("utf-8", "replace")
    if internal:
        # Still drop reasoning_content, but never rewrite or truncate the body.
        try:
            o = json.loads(text)
            return json.dumps(o).encode() if _strip_reasoning(o) else raw
        except Exception:
            return raw

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
        req_internal = req_stale = False
        if method == "POST" and is_v1_chat and body:
            # Strata separates reasoning itself; there is nothing to inject here.
            # Read the model only so the gate can tell whose output this is.
            try:
                payload = json.loads(body)
                if isinstance(payload, dict):
                    req_model = payload.get("model")
                    req_internal = is_internal_request(payload)
                    if not req_internal and is_stale(payload):
                        req_stale = "stale"
                    elif not req_internal and needs_summon(payload):
                        req_stale = "unsummoned"
                    # Every /v1 request through this proxy is the assistant.
                    # Ingest talks to Strata directly on :8080, so nothing else
                    # is affected. Reasoning is billed against the same
                    # max_tokens as the answer and the gate drops
                    # reasoning_content regardless, so on a turn with several
                    # tool results it bought nothing and spent the whole budget:
                    # stopReason=length, and no reply at all.
                    if not req_internal:
                        apply_sampling(payload)
                    if suppress_thinking(payload) or not req_internal:
                        # A session summary gains nothing from chain-of-thought,
                        # but reasoning is billed against the same max_tokens as
                        # the summary itself. On a long transcript it consumed the
                        # whole budget and OpenClaw failed with "model returned no
                        # summary text", which cancels compaction and ultimately
                        # produces no reply at all.
                        body = json.dumps(payload).encode()
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

        if DUMP and is_v1_chat and body:
            try:
                import time as _t
                pathlib.Path(DUMP).mkdir(parents=True, exist_ok=True)
                (pathlib.Path(DUMP) / f"req-{_t.time():.3f}.json").write_bytes(body)
            except Exception:
                pass

        if req_stale:
            if req_stale == "stale":
                age = message_age_minutes(json.loads(body))
                print(f"stale: {age:.0f} min old (> {STALE_AFTER_MIN}) -> NO_REPLY", flush=True)
            else:
                snippet = active_message_body(json.loads(body))[:60]
                print(f"unsummoned: group message without "
                      f"{_summon_name()!r} -> NO_REPLY  {snippet!r}", flush=True)
            out = no_reply_response(req_model, bool(json.loads(body).get("stream")))
            ctype = ("text/event-stream" if json.loads(body).get("stream")
                     else "application/json")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

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
                    out = (apply_gate_to_openai(raw, req_model, req_internal) if is_v1_chat
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
