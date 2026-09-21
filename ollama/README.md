# Modelfiles

Ollama defaults a model's context to whatever the weights advertise (often 262144),
which allocates an enormous KV cache. These pin a usable window.

```bash
ollama create qwen3-24k     -f ollama/Modelfile.qwen27b-24k    # 27B-class, 24k ctx
ollama create qwen3-8b-24k  -f ollama/Modelfile.qwen8b-24k     # 8B, 24k ctx
```

Then set the `id` in `config/00-provider.json5` to the name you created.

**Pick the context size deliberately.** OpenClaw's system prompt runs ~8k tokens
before any of your history. Measured on this setup:

| `num_ctx` | Result |
| --- | --- |
| 8192 | system prompt alone overflows; instant truncation |
| 16384 | overflows once history accumulates (`stopReason=length`) |
| **24576** | ~8k prompt + ~16k history and output — works |

Also set `maxTokens` in the provider config (~1800) so a reply cannot run to the
context limit and get truncated mid-sentence.

`num_ctx` is the only difference between these files — same weights either way.
A 27B at 24k needs ~22GB and may split across two GPUs; an 8B fits on one.
