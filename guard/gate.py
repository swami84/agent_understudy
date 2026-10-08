#!/usr/bin/env python3
"""Deterministic outbound gate for the Understudy.

Every rule here exists because a real message reached a real WhatsApp group during
development. Nothing in this file calls a model — it is pure text and HTTP, so it
behaves identically every time. That is the point: a 90%-accurate guard is not a
guard when the failure is visible to ten people.

    from guard.gate import check_message, preflight_media
    verdict = check_message(text)          # .action: send | strip | block
    ok, why = preflight_media(url)

CLI:
    python3 guard/gate.py --text "Let me search for that."
    python3 guard/gate.py --url https://example.com/x.jpg
"""
from __future__ import annotations

import argparse
import os, re
import sys
from dataclasses import dataclass, field

MAX_CHARS = 400

# --- narration / internal-state markers -------------------------------------
# Each entry is (compiled pattern, short label). Labels appear in logs so a
# suppressed message can be explained without guessing.
_NARRATION = [
    (r"^\s*(?:ok(?:ay)?[,.]?\s*)?let me\b",              "announces-next-action"),
    (r"^\s*i'?ll (?:check|search|fetch|look|try|get|pull)\b", "announces-next-action"),
    (r"\bthe user (?:is asking|wants|asked|says)\b",      "talks-about-the-user"),
    (r"^\s*wait\b[\s,—-]",                           "visible-deliberation"),
    (r"\bi already (?:answered|responded|sent)\b",        "visible-deliberation"),
    (r"\bi'?m in the middle of\b",                        "visible-deliberation"),
    (r"\blet me (?:think|check|be honest|see)\b",         "visible-deliberation"),
    (r"\bi should (?:reply|respond|check|say)\b",         "visible-deliberation"),
    (r"\b(?:web_fetch|web_search|memory_search|message tool|read tool|write tool)\b",
                                                          "mentions-tooling"),
    (r"\bi (?:don'?t|do not) have (?:a|the|any)\s+\w+\s+tool\b", "mentions-tooling"),
    (r"\b(?:token limit|context window|runtime (?:event|context)|output token)\b",
                                                          "mentions-runtime"),
    (r"\bper the chat rules\b",                           "mentions-runtime"),
    (r"\bcorpus is (?:fresher|authoritative)\b",          "mentions-runtime"),
]

# Internal-disclosure rules. The assistant must never expose how it is built:
# these were prompted by a real reply that listed the workspace file tree into a
# group chat. Applied to every profile.
_LEAKS = [
    # 1. system prompt / instructions
    (r"\b(?:my|the) (?:system )?(?:prompt|instructions?|rules?)\b",   "leaks-system-prompt"),
    (r"\bi (?:was|am) (?:instructed|configured|told|programmed) to\b", "leaks-system-prompt"),
    (r"\baccording to my (?:instructions|prompt|config)\b",           "leaks-system-prompt"),
    (r"\b(?:AGENTS|USER|MEMORY|SOUL|IDENTITY|DREAMS)\.md\b",          "leaks-system-prompt"),

    # 2. stored context about people and groups
    (r"\b(?:corpus|profile card|context card|roster|memory index)\b",  "leaks-stored-context"),
    (r"\bi have (?:stored|saved|a record of|notes on|context on)\b",   "leaks-stored-context"),
    (r"\bfrom (?:my|the) (?:memory|corpus|index|notes) (?:file|store)\b", "leaks-stored-context"),
    (r"\b(?:chat|message) history (?:file|export|corpus)\b",           "leaks-stored-context"),

    # 3. files and paths it can reach
    (r"\bi have access to the following\b",                           "leaks-file-list"),
    (r"(?:^|\s)/(?:home|etc|var|usr|tmp|opt)/\S+",                     "leaks-file-list"),
    (r"\b[\w./-]+\.(?:md|json|json5|ya?ml|py|mjs|sqlite|log|tsv|csv|env)\b", "leaks-file-list"),
    (r"\b(?:workspace|file tree|directory listing)\b",                 "leaks-file-list"),

    # 4. model and runtime configuration
    (r"\b(?:qwen|llama|mistral|gemma|deepseek|gpt-\d|claude-[a-z0-9-]+)\b", "leaks-model"),
    (r"\b(?:ollama|openclaw|anthropic api|openai api)\b",              "leaks-model"),
    (r"\b(?:num_ctx|max_?tokens|context window|temperature|keep_alive|top_[kp])\b", "leaks-model"),
    (r"\bi(?:'m| am) (?:a|an) [\w.-]*\b(?:model|llm)\b",              "leaks-model"),

    # 5. configuration generally
    (r"\b(?:config(?:uration)? file|settings file|openclaw\.json)\b",  "leaks-config"),
    (r"\b(?:systemPrompt|mentionPatterns|groupAllowFrom|responsePrefix|requireMention)\b", "leaks-config"),
    (r"\b(?:api[_ ]?key|auth token|gateway token)\b",                  "leaks-config"),
]

