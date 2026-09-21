# Findings from reading the installed source (2026-09-03)

## GitHub #56365 is outdated for 2026.9.1

The issue claims `createWaSocket` calls `makeWASocket({ authDir })` with no other
config. Not true in this version. From
`~/.openclaw/extensions/whatsapp/dist/socket-close-B3X9uerq.js:407`:

    makeWASocket({
      auth: { creds, keys: signalKeys },
      version, logger,
      printQRInTerminal: false,
      browser: ["openclaw", "cli", VERSION],   // STABLE fingerprint
      syncFullHistory: false,
      fireInitQueries: receiveMode !== "directory",
      markOnlineOnConnect: false,
      ...socketTiming,
      agent, fetchAgent,                        // proxy support
      ...
    })

The official plugin already implements most sane anti-ban hygiene.

## What is genuinely missing

`resolveWhatsAppSocketTiming(overrides)` accepts overrides, but login call sites
(`login-6j2iP9nR.js:20`, `login-qr-DRFgI6k1.js:194`) call it with no arguments, and
`keepAliveIntervalMs` / `defaultQueryTimeoutMs` do not appear in `openclaw config schema`
under `channels.whatsapp`. So socket timing is effectively hardcoded at
25s keepalive / 60s connect / 60s query.

Also absent: outbound send pacing (sends are serialized via
`runSerializedSocketSendMessage` but not delayed or jittered).

## Architecture: wrap, don't fork

`channels.whatsapp.pluginHooks` is `{ messageReceived }` only — inbound. The top-level
`hooks` key is inbound webhook ingress. Neither is an outbound seam.

The outbound seam is the plugin API: a plugin can wrap another plugin's *registered
outbound adapter*. Precedent on ClawHub:
- `openclaw-slack-buttons-hotfix` — wraps @openclaw/slack's outbound adapter
- `agent-audit-gate` — holds outbound WhatsApp messages for review

## From baileys-antiban: take vs skip

Take: send pacing with jitter, disconnect classification, recovery backoff.
Skip: randomized device fingerprinting — it fights the official plugin's deliberately
stable `browser` triple and makes a long-lived personal session look *more* anomalous.
Most of the rest (7-day warmup, contact-graph handshakes, topology throttling,
cross-instance token buckets) targets bulk senders and is irrelevant to a reactive
personal assistant.
