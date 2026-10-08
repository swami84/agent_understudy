#!/usr/bin/env python3
"""Offline tests for the OpenAI-shaped half of the gate proxy (Strata).

Strata speaks /v1/chat/completions, not Ollama's /api/chat, so none of the
existing gate coverage touches this path. Pointing OpenClaw straight at Strata
works perfectly and silently ships every reply ungated, which is exactly the
kind of failure this repo exists to make impossible.

    python3 guard/test_v1_gate.py        # no model, no network
"""
import json, os, sys, pathlib
os.environ["UNDERSTUDY_GATE_MODELS"] = "qwen3.8-flash-next-iq3_s"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "proxy"))
import ollama_nothink as P
M = "qwen3.8-flash-next-iq3_s"
fails = []
def ck(n, c, d=""):
    print(f"  {'ok  ' if c else 'FAIL'}  {n}" + ("" if c else f"  — {d}"))
    if not c: fails.append(n)

leak = "🤖 Assistant: My system prompt says to keep replies short."
good = "🤖 Assistant: Sounds good, see you at 7."

print("non-streaming")
r = json.loads(P.apply_gate_to_openai(json.dumps({"model":M,"choices":[{"index":0,
    "message":{"role":"assistant","content":leak,"reasoning_content":"secret CoT"},
    "finish_reason":"stop"}]}).encode(), M))
m = r["choices"][0]["message"]
ck("leak blocked", m["content"] == "NO_REPLY", repr(m["content"]))
ck("reasoning_content dropped", "reasoning_content" not in m, str(m.keys()))

r = json.loads(P.apply_gate_to_openai(json.dumps({"model":M,"choices":[{"index":0,
    "message":{"role":"assistant","content":good,"reasoning_content":"cot"},
    "finish_reason":"stop"}]}).encode(), M))
m = r["choices"][0]["message"]
ck("good reply survives", m["content"] == good, repr(m["content"]))
ck("reasoning dropped on good reply too", "reasoning_content" not in m)

print("\ntool calls are machinery, not a message")
tc = {"model":M,"choices":[{"index":0,"message":{"role":"assistant","content":None,
      "reasoning_content":"cot",
      "tool_calls":[{"id":"c1","type":"function","function":{"name":"memory_search","arguments":"{\"query\":\"x\"}"}}]},
      "finish_reason":"tool_calls"}]}
r = json.loads(P.apply_gate_to_openai(json.dumps(tc).encode(), M))
m = r["choices"][0]["message"]
ck("tool_calls preserved", m.get("tool_calls") and m["tool_calls"][0]["function"]["name"]=="memory_search")
ck("args intact", json.loads(m["tool_calls"][0]["function"]["arguments"])["query"]=="x")
ck("reasoning dropped on tool round", "reasoning_content" not in m)

print("\nstreaming SSE")
def sse(parts, fin="stop"):
    out=[]
    for p in parts:
        out.append("data: "+json.dumps({"id":"1","model":M,"created":1,
            "choices":[{"index":0,"delta":{"content":p},"finish_reason":None}]}))
    out.append("data: "+json.dumps({"id":"1","model":M,"created":1,
        "choices":[{"index":0,"delta":{},"finish_reason":fin}]}))
    out.append("data: [DONE]")
    return ("\n\n".join(out)+"\n\n").encode()

res = P.apply_gate_to_openai(sse(["🤖 Assistant: My system ","prompt says to keep replies short."]), M).decode()
ck("stream leak blocked", "NO_REPLY" in res, res[:160])
ck("stream ends with DONE", res.rstrip().endswith("[DONE]"))
res = P.apply_gate_to_openai(sse(["🤖 Assistant: Sounds good, ","see you at 7."]), M).decode()
ck("stream good reply untouched", "Sounds good" in res and "NO_REPLY" not in res)

print("\ninternal machinery is not a chat message")
# Shapes taken verbatim from captured /v1/chat/completions requests.
reply_req = {"model": M, "tools": [{"type": "function"}] * 5, "messages": [
    {"role": "system", "content": "<!-- openclaw:attempt:STABLE -->\nYou are a personal "
                                  "assistant running inside OpenClaw.\n## Tooling"},
    {"role": "user", "content": "[Wed 2026-10-07 22:07 EDT] Hi SwamAI"}]}
compact_req = {"model": M, "messages": [
    {"role": "system", "content": "You are a context summarization assistant. Your task is "
                                  "to read a conversation... Do NOT continue the conversation."},
    {"role": "user", "content": "<conversation>...</conversation>"}]}
