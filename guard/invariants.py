#!/usr/bin/env python3
"""Config invariants for the Understudy. Exits non-zero on any violation.

Every check corresponds to a real incident. Run it before starting the gateway —
wire it as ExecStartPre so a bad config refuses to start rather than failing
silently in a group. No model server is contacted.

    python3 guard/invariants.py            # check, human output
    python3 guard/invariants.py --json
    python3 guard/invariants.py --config path/to/openclaw.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

DEFAULT_CFG = pathlib.Path.home() / ".openclaw/openclaw.json"
WORKSPACE = pathlib.Path.home() / ".openclaw/workspace"

# Messages the bot itself has sent. A mention pattern matching ANY of these
# creates a self-reply loop — this is what produced 93 messages to one group.
BOT_SAMPLES = [
    "🤖 SwamAI: SwamAI is now set up and listening.",
    "🤖 SwamAI: I'm unable to fetch that. Ask SwamAI again later.",
    "🤖 SwamAI: Noted — 7:30 works.",
    # A bare "SwamAI: ..." is NOT listed here any more. It is indistinguishable
    # from a human typing the name with a colon — a group member did exactly that and their
    # message was dropped as "no mention detected". guard/gate.py now normalizes
    # the assistant's own bare attribution to carry the emoji (see
    # normalize_attribution and its test), so the pattern can key on that emoji
    # and let humans through.
    "🤖 SwamAI: Here's a picture of a golden retriever.",
]
HUMAN_SAMPLES = [
    "SwamAI what is the price of SPCX?",
    "swamai remind us at 9pm",
    '"SwamAI" what does this mean',
    "hey SwamAI can you check the news",
]


def _norm(text: str) -> str:
    """Mirror OpenClaw's normalizeMentionText: lowercase, strip zero-width."""
    return re.sub(r"[​-‏‪-‮⁠-⁯]", "", (text or "")).lower()


