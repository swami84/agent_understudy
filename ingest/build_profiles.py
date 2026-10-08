#!/usr/bin/env python3
"""Build per-person profile cards from the parsed chat corpus, using local Ollama.

The raw corpus is the detail store; these cards are what actually make the
assistant useful — a compact, retrievable summary of who each person is, what
you talk about, and how they write. Runs entirely on local GPU.

    python3 ingest/build_profiles.py --min-messages 30
    python3 ingest/build_profiles.py --only "Ravi" --force
"""
import argparse, json, pathlib, re, sys, urllib.error, urllib.request
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import temporal as T
import llm

BULLET = re.compile(r"^- \*\*(?P<sender>.+?)\*\* \((?P<time>\d{2}:\d{2})\): (?P<body>.*)$")

SELF_PROMPT = """These are the chat owner's OWN messages — this is the user of the \
assistant, not a third party. Write a reference card about the USER, addressed to an \
assistant that works for them. Use only what the excerpts support; never invent.

User: {name}
Appears in: {chats}
Total messages: {count}

Excerpts:
{sample}

Write markdown with exactly these sections:

## Context
Who the user is, as evidenced here: where they live, household, recurring roles. 2-4 \
sentences. Refer to them as "the user", never by guessing a different owner.

## Topics
Bullet list of what the user habitually discusses or organises.

## Communication style
2-3 sentences on how the user writes: length, tone, formality.

## Open threads
Unresolved plans or commitments the user is carrying. "None evident" if none.

No preamble. Start at "## Context"."""


PROMPT = """You are building a factual reference card about a person, from real \
group-chat excerpts. Write ONLY what the excerpts support. Never invent details.

Person: {name}
Appears in: {chats}
Total messages: {count}

Excerpts:
{sample}

Write markdown with exactly these sections:

## Context
Who they appear to be and how they relate to the chat owner. 2-3 sentences. If \
the excerpts don't establish this, say "Unclear from available chats."

## Topics
Bullet list of recurring subjects this person engages with.

## Communication style
2-3 sentences: message length, tone, formality, response habits.

## Open threads
Bullet list of unresolved questions, plans, or commitments. Write "None evident" \
if there are none.

No preamble. Start at "## Context"."""


def load_self():
    """Who is the chat owner? Exports label their own messages ME / You / <name>."""
    name = ""
    ident = pathlib.Path(__file__).parent.parent / "config/identity.json"
    if ident.exists():
        try:
            name = json.loads(ident.read_text()).get("selfName", "") or ""
        except Exception:
            pass
    labels = {"me", "you", "you (owner)"}
    if name:
        labels.add(name.lower())
        labels.add(name.split()[0].lower())
    return name, labels


def slug(s):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "person"


def collect(chats_dir):
    """-> {person: {"chats": set, "msgs": [(chat, month, text)]}}"""
    people = defaultdict(lambda: {"chats": set(), "msgs": []})
    for md in sorted(pathlib.Path(chats_dir).rglob("*.md")):
        chat = md.parent.name
        month = md.stem
        for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
            m = BULLET.match(line)
            if not m:
                continue
            body = m.group("body").strip()
            if not body or body == "[media]":
                continue
            sender = m.group("sender").strip()
            people[sender]["chats"].add(chat)
            people[sender]["msgs"].append((chat, month, body))
    return people


