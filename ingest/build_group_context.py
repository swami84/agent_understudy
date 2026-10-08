#!/usr/bin/env python3
"""Generate a context card per chat/group from the parsed corpus.

Complements build_profiles.py (which is per-person). Writes
corpus/groups/<slug>-context.md, leaving the roster file (<slug>.md) untouched.

    python3 ingest/build_group_context.py
    python3 ingest/build_group_context.py --only dfs --force
"""
import argparse, json, pathlib, re, sys, urllib.error, urllib.request
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T
import llm

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


def ask(model, prompt, timeout, base_url=None):
    return llm.chat(prompt, model=model, base_url=base_url, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", default="corpus/chats")
    ap.add_argument("--out", default="corpus/groups")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--base-url", default=llm.DEFAULT_URL,
                    help="http://127.0.0.1:11434 (ollama) or http://127.0.0.1:8080/v1 (strata)")
    ap.add_argument("--sample-chars", type=int, default=14000)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--min-messages", type=int, default=20)
    ap.add_argument("--only")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--min-delta", type=int, default=40,
                    help="new messages needed before a card is worth rewriting")
    ap.add_argument("--max-age-days", type=int, default=90)
    ap.add_argument("--dry-run", action="store_true", help="list what would be built")
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
    man = T.Manifest()
    built = skipped = adopted = failed = 0

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
        key = f"group:{d.name}"
        ihash = T.content_hash(*(b for _, _, b in msgs))
        need, why = man.stale(key, ihash, count=len(msgs), min_delta=args.min_delta,
                              max_age_days=args.max_age_days, force=args.force)
        if dest.exists() and why == "new":
            man.adopt(key, ihash, count=len(msgs), model=args.model)
            adopted += 1
            continue
        if dest.exists() and not need:
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would build: {d.name} ({len(msgs)} msgs) — {why}")
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
            body = ask(args.model, prompt, args.timeout, args.base_url)
        except (urllib.error.URLError, TimeoutError, KeyError, OSError, ValueError) as e:
            print(f"  FAILED {d.name}: {e}", file=sys.stderr)
            failed += 1
            continue

        header = (f"# {d.name} — group context\n\n"
                  f"**Source:** {len(msgs)} messages, {months[0] if months else '?'} to "
                  f"{months[-1] if months else '?'}\n"
                  f"**Members:** {', '.join(n for n, _ in senders.most_common(15))}\n"
                  f"**Generated:** locally by {args.model}. Derived, not verbatim.\n\n")
        dest.write_text(header + body + "\n", encoding="utf-8")
        man.record(key, ihash, count=len(msgs), model=args.model)
        built += 1
        print(f"  {d.name} ({len(msgs)} msgs, {why}) -> {dest}", file=sys.stderr)

    if not args.dry_run:
        man.save()
    print(f"\nbuilt={built} skipped={skipped + adopted} failed={failed}"
          + (f"  ({adopted} existing card(s) {'would be adopted' if args.dry_run else 'adopted'} into the manifest)" if adopted else "")
          + ("  (unchanged since last build; --force to rewrite)" if skipped else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
