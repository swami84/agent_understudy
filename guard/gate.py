#!/usr/bin/env python3
"""Deterministic outbound gate for the swamai assistant.

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
import re
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
NARRATION = [(re.compile(p, re.I), label) for p, label in _NARRATION]

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


def _strip_attribution(line: str) -> str:
    """Remove a leading '🤖 SwamAI: ' style prefix so anchors see the real text."""
    return ATTRIB.sub("", line, count=1)


def _is_narration(line: str) -> str | None:
    line = _strip_attribution(line)
    for rx, label in NARRATION:
        if rx.search(line):
            return label
    return None


def check_message(text: str, max_chars: int = MAX_CHARS) -> Verdict:
    """Decide whether an outbound message may be sent, and clean it if so."""
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
        label = _is_narration(line)
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


def check_outbound(text: str, verify_media: bool = True) -> Verdict:
    """Full gate: narration/length checks plus MEDIA: URL verification."""
    v = check_message(text)
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
    a = ap.parse_args()
    if a.url:
        ok, why = preflight_media(a.url)
        print(f"{'OK  ' if ok else 'FAIL'}  {why}  {a.url}")
        return 0 if ok else 1
    if a.text is None:
        a.text = sys.stdin.read()
    v = check_outbound(a.text, verify_media=not a.no_media_check)
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