def sample(msgs, budget_chars):
    """Spread the sample across the whole timeline, not just the newest messages."""
    if not msgs:
        return ""
    step = max(1, len(msgs) // 200)
    picked, used = [], 0
    for chat, month, body in msgs[::step]:
        line = f"[{chat} {month}] {body}"
        if used + len(line) > budget_chars:
            break
        picked.append(line)
        used += len(line)
    return "\n".join(picked)


def ask(model, prompt, timeout, base_url=None):
    return llm.chat(prompt, model=model, base_url=base_url, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chats", default="corpus/chats")
    ap.add_argument("--out", default="corpus/people")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--base-url", default=llm.DEFAULT_URL,
                    help="http://127.0.0.1:11434 (ollama) or http://127.0.0.1:8080/v1 (strata)")
    ap.add_argument("--min-messages", type=int, default=30)
    ap.add_argument("--sample-chars", type=int, default=12000)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", help="substring match on name")
    ap.add_argument("--force", action="store_true", help="rewrite existing cards")
    ap.add_argument("--min-delta", type=int, default=25,
                    help="new messages needed before a card is worth rewriting")
    ap.add_argument("--max-age-days", type=int, default=90,
                    help="refresh a changed card at least this often")
    ap.add_argument("--dry-run", action="store_true", help="list who would be built")
    args = ap.parse_args()

    self_name, self_labels = load_self()
    people = collect(args.chats)
    if not people:
        print(f"No parsed chats under {args.chats}. Run ingest/parse_export.py first.", file=sys.stderr)
        return 1

    targets = sorted(
        ((n, d) for n, d in people.items()
         if len(d["msgs"]) >= args.min_messages
         and (not args.only or args.only.lower() in n.lower())),
        key=lambda kv: -len(kv[1]["msgs"]),
    )
    if not targets:
        print(f"Nobody has >= {args.min_messages} messages. Lower --min-messages.", file=sys.stderr)
        return 1

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{len(targets)} profile(s) to build (>= {args.min_messages} msgs)", file=sys.stderr)

    man = T.Manifest()
    built = skipped = adopted = failed = 0
    for name, d in targets:
        dest = outdir / f"{slug(self_name or name) if name.strip().lower() in self_labels else slug(name)}.md"
        # Hash the messages that feed this card, not the file's mtime: re-parsing
        # the same export rewrites every month file, and mtime would then rebuild
        # the entire corpus on a run that changed nothing.
        key = f"profile:{dest.stem}"
        ihash = T.content_hash(*(b for _, _, b in d["msgs"]))
        need, why = man.stale(key, ihash, count=len(d["msgs"]), min_delta=args.min_delta,
                              max_age_days=args.max_age_days, force=args.force)
        if dest.exists() and why == "new":
            man.adopt(key, ihash, count=len(d["msgs"]), model=args.model)
            adopted += 1
            continue
        if dest.exists() and not need:
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would build: {name} ({len(d['msgs'])} msgs, {len(d['chats'])} chats) — {why}")
            continue
        is_self = name.strip().lower() in self_labels
        tmpl = SELF_PROMPT if is_self else PROMPT
        display = self_name or name if is_self else name
        prompt = tmpl.format(
            name=display,
            chats=", ".join(sorted(d["chats"])[:12]),
            count=len(d["msgs"]),
            sample=sample(d["msgs"], args.sample_chars),
        )
        try:
            body = ask(args.model, prompt, args.timeout, args.base_url)
        except (urllib.error.URLError, TimeoutError, KeyError, OSError, ValueError) as e:
            print(f"  FAILED {name}: {e}", file=sys.stderr)
            failed += 1
            continue
        header = (
            f"# {display}{' (you)' if is_self else ''}\n\n"
            f"**Source:** WhatsApp corpus · {len(d['msgs'])} messages across "
            f"{len(d['chats'])} chat(s): {', '.join(sorted(d['chats'])[:12])}\n"
            f"**Generated:** locally by {args.model}. Derived, not verbatim — verify before relying on it.\n\n"
        )
        dest.write_text(header + body + "\n", encoding="utf-8")
        man.record(key, ihash, count=len(d["msgs"]), model=args.model)
        built += 1
        print(f"  {name} ({len(d['msgs'])} msgs, {why}) -> {dest}", file=sys.stderr)

    if not args.dry_run:
        man.save()
    print(f"\nbuilt={built} skipped={skipped + adopted} failed={failed}"
          + (f"  ({adopted} existing card(s) {'would be adopted' if args.dry_run else 'adopted'} into the manifest)" if adopted else "")
          + ("  (unchanged since last build; --force to rewrite)" if skipped else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
