#!/usr/bin/env python3
"""Regenerate channels.whatsapp.groupAllowFrom from group rosters, minus a blocklist.

OpenClaw's groupPolicy is open | disabled | allowlist, with no sender blocklist.
"open" admits every sender in every scoped group and cannot exclude anyone;
"allowlist" can exclude, but only by hand-maintaining every member's number,
which drifts silently as people join.

This gives the behaviour we actually want — open to all members by default, with
an explicit blocklist — by generating the allowlist: every participant of every
enabled group, minus config/block-list.txt. Re-run it when membership changes.

    python3 guard/sync_allowlist.py              # show what would change
    python3 guard/sync_allowlist.py --apply
"""
import argparse, json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = pathlib.Path.home() / ".openclaw/openclaw.json"
ROSTERS = ROOT / "corpus" / "groups"
BLOCKLIST = ROOT / "config" / "block-list.txt"


def digits(s):
    return re.sub(r"\D", "", s or "")


def load_blocklist():
    """One number per line; '#' comments. Any format — reduced to digits."""
    out = {}
    if not BLOCKLIST.exists():
        return out
    for line in BLOCKLIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        d = digits(parts[0])
        if d:
            out[d] = parts[1].strip() if len(parts) > 1 else ""
    return out


def rosters_for(jids):
    """-> {jid: [(phone, name)]} from corpus/groups/<slug>.json files."""
    found = {}
    for f in sorted(ROSTERS.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        jid = d.get("id")
        if jid in jids:
            found[jid] = [(digits(p.get("phone")), p.get("name") or "")
                          for p in d.get("participants", []) if p.get("phone")]
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--config", default=str(CFG))
    args = ap.parse_args()

    cfg_path = pathlib.Path(args.config)
    cfg = json.loads(cfg_path.read_text())
    wa = cfg.setdefault("channels", {}).setdefault("whatsapp", {})
    enabled = [j for j in (wa.get("groups") or {})]
    if not enabled:
        print("No groups enabled in channels.whatsapp.groups.", file=sys.stderr)
        return 1

    blocked = load_blocklist()
    rosters = rosters_for(set(enabled))
    missing = [j for j in enabled if j not in rosters]

    members, by_group = {}, {}
    for jid, people in rosters.items():
        kept = [(p, n) for p, n in people if p and p not in blocked]
        by_group[jid] = (len(people), len(kept))
        for p, n in kept:
            members[p] = n

    self_num = digits((cfg.get("channels", {}).get("whatsapp", {}) or {}).get("selfNumber", ""))
    if self_num:
        members.setdefault(self_num, "self")

    new = enabled + sorted(members)
    old = wa.get("groupAllowFrom", [])

    print(f"enabled groups : {len(enabled)}")
    for jid in enabled:
        if jid in by_group:
            total, kept = by_group[jid]
            print(f"  {jid}  {kept}/{total} members allowed")
    for jid in missing:
        print(f"  {jid}  NO ROSTER — run ingest/extract_group.mjs --group {jid}")
    print(f"blocked        : {len(blocked)}" + (f"  {list(blocked)[:5]}" if blocked else ""))
    print(f"groupAllowFrom : {len(old)} -> {len(new)}")

    if missing:
        print("\nRefusing to write while a roster is missing: it would silently "
              "lock out every member of that group.", file=sys.stderr)
        return 2
    if not args.apply:
        print("\nplan only. re-run with --apply to write.")
        return 0

    wa["groupAllowFrom"] = new
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("\nwritten. groupPolicy stays 'allowlist' — the list is now generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