NARRATION = [(re.compile(p, re.I), label) for p, label in _NARRATION]

# Rule sets by conversation mode. Impersonation currently shares the assistant's
# rules — kept as a separate named profile so the two can diverge (an
# impersonating assistant plausibly needs *stricter* rules, e.g. refusing to
# commit on the owner's behalf) without restructuring every call site.
# A hard cap still exists, but as a runaway stop rather than a style rule. At 400
# it was doing the styling: ordinary, well-formed answers were being cut
# mid-sentence, and the chat saw a truncated reply with no indication why.
# Brevity belongs in the prompt, where the model can end a sentence properly.
MAX_CHARS = int(os.environ.get("UNDERSTUDY_MAX_CHARS", "1600"))

PROFILES = {
    "assistant": {"narration": _NARRATION + _LEAKS, "max_chars": MAX_CHARS},
    "impersonation": {"narration": _NARRATION + _LEAKS, "max_chars": MAX_CHARS},
}
_COMPILED = {k: [(re.compile(p, re.I), lbl) for p, lbl in v["narration"]]
             for k, v in PROFILES.items()}


def profile_rules(profile):
    """Compiled narration rules + length cap for a profile name."""
    name = profile if profile in PROFILES else "assistant"
    return _COMPILED[name], PROFILES[name]["max_chars"]

# Runtime error strings OpenClaw itself emits into the channel.
_RUNTIME_ERRORS = [
    r"⚠️\s*reply truncated",
    r"\breply truncated at the model'?s output token limit\b",
    r"\bno reply was generated for this message\b",
    r"\bagent couldn'?t generate a response\b",
    r"\btemporary model failure\b",
]
RUNTIME_ERRORS = [re.compile(p, re.I) for p in _RUNTIME_ERRORS]

SILENT_TOKEN = re.compile(r"^\s*NO_REPLY\s*$", re.I)
MEDIA_LINE = re.compile(r"^\s*(?:MEDIA|Attachment)\s*:\s*(\S+)\s*$", re.I)


@dataclass
class Verdict:
    action: str                       # "send" | "strip" | "block"
    text: str                         # possibly-modified text to send
    reasons: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.action != "block"


ATTRIB = re.compile(r"^\s*(?:[^\w\s]{1,3}\s*)?[A-Za-z][\w ]{0,20}:\s*")


# "<Name>:" with no emoji in front. The summon pattern must be able to tell the
# assistant's own output from a human typing the name, and the only signal
# available is that leading emoji — OpenClaw silently rejects any mention pattern
# containing a literal emoji, so the pattern can only match it as \W.
BARE_ATTRIB = re.compile(r"^\s*([A-Za-z][\w ]{0,20}):\s")


def _assistant_name():
    """The configured assistant name, from responsePrefix ("🤖 SwamAI:")."""
    env = os.environ.get("UNDERSTUDY_NAME")
    if env:
        return env.strip()
    try:
        import json, pathlib as _p
        cfg = json.loads((_p.Path.home() / ".openclaw/openclaw.json").read_text())
        prefix = (cfg.get("channels", {}).get("whatsapp", {}) or {}).get("responsePrefix", "")
        m = re.search(r"([A-Za-z][\w ]{0,20}):\s*$", prefix.strip())
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def normalize_attribution(text: str, name: str, emoji: str = "\U0001f916") -> str:
    """Prepend the emoji when the assistant signs itself without one.

    responsePrefix is applied by OpenClaw's own code on replies, so those always
    carry it. Scheduled sends rely on an instruction the model can ignore, and a
    bare "SwamAI: ..." is indistinguishable from a human writing it — which is
    what reopens the self-reply loop.
    """
    if not text or not name:
        return text
    m = BARE_ATTRIB.match(text)
    if m and m.group(1).strip().lower() == name.strip().lower():
        return f"{emoji} {text.lstrip()}"
    return text


def _strip_attribution(line: str) -> str:
    """Remove a leading '🤖 SwamAI: ' style prefix so anchors see the real text."""
    return ATTRIB.sub("", line, count=1)


def _is_narration(line: str, rules=None) -> str | None:
    line = _strip_attribution(line)
    for rx, label in (rules if rules is not None else NARRATION):
        if rx.search(line):
            return label
    return None


