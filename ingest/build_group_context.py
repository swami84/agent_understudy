#!/usr/bin/env python3
"""Generate a context card per chat/group from the parsed corpus.

Complements build_profiles.py (which is per-person). Writes
corpus/groups/<slug>-context.md, leaving the roster file (<slug>.md) untouched.

    python3 ingest/build_group_context.py
    python3 ingest/build_group_context.py --only dfs --force
"""
import argparse, json, pathlib, re, sys, urllib.error, urllib.request
from collections import Counter

OLLAMA = "http://127.0.0.1:11434/api/chat"
BULLET = re.compile(r"^- \*\*(?P<sender>.+?)\*\* \((?P<time>\d{2}:\d{2})\): (?P<body>.*)$")

PROMPT = """You are writing a reference card about a group chat, for an assistant \
that participates in it. Use only what the excerpts support; never invent.

Chat: {name}
Members seen: {members}
Months covered: {months}
Total messages: {count}
The chat owner (the assistant's user) appears as: {self_name}

Excerpts (sampled across the whole period):
{sample}

Write markdown with exactly these sections:

## What this group is for
2-3 sentences on the group's actual purpose, as evidenced.

## Who's who
One bullet per member: their role in this group specifically.

## Recurring topics
Bullet list of subjects that come up repeatedly.

## Norms
2-3 sentences: tone, formality, pace, in-jokes, what kind of message fits here.

## Active threads
Unresolved plans, dates, or commitments. "None evident" if none.

No preamble. Start at "## What this group is for"."""


def self_name():
    f = pathlib.Path(__file__).parent.parent / "config/identity.json"
    if f.exists():
        try:
            return json.loads(f.read_text()).get("selfName", "") or "ME"
        except Exception:
            pass
    return "ME"


def ask(model, prompt, timeout):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "stream": False, "think": False,
               "options": {"temperature": 0.2, "num_ctx": 32768}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", default="corpus/chats")
    ap.add_argument("--out", default="corpus/groups")
    ap.add_argument("--model", default="qwen3.8:27b")
    ap.add_argument("--sample-chars", type=int, default=14000)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--min-messages", type=int, default=20)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    me = self_name()
    root = pathlib.Path(args.chats)
    if not root.exists():
        print(f"No {root}. Run ingest/parse_export.py first.", file=sys.stderr)
        return 1

    dirs = sorted([d for d in root.iterdir() if d.is_dir()])
    if args.only:
        dirs = [d for d in dirs if args.only.lower() in d.name.lower()]
    if not dirs:
        print("No chats matched.", file=sys.stderr)
        return 1

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    built = skipped = failed = 0

    for d in dirs:
        months = sorted(f.stem for f in d.glob("*.md"))
        msgs, senders = [], Counter()
        for f in sorted(d.glob("*.md")):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = BULLET.match(line)
                if not m:
                    continue
                body = m.group("body").strip()
                if not body or body == "[media]":
                    continue
                senders[m.group("sender").strip()] += 1
                msgs.append((f.stem, m.group("sender").strip(), body))

        if len(msgs) < args.min_messages:
            print(f"  skip {d.name}: only {len(msgs)} message(s)", file=sys.stderr)
            continue
        dest = outdir / f"{d.name}-context.md"
        if dest.exists() and not args.force:
            skipped += 1
            continue

        step = max(1, len(msgs) // 250)
        picked, used = [], 0
        for month, sender, body in msgs[::step]:
            line = f"[{month}] {sender}: {body}"
            if used + len(line) > args.sample_chars:
                break
            picked.append(line)
            used += len(line)

        prompt = PROMPT.format(
            name=d.name, members=", ".join(f"{n} ({c})" for n, c in senders.most_common(15)),
            months=f"{months[0]} to {months[-1]}" if months else "?",
            count=len(msgs), self_name=me, sample="\n".join(picked))
        try:
            body = ask(args.model, prompt, args.timeout)
        except (urllib.error.URLError, TimeoutError, KeyError, OSError) as e:
            print(f"  FAILED {d.name}: {e}", file=sys.stderr)
            failed += 1
            continue

        header = (f"# {d.name} — group context\n\n"
                  f"**Source:** {len(msgs)} messages, {months[0] if months else '?'} to "
                  f"{months[-1] if months else '?'}\n"
                  f"**Members:** {', '.join(n for n, _ in senders.most_common(15))}\n"
                  f"**Generated:** locally by {args.model}. Derived, not verbatim.\n\n")
        dest.write_text(header + body + "\n", encoding="utf-8")
        built += 1
        print(f"  {d.name} ({len(msgs)} msgs) -> {dest}", file=sys.stderr)

    print(f"\nbuilt={built} skipped={skipped} failed={failed}"
          + ("  (--force to rewrite)" if skipped else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
