#!/usr/bin/env python3
"""Independent send-rate watchdog for the swamai assistant.

OpenClaw implements botLoopProtection for Slack/Discord/Matrix/GoogleChat but NOT
for WhatsApp — verified by grep against the installed plugin. A self-triggering
reply loop therefore has nothing stopping it. This is that missing stop.

Counts outbound sends per recipient in a sliding window. If any single recipient
exceeds the threshold it disables group traffic and restarts the gateway, then
records why. Fails safe: any error leaves the system untouched.

    python3 guard/watchdog.py            # one check (run from a timer)
    python3 guard/watchdog.py --status   # show current counts, change nothing
"""
import json, os, pathlib, re, subprocess, sys, time
from collections import defaultdict

def _node_bin():
    """Find a Node bin dir without pinning a version (nvm installs vary)."""
    import glob, os, shutil
    n = shutil.which("node")
    if n:
        return os.path.dirname(n)
    cands = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    return cands[0] if cands else ""


WINDOW_MIN = int(os.environ.get("GUARD_WINDOW_MIN", "10"))
MAX_PER_RECIPIENT = int(os.environ.get("GUARD_MAX", "6"))
CFG = pathlib.Path.home() / ".openclaw/openclaw.json"
STATE = pathlib.Path(__file__).parent / "tripped.json"
NVM = _node_bin()
SENT = re.compile(r"Sent message \S+ -> (\S+)")


def recent_sends(window_min):
    logs = sorted(pathlib.Path("/tmp/openclaw").glob("openclaw-*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {}
    cutoff = time.time() - window_min * 60
    counts = defaultdict(int)
    for line in logs[0].read_text(errors="replace").splitlines()[-6000:]:
        if "Sent message" not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ts = str(d.get("time", ""))
        try:
            # "2026-09-06T13:01:58.123-04:00"
            secs = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if secs < cutoff:
            continue
        m = SENT.search(str(d.get("message", "")))
        if m:
            counts[m.group(1)] += 1
    return dict(counts)


def trip(recipient, n):
    cfg = json.loads(CFG.read_text())
    wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
    if wa.get("groupPolicy") == "disabled":
        return False                      # already stopped; nothing to do
    wa["groupPolicy"] = "disabled"
    wa.setdefault("actions", {})["sendMessage"] = False
    CFG.write_text(json.dumps(cfg, indent=2))
    env = dict(os.environ, PATH=f"{NVM}:{os.environ.get('PATH','')}")
    subprocess.run(["systemctl", "--user", "restart", "openclaw-gateway.service"],
                   capture_output=True, env=env)
    STATE.write_text(json.dumps({
        "trippedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "recipient": recipient, "sends": n,
        "windowMinutes": WINDOW_MIN, "threshold": MAX_PER_RECIPIENT,
    }, indent=2))
    return True


def main():
    dry = "--dry-run" in sys.argv
    counts = recent_sends(WINDOW_MIN)
    if "--status" in sys.argv:
        print(f"sends per recipient in last {WINDOW_MIN} min (limit {MAX_PER_RECIPIENT}):")
        for r, n in sorted(counts.items(), key=lambda kv: -kv[1]) or [("(none)", 0)]:
            flag = "  <-- OVER" if n > MAX_PER_RECIPIENT else ""
            print(f"  {n:>3}  {r}{flag}")
        if STATE.exists():
            print("\nlast trip:", STATE.read_text())
        return 0
    for recipient, n in counts.items():
        if n > MAX_PER_RECIPIENT:
            if dry:
                print(f"WOULD TRIP: {n} sends to {recipient} in {WINDOW_MIN} min "
                      f"(limit {MAX_PER_RECIPIENT}) — dry run, config untouched")
                return 1
            if trip(recipient, n):
                print(f"TRIPPED: {n} sends to {recipient} in {WINDOW_MIN} min "
                      f"(limit {MAX_PER_RECIPIENT}). Group traffic disabled.")
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"watchdog error (taking no action): {e}", file=sys.stderr)
        sys.exit(0)