def check_message(text: str, max_chars: int | None = None,
                  profile: str = "assistant") -> Verdict:
    """Decide whether an outbound message may be sent, and clean it if so.

    `profile` selects a rule set — "assistant" (signed) or "impersonation"
    (writing as the account owner). See PROFILES.
    """
    rules, profile_max = profile_rules(profile)
    max_chars = profile_max if max_chars is None else max_chars
    raw = (text or "").strip()
    if not raw:
        return Verdict("block", "", ["empty"])

    # The silence token must never be delivered as literal text.
    if SILENT_TOKEN.match(raw):
        return Verdict("block", "", ["silent-token"])

    # Runtime error strings are never the assistant's message.
    for rx in RUNTIME_ERRORS:
        if rx.search(raw):
            return Verdict("block", "", ["runtime-error-text"])

    kept, dropped, reasons = [], [], []
    for line in raw.splitlines():
        if not line.strip():
            kept.append(line)
            continue
        # A MEDIA:/Attachment: line is structural — never treat it as prose.
        if MEDIA_LINE.match(line):
            kept.append(line)
            continue
        label = _is_narration(line, rules)
        if label:
            dropped.append(line.strip())
            if label not in reasons:
                reasons.append(label)
        else:
            kept.append(line)

    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    # Nothing of substance survived -> the whole message was commentary.
    prose = "\n".join(l for l in cleaned.splitlines() if not MEDIA_LINE.match(l)).strip()
    if not prose:
        return Verdict("block", "", reasons + ["only-narration"], dropped)

    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        # prefer a sentence boundary, else a word boundary
        m = re.search(r"^(.*[.!?])\s", cut[::-1])
        trimmed = cut[: len(cut) - m.start()] if m else cut.rsplit(" ", 1)[0]
        cleaned = trimmed.strip()
        reasons.append("truncated")

    # Last step: a bare "<Name>: ..." is indistinguishable from a human typing it,
    # and the summon pattern has to tell them apart. Give the assistant's own
    # output the emoji the pattern keys on.
    name = _assistant_name()
    if name:
        normalized = normalize_attribution(cleaned, name)
        if normalized != cleaned:
            cleaned = normalized
            reasons.append("attribution-normalized")

    return Verdict("strip" if dropped or "truncated" in reasons else "send",
                   cleaned, reasons, dropped)


def preflight_media(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """HEAD a media URL before we hand it to the channel. No model involved."""
    import urllib.error
    import urllib.request

    if not re.match(r"^https?://", url or "", re.I):
        return False, "not an http(s) url"
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "Mozilla/5.0 (swamai-guard)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = getattr(r, "status", r.getcode())
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length = r.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:                                   # DNS, TLS, timeout
        return False, f"unreachable: {type(e).__name__}"
    if not (200 <= int(status) < 300):
        return False, f"HTTP {status}"
    if not ctype.startswith("image/"):
        return False, f"not an image (Content-Type: {ctype or 'unknown'})"
    if length and int(length) > 50 * 1024 * 1024:
        return False, f"too large ({int(length)/1e6:.1f}MB > 50MB)"
    return True, f"ok ({ctype})"


def check_outbound(text: str, verify_media: bool = True,
                   profile: str = "assistant") -> Verdict:
    """Full gate: narration/length checks plus MEDIA: URL verification."""
    v = check_message(text, profile=profile)
    if not v.ok or not verify_media:
        return v
    kept = []
    for line in v.text.splitlines():
        m = MEDIA_LINE.match(line)
        if not m:
            kept.append(line)
            continue
        ok, why = preflight_media(m.group(1))
        if ok:
            kept.append(line)
        else:
            v.reasons.append(f"media-rejected:{why}")
            v.dropped.append(line.strip())
            v.action = "strip"
    v.text = "\n".join(kept).strip()
    if not v.text:
        v.action = "block"
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--url")
    ap.add_argument("--no-media-check", action="store_true")
    ap.add_argument("--profile", default="assistant", choices=sorted(PROFILES))
    a = ap.parse_args()
    if a.url:
        ok, why = preflight_media(a.url)
        print(f"{'OK  ' if ok else 'FAIL'}  {why}  {a.url}")
        return 0 if ok else 1
    if a.text is None:
        a.text = sys.stdin.read()
    v = check_outbound(a.text, verify_media=not a.no_media_check, profile=a.profile)
    print(f"action : {v.action}")
    if v.reasons:
        print(f"reasons: {', '.join(v.reasons)}")
    for d in v.dropped:
        print(f"dropped: {d[:100]}")
    print(f"--- text ({len(v.text)} chars) ---")
    print(v.text)
    return 0 if v.ok else 2


if __name__ == "__main__":
    sys.exit(main())
