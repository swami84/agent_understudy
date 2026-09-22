#!/usr/bin/env python3
"""Offline tests for the guard. No model server, no network unless --net.

    python3 guard/test_guard.py
    python3 guard/test_guard.py --net      # also exercise preflight_media
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from guard.gate import check_message, check_outbound, preflight_media
from guard import invariants

# Verbatim from the session — each of these was actually delivered to a group.
LEAKED = [
    "🤖 SwamAI: Let me fetch today's headlines from India, Canada, and the US.",
    "🤖 SwamAI: The user is asking for news headlines from India, Canada, and US. "
    "The search result was only for US news and it was generic (not actual headlines). "
    "Let me search for India and Canada news as well.",
    "🤖 SwamAI: I don't have a write tool available (only `read`), so I cannot append "
    "to the memory file. Since I can't persist the memory as instructed, I'll report "
    "this as a blocker rather than falsely claiming success.",
    "🤖 SwamAI: ⚠️ Reply truncated at the model's output token limit. The text above "
    "is partial — ask to continue it.",
    "🤖 SwamAI: No reply was generated for this message. This is usually a temporary "
    "model failure - please try again.",
    "🤖 SwamAI: I already answered the bond yields question and sent the message. "
    "The runtime event is just a continuation. Since I've already responded, I should "
    "reply NO_REPLY.",
    "🤖 SwamAI: The corpus is fresher and authoritative. Using it per the chat rules.",
    "Wait — I should check whether this is even something I can act on.",
    "NO_REPLY",
]

# Internal disclosure — the assistant must never explain how it is built.
LEAKS = [
    "🤖 Assistant: I have access to the following files in the workspace: AGENTS.md, USER.md, MEMORY.md",
    "🤖 Assistant: My system prompt says to keep replies short.",
    "🤖 Assistant: I'm running qwen3.8:27b via Ollama with num_ctx 24576.",
    "🤖 Assistant: According to my instructions I should not discuss work.",
    "🤖 Assistant: I have stored context on that person from the corpus.",
    "🤖 Assistant: Your config is at /home/user/.openclaw/openclaw.json",
    "🤖 Assistant: The groupAllowFrom setting controls who can message me.",
    "🤖 Assistant: I was instructed to only reply when summoned.",
]

# Real good replies that must survive untouched.
GOOD = [
    "🤖 SwamAI: SPCX closed yesterday at $150.21, +8.34%.",
    "🤖 SwamAI: Noted — not today, tomorrow AM instead. 6 AM departure, brunch at "
    "Salt and Light Cafe in Groton.",
    "🤖 SwamAI: It's the coordination hub for a regular tennis circle — scheduling "
    "matches, booking courts, and confirming attendance.",
    "🤖 SwamAI: Here's a tennis racket.\nMEDIA: https://example.com/racket.jpg",
]

def run():
    fails = []

    print("── leaked messages must be blocked or stripped ──")
    for t in LEAKED:
        v = check_message(t)
        ok = v.action in ("block", "strip")
        # a strip must actually remove the offending sentence
        if v.action == "strip":
            ok = all(k not in v.text.lower() for k in
                     ("let me", "the user is", "i already answered", "write tool",
                      "truncated", "no reply was generated", "per the chat rules"))
        print(f"  {'PASS' if ok else 'FAIL'}  {v.action:<5} {','.join(v.reasons)[:34]:<34} {t[:46]!r}")
        if not ok: fails.append(("leak", t))

    print("\n── internal disclosure must be blocked (both profiles) ──")
    for prof in ("assistant", "impersonation"):
        for t in LEAKS:
            v = check_message(t, profile=prof)
            ok = v.action in ("block", "strip")
            if prof == "assistant":
                print(f"  {'PASS' if ok else 'FAIL'}  {v.action:<5} {','.join(v.reasons)[:30]:<30} {t[14:54]!r}")
            if not ok: fails.append(("leak-" + prof, t))
    print(f"  (both profiles checked: {len(LEAKS)} cases x 2)")

    print("\n── good replies must pass unchanged ──")
    for t in GOOD:
        v = check_message(t)
        ok = v.action == "send" and v.text.strip() == t.strip()
        print(f"  {'PASS' if ok else 'FAIL'}  {v.action:<5} {t[:56]!r}")
        if not ok:
            fails.append(("good", t)); print(f"        -> {v.reasons} {v.text[:70]!r}")

    print("\n── length cap ──")
    long = "🤖 SwamAI: " + ("This is a normal sentence about tennis. " * 30)
    v = check_message(long)
    ok = len(v.text) <= 400 and "truncated" in v.reasons
    print(f"  {'PASS' if ok else 'FAIL'}  {len(long)} chars -> {len(v.text)}")
    if not ok: fails.append(("len", "cap"))

    print("\n── config invariants catch the real misconfigurations ──")
    base = {
        "channels": {"whatsapp": {
            "groupPolicy": "allowlist",
            "groupAllowFrom": ["120363000000000000@g.us", "15551234567"],
            "groups": {"120363000000000000@g.us": {"requireMention": True}},
            "mentionPatterns": {"mode": "allow", "allowIn": ["120363000000000000@g.us"]},
            "streaming": {"block": {"enabled": False}}}},
        "messages": {"groupChat": {"mentionPatterns": [r"^(?!\W{0,4}\s*swamai\s*:).*\bswamai\b"]}},
        "agents": {"defaults": {"blockStreamingDefault": "off"}},
        "models": {"providers": {"ollama": {"baseUrl": "http://127.0.0.1:11435",
                   "models": [{"contextTokens": 24576, "maxTokens": 1800}]}}},
    }
    import copy, json as _j
    cases = [
        ("clean config", base, None),
        ("requireMention off", {"groups": {"120363000000000000@g.us": {"requireMention": False}}}, "require-mention"),
        ("self-matching pattern", {"_pat": [r"\bswamai\b(?!:)"]}, "pattern-self-match"),
        ("emoji pattern (silently rejected)", {"_pat": [r"^(?!\s*(?:🤖\s*)?swamai\s*:).*\bswamai\b"]}, "pattern-emoji"),
        ("no member numbers", {"groupAllowFrom": ["120363000000000000@g.us"]}, "no-sender-allowlist"),
        ("scope gap", {"mentionPatterns": {"mode": "allow", "allowIn": []}}, "scope-gap"),
        ("block streaming on", {"_bs": "on"}, "block-streaming"),
        ("direct ollama, no proxy", {"_url": "http://127.0.0.1:11434"}, "no-nothink-proxy"),
    ]
    for name, patch, want in cases:
        cfg = copy.deepcopy(base)
        if "_pat" in patch: cfg["messages"]["groupChat"]["mentionPatterns"] = patch["_pat"]
        elif "_bs" in patch: cfg["agents"]["defaults"]["blockStreamingDefault"] = patch["_bs"]
        elif "_url" in patch: cfg["models"]["providers"]["ollama"]["baseUrl"] = patch["_url"]
        else: cfg["channels"]["whatsapp"].update(patch)
        codes = [c for s, c, _ in invariants.check(cfg) if s == "error"]
        ok = (want in codes) if want else not codes
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<36} errors={codes or '[]'}")
        if not ok: fails.append(("inv", name))

    if "--net" in sys.argv:
        print("\n── media preflight (network) ──")
        for url, expect in [
            ("https://upload.wikimedia.org/wikipedia/commons/4/4e/Golden_Retriever_Dog.jpg", False),
            ("https://commons.wikimedia.org/w/api.php", False),
            ("not-a-url", False),
        ]:
            ok, why = preflight_media(url)
            good = ok == expect
            print(f"  {'PASS' if good else 'FAIL'}  {str(ok):<5} {why:<34} {url[:44]}")
            if not good: fails.append(("net", url))

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'}")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(run())
