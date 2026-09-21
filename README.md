# swamai

A self-hosted personal assistant that reads and replies to your WhatsApp, built on
[OpenClaw](https://docs.openclaw.ai/). Runs on a **local model** (Ollama) or a
**hosted API** (Anthropic / OpenAI) — your choice at setup.

It builds a searchable memory from your own chat history: exported conversations are
parsed into session-chunked markdown, summarised into per-person and per-group context
cards, and indexed into SQLite FTS5 + sqlite-vec. No external vector database.

> **Read this first.** WhatsApp has no sanctioned automation path for personal
> accounts. This connects via WhatsApp Web (Baileys) as a linked device, which is
> against WhatsApp's terms and can get your number banned. Enforcement correlates
> strongly with spam and cold outreach, so a reactive reply-only assistant is the
> lowest-risk profile — but the risk is not zero and it is **your personal number**.
> The `actions.sendMessage: false` default keeps it reply-only. Consider a spare
> number. Anyone you talk to will be talking to an LLM without being told, unless you
> keep the attribution prefix on.

## Why this exists

OpenClaw is built around frontier APIs, where a model call returns in ~1s. Point it at
a local 27B and a lot of assumptions break quietly. Most of this repo is the
scaffolding that makes that combination behave — and a guard layer that exists because
each of its rules corresponds to a real message that reached a real group chat.

## Quickstart

```bash
# 1. install OpenClaw  (the flags matter: npm blocks the bundled-plugin postinstall)
npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs openclaw@latest

# 2. clone and configure
git clone <this-repo> swamai && cd swamai
./setup.sh                      # pick ollama | anthropic | openai

# 3. install the WhatsApp channel and link a device
openclaw plugins install clawhub:@openclaw/whatsapp
openclaw channels login --channel whatsapp      # needs a TTY; scan the QR

# 4. check everything
./diag.sh
python3 guard/invariants.py
```

### Local models (Ollama)

```bash
ollama pull qwen3:8b
ollama pull qwen3-embedding:0.6b
ollama create qwen3-24k -f ollama/Modelfile.qwen27b-24k   # see ollama/README.md
systemctl --user enable --now ollama-nothink.service      # REQUIRED for reasoning models
```

The proxy is not optional if your model reasons. OpenClaw never sends `think: false`,
so Qwen3/DeepSeek-R1 return their chain-of-thought as ordinary message content, and it
gets delivered into your chat. `proxy/ollama_nothink.py` injects the flag.

### Hosted API

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # or OPENAI_API_KEY
./setup.sh --provider anthropic
```

No proxy needed. Defaults to `claude-opus-5`; `claude-sonnet-5` and
`claude-haiku-4-5` are cheaper. Hosted models follow instructions far more reliably
than a local 8B, which matters more than it sounds — see Known limits.

## Building the memory

```bash
# export chats from your phone: open chat -> ⋮ -> Export chat -> Without media
cp "WhatsApp Chat - Foo.zip" corpus/raw/
cp contacts.csv corpus/raw/            # optional: resolves numbers to names
./ingest/run.sh
```

`ingest/run.sh` parses exports into session-chunked markdown (grouped by conversational
gap, not fixed size — fixed chunks slice exchanges into incoherent fragments), builds
profile cards on your model, applies contact names, and reindexes.

Phone export gives you the **full** history. A linked-device capture
(`ingest/capture_history.mjs`) only yields what WhatsApp pushes on an initial pair —
about 12 days in testing.

## The guard layer

`python3 guard/test_guard.py` — offline, no model, no network.

| Guard | What it stops |
| --- | --- |
| `guard/gate.py` | Narration, deliberation, tool/runtime chatter, over-long replies. Applied in the proxy. |
| `guard/watchdog.py` | Runaway send loops. OpenClaw's `botLoopProtection` **does not cover WhatsApp**. |
| `guard/invariants.py` | Config mistakes that fail silently. Wired as `ExecStartPre`. |
| URL preflight | Invented image URLs (models hallucinate plausible ones that 404). |

Read `guard/README.md` before enabling any group.

## Gotchas this repo encodes

Things that cost real debugging time, in case they save you some:

- **`groupAllowFrom` matches the *sender*, not the group.** Listing only group JIDs
  means every group message is silently blocked.
- **An emoji in a mention pattern gets the whole pattern rejected**
  (`Ignoring unsupported group mention pattern`) — leaving zero patterns, so nothing
  can summon it, with no error at the call site.
- **A mention pattern that matches the bot's own output loops forever.** This produced
  93 messages to one group. Nothing in OpenClaw stops it on WhatsApp.
- **`blockStreamingDefault` defaults to on**, splitting one reply into several chat
  messages — including half-formed reasoning emitted before the answer.
- **The heartbeat is a cron job, not a setting.** It DMs you every 30 minutes and
  `openclaw cron disable` refuses it ("system-owned"). Silence it via
  `agents.defaults.heartbeat`.
- **`tools.loopDetection` defaults to false.** With it off, one trivial reply took 18
  model calls.
- **Ollama's `/v1` OpenAI-compatible endpoint breaks tool calling** — it emits
  `tool_calls` as plain text. Use the native API.
- **`web_fetch` blocks private IPs**, so an agent cannot reach a localhost helper.
- **`openclaw cron list --json` prints a docs footer after the JSON**; the schedule
  field is `expr`, not `expression`.
- **Context**: OpenClaw's system prompt runs ~8k tokens before your history. A 16k
  context window overflows and truncates mid-sentence (`stopReason=length`).

## Layout

```
guard/      output gate, rate limiter, config invariants, tests
ingest/     export parser, profile/context builders, contact import, news digest
proxy/      Ollama no-think proxy (also where the output gate is enforced)
web/        local control panel on :8765
ollama/     Modelfiles with pinned context sizes
config/     your config (gitignored) + examples/ templates
corpus/     your data (gitignored)
```

`./diag.sh` for health, `./logs.sh -f` for readable logs (OpenClaw writes JSON-lines).

## Known limits

- **Small local models are the weak link.** An 8B ignores instructions the guard then
  has to catch. A 27B is markedly better and a hosted model better still.
- **Latency**: ~40-75s per reply on a 27B on 2×3090. Hosted APIs are seconds.
- **Attribution is only enforced on replies** (`responsePrefix` is applied by code).
  Scheduled sends rely on an instruction the model can ignore.
- **Images** need a real image-search API; keyless sources cover reference photos, not
  news.
- **Slack** is configurable but this repo has focused on WhatsApp.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with WhatsApp, Meta, Anthropic, OpenAI,
or OpenClaw.
