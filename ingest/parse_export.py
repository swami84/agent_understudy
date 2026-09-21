#!/usr/bin/env python3
"""Parse WhatsApp 'Export chat' .txt/.zip files into session-chunked markdown.

Why session chunks: WhatsApp chats are thousands of short lines. Fixed-size
chunks slice exchanges mid-conversation and retrieve as incoherent fragments.
Grouping consecutive messages separated by < GAP minutes yields chunks that are
actual conversations, each with a self-describing header so a retrieved chunk
makes sense on its own.

Usage:
    python3 ingest/parse_export.py corpus/raw/*.txt corpus/raw/*.zip
"""
import argparse, io, pathlib, re, sys, unicodedata, zipfile
from collections import Counter
from datetime import datetime, timedelta

# iOS:     [3/9/26, 10:15:23 AM] Sender: body     (often wrapped in U+200E)
# Android: 3/9/26, 10:15 AM - Sender: body
IOS = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*(?P<rest>.*)$")
ANDROID = re.compile(r"^(?P<ts>\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4},\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap][Mm])?)\s+-\s+(?P<rest>.*)$")

TS_FORMATS = [
    "%m/%d/%y, %I:%M:%S %p", "%m/%d/%Y, %I:%M:%S %p",
    "%m/%d/%y, %H:%M:%S",    "%m/%d/%Y, %H:%M:%S",
    "%m/%d/%y, %I:%M %p",    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%y, %H:%M",       "%m/%d/%Y, %H:%M",
    "%d/%m/%y, %I:%M:%S %p", "%d/%m/%Y, %I:%M:%S %p",
    "%d/%m/%y, %H:%M:%S",    "%d/%m/%Y, %H:%M:%S",
    "%d/%m/%y, %I:%M %p",    "%d/%m/%Y, %I:%M %p",
    "%d/%m/%y, %H:%M",       "%d/%m/%Y, %H:%M",
    "%Y-%m-%d, %H:%M:%S",    "%Y-%m-%d, %H:%M",
]

# Lines WhatsApp injects that carry no conversational value.
NOISE = re.compile(
    r"^(messages and calls are end-to-end encrypted|"
    r"you (created|added|removed|joined|left)|"
    r".{0,80}(joined using this group's invite link|"
    r"changed the (subject|group description|group icon|their phone number)|"
    r"was added|were added|left$|removed .+$|"
    r"turned on disappearing messages|"
    r"deleted this message|this message was deleted|"
    r"missed (voice|video) call|"
    r"changed to \+?\d+|security code changed))",
    re.I,
)
MEDIA = re.compile(r"^(<media omitted>|image omitted|video omitted|audio omitted|sticker omitted|document omitted|gif omitted|<attached:.*>)$", re.I)


def clean(s: str) -> str:
    # iOS exports pepper lines with LTR/RTL marks and NBSP.
    return "".join(c for c in s if unicodedata.category(c) != "Cf").replace(" ", " ").strip()


def parse_ts(raw: str):
    raw = clean(raw).replace(" ", " ")
    raw = re.sub(r"\s+", " ", raw)
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_lines(text):
    """Yield (datetime, sender, body). Continuation lines fold into the previous."""
    msgs = []
    for raw_line in text.splitlines():
        line = clean(raw_line)
        if not line:
            continue
        m = IOS.match(line) or ANDROID.match(line)
        if not m:
            if msgs:  # continuation of a multi-line message
                msgs[-1][2] += "\n" + line
            continue
        ts = parse_ts(m.group("ts"))
        rest = m.group("rest")
        if ts is None:
            if msgs:
                msgs[-1][2] += "\n" + line
            continue
        if ":" in rest:
            sender, body = rest.split(":", 1)
            sender, body = sender.strip(), body.strip()
            if len(sender) > 60 or not sender:  # system line, not "Name: body"
                continue
        else:
            continue  # system notice
        if NOISE.match(body) or NOISE.match(rest):
            continue
        msgs.append([ts, sender, body])
    return [(a, b, c) for a, b, c in msgs]