def check(cfg: dict) -> list[tuple[str, str, str]]:
    """Return [(severity, code, message)] — severity is 'error' or 'warn'."""
    out: list[tuple[str, str, str]] = []
    wa = (cfg.get("channels") or {}).get("whatsapp") or {}
    groups_cfg = wa.get("groups") or {}
    allow = wa.get("groupAllowFrom") or []
    group_jids = [x for x in allow if str(x).endswith("@g.us")]
    senders = [re.sub(r"\D", "", str(x)) for x in allow if not str(x).endswith("@g.us")]
    gc = ((cfg.get("messages") or {}).get("groupChat")) or {}
    patterns = gc.get("mentionPatterns") or []

    # 1. Every allowlisted group must require a summon.
    for jid in group_jids:
        if (groups_cfg.get(jid) or {}).get("requireMention") is not True:
            out.append(("error", "require-mention",
                        f"group {jid} does not set requireMention=true — it will "
                        f"reply to every message, including its own"))

    # 2. Mention patterns must exist, compile, and be OpenClaw-acceptable.
    #    Validate ALWAYS, not just when groups are live: a bad pattern sitting in a
    #    disabled config is a loaded gun for whenever groups are re-enabled.
    if not patterns and wa.get("groupPolicy") != "disabled":
        out.append(("error", "no-mention-pattern",
                    "messages.groupChat.mentionPatterns is empty — nothing can summon it"))
    if True:
        for p in patterns:
            if re.search(r"[\U0001F300-\U0001FAFF]", p):
                out.append(("error", "pattern-emoji",
                            f"pattern contains an emoji and OpenClaw will reject it "
                            f"wholesale (logs: 'Ignoring unsupported group mention "
                            f"pattern'), leaving zero patterns: {p!r}"))
            try:
                rx = re.compile(p, re.I)
            except re.error as e:
                out.append(("error", "pattern-invalid", f"pattern {p!r} does not compile: {e}"))
                continue
            # 3. Must not match the bot's own output.
            for s in BOT_SAMPLES:
                if rx.search(_norm(s)):
                    out.append(("error", "pattern-self-match",
                                f"pattern {p!r} matches the bot's own message "
                                f"{s[:48]!r} — self-reply loop"))
                    break
            # 4. Must still match ordinary human phrasings.
            missed = [s for s in HUMAN_SAMPLES if not rx.search(_norm(s))]
            if len(missed) == len(HUMAN_SAMPLES):
                out.append(("error", "pattern-matches-nothing",
                            f"pattern {p!r} matches none of the sample human summons"))
            elif missed:
                out.append(("warn", "pattern-partial",
                            f"pattern {p!r} misses: {missed[0][:44]!r}"))

    # 5. Summon scope must cover every enabled group.
    scope = wa.get("mentionPatterns") or {}
    allow_in = scope.get("allowIn")
    if allow_in is not None:
        for jid in group_jids:
            if jid not in allow_in:
                out.append(("error", "scope-gap",
                            f"group {jid} is enabled but not in "
                            f"channels.whatsapp.mentionPatterns.allowIn — summons "
                            f"will be silently ignored there"))

    # 6. groupAllowFrom gates SENDERS; a group with no member numbers is deaf.
    if group_jids and not senders:
        out.append(("error", "no-sender-allowlist",
                    "groupAllowFrom contains group JIDs but no member phone numbers — "
                    "every group message will be blocked (it matches on sender)"))

    # 7. Block streaming splits one reply into several messages.
    if ((cfg.get("agents") or {}).get("defaults") or {}).get("blockStreamingDefault") != "off":
        out.append(("error", "block-streaming",
                    "agents.defaults.blockStreamingDefault is not 'off' — replies will "
                    "be delivered in fragments as they generate"))
    if (((wa.get("streaming") or {}).get("block")) or {}).get("enabled") is not False:
        out.append(("warn", "block-streaming-channel",
                    "channels.whatsapp.streaming.block.enabled is not false"))

    # 8. The gate proxy is the only place model output can be filtered.
    #
    # Checked against whichever provider is primary, not against "ollama".
    # Pointing a local provider straight at its engine still works perfectly —
    # replies just arrive ungated, with no error anywhere. That is how this gets
    # lost: nothing breaks, the guard simply stops running. For Ollama the proxy
    # also injects think:false; for Strata reasoning already arrives in a
    # separate field, so there the proxy is purely the gate.
    providers = (cfg.get("models") or {}).get("providers") or {}
    primary = ((((cfg.get("agents") or {}).get("defaults") or {}).get("model")) or {}).get("primary", "")
    pkey = primary.split("/")[0] if "/" in primary else ""
    LOCAL = {"ollama", "strata", "ollama_oai", "llamacpp", "vllm"}
    # Every local provider defined, not only the primary one: an unset or
    # mistyped primary must not be able to silence this check.
    for key in sorted(set(providers) & LOCAL):
        base = providers.get(key, {}).get("baseUrl", "")
        if base and "11435" not in base:
            why = ("reasoning will be delivered into chat" if key == "ollama"
                   else "every reply bypasses guard/gate.py")
            out.append(("error", "no-gate-proxy",
                        f"{key} baseUrl is {base!r}, not the gate proxy on :11435 — {why}"))
    if pkey and pkey not in providers:
        out.append(("error", "primary-model-missing",
                    f"agents.defaults.model.primary is {primary!r} but no provider "
                    f"{pkey!r} is defined"))

    # 9. Context must fit the system prompt with room to spare.
    models = providers.get(pkey or "ollama", {}).get("models") or []
    if models:
        ctx = models[0].get("contextTokens") or 0
        chars = sum(f.stat().st_size for f in WORKSPACE.glob("*.md")) if WORKSPACE.exists() else 0
        est = chars // 4                      # ~4 chars/token, workspace files only
        if ctx and est and ctx < est * 2:
            out.append(("warn", "context-tight",
                        f"contextTokens={ctx} vs ~{est} tokens of workspace bootstrap "
                        f"alone; history will overflow (stopReason=length)"))
        if models[0].get("maxTokens") in (None, 0):
            out.append(("warn", "no-max-tokens",
                        "model has no maxTokens cap — replies can run to the context "
                        "limit and be truncated mid-sentence"))

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--warn-only", action="store_true",
                    help="exit 0 even when errors are present (for reporting)")
    a = ap.parse_args()

    path = pathlib.Path(a.config)
    if not path.exists():
        print(f"no config at {path}", file=sys.stderr)
        return 2
    cfg = json.loads(path.read_text())
    findings = check(cfg)
    errors = [f for f in findings if f[0] == "error"]

    if a.json:
        print(json.dumps([{"severity": s, "code": c, "message": m}
                          for s, c, m in findings], indent=2))
    else:
        if not findings:
            print("✓ all invariants hold")
        for sev, code, msg in findings:
            print(f"{'✗' if sev == 'error' else '!'} [{code}] {msg}")
        if errors:
            print(f"\n{len(errors)} error(s) — the gateway should not start with this config.")
    return 0 if (a.warn_only or not errors) else 1


if __name__ == "__main__":
    sys.exit(main())
