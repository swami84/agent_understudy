# guard — deterministic safety layer

Four checks, none of which call a model. Every rule exists because a real message
reached a real WhatsApp group during development.

Why not an LLM checker: of the six failure classes we hit, only two were content
judgments. The rest were rate, config, arithmetic and URL validity — where a
deterministic check is both cheaper and strictly more reliable. A 90%-accurate
gate is not a gate when the failure is visible to ten people.

## 1. Output gate — `gate.py`

Blocks or strips outbound text before it reaches a channel.

    python3 guard/gate.py --text "Let me search for that."
    echo "$MSG" | python3 guard/gate.py

Blocks: runtime error strings ("Reply truncated…", "No reply was generated…"), a
literal `NO_REPLY`, and messages that are entirely narration. Strips: individual
lines that announce an action ("Let me…", "I'll check…"), talk about the user
("The user is asking…"), show deliberation ("Wait —", "I already answered"), or
name internals (tools, token limits, runtime). Caps length at 400 chars on a
sentence boundary. Attribution prefixes are stripped before matching, so
`🤖 SwamAI: Let me…` is caught.

## 2. Rate limiter — `watchdog.py`

OpenClaw implements `botLoopProtection` for Slack, Discord, Matrix and Google Chat
but **not WhatsApp** (verified by grep against the installed plugin). This is that
missing stop.

    python3 guard/watchdog.py --status     # counts, no action
    python3 guard/watchdog.py --dry-run    # would-trip, config untouched
    python3 guard/watchdog.py              # enforce (for a timer)

More than `GUARD_MAX` (6) sends to one recipient within `GUARD_WINDOW_MIN` (10)
minutes disables group traffic and restarts the gateway, recording why in
`guard/tripped.json`. Fails safe: any error takes no action.

## 3. URL preflight — `gate.py:preflight_media`

    python3 guard/gate.py --url https://example.com/x.jpg

`HEAD`s every `MEDIA:`/`Attachment:` URL and drops it unless it returns 2xx with
an `image/*` content type under 50MB. The model has repeatedly invented
plausible-looking Wikimedia paths that 404; this catches them before delivery.

## 4. Config invariants — `invariants.py`

    python3 guard/invariants.py            # exit 1 on any error
    python3 guard/invariants.py --json

Wired as `ExecStartPre` on the gateway (drop-in at
`~/.config/systemd/user/openclaw-gateway.service.d/10-guard.conf`), so a bad
config refuses to start instead of failing silently in a group.

Checks, each from an incident:

| code | incident |
| --- | --- |
| `require-mention` | `requireMention:false` → replied to every message incl. its own |
| `pattern-self-match` | pattern matched the bot's own text → 93-message loop |
| `pattern-emoji` | emoji in pattern → OpenClaw drops it wholesale, nothing summons |
| `pattern-matches-nothing` | pattern compiles but matches no human phrasing |
| `scope-gap` | group enabled but absent from `mentionPatterns.allowIn` → silent |
| `no-sender-allowlist` | `groupAllowFrom` gates SENDERS; group JIDs alone = deaf |
| `block-streaming` | one reply delivered as several messages |
| `no-nothink-proxy` | not pointed at :11435 → reasoning delivered as content |
| `context-tight` / `no-max-tokens` | overflow → `stopReason=length`, truncation |

Pattern checks run **unconditionally**, including when groups are disabled — a bad
pattern in a dormant config is a loaded gun for whenever they are re-enabled.

## Tests

    python3 guard/test_guard.py            # offline, no model, no network
    python3 guard/test_guard.py --net      # also exercises preflight_media

Fixtures are verbatim messages that were actually delivered to your groups.
