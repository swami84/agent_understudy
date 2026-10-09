#!/usr/bin/env python3
"""Independent send-rate watchdog for the Understudy.

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
# A loop repeats itself; a busy group does not. Counting volume alone cannot tell
# them apart, and it shut down a 237-member group mid-conversation over five
# distinct, correct replies to three different people. Trip on repetition, or on
# a volume so high no human exchange explains it.
REPEAT_MAX = int(os.environ.get("GUARD_REPEAT_MAX", "3"))
HARD_MAX = int(os.environ.get("GUARD_HARD_MAX", "20"))
CFG = pathlib.Path.home() / ".openclaw/openclaw.json"
STATE = pathlib.Path(__file__).parent / "tripped.json"
NVM = _node_bin()
SENT = re.compile(r"Sent message \S+ -> (\S+)")


def _norm(text):
    """Normalise a reply for comparison: lowercase word tokens only."""
    return tuple(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))[:40]


def _similar(a, b, thresh=0.8):
    x, y = set(a), set(b)
    if not x or not y:
        return False
    return len(x & y) / min(len(x), len(y)) >= thresh


def max_repeats(bodies):
    """Largest group of near-identical replies. 1 means everything was distinct."""
    best = 0
    for i, a in enumerate(bodies):
        n = sum(1 for b in bodies[i:] if _similar(a, b))
        best = max(best, n)
    return best


def recent_sends(window_min):
    logs = sorted(pathlib.Path("/tmp/openclaw").glob("openclaw-*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {}, {}
    cutoff = time.time() - window_min * 60
    pending = []
    counts = defaultdict(int)
    bodies = defaultdict(list)
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
        msg = str(d.get("message", ""))
        m = SENT.search(msg)
        if m:
            counts[m.group(1)] += 1
            pending.append(m.group(1))
        elif "Reply body:" in msg and pending:
            bodies[pending[-1]].append(_norm(msg.split("Reply body:", 1)[1][:400]))
    return dict(counts), {k: v for k, v in bodies.items()}


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


def verdict(n, repeats):
    """-> (should_trip, reason). Volume alone is not evidence of a loop."""
    if repeats >= REPEAT_MAX:
        return True, f"{repeats} near-identical replies"
    if n >= HARD_MAX:
        return True, f"{n} sends, beyond any plausible exchange"
    return False, ""


def main():
    dry = "--dry-run" in sys.argv
    counts, bodies = recent_sends(WINDOW_MIN)
    if "--status" in sys.argv:
        print(f"sends per recipient in last {WINDOW_MIN} min "
              f"(repeat limit {REPEAT_MAX}, hard limit {HARD_MAX}):")
        rows = sorted(counts.items(), key=lambda kv: -kv[1]) or [("(none)", 0)]
        for r, n in rows:
            rep = max_repeats(bodies.get(r, []))
            trip_now, why = verdict(n, rep)
            flag = f"  <-- WOULD TRIP: {why}" if trip_now else ""
            print(f"  {n:>3} sends, max {rep} alike  {r}{flag}")
        if STATE.exists():
            print("\nlast trip:", STATE.read_text())
        return 0
    for recipient, n in counts.items():
        repeats = max_repeats(bodies.get(recipient, []))
        trip_now, why = verdict(n, repeats)
        if not trip_now:
            continue
        if dry:
            print(f"WOULD TRIP: {recipient} — {why} in {WINDOW_MIN} min "
                  f"— dry run, config untouched")
            return 1
        if trip(recipient, n):
            print(f"TRIPPED: {recipient} — {why} in {WINDOW_MIN} min. "
                  f"Group traffic disabled.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"watchdog error (taking no action): {e}", file=sys.stderr)
        sys.exit(0)
