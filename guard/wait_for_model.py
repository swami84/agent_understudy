#!/usr/bin/env python3
"""Block until the configured model endpoint can actually serve, or give up.

At boot the gateway, the gate proxy and the inference engine all start at once,
but Strata needs ~45s to load its weights. A gateway that wins that race answers
real messages with "No reply was generated" until the engine is up. Restart=always
does not help: the gateway is healthy, its model is not.

Checks through the proxy, not the engine directly, so a dead proxy fails here too
rather than surfacing as ungated replies later.

    python3 guard/wait_for_model.py --timeout 300
"""
import argparse, json, os, pathlib, sys, time, urllib.error, urllib.request

CFG = pathlib.Path.home() / ".openclaw/openclaw.json"
LOCAL = {"ollama", "strata", "ollama_oai", "llamacpp", "vllm"}


def target():
    """-> (url, model) for the primary provider, or (None, None) if hosted."""
    try:
        cfg = json.loads(CFG.read_text())
    except Exception:
        return None, None
    primary = (((cfg.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary", "")
    key = primary.split("/")[0] if "/" in primary else ""
    if key not in LOCAL:
        return None, None            # hosted API: nothing local to wait for
    prov = ((cfg.get("models") or {}).get("providers") or {}).get(key) or {}
    base = (prov.get("baseUrl") or "").rstrip("/")
    if not base:
        return None, None
    url = base + "/models" if base.endswith("/v1") else base + "/api/tags"
    return url, primary.split("/", 1)[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("UNDERSTUDY_WAIT", "300")))
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    url, model = target()
    if not url:
        print("no local provider to wait for", flush=True)
        return 0

    deadline = time.time() + args.timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    waited = args.timeout - int(deadline - time.time())
                    print(f"model endpoint ready after {waited}s: {url}", flush=True)
                    return 0
                last = f"HTTP {r.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(args.interval)

    # Do NOT block startup forever: a gateway that never starts cannot be
    # debugged from WhatsApp. Warn and let it come up degraded.
    print(f"WARNING: {url} not ready after {args.timeout}s ({last}) — "
          f"starting anyway; replies will fail until it is", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