ck("a reply request is gated", P.is_internal_request(reply_req) is False)
ck("a compaction request is not", P.is_internal_request(compact_req) is True)
ck("no tools and no persona means machinery",
   P.is_internal_request({"messages": [{"role": "system", "content": "Summarise this."}]}) is True)
ck("persona without tools is still a reply",
   P.is_internal_request({"messages": [
       {"role": "system", "content": "<!-- openclaw:attempt:STABLE --> you are a personal "
                                     "assistant running inside OpenClaw"}]}) is False)

# The bug: a long summary was truncated to 400 chars, so compaction failed with
# guard_blocked and no reply was ever produced.
summary = ("## Goal\n- Resolve the spamming concern for the group. " * 40)
body = json.dumps({"model": M, "choices": [{"index": 0, "finish_reason": "stop",
          "message": {"role": "assistant", "content": summary,
                      "reasoning_content": "cot"}}]}).encode()
out = json.loads(P.apply_gate_to_openai(body, M, True))["choices"][0]["message"]
ck("internal body is passed through whole", out["content"] == summary,
   f'{len(out["content"])} of {len(summary)} chars survived')
ck("reasoning still dropped on internal calls", "reasoning_content" not in out)
out2 = json.loads(P.apply_gate_to_openai(body, M, False))["choices"][0]["message"]
ck("the same body IS truncated when it is a reply", len(out2["content"]) < len(summary))

print("\nscoping: other models untouched")
other = json.loads(P.apply_gate_to_openai(json.dumps({"model":"some-other-model",
    "choices":[{"index":0,"message":{"role":"assistant","content":leak},"finish_reason":"stop"}]}).encode(),
    "some-other-model"))
ck("foreign model not gated", other["choices"][0]["message"]["content"] == leak)



# ---------------------------------------------------------------- staleness
print("\nstale messages are not answered")
import datetime as _dt

NOW = _dt.datetime(2026, 10, 8, 14, 20)


def req(stamp_text, tools=True):
    r = {"model": M, "messages": [
        {"role": "system", "content": "<!-- openclaw:attempt:STABLE --> You are a personal "
                                      "assistant running inside OpenClaw."},
        {"role": "user", "content": stamp_text}]}
    if tools:
        r["tools"] = [{"type": "function"}] * 5
    return r

fresh = req("[Thu 2026-10-08 14:18 EDT] SwamAI what's the plan?")
old   = req("[Wed 2026-10-07 09:12 EDT] SwamAI what's the plan?")
edge  = req("[Thu 2026-10-08 13:49 EDT] SwamAI ping")      # 31 min
inside = req("[Thu 2026-10-08 13:51 EDT] SwamAI ping")     # 29 min

ck("2 minutes old is answered", P.is_stale(fresh, NOW) is False)
ck("29 hours old is suppressed", P.is_stale(old, NOW) is True)
ck("31 min (past cutoff) suppressed", P.is_stale(edge, NOW) is True)
ck("29 min (inside cutoff) answered", P.is_stale(inside, NOW) is False)
ck("age is computed, not guessed",
   round(P.message_age_minutes(edge, NOW)) == 31, str(P.message_age_minutes(edge, NOW)))

# No stamp must never read as stale, or internal calls would all be suppressed.
ck("unstamped message is not stale", P.is_stale(req("SwamAI hello"), NOW) is False)
# The relay suppresses only when NOT internal. Compaction summarises an old
# transcript by definition, so is_stale() alone says True — gating on that would
# break compaction exactly as truncating it did.
compaction = {"model": M, "messages": [
    {"role": "system", "content": "You are a context summarization assistant."},
    {"role": "user", "content": "[Wed 2026-10-07 09:12 EDT] old transcript"}]}


def relay_suppresses(payload, now=NOW):
    """Mirror of the condition in _relay: internal calls are never suppressed."""
    return not P.is_internal_request(payload) and P.is_stale(payload, now)

ck("old compaction input is NOT suppressed", relay_suppresses(compaction) is False)
ck("old user message IS suppressed", relay_suppresses(old) is True)
ck("fresh user message is not", relay_suppresses(fresh) is False)

body = json.loads(P.no_reply_response(M, False))
ck("suppression body is a valid completion",
   body["choices"][0]["message"]["content"] == "NO_REPLY")
sse = P.no_reply_response(M, True).decode()
ck("streaming suppression ends properly",
   "NO_REPLY" in sse and sse.rstrip().endswith("[DONE]"))

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all v1 gate tests passed")
