#!/usr/bin/env python3
"""Incremental corpus refresh: rebuild only what the new messages actually changed.

A full build reads every message with a 27B and takes hours. Almost none of that
work is new — a week of chat changes one month of one chat, and leaves every other
card in the corpus exactly as it was. This plans the delta, prices it, and rebuilds
only that.

It plans by default and spends nothing. `--apply` is how you authorise the GPU
time, which matters on a shared box: loading a 27B next to someone else's job
costs them throughput, so this refuses to start when another model is resident
unless you say `--shared`, and it unloads what it loaded when it finishes.

    python3 ingest/refresh.py                 # plan only — what would rebuild, and what it costs
    python3 ingest/refresh.py --apply         # do it
    python3 ingest/refresh.py --apply --no-llm   # deterministic layers only, no GPU at all
"""
import argparse, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T
import build_temporal as BT
import llm

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

# Measured on 2x3090 with qwen3.8:27b Q4_K_M. Prefill is the uncached rate — the
# cached figure is ~50x higher and quoting it here would understate a cold build
# by an order of magnitude.
PREFILL_TPS = 1260
GEN_TPS = 40
CHARS_PER_TOK = 3.6


def secs(prompt_chars, out_tokens=500):
    return prompt_chars / CHARS_PER_TOK / PREFILL_TPS + out_tokens / GEN_TPS


def human(s):
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def run(cmd, **kw):
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=ROOT, **kw)


def would_build(script, extra):
    """Count what a builder reports it would rebuild, using its own dry run so the
    plan can never disagree with what the builder actually does."""
    r = subprocess.run([sys.executable, f"ingest/{script}", "--dry-run", *extra],
                       cwd=ROOT, capture_output=True, text=True)
    lines = [l for l in (r.stdout + r.stderr).splitlines() if "would build:" in l]
    return len(lines), lines


def resident():
    try:
        with urllib.request.urlopen(f"{HOST}/api/ps", timeout=5) as r:
            return [m["model"] for m in json.load(r).get("models", [])]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []


def unload(model):
    try:
        req = urllib.request.Request(
            f"{HOST}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        print(f"  unloaded {model}", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually rebuild (default: plan only)")
    ap.add_argument("--no-llm", action="store_true", help="deterministic layers only, no GPU")
    ap.add_argument("--force", action="store_true", help="rebuild everything")
    ap.add_argument("--shared", action="store_true",
                    help="proceed even if another model is loaded on this GPU")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--base-url", default=llm.DEFAULT_URL,
                    help="http://127.0.0.1:11434 (ollama) or http://127.0.0.1:8080/v1 (strata)")
    ap.add_argument("--min-delta", type=int, default=25)
    ap.add_argument("--only")
    ap.add_argument("--no-index", action="store_true", help="skip the OpenClaw reindex")
    args = ap.parse_args()

    common = ["--model", args.model, "--base-url", args.base_url] \
             + (["--force"] if args.force else [])
    only = ["--only", args.only] if args.only else []

    # 1. Parse. Deterministic and cheap; always safe to redo.
    raws = sorted(p for p in (ROOT / "corpus" / "raw").glob("*")
                  if p.suffix.lower() in (".txt", ".zip"))
    man = T.Manifest()
    raw_hash = T.content_hash(*(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}" for p in raws))
    reparse, why = man.stale("raw", raw_hash, count=len(raws), force=args.force)

    print(f"\n── plan ─────────────────────────────────────────")
    print(f"  backend      {args.base_url}")
    print(f"  exports      {len(raws)} file(s) — {'reparse (' + why + ')' if reparse else 'unchanged'}")

    if reparse and args.apply and raws:
        run([sys.executable, "ingest/parse_export.py", *[str(p) for p in raws]],
            stdout=subprocess.DEVNULL)
        man = T.Manifest()
        man.record("raw", raw_hash, count=len(raws))
        man.save()

    # 2. Price the model work.
    n_prof, prof_lines = would_build("build_profiles.py", common + only + ["--min-delta", str(args.min_delta)])
    n_grp, grp_lines = would_build("build_group_context.py", common + only)
    cached, uncached, n_chats = BT.plan_months(ROOT / "corpus" / "chats", args.model, args.only)
    backend = "strata" if llm.is_openai(args.base_url) else "ollama"
    if args.force:
        uncached, cached = cached + uncached, 0

    est = n_prof * secs(12000) + n_grp * secs(14000) + uncached * secs(24000, 400)
    calls = n_prof + n_grp + uncached

    print(f"  profiles     {n_prof} card(s) to rebuild")
    for l in prof_lines[:6]:
        print(f"               {l.strip()}")
    print(f"  groups       {n_grp} card(s) to rebuild")
    print(f"  timeline     {uncached} month(s) need the model, {cached} already cached "
          f"({n_chats} chat(s))")
    print(f"  ────")
    print(f"  {calls} model call(s), roughly {human(est)} on {args.model}"
          if not args.no_llm else "  --no-llm: no model calls at all")
    print()

    if not args.apply:
        print("plan only. re-run with --apply to rebuild.\n")
        return 0

    # 3. Preflight the shared GPU.
    if not args.no_llm and calls and not llm.is_openai(args.base_url):
        others = [m for m in resident() if m != args.model]
        if others and not args.shared:
            print(f"refusing to start: {', '.join(others)} is loaded on this GPU.", file=sys.stderr)
            print("another job is using it — wait, or pass --shared to load anyway.\n", file=sys.stderr)
            return 2

    t0 = time.time()
    llm_flag = [] if not args.no_llm else ["--no-llm"]
    if not args.no_llm:
        run([sys.executable, "ingest/build_profiles.py", *common, *only,
             "--min-delta", str(args.min_delta)])
        run([sys.executable, "ingest/build_group_context.py", *common, *only])
    run([sys.executable, "ingest/build_temporal.py", *common, *only, *llm_flag])

    if not args.no_llm and calls and not llm.is_openai(args.base_url):
        unload(args.model)

    if not args.no_index:
        r = run(["openclaw", "memory", "index", "--force", "--agent", "main"])
        if r.returncode:
            print("  (reindex failed — run it yourself once the gateway is up)", file=sys.stderr)

    print(f"\ndone in {human(time.time() - t0)} "
          f"(estimated {human(est)}).\n", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
