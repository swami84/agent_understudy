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



# ------------------------------------------------- summon enforcement (groups)
print("\ngroup messages must name the assistant")
os.environ["UNDERSTUDY_NAME"] = "SwamAI"
import importlib
importlib.reload(P)

CTX = '⟦openclaw:ctx⟧\n```json\n{"sender":{"id":"+15551234567","name":"Example Member"}}\n```'
RUNTIME = ('OpenClaw runtime context for the active user request in this turn.\n'
           '<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nConversation info: ⟦openclaw:ctx⟧\n'
           '```json\n{"chat_id":"%s","group_subject":"NIT-T"}\n```\n'
           '<<<END_OPENCLAW_INTERNAL_CONTEXT>>>')


def turn(body, chat_id="120363000000000000@g.us", stamp="[Fri 2026-10-09 10:18 EDT]"):
    """Mirrors a real channel turn: stamped message, then the runtime block."""
    return {"model": M, "tools": [{"type": "function"}] * 5, "messages": [
        {"role": "system", "content": "<!-- openclaw:attempt:STABLE --> You are a personal "
                                      "assistant running inside OpenClaw."},
        {"role": "user", "content": f"{stamp} Conversation info: {CTX}  {body}"},
        {"role": "user", "content": RUNTIME % chat_id}]}

summoned = turn("SwamAI give top 10 contributors with message count.")
quiet    = turn("Don’t know man. Making it safe for myself when he grows a conscience.")
dm       = turn("Don’t know man.", chat_id="+19787959422")

ck("the runtime block is not mistaken for the message",
   P.active_message_body(summoned).startswith("SwamAI give top 10"),
   repr(P.active_message_body(summoned))[:80])
ck("chat_id is read from the runtime block",
   P.openclaw_ctx(summoned).get("chat_id") == "120363000000000000@g.us")
ck("named in a group -> answered", P.needs_summon(summoned) is False)
ck("unnamed in a group -> suppressed", P.needs_summon(quiet) is True)
ck("unnamed in a DM -> still answered", P.needs_summon(dm) is False)
ck("name match is case-insensitive",
   P.needs_summon(turn("hey swamai what's up")) is False)
ck("a substring is not a summon",
   P.needs_summon(turn("that swamaiish thing")) is True)

# Replying to the assistant quotes its message; that quote must not count.
ck("a quoted bot reply is NOT a summon",
   P.needs_summon(turn("\U0001f916 SwamAI: earlier answer here\nDon\u2019t know man, "
                       "making it safe for myself")) is True)
ck("a human typing 'SwamAI:' IS a summon",
   P.needs_summon(turn("SwamAI: who is coming tonight?")) is False)
ck("quote plus a real summon still answers",
   P.needs_summon(turn("\U0001f916 SwamAI: earlier answer\nSwamAI what about Tuesday?")) is False)

# Staleness must read the stamped message, not the unstamped runtime block.
old_turn = turn("SwamAI ping", stamp="[Wed 2026-10-07 09:12 EDT]")
ck("staleness reads the stamped message, not the runtime block",
   P.message_age_minutes(old_turn, NOW) is not None)
ck("an old real-shaped turn is stale", P.is_stale(old_turn, NOW) is True)
ck("a fresh real-shaped turn is not",
   P.is_stale(turn("SwamAI ping", stamp="[Thu 2026-10-08 14:18 EDT]"), NOW) is False)



print("\nsampling is applied to replies, not to internal calls")
reply_p = {"model": M, "tools": [{"type": "function"}] * 5, "messages": [
    {"role": "system", "content": "<!-- openclaw:attempt:STABLE --> personal assistant "
                                  "running inside OpenClaw"}]}
P.apply_sampling(reply_p)
ck("temperature set on a reply", reply_p.get("temperature") == float(P.TEMPERATURE))
ck("top_p set on a reply", reply_p.get("top_p") == float(P.TOP_P))

caller = {"model": M, "temperature": 0.1, "messages": []}
P.apply_sampling(caller)
ck("an explicit temperature is respected", caller["temperature"] == 0.1)

compact_p = {"model": M, "messages": [
    {"role": "system", "content": "You are a context summarization assistant."}]}
ck("compaction is still classed internal", P.is_internal_request(compact_p) is True)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all v1 gate tests passed")
