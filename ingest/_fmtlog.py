#!/usr/bin/env python3
"""Format OpenClaw's JSON-lines log into readable timestamped output."""
import json, os, re, sys

GREP = os.environ.get("G", "")
MODE = os.environ.get("M", "")
ALL = os.environ.get("A") == "1"
rx = re.compile(GREP, re.I) if GREP else None
NOISE = re.compile(r"web gateway heartbeat|all slots are idle|print_timing|slot (release|operator)", re.I)
# CLI table output lands in the same log file; it is echo, not events.
CLI_ECHO = re.compile(r"^(ID\s+Declaration|Chat channels:|Plugins \(|Memory Search|\u2500|\u250c|\u2502)")
DIM, RST = "\033[90m", "\033[0m"
COL = {"E": "\033[31m", "W": "\033[33m", "I": "\033[36m", "D": DIM}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except Exception:
        if ALL:
            print(line[:300])
        continue
    msg = str(d.get("message", "")).strip()
    if not msg:
        continue
    t = str(d.get("time", ""))[11:19] or "--:--:--"
    meta = d.get("_meta", {}) or {}
    lvl = str(meta.get("logLevelName", "INFO"))[:1]
    raw_mod = str(meta.get("name", ""))
    m = re.search(r'"(?:subsystem|module)":"([^"]+)"', raw_mod)
    mod = m.group(1) if m else raw_mod[:22]
    if MODE == "sent" and not re.search(r"deliver|sent|announce|outbound|->", msg, re.I):
        continue
    if MODE == "cron" and not re.search(r"cron|schedul|job|automation", msg + mod, re.I):
        continue
    if CLI_ECHO.search(msg) or "Declaration              Name" in msg:
        continue
    if not ALL and not GREP and NOISE.search(msg):
        continue
    if rx and not (rx.search(msg) or rx.search(mod)):
        continue
    body = msg if len(msg) < 600 else msg[:600] + " …"
    body = body.replace("\n", " ⏎ ")
    print(f"{DIM}{t}{RST} {COL.get(lvl, RST)}{lvl}{RST} {DIM}{mod[:20]:<20}{RST} {body}",
          flush=True)
