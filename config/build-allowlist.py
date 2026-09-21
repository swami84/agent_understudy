#!/usr/bin/env python3
"""Turn whatsapp-groups.tsv (+ optional dm-allow.txt) into a config patch.

Edit whatsapp-groups.tsv: change the `allow` column to `yes` for each group the
assistant may participate in. Put one phone number per line in dm-allow.txt for
individual people. Then:

    python3 config/build-allowlist.py > config/20-whatsapp-allow.patch.json5
    openclaw config patch --file config/20-whatsapp-allow.patch.json5 --dry-run
"""
import json, pathlib, re, sys

here = pathlib.Path(__file__).parent
groups, names = [], {}
tsv = here / "whatsapp-groups.tsv"
for i, line in enumerate(tsv.read_text().splitlines()):
    if i == 0 or not line.strip():
        continue
    parts = line.split("\t")
    if len(parts) < 2:
        continue
    allow, gid = parts[0].strip().lower(), parts[1].strip()
    name = parts[2].strip() if len(parts) > 2 else ""
    if allow in ("yes", "y", "true", "1") and gid.endswith("@g.us"):
        groups.append(gid)
        names[gid] = name

dms = []  # self is added from config/identity.json
dmfile = here / "dm-allow.txt"
if dmfile.exists():
    for line in dmfile.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        digits = re.sub(r"\D", "", line)
        if len(digits) < 7:
            print(f"// SKIPPED unparseable DM entry: {line}", file=sys.stderr)
            continue
        if digits not in dms:
            dms.append(digits)

out = ["{", "  channels: {", "    whatsapp: {",
       f"      allowFrom: {json.dumps(dms)},",
       "      groupAllowFrom: ["]
for gid in groups:
    out.append(f'        "{gid}",'.ljust(46) + f"// {names[gid]}")
out += ["      ],", "    },", "  },", "}"]
print("\n".join(out))
print(f"// {len(groups)} group(s), {len(dms)} DM number(s)", file=sys.stderr)
