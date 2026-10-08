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

print("\nscoping: other models untouched")
other = json.loads(P.apply_gate_to_openai(json.dumps({"model":"some-other-model",
    "choices":[{"index":0,"message":{"role":"assistant","content":leak},"finish_reason":"stop"}]}).encode(),
    "some-other-model"))
ck("foreign model not gated", other["choices"][0]["message"]["content"] == leak)

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("all v1 gate tests passed")
