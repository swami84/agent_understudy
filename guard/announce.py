#!/usr/bin/env python3
"""Announce "SwamAI is active" to chosen conversations when the gateway starts.

Runs as ExecStartPost on openclaw-gateway. Three things make this less trivial
than it sounds:

- **Restarts are frequent.** Config edits, crashes and Restart=always all bring
  the gateway back, and every one of those would otherwise announce again. A
  per-target cooldown means a restart loop cannot turn into a burst of
  announcements in a 237-member group.
- **The channel is not ready when the unit is.** WhatsApp needs to connect and
  resync before a send will land, so this waits for the gateway's HTTP port and
  then a little longer, rather than firing immediately.
- **It is a proactive send.** Nothing the user said prompted it, so it is held to
  the targets explicitly enabled in the UI and nothing else.

    python3 guard/announce.py --dry-run
    python3 guard/announce.py
"""
import argparse, json, os, pathlib, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
STORE = ROOT / "config" / "announce.json"
STATE = ROOT / "config" / "announce-state.json"
NVM = str(pathlib.Path.home() / ".nvm/versions/node/v26.8.1/bin")
GATEWAY = "http://127.0.0.1:18789"

DEFAULT_MESSAGE = "🤖 SwamAI: back online and listening. Summon me by name."
DEFAULT_COOLDOWN_H = 6


def load_store():
    """{"enabled": {target: bool}, "message": str, "cooldownHours": int}"""
    if STORE.exists():
        try:
            d = json.loads(STORE.read_text())
            return {
                "enabled": d.get("enabled") or {},
                "message": (d.get("message") or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE,
                "cooldownHours": int(d.get("cooldownHours", DEFAULT_COOLDOWN_H)),
            }
        except Exception:
            pass
    return {"enabled": {}, "message": DEFAULT_MESSAGE, "cooldownHours": DEFAULT_COOLDOWN_H}


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")


def gateway_ready(timeout, settle):
    """Wait for the gateway port, then let the channel finish connecting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(GATEWAY, timeout=3)
            break
        except urllib.error.HTTPError:
            break                      # answering at all is enough
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
    else:
        return False
    time.sleep(settle)
    return True


def send(target, message, dry):
    cmd = ["openclaw", "message", "send", "--channel", "whatsapp",
           "-t", target, "-m", message]
    if dry:
        cmd.append("--dry-run")
    env = dict(os.environ, PATH=f"{NVM}:{os.environ.get('PATH', '')}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    ok = r.returncode == 0
    detail = (r.stdout or r.stderr or "").strip().splitlines()
    return ok, (detail[-1][:120] if detail else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the cooldown")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for the gateway")
    ap.add_argument("--settle", type=int, default=25,
                    help="extra seconds for the channel to connect")
    args = ap.parse_args()

    store = load_store()
    targets = [t for t, on in store["enabled"].items() if on]
    if not targets:
        print("announce: nothing enabled", flush=True)
        return 0

    state = load_state()
    now = time.time()
    cooldown = store["cooldownHours"] * 3600
    due, held = [], []
    for t in targets:
        last = state.get(t, {}).get("lastSent", 0)
        if args.force or (now - last) >= cooldown:
            due.append(t)
        else:
            held.append((t, int((cooldown - (now - last)) / 60)))

    for t, mins in held:
        print(f"announce: {t} held, {mins} min of cooldown left", flush=True)
    if not due:
        return 0

    if not args.dry_run and not gateway_ready(args.wait, args.settle):
        print(f"announce: gateway not reachable within {args.wait}s — skipping",
              file=sys.stderr, flush=True)
        return 0

    sent = 0
    for t in due:
        ok, detail = send(t, store["message"], args.dry_run)
        print(f"announce: {'sent' if ok else 'FAILED'} -> {t}  {detail}", flush=True)
        if ok and not args.dry_run:
            state.setdefault(t, {})["lastSent"] = now
            state[t]["lastSentAt"] = datetime.now().isoformat(timespec="seconds")
            sent += 1
    if sent:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