def sessionize(msgs, gap_minutes):
    gap = timedelta(minutes=gap_minutes)
    out, cur = [], []
    for m in msgs:
        if cur and m[0] - cur[-1][0] > gap:
            out.append(cur)
            cur = []
        cur.append(m)
    if cur:
        out.append(cur)
    return out


def slug(s):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "chat"


def read_source(p: pathlib.Path):
    """Return (chat_name, text) from a .txt or a WhatsApp export .zip."""
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not names:
                return None, None
            with z.open(names[0]) as fh:
                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace").read()
            stem = pathlib.Path(names[0]).stem
            # WhatsApp always names the inner file "_chat.txt", so it carries no
            # identity. Fall back to the zip's own name or every import would
            # land in the same folder and overwrite the previous one.
            if stem.strip("_").lower() in ("chat", ""):
                stem = p.stem
    else:
        text = p.read_text(encoding="utf-8", errors="replace")
        stem = p.stem
    name = re.sub(r"^WhatsApp Chat (with|-)\s*", "", stem, flags=re.I).strip()
    return (name or stem), text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default="corpus/chats")
    ap.add_argument("--gap", type=int, default=45, help="minutes of silence that ends a session")
    ap.add_argument("--min-messages", type=int, default=1,
                    help="drop sessions shorter than this (default 1 = keep everything)")
    args = ap.parse_args()

    outdir = pathlib.Path(args.out)
    people = Counter()
    totals = Counter()

    for f in args.files:
        p = pathlib.Path(f)
        if not p.exists():
            print(f"skip (missing): {p}", file=sys.stderr)
            continue
        name, text = read_source(p)
        if not text:
            print(f"skip (no .txt inside): {p}", file=sys.stderr)
            continue
        msgs = parse_lines(text)
        if not msgs:
            print(f"skip (no messages parsed): {p} — unrecognized export format?", file=sys.stderr)
            continue
        sessions = [s for s in sessionize(msgs, args.gap) if len(s) >= args.min_messages]
        participants = Counter(m[1] for m in msgs)
        people.update(participants)

        d = outdir / slug(name)
        d.mkdir(parents=True, exist_ok=True)
        by_month = {}
        for s in sessions:
            by_month.setdefault(s[0][0].strftime("%Y-%m"), []).append(s)

        for month, group in by_month.items():
            lines = [f"# {name} — {month}", "",
                     f"Participants: {', '.join(n for n, _ in participants.most_common(12))}", ""]
            for s in group:
                start, end = s[0][0], s[-1][0]
                span = start.strftime("%Y-%m-%d %H:%M")
                if end.date() != start.date():
                    span += f" → {end.strftime('%Y-%m-%d %H:%M')}"
                who = ", ".join(sorted({m[1] for m in s}))
                lines.append(f"## {span} — {who}")
                for ts, sender, body in s:
                    body = "[media]" if MEDIA.match(body) else body
                    body = body.replace("\n", "\n  ")  # keep continuations inside the bullet
                    lines.append(f"- **{sender}** ({ts.strftime('%H:%M')}): {body}")
                lines.append("")
            (d / f"{month}.md").write_text("\n".join(lines), encoding="utf-8")

        totals["chats"] += 1
        totals["messages"] += len(msgs)
        totals["sessions"] += len(sessions)
        print(f"{name}: {len(msgs)} msgs -> {len(sessions)} sessions across {len(by_month)} months")

    print(f"\n{totals['chats']} chat(s), {totals['messages']} messages, {totals['sessions']} sessions",
          file=sys.stderr)
    if people:
        print("Top participants: " + ", ".join(f"{n} ({c})" for n, c in people.most_common(15)),
              file=sys.stderr)


if __name__ == "__main__":
    main()
